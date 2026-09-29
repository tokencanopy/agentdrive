// Sign-in against Token Canopy Hub.
//
// The extension is an OAuth public client: PKCE S256 through
// `chrome.identity.launchWebAuthFlow`, a refresh token that rotates, and no
// client secret anywhere. The properties pinned here are the ones whose
// absence is a vulnerability rather than a bug:
//
//   * the verifier never leaves session storage, and never rides the
//     authorize URL;
//   * the callback is checked for an exact origin+path match and a single
//     `state` that equals the one we sent;
//   * a refresh is single-flighted, because a rotating refresh token used
//     twice concurrently invalidates the whole grant;
//   * a session minted against one Hub is not reused against another.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

import {
  installChromeMock,
  installFetchMock,
  jsonResponse,
} from "../support/chrome-mock.js";

installChromeMock();

const { OAUTH, ENVIRONMENTS } = await import("../../src/lib/config.js");
const { signIn, signOut, hubToken, readSession, HUB_SESSION_KEY } =
  await import("../../src/lib/hub-auth.js");

const HUB = ENVIRONMENTS.staging.hubBase;
const REDIRECT = OAUTH.redirectUri;

function discoveryDocument() {
  return {
    issuer: `${HUB}/oidc`,
    authorization_endpoint: `${HUB}/oidc/auth`,
    token_endpoint: `${HUB}/oidc/token`,
    revocation_endpoint: `${HUB}/oidc/token/revocation`,
  };
}

let state;
let tokenResponses;
let revocations;

function routes(overrides = {}) {
  return [
    {
      match: "/.well-known/openid-configuration",
      respond: async () =>
        overrides.discovery
          ? await overrides.discovery()
          : jsonResponse(discoveryDocument()),
    },
    {
      match: "/oidc/token/revocation",
      method: "POST",
      respond: async (_url, init) => {
        revocations.push(String(init.body));
        return jsonResponse({}, 200);
      },
    },
    {
      match: "/oidc/token",
      method: "POST",
      respond: async (_url, init) => {
        const body = new URLSearchParams(String(init.body));
        const next = tokenResponses.shift();
        if (typeof next === "function") return await next(body);
        return jsonResponse(next ?? { error: "no response queued" }, 200);
      },
    },
  ];
}

function tokenBody(overrides = {}) {
  return {
    access_token: "hub-access-1",
    refresh_token: "hub-refresh-1",
    token_type: "Bearer",
    expires_in: 3600,
    ...overrides,
  };
}

/** Answer the auth flow the way Hub would: echo the state we were sent. */
function successfulCallback(code = "auth-code-1") {
  return (details) => {
    const sent = new URL(details.url).searchParams;
    return `${REDIRECT}?code=${code}&state=${sent.get("state")}`;
  };
}

beforeEach(() => {
  state = installChromeMock({ installType: "development" });
  tokenResponses = [];
  revocations = [];
  installFetchMock(routes());
});

test("signs in with PKCE S256 and stores the session", async () => {
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody());

  await signIn();

  const [flow] = state.authFlows;
  const authorize = new URL(flow.url);
  assert.equal(authorize.origin + authorize.pathname, `${HUB}/oidc/auth`);
  assert.equal(authorize.searchParams.get("client_id"), OAUTH.clientId);
  assert.equal(authorize.searchParams.get("redirect_uri"), REDIRECT);
  assert.equal(authorize.searchParams.get("response_type"), "code");
  assert.equal(authorize.searchParams.get("scope"), OAUTH.scope);
  assert.equal(authorize.searchParams.get("code_challenge_method"), "S256");
  assert.ok(authorize.searchParams.get("code_challenge"));
  assert.ok(authorize.searchParams.get("state"));
  assert.equal(flow.interactive, true);
  // Hub grants `offline_access` only on a CONSENTED authorization. Drop
  // this and sign-in still appears to work, then expires in an hour with
  // no refresh token — a failure nobody would connect to this line.
  assert.equal(authorize.searchParams.get("prompt"), "consent");

  // The verifier is the secret half — it must never ride the URL, and it
  // must never be written anywhere either. It lives only in the closure of
  // the in-flight sign-in, so a service-worker teardown loses it and the
  // flow fails closed rather than leaving a usable secret behind.
  assert.equal(authorize.searchParams.get("code_verifier"), null);
  assert.deepEqual(state.session, {}, "nothing is persisted for the attempt");

  const session = await readSession();
  assert.equal(session.accessToken, "hub-access-1");
  assert.equal(session.refreshToken, "hub-refresh-1");
  assert.equal(session.hubBase, HUB);
  assert.ok(session.expiresAtMs > Date.now());
});

