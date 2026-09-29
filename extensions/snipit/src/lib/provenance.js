// What a capture records about where it came from.
//
// Written into the artifact's `metadata` at create time and read back by the
// TokenCanopy console, which renders `source.url` as a link. Two consequences
// shape everything here:
//
//   1. The console renders it, so a URL that is not http(s) must never be
//      recorded — the reader refuses those too, but the write side is where
//      it is cheapest to be certain.
//   2. AgentDrive indexes `metadata` for search and does not bound it, so
//      every field here is length-capped.
//
// The shape is a platform CONVENTION, not a server schema: any client that
// writes captures — a future share-sheet target, say — writes these keys so
// the console's Source row works for all of them.

/** Identifies the client and version that wrote the capture, so support can
 *  tell builds apart. Bump with the manifest version. */
export const PRODUCER = "snipit-chrome/0.4.0";

const MAX_URL_LENGTH = 2048;
const MAX_TITLE_LENGTH = 256;

/**
 * The one URL rule, shared by the writer here and by anything else that
 * needs it.
 *
 * `http(s)` only, no credentials, bounded length, and no leading or
 * trailing whitespace (which is how a `javascript:` URL gets smuggled past
 * a naive prefix check).
 *
 * @param {unknown} value
 * @returns {string | null}
 */
export function sanitizeSourceUrl(value) {
  if (typeof value !== "string" || value.length === 0) return null;
  if (value !== value.trim()) return null;
  if (value.length > MAX_URL_LENGTH) return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  // Never record a credential, whatever else the settings say.
  url.username = "";
  url.password = "";
  const cleaned = url.toString();
  return cleaned.length > MAX_URL_LENGTH ? null : cleaned;
}

function cleanTitle(value) {
  if (typeof value !== "string") return null;
  // Control characters would ride into a JSON field the console renders.
  const stripped = value.replace(/[\u0000-\u001F\u007F]/g, "").trim();
  if (stripped.length === 0) return null;
  return stripped.slice(0, MAX_TITLE_LENGTH);
}

/**
 * Build the artifact metadata for one capture.
 *
 * @param {object} args
 * @param {string} [args.pageUrl] the captured tab's URL
 * @param {string} [args.title] the captured tab's title
 * @param {"region" | "viewport"} args.mode
 * @param {number} [args.devicePixelRatio]
 * @param {boolean} [args.stripQuery] the "saved page address" setting
 * @param {Date} [args.now]
 * @returns {{source: object, capture: object}}
 */
export function buildProvenance({
  pageUrl,
  title,
  mode,
  devicePixelRatio,
  stripQuery = false,
  now = new Date(),
}) {
  const source = {};
  const sanitized = sanitizeSourceUrl(pageUrl);

  if (sanitized) {
    if (stripQuery) {
      const url = new URL(sanitized);
      // The fragment goes with the query: SPA routers put state in both,
      // and the promise in the settings copy is "the address, not what I
      // was looking at".
      url.search = "";
      url.hash = "";
      source.kind = "web_page";
      source.url = url.toString();
      source.url_redacted = true;
    } else {
      source.kind = "web_page";
      source.url = sanitized;
    }
  }
  // `title` and `captured_at` are recorded even for a page whose address we
  // will not keep — a local file still has a name worth remembering.
  source.title = cleanTitle(title);
  source.captured_at = now.toISOString();

  const capture = { producer: PRODUCER, mode };
  if (
    typeof devicePixelRatio === "number" &&
    Number.isFinite(devicePixelRatio) &&
    devicePixelRatio > 0 &&
    devicePixelRatio <= 8
  ) {
    capture.device_pixel_ratio = devicePixelRatio;
  }

  return { source, capture };
}
