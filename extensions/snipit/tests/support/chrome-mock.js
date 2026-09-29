// A `chrome.*` test double, shared by the lib suites.
//
// The background suites (tests/background/) keep their own richer mock
// because they drive the real service worker through its message listener
// and need tabs, offscreen documents and notifications. This one covers the
// surface the lib modules touch: storage, identity, management and runtime.
//
// Install it BEFORE importing any module under test — the modules read
// `chrome` at call time, not at import time, but `navigator.locks` and
// `crypto` are read at import time by the auth modules.

/** @typedef {{ installType: "development" | "normal" }} SelfInfo */

export function installChromeMock(options = {}) {
  const state = {
    local: { ...(options.local ?? {}) },
    session: { ...(options.session ?? {}) },
    /** Every launchWebAuthFlow call, so a test can assert the authorize URL. */
    authFlows: [],
    installType: options.installType ?? "normal",
    extensionId: options.extensionId ?? "kpdpkhkhinihhehlakjbdlloagcmpkok",
    /** Set by a test to answer the next launchWebAuthFlow. A function so a
     *  test can vary the answer per call, or throw to simulate the user
     *  closing the window. */
    authFlowResponder:
      options.authFlowResponder ??
      (() => {
        throw new Error("no authFlowResponder configured");
      }),
  };

  function pick(store, keys) {
    if (keys === null || keys === undefined) return { ...store };
    const list = typeof keys === "string" ? [keys] : keys;
    const out = {};
    for (const key of list) if (key in store) out[key] = store[key];
    return out;
  }

  function area(name) {
    return {
      get: async (keys) => pick(state[name], keys),
      set: async (obj) => {
        Object.assign(state[name], obj);
      },
      remove: async (keys) => {
        for (const key of [].concat(keys)) delete state[name][key];
      },
      clear: async () => {
        for (const key of Object.keys(state[name])) delete state[name][key];
      },
    };
  }

  globalThis.chrome = {
    runtime: {
      id: state.extensionId,
      getURL: (path) => `chrome-extension://${state.extensionId}/${path}`,
      lastError: undefined,
    },
    storage: { local: area("local"), session: area("session") },
    management: {
      getSelf: async () => ({ installType: state.installType }),
    },
    identity: {
      getRedirectURL: (path = "") =>
        `https://${state.extensionId}.chromiumapp.org/${path}`,
      launchWebAuthFlow: async (details) => {
        state.authFlows.push(details);
        return await state.authFlowResponder(details, state.authFlows.length);
      },
    },
  };

  // `navigator.locks` is how the auth module single-flights a refresh across
  // a popup and a service worker. Node has no Web Locks; this serializes
  // per name, which is the property the code depends on.
  const held = new Map();
  // Node's `navigator` is a getter-only global, so define the property
  // rather than assigning through it.
  if (!globalThis.navigator) {
    Object.defineProperty(globalThis, "navigator", {
      value: {},
      configurable: true,
      writable: true,
    });
  }
  Object.defineProperty(globalThis.navigator, "locks", {
    configurable: true,
    writable: true,
    value: {
      request: async (name, _options, callback) => {
        const previous = held.get(name) ?? Promise.resolve();
        let release;
        const current = new Promise((resolve) => {
          release = resolve;
        });
        held.set(
          name,
          previous.then(() => current),
        );
        await previous;
        try {
          return await callback();
        } finally {
          release();
        }
      },
    },
  });

  return state;
}

/** Build a fetch double that answers a route table and records every call. */
export function installFetchMock(routes) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const method = (init.method ?? "GET").toUpperCase();
    const href = String(url);
    calls.push({ url: href, method, init });
    for (const route of routes) {
      if (route.method && route.method !== method) continue;
      if (typeof route.match === "function" ? route.match(href) : href.includes(route.match)) {
        return await route.respond(href, init, calls.length);
      }
    }
    throw new Error(`unexpected fetch in test: ${method} ${href}`);
  };
  return calls;
}

/** A `Response`-shaped object. `node:test` runs without undici's Response
 *  being convenient to construct for every case, and the modules only use
 *  `ok`, `status`, `json()` and `text()`. */
export function jsonResponse(body, status = 200, headers = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(headers),
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}
