// AgentDrive SnipIt — service worker.
//
// Responsibilities:
//   * Sign in / out through Token Canopy Hub (src/lib/hub-auth.js). The
//     old flow opened AgentDrive's own `/auth/extension/start` in a tab and
//     redeemed a ticket; AgentDrive stopped being an authorization server
//     at the v0 contract reset, so this is now a PKCE flow against Hub via
//     `chrome.identity.launchWebAuthFlow`.
//   * Capture: viewport (`captureViewport`) or region (`captureRegion`).
//     Region capture injects the overlay content script into the active
//     tab, listens for the result, and crops the PNG in an offscreen canvas.
//   * Upload: create the artifact under the SAVED LOCATION, recording the
//     source page in its metadata, then put the configured link on the
//     clipboard (src/lib/upload.js).
//
// Message protocol (chrome.runtime.sendMessage):
//   from popup:    { type: "sign-in" }
//                  { type: "sign-out" }
//                  { type: "capture", mode: "viewport"|"region" }
//                  { type: "get-state" }
//                  { type: "open-options" }
//   from content:  { type: "region-selected", rect, dpr, title, pageUrl }
//                  { type: "region-cancelled" }

import { getEndpoints } from "../lib/config.js";
import { clearDriveTokens } from "../lib/drive-token.js";
import { isSignedIn, signIn, signOut } from "../lib/hub-auth.js";
import {
  clearLocation,
  markLocationStale,
  readSettings,
} from "../lib/settings.js";
import { uploadCapture } from "../lib/upload.js";

// BUSY is a mutex over the capture→upload pipeline ONLY. It is set when a
// capture starts and released when the upload finishes (or on any failure)
// — NOT held for anything the user does afterwards. Holding it longer made
// the popup show "Capturing…" indefinitely, which was indistinguishable
// from the real stuck states it kept getting confused with.
let BUSY = false;
// What the pipeline is doing, for the popup's status label:
// "selecting" (overlay up, waiting for the drag) | "uploading".
let PHASE = null;

// Popup-facing bookkeeping lives in chrome.storage.session, not in SW
// globals: MV3 SWs terminate after ~30s idle, and in-memory state made
// the popup's last-error notice vanish half a minute after a failed
// capture. Session storage clears on browser restart — the right lifetime.
//
// Deliberately NOT stored here: the capture PNG. An earlier revision
// parked the full data URL in storage.session for the (since-removed)
// extension-local editor, and nothing deleted it on success. Chrome caps
// storage.session at 10 MiB, so a handful of full-tab retina captures
// filled the quota and every capture after that died with "QUOTA_BYTES
// quota exceeded" before it started — in the field this read as "Capture
// tab stopped working" (while smaller region crops kept squeaking
// through). The PNG now lives only in the in-flight upload;
// tests/background/capture-regressions.test.js pins this.
const LAST_ERROR_KEY = "last_error";  // { message, at }

/** Failures that mean the SAVED LOCATION is unusable, not that the capture
 *  hit a bad moment. Marking stale is what swaps the popup's capture
 *  buttons for "choose a new location", so a code here must be one that
 *  will not fix itself. */
const STALE_LOCATION_CODES = new Set([
  "LOCATION_UNAVAILABLE",   // the folder or drive answered 403/404
  "WORKSPACE_UNAVAILABLE",  // hub will not mint for that workspace any more
]);

async function setLastError(message) {
  await chrome.storage.session.set({
    [LAST_ERROR_KEY]: { message, at: Date.now() },
  });
}
async function clearLastError() {
  await chrome.storage.session.remove(LAST_ERROR_KEY);
}

// -- offscreen lifecycle ----------------------------------------------------

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: "src/offscreen/offscreen.html",
    reasons: ["BLOBS", "CLIPBOARD"],
    justification: "Stream-upload and copy the capture's link to the clipboard.",
  });
}

async function closeOffscreen() {
  if (await chrome.offscreen.hasDocument()) {
    await chrome.offscreen.closeDocument();
  }
}

async function copyToClipboard(text) {
  await ensureOffscreen();
  const r = await chrome.runtime.sendMessage({
    target: "offscreen", type: "copy", text,
  });
  return r && r.ok;
}

