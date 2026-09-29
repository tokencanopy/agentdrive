// Sign-in against Token Canopy Hub, as an OAuth public client.
//
// Replaces the extension's original handshake, which redeemed a ticket at
// AgentDrive's own `/auth/extension/*` endpoints. AgentDrive stopped being
// an authorization server at the v0 contract reset; Hub is the only one now,
// and the extension talks to it the way any native public client does:
// PKCE S256 through `chrome.identity.launchWebAuthFlow`, no client secret,
// a rotating refresh token.
//
// The session this module holds is a HUB session — an identity, not a
// product credential. It is never sent to AgentDrive. `drive-token.js`
// exchanges it for a short-lived, audience-bound drive token.

import { getEndpoints, OAUTH } from "./config.js";
import { createAttempt } from "./pkce.js";

/**
 * `chrome.storage.local` — on disk, because the sign-in must survive a
 * browser restart. That is a deliberate asymmetry with the drive token,
 * which is session-only precisely so a PRODUCT credential does not outlive
 * the capture it was minted for (drive-token.js).
 *
 * The refresh token here can mint more of those, so the asymmetry buys
 * blast radius and lifetime, not secrecy: a five-minute product token
 * cannot be replayed later, while this one is revocable at Hub and is what
 * "sign in once" costs. Sign-out revokes it.
 */
export const HUB_SESSION_KEY = "hub_session";
/**
 * The PKCE attempt is NOT persisted.
 *
 * An earlier revision wrote it to `chrome.storage.session` and never read
 * it back — the flow uses the in-scope value throughout — so the write was
 * pure exposure: the verifier and state sat readable by every extension
 * context for the length of the interactive flow, and a service-worker
 * teardown mid-`launchWebAuthFlow` skipped the cleanup and left both there
 * until the browser restarted, with no code path that would ever use or
 * remove them.
 *
 * Losing the attempt on a teardown is the CORRECT failure: the callback
 * then has no verifier, sign-in fails closed, and the person clicks again.
 */

/** Refresh this long before expiry, so a request that starts now still has
 *  a valid token when it lands. */
const REFRESH_LEAD_MS = 60_000;

/** Discovery is immutable for an origin in practice; cache per hubBase so a
 *  capture does not pay for it. Module-scope, so a service-worker restart
 *  re-fetches — which is the correct lifetime, not a bug. */
const discoveryCache = new Map();

function authError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

/**
 * Fetch and validate Hub's OpenID configuration.
 *
 * Every endpoint must live on the SAME origin as the configured Hub. An
 * authorization or token endpoint on another origin would send the
 * authorization code — and with it the refresh token — somewhere Hub does
 * not control, which is the one thing this document must never be able to
 * do to us.
 */
export async function discovery(hubBase) {
  const cached = discoveryCache.get(hubBase);
  if (cached) return cached;

  let document;
  try {
    const response = await fetch(
      `${hubBase}/oidc/.well-known/openid-configuration`,
      { credentials: "omit" },
    );
    if (!response.ok) {
      throw authError(
        "DISCOVERY_FAILED",
        `Token Canopy sign-in is unavailable (${response.status}).`,
      );
    }
    document = await response.json();
  } catch (cause) {
    if (cause.code) throw cause;
    throw authError(
      "DISCOVERY_FAILED",
      "Couldn't reach Token Canopy. Check your connection and try again.",
    );
  }

  const endpoints = {
    authorization_endpoint: document?.authorization_endpoint,
    token_endpoint: document?.token_endpoint,
    revocation_endpoint: document?.revocation_endpoint,
  };
  for (const [name, value] of Object.entries(endpoints)) {
    if (typeof value !== "string" || !onOrigin(value, hubBase)) {
      throw authError(
        "DISCOVERY_INVALID",
        `Token Canopy returned an unusable ${name.replace(/_/g, " ")}.`,
      );
    }
  }
  discoveryCache.set(hubBase, endpoints);
  return endpoints;
}

function onOrigin(value, origin) {
  try {
    const url = new URL(value);
    return (
      url.origin === origin &&
      url.protocol === new URL(origin).protocol &&
      !url.username &&
      !url.password &&
      !url.hash
    );
  } catch {
    return false;
  }
}

/**
 * The callback `launchWebAuthFlow` returned, checked against the redirect
 * URI we asked for: same origin, same path, no credentials, no fragment.
 * Anything else and we do not look at its parameters at all.
 */
function exactCallback(callback, redirectUri) {
  let actual;
  let expected;
  try {
    actual = new URL(callback);
    expected = new URL(redirectUri);
  } catch {
    return null;
  }
  if (
    actual.origin !== expected.origin ||
    actual.pathname !== expected.pathname ||
    actual.username ||
    actual.password ||
    actual.hash
  ) {
    return null;
  }
  return actual;
}

