// Regression tests for the service worker's BUSY/PHASE lifecycle.
// Run with (from extensions/snipit/): node --test "tests/**/*.test.js"
//
// These drive the REAL background.js through its chrome.runtime message
// listener, with `chrome.*` and `fetch` mocked. The lifecycle contract
// under test (which broke twice in the wild as a popup stuck on
// "Capturing…"):
//
//   1. BUSY covers capture→upload ONLY, and releases the moment the upload
//      finishes — never held for anything the user does afterwards.
//   2. Upload failure releases BUSY, records last_error, and raises a
//      notification (never a silent wedge).
//   3. Back-to-back captures work; concurrent capture during an in-flight
//      upload is rejected as BUSY without a notification.
//   4. Region-selection cancel (Esc) releases BUSY.
//   5. get-state reports phase "selecting" | "uploading" for the popup.
//
// Since the v0 rewrite it also pins the two refusals that must happen
// BEFORE a capture is taken: not signed in, and no saved location.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

// background.js's captureRegion schedules a 60s orphan-guard timeout
// that is never cleared on the success/cancel paths. Real SWs don't
// care; node's event loop would stay alive for the full 60s. Unref
// long timers so the test process can exit.
const _setTimeout = global.setTimeout;
global.setTimeout = (fn, ms, ...args) => {
  const t = _setTimeout(fn, ms, ...args);
  if (ms >= 10_000 && typeof t.unref === "function") t.unref();
  return t;
};

// Node has no canvas. The crop path (region captures) needs just
// enough shape to flow a fake PNG through: createImageBitmap →
// drawImage → convertToBlob. Pixel correctness is not under test
// here — the BUSY/PHASE lifecycle is.
global.createImageBitmap = async () => ({ width: 1, height: 1 });
global.OffscreenCanvas = class {
  constructor(w, h) { this.width = w; this.height = h; }
  getContext() { return { drawImage() {} }; }
  async convertToBlob() {
    return new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], { type: "image/png" });
  }
};

// ---------------------------------------------------------------------------
// chrome.* mock — just enough surface for background.js + its imports.
// ---------------------------------------------------------------------------

const messageListeners = [];   // chrome.runtime.onMessage handlers
const tabRemovedListeners = []; // chrome.tabs.onRemoved handlers
const state = {
  localStore: {},      // chrome.storage.local backing
  sessionStore: {},    // chrome.storage.session backing
  createdTabs: [],     // every chrome.tabs.create call
  notifications: [],   // every chrome.notifications.create call
  optionsOpened: 0,
  clipboard: [],       // every text copied via the offscreen document
  clipboardWorks: true,
  nextTabId: 100,
};

function pick(store, keys) {
  if (typeof keys === "string") keys = [keys];
  const out = {};
  for (const k of keys) if (k in store) out[k] = store[k];
  return out;
}

global.chrome = {
  runtime: {
    id: "kpdpkhkhinihhehlakjbdlloagcmpkok",
    getURL: (p) => `chrome-extension://kpdpkhkhinihhehlakjbdlloagcmpkok/${p}`,
    openOptionsPage: async () => { state.optionsOpened += 1; },
    onMessage: {
      addListener: (fn) => messageListeners.push(fn),
      removeListener: (fn) => {
        const i = messageListeners.indexOf(fn);
        if (i >= 0) messageListeners.splice(i, 1);
      },
    },
    // SW→offscreen clipboard copy; a real SW's sendMessage does not
    // loop back to its own onMessage, so just record and acknowledge.
    sendMessage: async (msg) => {
      if (msg && msg.target === "offscreen" && msg.type === "copy") {
        state.clipboard.push(msg.text);
        return { ok: state.clipboardWorks };
      }
      return { ok: true };
    },
  },
  storage: {
    local: {
      get: async (keys) => pick(state.localStore, keys),
      set: async (obj) => Object.assign(state.localStore, obj),
      remove: async (keys) => {
        for (const k of [].concat(keys)) delete state.localStore[k];
      },
    },
    session: {
      get: async (keys) => pick(state.sessionStore, keys),
      set: async (obj) => Object.assign(state.sessionStore, obj),
      remove: async (keys) => {
        for (const k of [].concat(keys)) delete state.sessionStore[k];
      },
    },
  },
  tabs: {
    query: async () => [{ id: 1, windowId: 1, title: "Test Page", url: "https://example.com/x" }],
    // 1x1 transparent PNG — small but structurally real enough for
    // dataUrlToBlob (which only base64-decodes).
    captureVisibleTab: async () =>
      "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
    create: async (opts) => {
      const tab = { id: state.nextTabId++, ...opts };
      state.createdTabs.push(tab);
      return tab;
    },
    onRemoved: { addListener: (fn) => tabRemovedListeners.push(fn) },
  },
  offscreen: {
    hasDocument: async () => true,   // skip createDocument entirely
    createDocument: async () => {},
    closeDocument: async () => {},
  },
  notifications: {
    create: async (opts) => { state.notifications.push(opts); return "nid"; },
  },
  management: {
    getSelf: async () => ({ installType: "development" }),
  },
  scripting: {
    executeScript: async () => [{}],   // overlay "injected"
  },
  identity: {
    getRedirectURL: () =>
      "https://kpdpkhkhinihhehlakjbdlloagcmpkok.chromiumapp.org/oauth2",
    launchWebAuthFlow: async () => {
      throw new Error("interactive sign-in is not exercised here");
    },
  },
};