test("sends the verifier — not the challenge — on the code exchange", async () => {
  state.authFlowResponder = successfulCallback();
  let exchanged;
  tokenResponses.push((body) => {
    exchanged = body;
    return jsonResponse(tokenBody());
  });

  await signIn();

  const challenge = new URL(state.authFlows[0].url).searchParams.get(
    "code_challenge",
  );
  assert.equal(exchanged.get("grant_type"), "authorization_code");
  assert.equal(exchanged.get("client_id"), OAUTH.clientId);
  assert.equal(exchanged.get("redirect_uri"), REDIRECT);
  assert.equal(exchanged.get("code"), "auth-code-1");
  assert.ok(exchanged.get("code_verifier"));
  assert.notEqual(exchanged.get("code_verifier"), challenge);
  // No secret: this is a public client.
  assert.equal(exchanged.get("client_secret"), null);
});

test("leaves no PKCE secret behind, whether sign-in succeeds or fails", async () => {
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody());
  await signIn();
  assert.deepEqual(state.session, {});

  state.authFlowResponder = () => {
    throw new Error("The user closed the window.");
  };
  await assert.rejects(() => signIn());
  assert.deepEqual(state.session, {});
});

test("refuses a callback whose state does not match the request", async () => {
  state.authFlowResponder = () => `${REDIRECT}?code=abc&state=not-the-state`;

  await assert.rejects(() => signIn(), /sign-in/i);
  assert.equal(await readSession(), null);
});

test("refuses a callback carrying more than one state", async () => {
  // Parameter smuggling: `getAll("state")` must be exactly one entry, or a
  // proxy could append its own and a naive `get()` would read the first.
  state.authFlowResponder = (details) => {
    const sent = new URL(details.url).searchParams.get("state");
    return `${REDIRECT}?code=abc&state=${sent}&state=injected`;
  };

  await assert.rejects(() => signIn(), /sign-in/i);
  assert.equal(await readSession(), null);
});

test("refuses a callback on any other origin or path", async () => {
  for (const callback of [
    "https://attacker.example.test/oauth2?code=abc",
    `${REDIRECT}/extra?code=abc`,
    "https://kpdpkhkhinihhehlakjbdlloagcmpkok.chromiumapp.org/evil?code=abc",
  ]) {
    state = installChromeMock({ installType: "development" });
    installFetchMock(routes());
    state.authFlowResponder = (details) => {
      const sent = new URL(details.url).searchParams.get("state");
      return `${callback}&state=${sent}`;
    };

    await assert.rejects(() => signIn(), /sign-in/i);
    assert.equal(await readSession(), null);
  }
});

test("surfaces an error the authorization server returned", async () => {
  state.authFlowResponder = (details) => {
    const sent = new URL(details.url).searchParams.get("state");
    return `${REDIRECT}?error=access_denied&state=${sent}`;
  };

  await assert.rejects(() => signIn(), /access_denied|declined/i);
  assert.equal(await readSession(), null);
});

test("refuses a discovery document pointing off the Hub origin", async () => {
  // An endpoint on another origin would send the code — and the refresh
  // token — somewhere Hub does not control.
  installFetchMock(
    routes({
      discovery: async () =>
        jsonResponse({
          ...discoveryDocument(),
          token_endpoint: "https://attacker.example.test/token",
        }),
    }),
  );
  state.authFlowResponder = successfulCallback();

  await assert.rejects(() => signIn(), /Token Canopy|endpoint/i);
});

test("hubToken returns the stored token while it is fresh", async () => {
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody());
  await signIn();

  const token = await hubToken();

  assert.equal(token, "hub-access-1");
  // No refresh call was needed.
  assert.equal(tokenResponses.length, 0);
});

test("hubToken refreshes near expiry and honours token rotation", async () => {
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody({ expires_in: 30 }));
  await signIn();

  tokenResponses.push((body) => {
    assert.equal(body.get("grant_type"), "refresh_token");
    assert.equal(body.get("refresh_token"), "hub-refresh-1");
    return jsonResponse(
      tokenBody({ access_token: "hub-access-2", refresh_token: "hub-refresh-2" }),
    );
  });

  const token = await hubToken();

  assert.equal(token, "hub-access-2");
  const session = await readSession();
  assert.equal(session.refreshToken, "hub-refresh-2");
});