// -- capture: viewport ------------------------------------------------------

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function captureViewport(tab) {
  // captureVisibleTab returns a PNG data URL. It rejects on
  // chrome:// URLs and the Web Store; we surface that to the popup.
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
    format: "png",
  });
  if (!dataUrl) throw new Error("Could not capture this page.");
  return dataUrl;
}

// -- capture: region (overlay) ---------------------------------------------

/**
 * Inject the content script, wait for `region-selected` (or cancel),
 * then crop the viewport capture to the rect inside an offscreen
 * canvas. Returns a PNG data URL.
 */
async function captureRegion(tab) {
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["src/overlay/content.js"],
  });

  // Wait for the content script to post back.
  const sel = await new Promise((resolve, reject) => {
    const onMessage = (msg, sender) => {
      if (!sender.tab || sender.tab.id !== tab.id) return;
      if (msg.type === "region-selected") {
        chrome.runtime.onMessage.removeListener(onMessage);
        resolve(msg);
      } else if (msg.type === "region-cancelled") {
        chrome.runtime.onMessage.removeListener(onMessage);
        reject(new Error("CANCELLED"));
      }
    };
    chrome.runtime.onMessage.addListener(onMessage);
    // Safety timeout — content script orphaned (page navigated).
    setTimeout(() => {
      chrome.runtime.onMessage.removeListener(onMessage);
      reject(new Error("Region selection timed out."));
    }, 60_000);
  });

  // Capture full viewport, then crop in an offscreen canvas.
  const viewportDataUrl = await captureViewport(tab);
  const cropped = await cropDataUrl(viewportDataUrl, sel.rect, sel.dpr || 1);
  return {
    dataUrl: cropped,
    title: sel.title || tab.title,
    pageUrl: sel.pageUrl || tab.url,
    dpr: sel.dpr || 1,
  };
}

async function cropDataUrl(dataUrl, rect, dpr) {
  // OffscreenCanvas in the SW; `createImageBitmap` accepts a Blob
  // directly so we never need a DOM Image element here.
  //
  // Do NOT use `fetch(dataUrl)` — extension pages' `connect-src` CSP
  // does not include `data:`, so the fetch gets blocked with
  // "Refused to connect because it violates the document's CSP."
  // Going through the dataUrlToBlob helper (string → Blob, no network
  // request) sidesteps the CSP entirely.
  const blob = dataUrlToBlob(dataUrl);
  const bitmap = await createImageBitmap(blob);
  const sx = Math.max(0, Math.floor(rect.x * dpr));
  const sy = Math.max(0, Math.floor(rect.y * dpr));
  const sw = Math.max(1, Math.floor(rect.w * dpr));
  const sh = Math.max(1, Math.floor(rect.h * dpr));
  const canvas = new OffscreenCanvas(sw, sh);
  const ctx = canvas.getContext("2d");
  ctx.drawImage(bitmap, sx, sy, sw, sh, 0, 0, sw, sh);
  const outBlob = await canvas.convertToBlob({ type: "image/png" });
  return await blobToDataUrl(outBlob);
}

async function blobToDataUrl(blob) {
  // FileReader exists in MV3 SW in recent Chrome, but `arrayBuffer()` +
  // `btoa` is the portable path. We chunk the binary→string conversion
  // because for large PNGs (multi-MB), `String.fromCharCode(...bytes)`
  // can blow the argument stack with "Maximum call stack size exceeded".
  const buf = await blob.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = "";
  const CHUNK = 0x8000;  // 32 KB at a time — well under the limit
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return `data:${blob.type || "image/png"};base64,${btoa(binary)}`;
}

