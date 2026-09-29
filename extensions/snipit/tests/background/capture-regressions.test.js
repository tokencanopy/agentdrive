// Regression tests for two field bugs in the capture→upload pipeline.
// Run with (from extensions/snipit/): node --test "tests/**/*.test.js"
//
// Bug 1 — "Capture tab stops working after a few captures":
//   startCapture persisted the full PNG data URL into
//   chrome.storage.session and nothing deleted it on success. Chrome
//   caps storage.session at 10 MiB, so after a handful of full-tab
//   retina captures every subsequent write threw "quota exceeded" and
//   the capture died before it started. Region crops are 10–50× smaller,
//   which is why region kept working while Capture tab looked dead. The
//   session mock here ENFORCES the Chrome quota so the leak is a test
//   failure, not a field report.
//
//   Still live after the v0 rewrite, and now with a second thing to keep
//   out of session storage: the drive token cache lives there too, so the
//   budget is no longer "empty" but "small".
//
// Bug 2 — uploads fail on any page whose title isn't Latin-1:
//   the page title went raw into an `X-AgentDrive-Source` request header.
//   Fetch header values are ByteStrings; an em-dash / smart quote / CJK /
//   emoji title made fetch throw a TypeError before any network I/O.
//
//   The v0 rewrite moved provenance OFF headers and into the multipart
//   `metadata` field, which removes the ByteString constraint entirely —
//   so the bug cannot recur in its original form. The property that
//   still matters is the user-visible one: a unicode title must not fail
//   the capture, and must round-trip intact into the artifact's metadata.
//   The fetch mock still constructs `Headers` exactly as Chrome does, so
//   a future change that puts a title back in a header fails here.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

// Unref the 60s region orphan-guard timer (same dance as sw-state.test.js).
const _setTimeout = global.setTimeout;
global.setTimeout = (fn, ms, ...args) => {
  const t = _setTimeout(fn, ms, ...args);
  if (ms >= 10_000 && typeof t.unref === "function") t.unref();
  return t;
};

global.createImageBitmap = async () => ({ width: 1, height: 1 });
global.OffscreenCanvas = class {
  constructor(w, h) { this.width = w; this.height = h; }
  getContext() { return { drawImage() {} }; }
  async convertToBlob() {
    return new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], { type: "image/png" });
  }
};

// ---------------------------------------------------------------------------
// chrome.* mock — storage.session enforces Chrome's real 10 MiB quota.
// ---------------------------------------------------------------------------

// Chrome documents storage.session's QUOTA_BYTES (10,485,760) as an
// estimate of the dynamically allocated memory of keys + values (the
// key-length + JSON-size accounting below is the documented model for
// storage.local/sync). For multi-MB base64 strings the two models
// agree to within a small constant, so this mock reproduces the field
// failure faithfully — and the headline assertion ("no PNG in session
// storage at all") doesn't depend on the counting model either way.
const SESSION_QUOTA_BYTES = 10 * 1024 * 1024;

const HUB = "https://auth.staging.tokencanopy.com";
const API = "https://drive.staging.tokencanopy.com";
const DRIVE = "drv_00000000000000a1";
const FOLDER = "fld_00000000000000b2";

const messageListeners = [];
const state = {
  localStore: {},
  sessionStore: {},
  createdTabs: [],
  notifications: [],
  nextTabId: 100,
  activeTab: { id: 1, windowId: 1, title: "Test Page", url: "https://example.com/x" },
};

function sessionBytesUsed(store) {
  let n = 0;
  for (const [k, v] of Object.entries(store)) n += k.length + JSON.stringify(v).length;
  return n;
}

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
    openOptionsPage: async () => {},
    onMessage: {
      addListener: (fn) => messageListeners.push(fn),
      removeListener: (fn) => {
        const i = messageListeners.indexOf(fn);
        if (i >= 0) messageListeners.splice(i, 1);
      },
    },
    sendMessage: async () => ({ ok: true }),
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
      set: async (obj) => {
        const next = { ...state.sessionStore, ...obj };
        if (sessionBytesUsed(next) > SESSION_QUOTA_BYTES) {
          // Chrome's wording, via runtime.lastError → rejected promise.
          throw new Error("QUOTA_BYTES quota exceeded");
        }
        state.sessionStore = next;
      },
      remove: async (keys) => {
        for (const k of [].concat(keys)) delete state.sessionStore[k];
      },
    },
  },
  tabs: {
    query: async () => [state.activeTab],
    // A ~3.5 MB data URL — the realistic size of one full-tab retina
    // PNG. Structurally valid base64 so dataUrlToBlob can decode it.
    captureVisibleTab: async () =>
      "data:image/png;base64," + "A".repeat(3_500_000),
    create: async (opts) => {
      const tab = { id: state.nextTabId++, ...opts };
      state.createdTabs.push(tab);
      return tab;
    },
    onRemoved: { addListener: () => {} },
  },
  offscreen: {
    hasDocument: async () => true,
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
    executeScript: async () => [{}],
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
// fetch mock — still validates headers the way Chrome does, so a title
// that gets put back into a header fails here rather than in the field.
// ---------------------------------------------------------------------------

let createCalls = [];
let nextArtSeq = 0;

global.fetch = async (url, opts = {}) => {
  // Chrome's fetch throws a TypeError during Request construction if
  // any header value isn't a ByteString (char codes > 0xFF). Node's
  // undici Headers applies the identical WebIDL conversion, so this
  // line reproduces the in-browser failure mode exactly.
  new Headers(opts.headers || {});

  const href = String(url);
  const method = (opts.method || "GET").toUpperCase();

  if (href.includes("/v0/snipit/agentdrive-token")) {
    // Echo the requested access: drive-token.js refuses a token minted at
    // a level it did not ask for.
    const requested = JSON.parse(String(opts.body ?? "{}")).access;
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
    const seq = String(++nextArtSeq).padStart(16, "0");
    return {
      ok: true,
      status: 201,
      json: async () => ({ id: `art_${seq}`, name: "capture.png" }),
      text: async () => "",
    };
  }
  if (href.includes("/shares") && method === "POST") {
    return {
      ok: true,
      status: 201,
      json: async () => ({ id: "shr_1", url: `${API}/s/secret/` }),
      text: async () => "",
    };
  }
  throw new Error(`unexpected fetch in test: ${method} ${href}`);
};

await import("../../src/background/background.js");

function dispatch(msg, sender = {}) {
  return new Promise((resolve) => {
    for (const fn of [...messageListeners]) fn(msg, sender, resolve);
  });
}

async function getState() {
  return await dispatch({ type: "get-state" });
}

async function waitFor(predicate, label, timeoutMs = 5000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await predicate()) return;
    await new Promise((r) => _setTimeout(r, 10));
  }
  assert.fail(`timed out waiting for: ${label}`);
}

