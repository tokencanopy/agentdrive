// The short-lived AgentDrive product token.
//
// Two credentials, deliberately: the HUB session (an identity, long-lived,
// `chrome.storage.local`) and the DRIVE token (product authority, five
// minutes, `chrome.storage.session`). The Hub session is never presented to
// AgentDrive, and the drive token is never presented to Hub.
//
// Session storage for the cache is the whole point of the lifetime: a
// product credential that survives a browser restart is a credential
// sitting on disk long after the capture it was minted for.

import { getEndpoints } from "./config.js";
import { hubToken } from "./hub-auth.js";

export const DRIVE_TOKEN_CACHE_KEY = "drive_tokens";

/** Re-mint this long before expiry, so an upload that starts now still has
 *  a valid token when its bytes land. */
const REFRESH_LEAD_MS = 30_000;

function driveError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

async function readCache() {
  try {
    const stored = await chrome.storage.session.get(DRIVE_TOKEN_CACHE_KEY);
    const cache = stored[DRIVE_TOKEN_CACHE_KEY];
    return cache && typeof cache === "object" && !Array.isArray(cache)
      ? cache
      : {};
  } catch {
    return {};
  }
}

async function writeCache(cache) {
  await chrome.storage.session.set({ [DRIVE_TOKEN_CACHE_KEY]: cache });
}

export async function clearDriveTokens() {
  await chrome.storage.session.remove(DRIVE_TOKEN_CACHE_KEY);
}

/**
 * Validate a mint response.
 *
 * `workspace_id` must equal what we asked for: a token for a DIFFERENT
 * workspace would be cached under the wrong key and then used to write into
 * a drive the user did not choose.
 */
function tokenFrom(body, { workspaceId, access, hubBase }) {
  const apiBase = (() => {
    if (typeof body?.api_base_url !== "string") return null;
    try {
      const url = new URL(body.api_base_url);
      if (url.protocol !== "http:" && url.protocol !== "https:") return null;
      if (url.username || url.password || url.hash || url.search) return null;
      return url.origin;
    } catch {
      return null;
    }
  })();

  if (
    body === null ||
    typeof body !== "object" ||
    typeof body.access_token !== "string" ||
    body.access_token.length === 0 ||
    body.access_token.length > 131_072 ||
    String(body.token_type).toLowerCase() !== "bearer" ||
    !Number.isSafeInteger(body.expires_in) ||
    body.expires_in <= 0 ||
    body.expires_in > 3_600 ||
    typeof body.scope !== "string" ||
    body.scope.length === 0 ||
    apiBase === null ||
    body.workspace_id !== workspaceId ||
    // The token we asked for, not merely a token. Caching a `capture`
    // token under the `browse` key would quietly restore the single wide
    // credential the split exists to remove.
    body.access !== access
  ) {
    throw driveError(
      "MINT_INVALID",
      "Token Canopy returned an unusable AgentDrive token.",
    );
  }
  return {
    accessToken: body.access_token,
    apiBase,
    scope: body.scope,
    access,
    workspaceId,
    hubBase,
    expiresAtMs: Date.now() + body.expires_in * 1000,
  };
}

async function mint(workspaceId, access, hubBase) {
  const bearer = await hubToken();
  let response;
  try {
    response = await fetch(`${hubBase}/v0/snipit/agentdrive-token`, {
      method: "POST",
      credentials: "omit",
      headers: {
        Authorization: `Bearer ${bearer}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ workspace_id: workspaceId, access }),
    });
  } catch {
    throw driveError(
      "DRIVE_UNAVAILABLE",
      "Couldn't reach Token Canopy. Check your connection and try again.",
    );
  }

  if (!response.ok) {
    // Each status means something the person can act on, so they do not
    // share one "something went wrong".
    if (response.status === 401 || response.status === 403) {
      throw driveError(
        "NOT_SIGNED_IN",
        "Your Token Canopy sign-in is no longer valid — sign in again.",
      );
    }
    if (response.status === 404) {
      throw driveError(
        "WORKSPACE_UNAVAILABLE",
        "You're no longer a member of the workspace SnipIt saves to — choose a new location in settings.",
      );
    }
    throw driveError(
      "DRIVE_UNAVAILABLE",
      `AgentDrive is unavailable right now (${response.status}).`,
    );
  }

  const body = await response.json().catch(() => null);
  return tokenFrom(body, { workspaceId, access, hubBase });
}

/**
 * A usable AgentDrive token for one workspace, at one access level.
 *
 * Cached per `workspace:access` pair, never per workspace: the two levels
 * are deliberately not supersets of each other, so a browse token cannot
 * stand in for a capture one and reusing either as the other would put the
 * old single wide credential back.
 *
 * @param {string} workspaceId
 * @param {"browse" | "capture"} access which token, by name
 * @param {{forceRefresh?: boolean}} [options] `forceRefresh` bypasses the
 *   cache — the 401-replay path, where AgentDrive has rejected a token the
 *   cache still believes in, so the cache is the thing that is wrong.
 */
export async function driveToken(workspaceId, access, options = {}) {
  if (access !== "browse" && access !== "capture") {
    throw driveError(
      "ACCESS_INVALID",
      `Unknown AgentDrive access level: ${String(access)}`,
    );
  }
  const { hubBase } = await getEndpoints();
  const cache = await readCache();
  const key = `${workspaceId}:${access}`;
  const cached = cache[key];

  if (
    !options.forceRefresh &&
    cached &&
    typeof cached.accessToken === "string" &&
    // A cache entry minted against another Hub carries another issuer's
    // token and another audience. Discard rather than present it.
    cached.hubBase === hubBase &&
    cached.access === access &&
    Number.isFinite(cached.expiresAtMs) &&
    cached.expiresAtMs - Date.now() > REFRESH_LEAD_MS
  ) {
    return cached;
  }

  const minted = await mint(workspaceId, access, hubBase);
  await writeCache({ ...cache, [key]: minted });
  return minted;
}