Object.defineProperty(globalThis, "navigator", {
  value: { locks: { request: async (_n, _o, cb) => await cb() } },
  configurable: true,
  writable: true,
});

// ---------------------------------------------------------------------------
// Network mock — the v0 pipeline: mint a drive token, then create the
// artifact, then (optionally) a share.
// ---------------------------------------------------------------------------

const HUB = "https://auth.staging.tokencanopy.com";
const API = "https://drive.staging.tokencanopy.com";
const DRIVE = "drv_00000000000000a1";
const FOLDER = "fld_00000000000000b2";

let createResponder = null;
let createCalls = [];
let shareCalls = [];
/** Lets a test make hub refuse to mint — the membership-removed case. */
let mintStatus = 200;

function okCreateResponse(id = "art_0000000000000001", name = "test-090000.png") {
  return {
    ok: true,
    status: 201,
    json: async () => ({ id, name, revision: "rev_1" }),
    text: async () => "",
  };
}

global.fetch = async (url, opts = {}) => {
  const href = String(url);
  const method = (opts.method || "GET").toUpperCase();

  if (href.includes("/v0/snipit/agentdrive-token")) {
    // Echo the requested access: drive-token.js refuses a token minted at
    // a level it did not ask for.
    const requested = JSON.parse(String(opts.body ?? "{}")).access;
    if (mintStatus !== 200) {
      return {
        ok: false,
        status: mintStatus,
        json: async () => ({ error: "workspace_not_found" }),
        text: async () => "{}",
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        schema_version: 1,
        access_token: `drive-token-${requested}`,
        token_type: "Bearer",
        expires_in: 300,
        access: requested,
        scope:
          requested === "browse"
            ? "drives:read content:read"
            : "content:read content:write sharing:write",
        resource: API,
        api_base_url: API,
        workspace_id: "tcws_0000000000000001",
      }),
      text: async () => "",
    };
  }
  if (href.includes("/artifacts") && method === "POST") {
    createCalls.push({ url: href, opts });
    return await createResponder(href, opts);
  }
  if (href.includes("/shares") && method === "POST") {
    shareCalls.push({ url: href, opts });
    return {
      ok: true,
      status: 201,
      json: async () => ({ id: "shr_1", url: `${API}/s/secret/` }),
      text: async () => "",
    };
  }
  throw new Error(`unexpected fetch in test: ${method} ${href}`);
};

// ---------------------------------------------------------------------------
// Harness helpers
// ---------------------------------------------------------------------------

// Import AFTER the mocks exist — module registers its listeners on load.
await import("../../src/background/background.js");

function dispatch(msg, sender = {}) {
  return new Promise((resolve) => {
    for (const fn of [...messageListeners]) fn(msg, sender, resolve);
  });
}

async function getState() {
  return await dispatch({ type: "get-state" });
}

async function waitFor(predicate, label, timeoutMs = 2000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await predicate()) return;
    await new Promise((r) => _setTimeout(r, 10));
  }
  assert.fail(`timed out waiting for: ${label}`);
}