function dataUrlToBlob(dataUrl) {
  const [header, b64] = dataUrl.split(",", 2);
  const mime = (header.match(/data:([^;]+);base64/) || [, "image/png"])[1];
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

// -- entry: capture flow ----------------------------------------------------

async function startCapture(mode) {
  if (BUSY) throw new Error("BUSY");
  if (!(await isSignedIn())) throw new Error("NOT_SIGNED_IN");
  // Refuse before capturing rather than after: taking a screenshot and
  // then discovering there is nowhere to put it wastes the moment the
  // person was trying to capture.
  const { location, preferences } = await readSettings();
  if (!location) throw new Error("NO_LOCATION");

  BUSY = true;
  let uploadStarted = false;
  try {
    // Inside the try so a storage error can't skip the finally's
    // BUSY release — an unreleased BUSY is the permanent-wedge class
    // this whole pipeline is designed against.
    await clearLastError();
    const tab = await getActiveTab();
    if (!tab || !tab.id) throw new Error("No active tab.");
    if (tab.url && (tab.url.startsWith("chrome://") || tab.url.startsWith("chrome-extension://")
                    || tab.url.startsWith("edge://") || tab.url.includes("chromewebstore.google"))) {
      throw new Error(
        "Can't capture this page — Chrome blocks screenshots of " +
        "chrome:// and Web Store pages. Switch to any other tab and retry.",
      );
    }
    let payload;
    if (mode === "viewport") {
      PHASE = "uploading";
      const dataUrl = await captureViewport(tab);
      payload = {
        dataUrl,
        title: tab.title || "untitled",
        pageUrl: tab.url || "",
        dpr: 1,
        mode: "viewport",
      };
    } else if (mode === "region") {
      PHASE = "selecting";
      payload = { ...(await captureRegion(tab)), mode: "region" };
      PHASE = "uploading";
    } else {
      throw new Error("Unknown capture mode.");
    }
    uploadStarted = true;
    // Kick off the upload without holding this handler open. The
    // failure path MUST release BUSY and tell the user: past this
    // point the upload's own completion is the only other release,
    // and a silent catch here leaves the popup stuck on "Capturing…"
    // forever (this happened in the wild via a 403 from a stale build
    // writing outside the folder it was scoped to).
    autoUploadInBackground(payload, location, preferences).catch(async (e) => {
      console.error("[snipit-bg] auto-upload failed", e);
      BUSY = false;
      PHASE = null;
      // A location that answered 403/404 is not going to start working;
      // mark it so the popup can say which one broke and offer settings.
      // WORKSPACE_UNAVAILABLE belongs here too: it is Hub refusing to mint
      // for a workspace this person is no longer a member of, which is the
      // same "your saved location is gone" fact one level up. Without it
      // every capture failed identically forever and the popup kept
      // offering the capture buttons.
      if (STALE_LOCATION_CODES.has(e && e.code)) {
        await markLocationStale().catch(() => {});
      }
      const message = String(e && e.message || e);
      // Best-effort: a storage error must not eat the notification —
      // it's the user's only other failure signal.
      await setLastError(message).catch(() => {});
      chrome.notifications.create({
        type: "basic",
        iconUrl: chrome.runtime.getURL("icons/icon-128.png"),
        title: "SnipIt — upload failed",
        message,
      });
    });
  } finally {
    // Release only on early failure (no upload started). On success or
    // upload failure, autoUploadInBackground / its catch releases.
    if (!uploadStarted) { BUSY = false; PHASE = null; }
  }
}

// The upload starts as soon as the capture finishes; the PNG stays in this
// function's scope and nowhere else (see the storage note at the top).
async function autoUploadInBackground(payload, location, preferences) {
  await ensureOffscreen();
  const blob = dataUrlToBlob(payload.dataUrl);
  const result = await uploadCapture({
    blob,
    title: payload.title,
    pageUrl: payload.pageUrl,
    mode: payload.mode,
    devicePixelRatio: payload.dpr,
    location,
    preferences,
  });

  // The share URL is returned exactly ONCE and is never readable again, so
  // a clipboard write that silently fails strands a live link nobody holds.
  // The notification below therefore reports what actually happened, and on
  // failure carries the URL so it can still be copied by hand.
  const copied = result.link ? await copyToClipboard(result.link) : false;

  // Nothing about the capture is stored. The share URL is a credential and
  // belongs on the clipboard, not in extension storage. Revoking a link is
  // a console action on the share (design §4.4) — the extension deliberately
  // owns no revoke control, so it keeps no share id either.

  await chrome.notifications.create({
    type: "basic",
    iconUrl: chrome.runtime.getURL("icons/icon-128.png"),
    title: "Clipped to AgentDrive",
    message: captureOutcome(result, copied),
  });
  if (result.link && !copied) {
    // Also record it as a "failure" the popup will show, because the
    // notification may be muted at the OS level and this is the only other
    // chance to hand over a link that cannot be re-read.
    await setLastError(
      `Saved as ${result.name}, but the link couldn't be copied: ${result.link}`,
    ).catch(() => {});
  }

  if (preferences.open_console) {
    const { consoleBase } = await getEndpoints();
    try {
      await chrome.tabs.create({
        url:
          `${consoleBase}/drive/${encodeURIComponent(location.drive_id)}` +
          `/a/${encodeURIComponent(result.artifactId)}/`,
      });
    } catch (e) {
      console.error("[snipit-bg] open-console failed", e);
    }
  }

  // Capture pipeline is done — the clip is uploaded and the link, if any,
  // is on the clipboard. Ready for the next capture immediately.
  BUSY = false;
  PHASE = null;
}

// -- message router ---------------------------------------------------------

/** What to tell someone after a capture. Three facts, in the order they
 *  care about them: it saved, where the link is, and what went wrong. */
function captureOutcome(result, copied) {
  if (result.linkError) {
    return `Saved as ${result.name}. Couldn't create a link: ${result.linkError}`;
  }
  if (!result.link) return `Saved as ${result.name}`;
  return copied
    ? `${result.name} — link copied`
    : `Saved as ${result.name}. Couldn't copy the link — it's in the SnipIt popup.`;
}

/** Internal error tokens read like gibberish in the popup's error box. */
function friendlyMessage(message) {
  if (message === "NOT_SIGNED_IN") {
    return "Not signed in — open the SnipIt popup and sign in first.";
  }
  if (message === "NO_LOCATION") {
    return "Choose where captures should be saved in SnipIt's settings first.";
  }
  return message;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg.type === "sign-in") {
        try {
          await signIn();
          return sendResponse({ ok: true });
        } catch (e) {
          return sendResponse({ ok: false, error: String(e && e.message || e) });
        }
      }
      if (msg.type === "sign-out") {
        await signOut();
        await clearDriveTokens();
        // The location names another account's workspace, drive and folder.
        // Leaving it behind shows the next person to sign in on this
        // profile a save target they never chose — and then fails their
        // first capture with a sentence about a folder they have never
        // seen.
        await clearLocation();
        await chrome.storage.session.remove(LAST_ERROR_KEY);
        await closeOffscreen();
        return sendResponse({ ok: true });
      }
      if (msg.type === "open-options") {
        await chrome.runtime.openOptionsPage();
        return sendResponse({ ok: true });
      }
      if (msg.type === "capture") {
        // The popup fires-and-forgets and closes immediately, so any
        // error here surfaces via a system notification AND the
        // last_error record (the popup renders it on next open —
        // notifications alone are mute when the user has Chrome
        // alerts disabled at the OS level).
        try {
          await startCapture(msg.mode);
        } catch (e) {
          const message = String(e && e.message || e);
          console.error("[snipit-bg] capture", e);
          if (message !== "CANCELLED" && message !== "BUSY") {
            const friendly = friendlyMessage(message);
            await setLastError(friendly).catch(() => {});
            chrome.notifications.create({
              type: "basic",
              iconUrl: chrome.runtime.getURL("icons/icon-128.png"),
              title: "SnipIt — capture failed",
              message: friendly,
            });
          }
        }
        return sendResponse({ ok: true });
      }
      if (msg.type === "get-state") {
        const signedIn = await isSignedIn();
        const { location, preferences } = await readSettings();
        const stored = await chrome.storage.session.get(LAST_ERROR_KEY);
        return sendResponse({
          ok: true,
          signedIn,
          busy: BUSY,
          phase: PHASE,
          location,
          preferences,
          lastError: stored[LAST_ERROR_KEY] || null,
        });
      }
      return sendResponse({ ok: false, error: "unknown" });
    } catch (e) {
      console.error("[snipit-bg]", msg.type, e);
      return sendResponse({ ok: false, error: String(e && e.message || e) });
    }
  })();
  return true;  // async sendResponse
});
