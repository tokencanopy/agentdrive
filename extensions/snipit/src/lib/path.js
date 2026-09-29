// Naming a capture.
//
// Under v0 an artifact has a `name` inside a `parent_id`, not a path — so
// this module no longer builds `screenshots/<date>/<file>`; the folder is
// resolved separately (upload.js) and this names the file inside it.
//
// The old `screenshots/` prefix is gone with the backend rule that required
// it: the extension used to be scope-narrowed to that one prefix, and the
// person now chooses the folder instead.

const MAX_SLUG_LEN = 60;
const FALLBACK_SLUG = "untitled";

/**
 * A page title reduced to a kebab-case slug.
 *
 * Kept from the pre-v0 module unchanged: names still need to be readable in
 * a listing and safe in a URL, and this is well-tested behaviour.
 *
 * @param {string} title
 * @returns {string}
 */
export function slugifyTitle(title) {
  if (typeof title !== "string") return FALLBACK_SLUG;
  let s = title
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "") // strip combining marks
    .toLowerCase()
    .replace(/[^a-z0-9\s-]+/g, "") // drop non-alphanumerics
    .replace(/[\s_]+/g, "-") // whitespace → dash
    .replace(/-+/g, "-") // collapse multi-dash
    .replace(/^-+|-+$/g, ""); // trim
  if (s.length === 0) return FALLBACK_SLUG;
  if (s.length > MAX_SLUG_LEN) s = s.slice(0, MAX_SLUG_LEN).replace(/-+$/, "");
  return s || FALLBACK_SLUG;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

/**
 * The date folder's name, in UTC so it sorts lexicographically and does not
 * shift when someone travels.
 *
 * @param {Date} [now]
 * @returns {string} `YYYY-MM-DD`
 */
export function dateFolderName(now = new Date()) {
  return [
    now.getUTCFullYear(),
    pad(now.getUTCMonth() + 1),
    pad(now.getUTCDate()),
  ].join("-");
}

/**
 * The artifact name for one capture: `<slug>-HHMMSS.png`.
 *
 * The time suffix keeps two captures of the same page in the same folder
 * apart. It is not a uniqueness guarantee — two in the same second collide,
 * which upload.js resolves by disambiguating on the 409.
 *
 * @param {{title?: string, now?: Date}} args
 * @returns {string}
 */
export function captureName({ title, now = new Date() }) {
  const time = [
    pad(now.getUTCHours()),
    pad(now.getUTCMinutes()),
    pad(now.getUTCSeconds()),
  ].join("");
  return `${slugifyTitle(title)}-${time}.png`;
}
