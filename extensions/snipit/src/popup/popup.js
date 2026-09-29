// Popup UI — renders one of five states from the SW's reported state:
//   * SIGNED_OUT   — "Sign in"
//   * NO_LOCATION  — "Choose where captures go" (settings)
//   * STALE        — the saved location stopped working; same call to action
//   * BUSY         — spinner + phase label
//   * IDLE         — capture buttons, the current location, "Open in drive"
//
// While the SW reports busy, the popup re-polls get-state every POLL_MS and
// settles on its own when the pipeline finishes. The popup used to read
// state exactly once on load, so opening it mid-upload showed the spinner
// forever — "still says Capturing… after the screenshot is done"
// (tests/popup/popup-refresh.test.js pins the recovery).

import { getEndpoints } from "../lib/config.js";

const POLL_MS = 500;

const body = document.getElementById("body");
const footer = document.getElementById("footer");

let state = {
  signedIn: false,
  busy: false,
  lastError: null,
  location: null,
  preferences: null,
};
let lastError = null;       // popup-local (message send failed)
let pollTimer = null;

async function refresh() {
  try {
    const r = await chrome.runtime.sendMessage({ type: "get-state" });
    if (r && r.ok) {
      state = r;
    }
  } catch (e) {
    console.error("[snipit-popup] state read failed", e);
  }
  render();
  // Single poll chain: re-arm only here, only while busy. Extra
  // refresh() calls (e.g. from send()) reset the timer instead of
  // forking a second chain.
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
  if (state.busy) pollTimer = setTimeout(refresh, POLL_MS);
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

function button(text, opts = {}) {
  const b = document.createElement("button");
  b.textContent = text;
  if (opts.className) b.className = opts.className;
  if (opts.disabled) b.disabled = true;
  if (opts.onClick) b.addEventListener("click", opts.onClick);
  return b;
}

function paragraph(text, className) {
  const p = document.createElement("p");
  p.className = className || "lede";
  p.textContent = text;
  return p;
}

function errorBox(text) {
  const err = document.createElement("div");
  err.className = "error";
  err.textContent = text;
  return err;
}

async function send(type, extra = {}) {
  lastError = null;
  try {
    const r = await chrome.runtime.sendMessage({ type, ...extra });
    if (!r || !r.ok) {
      lastError = (r && r.error) || "Something went wrong.";
    }
    return r;
  } catch (e) {
    lastError = String(e && e.message || e);
    return null;
  } finally {
    await refresh();
  }
}

/** "Acme › Design drive › Screenshots" — shown before a capture, so a wrong
 *  default is visible in advance rather than discovered afterwards. */
function locationLine(location) {
  return [
    location.workspace_name,
    location.drive_name,
    ...(location.folder_path ?? []),
  ]
    .filter(Boolean)
    .join(" › ");
}

function openSettingsButton(label = "Open settings") {
  return button(label, {
    className: "secondary",
    onClick: () => {
      chrome.runtime.sendMessage({ type: "open-options" });
      window.close();
    },
  });
}

// Deliberately synchronous: refresh() calls it unawaited, so an await
// in here would let two renders interleave on the same DOM.
function render() {
  clear(body);
  clear(footer);

  if (!state.signedIn) {
    body.appendChild(
      paragraph(
        "Sign in once to capture screenshots straight into your AgentDrive.",
      ),
    );
    body.appendChild(button("Sign in", { onClick: async () => {
      await send("sign-in");
      window.close();
    }}));
    if (lastError) body.appendChild(errorBox(lastError));
  } else if (!state.location) {
    // Fail closed: with several workspaces or drives there is no safe
    // guess, so the extension asks instead of picking one.
    body.appendChild(
      paragraph("Choose where SnipIt should save captures before your first one."),
    );
    body.appendChild(openSettingsButton("Choose where captures go"));
    if (lastError) body.appendChild(errorBox(lastError));
  } else if (state.busy) {
    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.alignItems = "center";
    row.style.gap = "8px";
    const sp = document.createElement("div");
    sp.className = "spinner";
    row.appendChild(sp);
    // Distinct labels per pipeline phase — one ambiguous "Capturing…"
    // made waiting-for-drag indistinguishable from a wedged upload.
    const label = state.phase === "selecting"
      ? "Waiting for selection…"
      : state.phase === "uploading"
        ? "Uploading…"
        : "Capturing…";
    const p = paragraph(label);
    p.style.margin = "0";
    row.appendChild(p);
    if (state.phase === "selecting") {
      const hint = paragraph("Drag on the page, or press Esc there to cancel.", "hint");
      hint.style.margin = "6px 0 0";
      body.appendChild(row);
      body.appendChild(hint);
    } else {
      body.appendChild(row);
    }
  } else {
    if (state.location.stale) {
      body.appendChild(
        errorBox(
          `SnipIt can't reach ${locationLine(state.location)} any more — it may have been deleted or your access removed.`,
        ),
      );
      body.appendChild(openSettingsButton("Choose a new location"));
    } else {
      body.appendChild(
        button("Capture region", {
          onClick: () => {
            // Close immediately so the popup isn't in the way during the
            // drag (the SW does the work asynchronously; errors surface
            // via chrome.notifications and the lastError record below).
            chrome.runtime.sendMessage({ type: "capture", mode: "region" });
            window.close();
          },
        }),
      );
      body.appendChild(
        button("Capture tab", {
          className: "secondary",
          onClick: () => {
            chrome.runtime.sendMessage({ type: "capture", mode: "viewport" });
            window.close();
          },
        }),
      );

      // Where the next capture will land, stated before it happens.
      const where = paragraph(locationLine(state.location), "hint");
      where.style.margin = "8px 0 0";
      body.appendChild(where);

      body.appendChild(
        button("Open in drive", {
          className: "secondary",
          onClick: async () => {
            const { consoleBase } = await getEndpoints();
            chrome.tabs.create({
              url:
                `${consoleBase}/drive/${encodeURIComponent(state.location.drive_id)}` +
                `/f/${encodeURIComponent(state.location.folder_id)}/`,
            });
          },
        }),
      );
    }

    // The SW records why the last capture failed (chrome.storage.session,
    // via get-state). Notifications alone were the only error channel
    // before, and with Chrome alerts muted at the OS level every failure
    // looked like "nothing happened".
    if (state.lastError && state.lastError.message) {
      body.appendChild(errorBox(`Last capture failed: ${state.lastError.message}`));
    }
    if (lastError) body.appendChild(errorBox(lastError));
  }

  // Footer: settings, plus sign out when applicable.
  if (state.signedIn) {
    const settings = openSettingsButton("Settings");
    settings.style.width = "auto";
    footer.appendChild(settings);
    const out = button("Sign out", { className: "danger", onClick: async () => {
      await send("sign-out");
    }});
    out.style.width = "auto";
    footer.appendChild(out);
  }
}

refresh();
