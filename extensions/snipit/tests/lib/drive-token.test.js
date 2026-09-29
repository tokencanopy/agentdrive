// Exchanging the Hub session for a short-lived AgentDrive token.
//
// The token is a product credential with real write authority, so the
// properties pinned here are about how little of it exists and how briefly:
// it lives in session storage (gone on browser restart), it is cached per
// workspace, and it is never presented to Hub or logged anywhere.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

import {
  installChromeMock,
  installFetchMock,
  jsonResponse,
} from "../support/chrome-mock.js";

installChromeMock();

const { ENVIRONMENTS } = await import("../../src/lib/config.js");
const { HUB_SESSION_KEY } = await import("../../src/lib/hub-auth.js");
const { driveToken, clearDriveTokens, DRIVE_TOKEN_CACHE_KEY } = await import(
  "../../src/lib/drive-token.js"
);

const HUB = ENVIRONMENTS.staging.hubBase;
const API = "https://drive.staging.tokencanopy.test";
const WORKSPACE = "tcws_0000000000000001";

function discoveryDocument() {
  return {
    issuer: `${HUB}/oidc`,
    authorization_endpoint: `${HUB}/oidc/auth`,
    token_endpoint: `${HUB}/oidc/token`,
    revocation_endpoint: `${HUB}/oidc/token/revocation`,
  };
}

const SCOPES = {
  browse: "drives:read content:read",
  capture: "content:read content:write sharing:write",
};

function mintBody(overrides = {}) {
  const access = overrides.access ?? "capture";
  return {
    schema_version: 1,
    access_token: "drive-token-1",
    token_type: "Bearer",
    expires_in: 300,
    access,
    scope: SCOPES[access] ?? SCOPES.capture,
    resource: API,
    api_base_url: API,
    workspace_id: WORKSPACE,
    ...overrides,
  };
}

let state;
let mintResponses;
let mintCalls;

function install(routes = []) {
  mintCalls = [];
  return installFetchMock([
    {
      match: "/.well-known/openid-configuration",
      respond: async () => jsonResponse(discoveryDocument()),
    },
    {
      match: "/v0/snipit/agentdrive-token",
      method: "POST",
      respond: async (url, init) => {
        mintCalls.push({ url, init });
        const next = mintResponses.shift();
        if (typeof next === "function") return await next(init);
        return jsonResponse(next ?? mintBody());
      },
    },
    ...routes,
  ]);
}

beforeEach(() => {
  state = installChromeMock({ installType: "development" });
  state.local[HUB_SESSION_KEY] = {
    accessToken: "hub-access-1",
    refreshToken: "hub-refresh-1",
    expiresAtMs: Date.now() + 3_600_000,
    hubBase: HUB,
  };
  mintResponses = [];
  install();
});

test("mints against Hub with the Hub bearer, the workspace and the access", async () => {
  mintResponses.push(mintBody());

  const token = await driveToken(WORKSPACE, "capture");

  assert.equal(token.accessToken, "drive-token-1");
  assert.equal(token.apiBase, API);
  assert.equal(token.workspaceId, WORKSPACE);
  assert.equal(mintCalls.length, 1);

  const [call] = mintCalls;
  assert.equal(call.url, `${HUB}/v0/snipit/agentdrive-token`);
  assert.equal(call.init.headers.Authorization, "Bearer hub-access-1");
  assert.equal(call.init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(call.init.body), {
    workspace_id: WORKSPACE,
    access: "capture",
  });
});

test("browse and capture are cached separately — neither stands in for the other", async () => {
  // The two levels are deliberately not supersets. Reusing one as the
  // other would restore the single wide credential the split removes.
  mintResponses.push(mintBody({ access: "browse", access_token: "browse-1" }));
  mintResponses.push(mintBody({ access: "capture", access_token: "capture-1" }));

  const browse = await driveToken(WORKSPACE, "browse");
  const capture = await driveToken(WORKSPACE, "capture");

  assert.equal(browse.accessToken, "browse-1");
  assert.equal(capture.accessToken, "capture-1");
  assert.equal(browse.scope, SCOPES.browse);
  assert.equal(capture.scope, SCOPES.capture);
  assert.equal(mintCalls.length, 2, "one mint each, no sharing");

  // And each is served from its own cache entry on the next ask.
  assert.equal((await driveToken(WORKSPACE, "browse")).accessToken, "browse-1");
  assert.equal((await driveToken(WORKSPACE, "capture")).accessToken, "capture-1");
  assert.equal(mintCalls.length, 2);
});

test("refuses a token minted at an access level we did not ask for", async () => {
  // Hub answering `browse` to a `capture` request would otherwise be
  // cached under the capture key and used to write.
  mintResponses.push(mintBody({ access: "browse" }));

  await assert.rejects(() => driveToken(WORKSPACE, "capture"), (error) => {
    assert.equal(error.code, "MINT_INVALID");
    return true;
  });
});

test("refuses an access level that is not one of the two", async () => {
  for (const access of ["admin", "", null, undefined, "BROWSE"]) {
    await assert.rejects(() => driveToken(WORKSPACE, access), (error) => {
      assert.equal(error.code, "ACCESS_INVALID");
      return true;
    });
  }
  assert.equal(mintCalls.length, 0, "nothing is requested");
});