beforeEach(() => {
  state.localStore = {
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
  };
  state.sessionStore = {};
  state.createdTabs = [];
  state.notifications = [];
  state.activeTab = { id: 1, windowId: 1, title: "Test Page", url: "https://example.com/x" };
  createCalls = [];
  nextArtSeq = 0;
});

// ---------------------------------------------------------------------------
// Bug 1 — the capture payload must never reach storage.session
// ---------------------------------------------------------------------------

test("bug 1: a successful capture does not park the PNG in storage.session", async () => {
  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => !(await getState()).busy, "capture finished");
  await waitFor(async () => createCalls.length === 1, "capture uploaded");

  // The capture payload must not outlive the upload. Small bookkeeping
  // (the last-capture record, the drive token) is fine; a multi-MB PNG
  // is not.
  const used = sessionBytesUsed(state.sessionStore);
  assert.ok(
    used < 64 * 1024,
    `storage.session holds ${used} bytes after a successful capture — the PNG leaked`,
  );
});

test("bug 1: repeated full-tab captures keep working (quota never exhausts)", async () => {
  // Five back-to-back ~3.5 MB captures. With the leak, the third
  // capture's session write crosses the 10 MiB quota and every capture
  // from then on dies with a "capture failed" notification — which is
  // exactly the "Capture tab doesn't work any more" field report.
  for (let i = 0; i < 5; i++) {
    await dispatch({ type: "capture", mode: "viewport" });
    await waitFor(async () => !(await getState()).busy, `capture ${i + 1} finished`);
  }
  const failures = state.notifications.filter((n) => n.title.includes("failed"));
  assert.deepEqual(
    failures.map((n) => n.message),
    [],
    "no capture may fail on storage quota",
  );
  assert.equal(createCalls.length, 5, "every capture uploads");
});

// ---------------------------------------------------------------------------
// Bug 2 — a non-Latin-1 page title must not kill the upload
// ---------------------------------------------------------------------------

test("bug 2: a unicode page title uploads and round-trips into metadata", async () => {
  const title = "Foo — Bar’s café 🚀";
  state.activeTab = { id: 1, windowId: 1, title, url: "https://example.com/unicode" };

  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => !(await getState()).busy, "capture finished");

  const failures = state.notifications.filter((n) => n.title.includes("failed"));
  assert.deepEqual(failures.map((n) => n.message), [], "capture must not fail");
  assert.equal(createCalls.length, 1, "upload must reach the network");

  // Provenance now rides the multipart body, not a header — so the title
  // arrives intact rather than \u-escaped to survive a ByteString.
  const metadata = JSON.parse(createCalls[0].opts.body.get("metadata"));
  assert.equal(metadata.source.title, title);
  assert.equal(metadata.source.url, "https://example.com/unicode");

  // No request header carries the title any more. If one ever does again,
  // the `new Headers(...)` call in the fetch mock above throws first.
  for (const value of Object.values(createCalls[0].opts.headers ?? {})) {
    assert.ok(
      !String(value).includes("café"),
      "the page title must not travel in a request header",
    );
  }
});

test("bug 2: an emoji-only title still produces a usable artifact name", async () => {
  // The slug drops non-alphanumerics entirely, so this title reduces to
  // nothing — the name must fall back rather than come out as "-090000".
  state.activeTab = { id: 1, windowId: 1, title: "🚀🚀🚀", url: "https://example.com/emoji" };

  await dispatch({ type: "capture", mode: "viewport" });
  await waitFor(async () => !(await getState()).busy, "capture finished");

  const name = createCalls[0].opts.body.get("name");
  assert.match(name, /^untitled-\d{6}\.png$/);
});
