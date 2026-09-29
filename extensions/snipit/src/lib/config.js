// Every origin the extension talks to, resolved in ONE place.
//
// Nothing else in the extension may hardcode a URL: the modules ask
// `getEndpoints()` and join paths onto what comes back. That is what lets a
// developer point an unpacked build at a local Hub + AgentDrive stack
// without editing a file, and what keeps a packaged build pinned to
// production no matter what is in extension storage.
//
// What deliberately does NOT vary by environment: the OAuth client id and
// redirect URI below. Both derive from the extension id, which the manifest
// pins with the Web Store item's public key, and
// `chrome.identity.getRedirectURL` offers the extension no other callback.
// A local Hub therefore registers the same client — it never has to reach
// the redirect URI, because `launchWebAuthFlow` intercepts it in the
// browser.

/** The two shipped environments. `custom` is not here: it exists only as a
 *  developer override (see `getEndpoints`). */
export const ENVIRONMENTS = Object.freeze({
  production: Object.freeze({
    hubBase: "https://auth.tokencanopy.com",
    apiBase: "https://drive.tokencanopy.com",
    consoleBase: "https://app.tokencanopy.com",
    shareBase: "https://share.tokencanopy.com",
  }),
  staging: Object.freeze({
    hubBase: "https://auth.staging.tokencanopy.com",
    apiBase: "https://drive.staging.tokencanopy.com",
    consoleBase: "https://app.staging.tokencanopy.com",
    shareBase: "https://share.staging.tokencanopy.com",
  }),
});

/** The fixed OAuth identity — see the note at the top of this file. */
export const OAUTH = Object.freeze({
  clientId: "snipit-chrome-kpdpkhkhinihhehlakjbdlloagcmpkok",
  /** Passed to `chrome.identity.getRedirectURL`; the resulting URL must
   *  equal `REDIRECT_URI` or the build cannot sign in. */
  redirectPath: "oauth2",
  redirectUri:
    "https://kpdpkhkhinihhehlakjbdlloagcmpkok.chromiumapp.org/oauth2",
  /** `profile` is deliberately absent: a capture tool has no use for a
   *  name. `offline_access` is present because the extension must survive a
   *  browser restart without re-prompting. */
  scope: "openid email offline_access",
});

export const ENDPOINT_OVERRIDE_KEY = "endpoint_override";

const ENDPOINT_KEYS = ["hubBase", "apiBase", "consoleBase", "shareBase"];

/**
 * Validate one endpoint set, returning a normalized copy or `null`.
 *
 * Every value must be an absolute `http(s)` ORIGIN — no path, no userinfo,
 * no fragment — because these are joined as `${base}/v0/...`. A base
 * carrying a path would silently produce a URL nobody wrote, and userinfo
 * in a base is how a credential ends up in a request line.
 *
 * @param {unknown} value
 * @returns {{hubBase: string, apiBase: string, consoleBase: string, shareBase: string} | null}
 */
export function validateEndpoints(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const out = {};
  for (const key of ENDPOINT_KEYS) {
    const raw = value[key];
    if (typeof raw !== "string" || raw.length === 0 || raw.length > 2048) {
      return null;
    }
    let url;
    try {
      url = new URL(raw);
    } catch {
      return null;
    }
    if (
      (url.protocol !== "http:" && url.protocol !== "https:") ||
      url.username ||
      url.password ||
      url.hash ||
      url.search ||
      (url.pathname !== "/" && url.pathname !== "")
    ) {
      return null;
    }
    out[key] = url.origin;
  }
  return out;
}

/** True for an unpacked ("Load unpacked") install. Conservative: anything
 *  that cannot be determined counts as packaged, so a failure to read the
 *  management API can only ever route traffic at production, never at a
 *  developer's override. */
async function isUnpackedInstall() {
  try {
    const self = await chrome.management.getSelf();
    return self.installType === "development";
  } catch {
    return false;
  }
}

/** The stored developer override, or `null`. Readable from any install so
 *  the options page can show what is set; only OBEYED when unpacked. */
export async function readEndpointOverride() {
  try {
    const stored = await chrome.storage.local.get(ENDPOINT_OVERRIDE_KEY);
    return validateEndpoints(stored[ENDPOINT_OVERRIDE_KEY]);
  } catch {
    return null;
  }
}

/**
 * Point an unpacked build at another stack.
 *
 * Refuses on a packaged install rather than writing a value that would be
 * ignored — a silent no-op here would read as "the override does not work".
 *
 * The CALLER is responsible for clearing credentials afterwards: tokens
 * minted by one issuer are meaningless to another, and a drive token
 * carries the old audience. `src/options/options.js` does that.
 */
export async function setEndpointOverride(value) {
  if (!(await isUnpackedInstall())) {
    throw new Error(
      "Endpoint overrides are ignored on a packaged install — this build always uses production.",
    );
  }
  const validated = validateEndpoints(value);
  if (!validated) throw new Error("Every endpoint must be an http(s) origin.");
  await chrome.storage.local.set({ [ENDPOINT_OVERRIDE_KEY]: validated });
  return validated;
}

export async function clearEndpointOverride() {
  await chrome.storage.local.remove(ENDPOINT_OVERRIDE_KEY);
}

/**
 * The endpoints this install talks to right now.
 *
 * @returns {Promise<{hubBase: string, apiBase: string, consoleBase: string,
 *   shareBase: string, env: "production" | "staging" | "custom"}>}
 */
export async function getEndpoints() {
  if (!(await isUnpackedInstall())) {
    return { ...ENVIRONMENTS.production, env: "production" };
  }
  const override = await readEndpointOverride();
  if (override) return { ...override, env: "custom" };
  return { ...ENVIRONMENTS.staging, env: "staging" };
}