test("caches per workspace and does not re-mint while fresh", async () => {
  mintResponses.push(mintBody());
  await driveToken(WORKSPACE, "capture");

  const again = await driveToken(WORKSPACE, "capture");

  assert.equal(again.accessToken, "drive-token-1");
  assert.equal(mintCalls.length, 1);
});

test("mints separately for a different workspace", async () => {
  const other = "tcws_0000000000000002";
  mintResponses.push(mintBody());
  mintResponses.push(
    mintBody({ access_token: "drive-token-2", workspace_id: other }),
  );

  const first = await driveToken(WORKSPACE, "capture");
  const second = await driveToken(other, "capture");

  assert.equal(first.accessToken, "drive-token-1");
  assert.equal(second.accessToken, "drive-token-2");
  assert.equal(mintCalls.length, 2);
});

test("re-mints once the cached token is near expiry", async () => {
  mintResponses.push(mintBody({ expires_in: 20 }));
  await driveToken(WORKSPACE, "capture");
  mintResponses.push(mintBody({ access_token: "drive-token-2" }));

  const refreshed = await driveToken(WORKSPACE, "capture");

  assert.equal(refreshed.accessToken, "drive-token-2");
  assert.equal(mintCalls.length, 2);
});

test("forceRefresh re-mints even when the cache looks fresh", async () => {
  // The 401-replay path: AgentDrive rejected a token the cache still
  // believes in, so the cache is wrong and must be bypassed.
  mintResponses.push(mintBody());
  await driveToken(WORKSPACE, "capture");
  mintResponses.push(mintBody({ access_token: "drive-token-2" }));

  const forced = await driveToken(WORKSPACE, "capture", { forceRefresh: true });

  assert.equal(forced.accessToken, "drive-token-2");
  assert.equal(mintCalls.length, 2);
});

test("the token lives in session storage, never in local", async () => {
  // A five-minute product credential has no business surviving a browser
  // restart, and `local` is what does.
  mintResponses.push(mintBody());

  await driveToken(WORKSPACE, "capture");

  assert.ok(state.session[DRIVE_TOKEN_CACHE_KEY]);
  assert.equal(state.local[DRIVE_TOKEN_CACHE_KEY], undefined);
  assert.ok(
    !JSON.stringify(state.local).includes("drive-token-1"),
    "no drive token in local storage",
  );
});

test("clearDriveTokens empties the cache", async () => {
  mintResponses.push(mintBody());
  await driveToken(WORKSPACE, "capture");

  await clearDriveTokens();

  assert.equal(state.session[DRIVE_TOKEN_CACHE_KEY], undefined);
  mintResponses.push(mintBody({ access_token: "drive-token-2" }));
  assert.equal((await driveToken(WORKSPACE, "capture")).accessToken, "drive-token-2");
});

test("a workspace Hub refuses surfaces as a stale-location error", async () => {
  mintResponses.push(() => jsonResponse({ error: "workspace_not_found" }, 404));

  await assert.rejects(() => driveToken(WORKSPACE, "capture"), (error) => {
    assert.equal(error.code, "WORKSPACE_UNAVAILABLE");
    assert.match(error.message, /workspace/i);
    return true;
  });
});

test("an unauthenticated mint clears nothing but reports sign-in", async () => {
  mintResponses.push(() => jsonResponse({ error: "unauthenticated" }, 401));

  await assert.rejects(() => driveToken(WORKSPACE, "capture"), (error) => {
    assert.equal(error.code, "NOT_SIGNED_IN");
    return true;
  });
});

test("a service outage is distinguishable from a refusal", async () => {
  mintResponses.push(() =>
    jsonResponse({ error: "agentdrive_unavailable" }, 503),
  );

  await assert.rejects(() => driveToken(WORKSPACE, "capture"), (error) => {
    assert.equal(error.code, "DRIVE_UNAVAILABLE");
    return true;
  });
});

test("rejects a mint response that is missing or malformed", async () => {
  for (const body of [
    mintBody({ access_token: "" }),
    mintBody({ token_type: "mac" }),
    mintBody({ expires_in: 0 }),
    mintBody({ expires_in: 99_999 }),
    mintBody({ api_base_url: "not-a-url" }),
    mintBody({ workspace_id: "tcws_somebody_else" }),
    {},
  ]) {
    state = installChromeMock({ installType: "development" });
    state.local[HUB_SESSION_KEY] = {
      accessToken: "hub-access-1",
      refreshToken: "hub-refresh-1",
      expiresAtMs: Date.now() + 3_600_000,
      hubBase: HUB,
    };
    mintResponses = [body];
    install();

    await assert.rejects(
      () => driveToken(WORKSPACE, "capture"),
      `should reject ${JSON.stringify(body)}`,
    );
  }
});

test("a cache entry from another Hub is discarded, not presented", async () => {
  mintResponses.push(mintBody());
  await driveToken(WORKSPACE, "capture");

  const cache = state.session[DRIVE_TOKEN_CACHE_KEY];
  const key = `${WORKSPACE}:capture`;
  state.session[DRIVE_TOKEN_CACHE_KEY] = {
    ...cache,
    [key]: { ...cache[key], hubBase: "https://auth.other.test" },
  };
  mintResponses.push(mintBody({ access_token: "drive-token-2" }));

  const token = await driveToken(WORKSPACE, "capture");

  assert.equal(token.accessToken, "drive-token-2");
});