function signedInStore(overrides = {}) {
  return {
    hub_session: {
      accessToken: "hub-access-token",
      refreshToken: "hub-refresh-token",
      expiresAtMs: Date.now() + 10 * 60 * 1000,
      hubBase: HUB,
    },
    settings: {
      schema: 1,
      location: {
        workspace_id: "tcws_0000000000000001",
        workspace_name: "Acme",
        drive_id: DRIVE,
        drive_name: "Design drive",
        folder_id: FOLDER,
        folder_path: ["Screenshots"],
        stale: false,
      },
      preferences: {
        group_by_date: false,
        link: "share",
        open_console: false,
        strip_query: false,
        link_expiry_days: null,
      },
    },
    ...overrides,
  };
}

beforeEach(() => {
  state.localStore = signedInStore();
  state.sessionStore = {};
  state.createdTabs = [];
  state.notifications = [];
  state.clipboard = [];
  state.clipboardWorks = true;
  state.optionsOpened = 0;
  createCalls = [];
  shareCalls = [];
  mintStatus = 200;
  createResponder = async () => okCreateResponse();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("idle state: not busy, no phase, location reported", async () => {
  const s = await getState();
  assert.equal(s.ok, true);
  assert.equal(s.signedIn, true);
  assert.equal(s.busy, false);
  assert.equal(s.phase, null);
  assert.equal(s.location.drive_id, DRIVE);
});

test("viewport capture: BUSY releases when the upload finishes", async () => {
  // Hold the upload so we can observe the mid-pipeline state.
  let releaseUpload;
  createResponder = () => new Promise((res) => {
    releaseUpload = () => res(okCreateResponse("art_aaaaaaaaaaaaaaa1"));
  });

  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => (await getState()).busy, "busy during upload");
  // Wait for the request to actually be in flight: the pipeline mints a
  // drive token first, so "busy" arrives well before the create call.
  await waitFor(async () => createCalls.length === 1, "create in flight");
  assert.equal((await getState()).phase, "uploading");

  releaseUpload();
  await waitFor(async () => !(await getState()).busy, "busy released at upload end");
  assert.equal((await getState()).phase, null);
  assert.equal(state.clipboard.length, 1, "the link was copied");
});

test("upload failure: releases BUSY, notifies, records last_error — never a silent wedge", async () => {
  createResponder = async () => ({
    ok: false, status: 500,
    text: async () => "{}",
    json: async () => ({}),
  });

  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(
    async () => state.notifications.some((n) => n.title.includes("upload failed")),
    "failure notification",
  );
  const s = await getState();
  assert.equal(s.busy, false);
  assert.equal(s.phase, null);
  // The failure is recorded for the popup (notifications can be muted
  // at the OS level — last_error is the channel that always works).
  assert.ok(s.lastError && s.lastError.message.includes("500"), "lastError recorded");
  // ...and a subsequent successful capture clears it.
  createResponder = async () => okCreateResponse();
  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => !(await getState()).busy, "follow-up capture done");
  assert.equal((await getState()).lastError, null, "lastError cleared on next capture");
});

test("a 404 on the target folder marks the saved location stale", async () => {
  // The person needs to be told their folder is gone, not shown the same
  // failure on every capture from now on.
  createResponder = async () => ({
    ok: false, status: 404,
    text: async () => "{}",
    json: async () => ({ error: { code: "NOT_FOUND" } }),
  });

  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(
    async () => (await getState()).location?.stale === true,
    "location marked stale",
  );
  assert.equal((await getState()).busy, false);
});

test("back-to-back captures both succeed (multiple screenshots)", async () => {
  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => !(await getState()).busy, "first capture done");
  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => !(await getState()).busy, "second capture done");

  assert.equal(createCalls.length, 2);
  assert.equal(state.clipboard.length, 2);
});

test("the SW registers no tabs.onRemoved listener (open tabs are not tracked)", () => {
  assert.equal(tabRemovedListeners.length, 0);
});

test("concurrent capture during an in-flight upload is rejected quietly (BUSY)", async () => {
  let releaseUpload;
  createResponder = () => new Promise((res) => {
    releaseUpload = () => res(okCreateResponse());
  });

  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => createCalls.length === 1, "first create in flight");
  const notificationsBefore = state.notifications.length;

  await dispatch({ type: "capture", mode: "viewport" });

  // Rejected without a notification and without a second request.
  assert.equal(state.notifications.length, notificationsBefore);
  assert.equal(createCalls.length, 1, "no second create while busy");

  releaseUpload();
  await waitFor(async () => !(await getState()).busy, "capture finished");
});

