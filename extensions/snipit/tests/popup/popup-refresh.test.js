// Regression test for the stale-popup bug ("still says Capturing…
// after the screenshot is done").
//
// popup.js read SW state exactly once, on load. Open the popup while
// an upload is in flight and it rendered the spinner forever — the
// upload finishing never re-rendered it. The popup must re-poll while
// the SW reports busy and settle to the idle UI when the pipeline
// completes.
//
// Drives the REAL popup.js with a ~50-line DOM stub: just enough of
// document/window/chrome for popup.js's render path.

import { test } from "node:test";
import assert from "node:assert/strict";

// ---------------------------------------------------------------------------
// Minimal DOM stub
// ---------------------------------------------------------------------------

function makeElement(tag) {
  return {
    tag,
    children: [],
    style: {},
    className: "",
    textContent: "",
    disabled: false,
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
    },
    get firstChild() { return this.children[0] || null; },
    addEventListener() {},
  };
}

function allText(el) {
  let out = el.textContent || "";
  for (const c of el.children) out += " " + allText(c);
  return out;
}

const body = makeElement("main");
const footer = makeElement("footer");

global.document = {
  getElementById: (id) => ({ body, footer }[id] || null),
  createElement: makeElement,
};
global.window = { close() {} };

// ---------------------------------------------------------------------------
// chrome mock — get-state serves whatever `swState` currently holds,
// so the test can flip the SW from "uploading" to "done" underneath
// the popup, exactly like a finishing upload does.
// ---------------------------------------------------------------------------

const LOCATION = {
  workspace_id: "tcws_test",
  workspace_name: "Acme",
  drive_id: "drv_test",
  drive_name: "Design drive",
  folder_id: "fld_test",
  folder_path: ["Screenshots"],
  stale: false,
};

let swState = {
  ok: true,
  signedIn: true,
  busy: true,
  phase: "uploading",
  location: LOCATION,
  preferences: { group_by_date: true, link: "share" },
  lastError: null,
};

global.chrome = {
  runtime: {
    sendMessage: async (msg) => {
      if (msg.type === "get-state") return { ...swState };
      return { ok: true };
    },
  },
  tabs: { create: async () => ({}) },
  // popup.js reads the console origin through lib/config.js.
  management: { getSelf: async () => ({ installType: "development" }) },
  storage: {
    local: { get: async () => ({}), set: async () => {}, remove: async () => {} },
    session: { get: async () => ({}), set: async () => {}, remove: async () => {} },
  },
};

// Import AFTER the mocks exist — popup.js renders on load.
await import("../../src/popup/popup.js");

async function waitFor(predicate, label, timeoutMs = 3000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  assert.fail(`timed out waiting for: ${label}`);
}

test("popup re-polls while busy and settles to idle when the upload finishes", async () => {
  // Popup opened mid-upload: spinner with the uploading label.
  await waitFor(() => allText(body).includes("Uploading"), "busy UI rendered");

  // The upload finishes AFTER the popup rendered. The SW now reports
  // idle with a fresh clip.
  swState = {
    ok: true,
    signedIn: true,
    busy: false,
    phase: null,
    location: LOCATION,
    preferences: { group_by_date: true, link: "share" },
    lastError: null,
  };

  // The popup must notice on its own — no reopen, no click.
  await waitFor(
    () => allText(body).includes("Capture region"),
    "popup left the spinner state after the SW went idle",
  );
  // The "Last clip" URL display was removed — captures land in the chosen
  // folder, reachable via the persistent "Open in drive" button.
  assert.ok(
    allText(body).includes("Open in drive"),
    "persistent Open in drive button shown once idle",
  );
  assert.ok(
    !allText(body).includes("art_0000000000000001"),
    "no artifact id rendered in the popup",
  );
  // Where the next capture will land, stated BEFORE it happens — a wrong
  // default is otherwise only discoverable after the fact.
  assert.ok(
    allText(body).includes("Acme › Design drive › Screenshots"),
    "the save location is shown",
  );
});

/**
 * Render the popup fresh against `nextState`.
 *
 * The popup reads state once on open and then polls ONLY while the SW
 * reports busy — so once it has settled to idle there is no poller left to
 * observe a change. Opening the popup is what re-reads, and a
 * cache-busting import is the closest thing to opening it again.
 */
let instance = 0;
async function openPopupOn(nextState) {
  swState = nextState;
  body.children.length = 0;
  footer.children.length = 0;
  await import(`../../src/popup/popup.js?instance=${++instance}`);
}

test("with no saved location the popup asks instead of offering a capture", async () => {
  await openPopupOn({
    ok: true,
    signedIn: true,
    busy: false,
    phase: null,
    location: null,
    preferences: {},
    lastError: null,
  });

  await waitFor(
    () => allText(body).includes("Choose where captures go"),
    "the location prompt replaced the capture buttons",
  );
  assert.ok(
    !allText(body).includes("Capture region"),
    "capture must not be offered with nowhere to save",
  );
});

test("a stale location is named, and the fix is offered", async () => {
  await openPopupOn({
    ok: true,
    signedIn: true,
    busy: false,
    phase: null,
    location: { ...LOCATION, stale: true },
    preferences: {},
    lastError: null,
  });

  await waitFor(
    () => allText(body).includes("Choose a new location"),
    "the stale-location prompt rendered",
  );
  const text = allText(body);
  // Naming it is the point: the person has to recognise which location
  // broke to know what to pick instead.
  assert.ok(text.includes("Acme › Design drive › Screenshots"), "names the location");
  assert.ok(!text.includes("Capture region"), "capture is not offered");
});