test("single-flights concurrent refreshes — a rotating token must be spent once", async () => {
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody({ expires_in: 30 }));
  await signIn();

  let refreshCalls = 0;
  tokenResponses.push(async () => {
    refreshCalls += 1;
    await new Promise((resolve) => setTimeout(resolve, 10));
    return jsonResponse(
      tokenBody({ access_token: "hub-access-2", refresh_token: "hub-refresh-2" }),
    );
  });

  const tokens = await Promise.all([hubToken(), hubToken(), hubToken()]);

  assert.equal(refreshCalls, 1, "the refresh token is spent exactly once");
  assert.deepEqual(tokens, ["hub-access-2", "hub-access-2", "hub-access-2"]);
});

test("a refused refresh clears the session rather than retrying forever", async () => {
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody({ expires_in: 30 }));
  await signIn();

  tokenResponses.push(() =>
    jsonResponse({ error: "invalid_grant" }, 400),
  );

  await assert.rejects(() => hubToken(), /sign in|signed out|invalid_grant/i);
  assert.equal(await readSession(), null);
});

test("a transient failure KEEPS the session — one 502 must not sign you out", async () => {
  // hubToken() runs on every drive-token mint, so a blip is not rare. The
  // old code cleared on any refresh failure, turning a bad moment at Hub
  // into a full interactive sign-in.
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody({ expires_in: 30 }));
  await signIn();

  tokenResponses.push(() => jsonResponse({ error: "server_error" }, 503));
  await assert.rejects(() => hubToken(), (error) => {
    assert.equal(error.code, "REFRESH_UNAVAILABLE");
    return true;
  });
  assert.notEqual(await readSession(), null, "the session survives a 503");

  // And a dropped connection is the same story.
  installFetchMock([
    {
      match: "/.well-known/openid-configuration",
      respond: async () => jsonResponse(discoveryDocument()),
    },
    {
      match: "/oidc/token",
      respond: async () => {
        throw new TypeError("Failed to fetch");
      },
    },
  ]);
  await assert.rejects(() => hubToken(), (error) => {
    assert.equal(error.code, "REFRESH_UNAVAILABLE");
    return true;
  });
  assert.notEqual(await readSession(), null, "the session survives a dropped connection");
});

test("a 4xx that is not a terminal OAuth error also keeps the session", async () => {
  // A 429 or a proxy's 400 says nothing about whether the grant is alive.
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody({ expires_in: 30 }));
  await signIn();

  tokenResponses.push(() => jsonResponse({ error: "slow_down" }, 429));

  await assert.rejects(() => hubToken(), /REFRESH_UNAVAILABLE|couldn't refresh/i);
  assert.notEqual(await readSession(), null);
});

test("a session minted against another Hub is not reused", async () => {
  // Switching the endpoint override must not leave a staging token being
  // presented to a local Hub, which would fail in a confusing way at best.
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody());
  await signIn();

  state.local[HUB_SESSION_KEY] = {
    ...state.local[HUB_SESSION_KEY],
    hubBase: "https://auth.other.example.test",
  };

  await assert.rejects(() => hubToken(), /sign in|signed out/i);
  assert.equal(await readSession(), null);
});

test("signOut revokes the refresh token and clears local state", async () => {
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody());
  await signIn();

  await signOut();

  assert.equal(await readSession(), null);
  assert.equal(revocations.length, 1);
  const revoked = new URLSearchParams(revocations[0]);
  assert.equal(revoked.get("token"), "hub-refresh-1");
  assert.equal(revoked.get("token_type_hint"), "refresh_token");
  assert.equal(revoked.get("client_id"), OAUTH.clientId);
});

test("signOut clears local state even when revocation fails", async () => {
  // Being unable to reach Hub must not leave the user signed in locally
  // with no way out.
  state.authFlowResponder = successfulCallback();
  tokenResponses.push(tokenBody());
  await signIn();

  installFetchMock([
    {
      match: "/.well-known/openid-configuration",
      respond: async () => jsonResponse(discoveryDocument()),
    },
    {
      match: "/oidc/token/revocation",
      respond: async () => {
        throw new Error("network down");
      },
    },
  ]);

  await signOut();

  assert.equal(await readSession(), null);
});