test("region capture: phase=selecting while waiting; Esc cancel releases BUSY", async () => {
  // NOT awaited: the capture handler stays open for the whole selection,
  // so awaiting it here would deadlock against the cancel below.
  const done = dispatch({ type: "capture", mode: "region" });
  await waitFor(
    async () => (await getState()).phase === "selecting",
    "phase selecting",
  );
  assert.equal((await getState()).busy, true);

  // Content script reports Esc. Sender must match the captured tab.
  await dispatch({ type: "region-cancelled" }, { tab: { id: 1 } });
  await done;

  await waitFor(async () => !(await getState()).busy, "cancel released busy");
  assert.equal((await getState()).phase, null);
  // Cancelling is not a failure — no notification, no recorded error.
  assert.equal(state.notifications.length, 0);
  assert.equal((await getState()).lastError, null);
});

test("region capture: selection completes → uploads → idle", async () => {
  const done = dispatch({ type: "capture", mode: "region" });
  await waitFor(
    async () => (await getState()).phase === "selecting",
    "phase selecting",
  );

  await dispatch(
    {
      type: "region-selected",
      rect: { x: 0, y: 0, w: 10, h: 10 },
      dpr: 2,
      title: "Region Page",
      pageUrl: "https://example.com/region",
    },
    { tab: { id: 1 } },
  );
  await done;

  await waitFor(async () => !(await getState()).busy, "region capture done");
  assert.equal(createCalls.length, 1);
});

test("refuses to capture when signed out — before taking a screenshot", async () => {
  delete state.localStore.hub_session;

  await dispatch({ type: "capture", mode: "viewport" });

  await waitFor(
    async () => state.notifications.some((n) => n.message.includes("Not signed in")),
    "signed-out notification",
  );
  assert.equal(createCalls.length, 0);
  assert.equal((await getState()).busy, false);
});

test("refuses to capture with no saved location — before taking a screenshot", async () => {
  // Taking the screenshot and only then discovering there is nowhere to
  // put it wastes the moment the person was trying to capture.
  state.localStore.settings = { schema: 1, location: null, preferences: {} };

  await dispatch({ type: "capture", mode: "viewport" });

  await waitFor(
    async () => state.notifications.some((n) => n.message.includes("Choose where")),
    "no-location notification",
  );
  assert.equal(createCalls.length, 0);
  assert.equal((await getState()).busy, false);
});

test("sign-out clears the drive-token cache AND the saved location", async () => {
  // The location names another account's workspace, drive and folder. On a
  // shared profile, leaving it behind shows the next person to sign in a
  // save target they never chose.
  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => !(await getState()).busy, "capture done");
  assert.ok(state.sessionStore.drive_tokens, "a drive token was cached");

  await dispatch({ type: "sign-out" });

  assert.equal(state.localStore.hub_session, undefined);
  assert.equal(state.sessionStore.drive_tokens, undefined);
  assert.equal(state.localStore.settings.location, null);
  const after = await getState();
  assert.equal(after.signedIn, false);
  assert.equal(after.location, null);
});

test("a workspace hub will no longer mint for marks the location stale", async () => {
  // Membership removed. Without this the same error notification fires on
  // every capture forever and the popup keeps offering the capture buttons,
  // because only the FOLDER's 403/404 was treated as a stale location.
  mintStatus = 404;

  await dispatch({ type: "capture", mode: "viewport" });

  await waitFor(
    async () => (await getState()).location?.stale === true,
    "location marked stale after a workspace refusal",
  );
  assert.equal((await getState()).busy, false);
  assert.equal(createCalls.length, 0, "no upload was attempted");
});

test("a clipboard failure hands the link over instead of claiming it copied", async () => {
  // The share URL is returned exactly once and is never readable again, so
  // "link copied" when nothing was copied strands a live link nobody holds.
  state.clipboardWorks = false;

  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => !(await getState()).busy, "capture done");

  const [notification] = state.notifications;
  assert.ok(
    !/link copied/.test(notification.message),
    `must not claim a copy that failed: ${notification.message}`,
  );
  assert.match(notification.message, /couldn't copy/i);
  // And the URL itself reaches the popup, which is the only other channel.
  const { lastError } = await getState();
  assert.ok(lastError.message.includes("/s/secret/"), lastError.message);
});

test("open-options opens the settings page", async () => {
  await dispatch({ type: "open-options" });

  assert.equal(state.optionsOpened, 1);
});