export async function readSession() {
  try {
    const stored = await chrome.storage.local.get(HUB_SESSION_KEY);
    const session = stored[HUB_SESSION_KEY];
    if (
      session === null ||
      typeof session !== "object" ||
      typeof session.accessToken !== "string" ||
      typeof session.refreshToken !== "string" ||
      typeof session.hubBase !== "string" ||
      !Number.isFinite(session.expiresAtMs)
    ) {
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

async function writeSession(session) {
  await chrome.storage.local.set({ [HUB_SESSION_KEY]: session });
}

async function clearSession() {
  await chrome.storage.local.remove(HUB_SESSION_KEY);
}

export async function isSignedIn() {
  return (await readSession()) !== null;
}

/**
 * Validate and normalize a token response.
 *
 * A rotating refresh token that comes back IDENTICAL to the one we spent
 * means rotation did not happen — treated as a failure rather than stored,
 * because continuing would leave two callers believing they hold a live
 * token when only one does.
 */
function sessionFrom(body, hubBase, previousRefreshToken = null) {
  if (
    body === null ||
    typeof body !== "object" ||
    typeof body.access_token !== "string" ||
    body.access_token.length === 0 ||
    body.access_token.length > 131_072 ||
    typeof body.refresh_token !== "string" ||
    body.refresh_token.length === 0 ||
    body.refresh_token.length > 131_072 ||
    String(body.token_type).toLowerCase() !== "bearer" ||
    !Number.isSafeInteger(body.expires_in) ||
    body.expires_in <= 0 ||
    body.expires_in > 604_800 ||
    (previousRefreshToken !== null &&
      body.refresh_token === previousRefreshToken)
  ) {
    throw authError(
      "TOKEN_INVALID",
      "Token Canopy returned an unusable sign-in response.",
    );
  }
  return {
    accessToken: body.access_token,
    refreshToken: body.refresh_token,
    expiresAtMs: Date.now() + body.expires_in * 1000,
    hubBase,
  };
}

async function postForm(url, params) {
  const response = await fetch(url, {
    method: "POST",
    credentials: "omit",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(params).toString(),
  });
  const body = await response.json().catch(() => null);
  return { response, body };
}

/**
 * Run the interactive sign-in.
 *
 * Throws on every failure, including the user closing the window. The
 * caller renders the message; nothing is stored unless the whole flow
 * completed.
 */
export async function signIn() {
  const { hubBase } = await getEndpoints();
  const redirectUri = chrome.identity.getRedirectURL(OAUTH.redirectPath);
  if (redirectUri !== OAUTH.redirectUri) {
    // The manifest pins the extension id with the Web Store item's public
    // key precisely so this holds. If it does not, the build would send a
    // callback Hub has never heard of, and the failure would surface as an
    // opaque `redirect_uri_mismatch` from the provider instead of here.
    throw authError(
      "CONFIGURATION_INVALID",
      "This build of SnipIt can't use Token Canopy sign-in — its extension ID does not match the published one.",
    );
  }

  const endpoints = await discovery(hubBase);
  const attempt = await createAttempt();

  {
    const authorize = new URL(endpoints.authorization_endpoint);
    authorize.search = new URLSearchParams({
      client_id: OAUTH.clientId,
      redirect_uri: redirectUri,
      response_type: "code",
      scope: OAUTH.scope,
      code_challenge: attempt.challenge,
      code_challenge_method: "S256",
      state: attempt.state,
      // LOAD-BEARING, not politeness. Hub grants `offline_access` only on a
      // consented authorization; without this the provider takes its
      // silent-SSO path and returns an access token with NO refresh token,
      // and every sign-in then dies an hour later with no way to renew.
      // Verified over the wire in apps/hub/test/e2e/snipit-live.ts.
      prompt: "consent",
    }).toString();

    const callback = await chrome.identity.launchWebAuthFlow({
      url: authorize.toString(),
      interactive: true,
    });
    const callbackUrl = exactCallback(callback, redirectUri);
    if (!callbackUrl) {
      throw authError(
        "CALLBACK_INVALID",
        "Sign-in returned to an unexpected address and was stopped.",
      );
    }

    // Exactly one `state`, equal to ours. `getAll` rather than `get`: a
    // duplicated parameter is smuggling, and `get` would read only the
    // first.
    const states = callbackUrl.searchParams.getAll("state");
    if (states.length !== 1 || states[0] !== attempt.state) {
      throw authError(
        "CALLBACK_INVALID",
        "Sign-in could not be verified and was stopped.",
      );
    }

    const returnedError = callbackUrl.searchParams.get("error");
    if (returnedError) {
      throw authError(
        "AUTHORIZATION_REFUSED",
        returnedError === "access_denied"
          ? "Sign-in was declined (access_denied)."
          : `Token Canopy refused the sign-in (${returnedError}).`,
      );
    }

    const codes = callbackUrl.searchParams.getAll("code");
    if (codes.length !== 1 || codes[0].length === 0) {
      throw authError(
        "CALLBACK_INVALID",
        "Sign-in returned no authorization code.",
      );
    }

    const { response, body } = await postForm(endpoints.token_endpoint, {
      grant_type: "authorization_code",
      client_id: OAUTH.clientId,
      redirect_uri: redirectUri,
      code: codes[0],
      code_verifier: attempt.verifier,
    });
    if (!response.ok) {
      throw authError(
        "TOKEN_EXCHANGE_FAILED",
        `Sign-in could not be completed (${body?.error ?? response.status}).`,
      );
    }
    const session = sessionFrom(body, hubBase);
    await writeSession(session);
    return session;
  }
}

/** The OAuth errors that mean this refresh token is dead. Anything else —
 *  a 5xx, a proxy error, a dropped connection — is Hub having a bad moment,
 *  and destroying the grant over one of those costs the user a full
 *  interactive sign-in for no reason. `hubToken` runs on every drive-token
 *  mint, so a transient blip is not a rare event. */
const TERMINAL_REFRESH_ERRORS = new Set([
  "invalid_grant",
  "invalid_client",
  "unauthorized_client",
]);

/**
 * Spend the refresh token for a new session.
 *
 * Clears the session ONLY on a definitive refusal. The two failure kinds are
 * distinguishable — a refusal comes back as a 4xx carrying an OAuth `error`,
 * a transport failure throws out of `fetch` — and treating them the same
 * turned one 502 into "sign in again".
 */
async function refresh(session) {
  const endpoints = await discovery(session.hubBase);
  let response;
  let body;
  try {
    ({ response, body } = await postForm(endpoints.token_endpoint, {
      grant_type: "refresh_token",
      client_id: OAUTH.clientId,
      refresh_token: session.refreshToken,
    }));
  } catch {
    // Kept: the token may well still be good.
    throw authError(
      "REFRESH_UNAVAILABLE",
      "Couldn't reach Token Canopy to refresh your sign-in. Check your connection and try again.",
    );
  }

  if (!response.ok) {
    const oauthError = typeof body?.error === "string" ? body.error : null;
    const terminal =
      response.status >= 400 &&
      response.status < 500 &&
      oauthError !== null &&
      TERMINAL_REFRESH_ERRORS.has(oauthError);
    if (!terminal) {
      throw authError(
        "REFRESH_UNAVAILABLE",
        `Token Canopy couldn't refresh your sign-in right now (${oauthError ?? response.status}). Try again in a moment.`,
      );
    }
    await clearSession();
    throw authError(
      "REFRESH_REFUSED",
      `Your Token Canopy sign-in expired (${oauthError}) — sign in again.`,
    );
  }

  let next;
  try {
    next = sessionFrom(body, session.hubBase, session.refreshToken);
  } catch (cause) {
    // A response we cannot use is not a session we can keep: the stored
    // refresh token was spent to get it.
    await clearSession();
    throw cause;
  }
  await writeSession(next);
  return next;
}

/**
 * A usable Hub access token, refreshing first if it is close to expiry.
 *
 * The refresh runs under a Web Lock. A rotating refresh token may be spent
 * exactly once: the popup and the service worker both call this, and two
 * concurrent refreshes would invalidate the grant and sign the user out for
 * no reason. Inside the lock we re-read the session, so the second caller
 * finds the first one's result and never sends a request at all.
 */
export async function hubToken() {
  const { hubBase } = await getEndpoints();
  const current = await readSession();
  if (!current) {
    throw authError("NOT_SIGNED_IN", "Sign in to SnipIt first.");
  }
  if (current.hubBase !== hubBase) {
    // The endpoints moved under a session minted elsewhere. Its tokens are
    // meaningless here, and presenting them would fail as an auth error
    // rather than as the configuration change it actually is.
    await clearSession();
    throw authError(
      "ENDPOINT_CHANGED",
      "The Token Canopy environment changed — sign in again.",
    );
  }
  if (current.expiresAtMs - Date.now() > REFRESH_LEAD_MS) {
    return current.accessToken;
  }

  return await navigator.locks.request(
    "snipit-hub-refresh",
    { mode: "exclusive" },
    async () => {
      const latest = await readSession();
      if (!latest) {
        throw authError("NOT_SIGNED_IN", "Sign in to SnipIt first.");
      }
      if (latest.expiresAtMs - Date.now() > REFRESH_LEAD_MS) {
        return latest.accessToken;
      }
      const refreshed = await refresh(latest);
      return refreshed.accessToken;
    },
  );
}

/**
 * Sign out.
 *
 * Revocation is best effort; clearing local state is not. Being unable to
 * reach Hub must never leave someone signed in with no way out.
 */
export async function signOut() {
  const session = await readSession();
  await clearSession();
  if (!session) return;
  try {
    const endpoints = await discovery(session.hubBase);
    await postForm(endpoints.revocation_endpoint, {
      token: session.refreshToken,
      token_type_hint: "refresh_token",
      client_id: OAUTH.clientId,
    });
  } catch {
    /* best effort — local state is already gone */
  }
}
