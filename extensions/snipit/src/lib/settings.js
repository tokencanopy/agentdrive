// Where captures go, and what happens after one.
//
// Chosen once on the options page and then relied on silently by every
// capture, which is why this module refuses to guess: an unreadable or
// incomplete stored value reads as "no location", and no location means the
// popup asks rather than the pipeline improvising. The old extension had no
// choice to get wrong — everything went to `screenshots/` in the single
// drive the credential could see.

export const SETTINGS_KEY = "settings";

/** Bump ONLY with a migration. An unknown schema is discarded (see
 *  `readSettings`), which is the safe direction: asking again costs one
 *  click, filing into the wrong drive costs trust. */
const SCHEMA = 1;

export const DEFAULT_PREFERENCES = Object.freeze({
  /** `Screenshots/2026-09-05/…` rather than one ever-growing folder. */
  group_by_date: true,
  /** What lands on the clipboard: a secret share link, the console URL
   *  (workspace members only), or nothing. */
  link: "share",
  /** Off: with no editor to open, a tab per capture is noise. */
  open_console: false,
  /** Off: the address is recorded as it was. On, the query and fragment
   *  are dropped and the metadata says so. */
  strip_query: false,
  /** `null` means the share link does not expire, matching how a pasted
   *  link is expected to behave. */
  link_expiry_days: null,
});

const REQUIRED_LOCATION_IDS = ["workspace_id", "drive_id", "folder_id"];

const PREFERENCE_VALIDATORS = {
  group_by_date: (value) => typeof value === "boolean",
  open_console: (value) => typeof value === "boolean",
  strip_query: (value) => typeof value === "boolean",
  link: (value) => value === "share" || value === "console" || value === "none",
  link_expiry_days: (value) =>
    value === null || value === 7 || value === 30 || value === 90,
};

function readLocation(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  for (const key of REQUIRED_LOCATION_IDS) {
    if (typeof value[key] !== "string" || value[key].length === 0) return null;
  }
  return {
    workspace_id: value.workspace_id,
    workspace_name:
      typeof value.workspace_name === "string" ? value.workspace_name : "",
    drive_id: value.drive_id,
    drive_name: typeof value.drive_name === "string" ? value.drive_name : "",
    folder_id: value.folder_id,
    folder_path: Array.isArray(value.folder_path)
      ? value.folder_path.filter((part) => typeof part === "string")
      : [],
    stale: value.stale === true,
  };
}

function readPreferences(value) {
  const out = { ...DEFAULT_PREFERENCES };
  if (value === null || typeof value !== "object") return out;
  for (const [key, isValid] of Object.entries(PREFERENCE_VALIDATORS)) {
    // An unreadable single preference falls back to its default rather
    // than discarding the whole set — losing someone's drive choice
    // because a boolean got corrupted would be the wrong trade.
    if (key in value && isValid(value[key])) out[key] = value[key];
  }
  return out;
}

/**
 * @returns {Promise<{location: object | null, preferences: object}>}
 */
export async function readSettings() {
  let stored;
  try {
    stored = (await chrome.storage.local.get(SETTINGS_KEY))[SETTINGS_KEY];
  } catch {
    stored = null;
  }
  if (
    stored === null ||
    typeof stored !== "object" ||
    stored.schema !== SCHEMA
  ) {
    return { location: null, preferences: { ...DEFAULT_PREFERENCES } };
  }
  return {
    location: readLocation(stored.location),
    preferences: readPreferences(stored.preferences),
  };
}

async function mutate(change) {
  const current = await readSettings();
  const next = change(current);
  await chrome.storage.local.set({
    [SETTINGS_KEY]: {
      schema: SCHEMA,
      location: next.location,
      preferences: next.preferences,
    },
  });
  return next;
}

/** Save a location. Writing one always clears a previous stale mark: the
 *  person just answered the question the mark was asking. */
export async function writeLocation(location) {
  for (const key of REQUIRED_LOCATION_IDS) {
    if (typeof location?.[key] !== "string" || location[key].length === 0) {
      throw new Error(`A capture location needs a ${key}.`);
    }
  }
  const validated = readLocation({ ...location, stale: false });
  return await mutate((current) => ({ ...current, location: validated }));
}

export async function clearLocation() {
  return await mutate((current) => ({ ...current, location: null }));
}

/**
 * Mark the saved location unusable — its folder or drive answered 403/404.
 *
 * The ids are KEPT. The popup shows which location broke, and the person
 * needs to recognise it to know what to pick instead; silently emptying it
 * would present the same "choose a location" prompt as a fresh install and
 * lose the fact that something changed underneath them.
 */
export async function markLocationStale() {
  return await mutate((current) =>
    current.location === null
      ? current
      : { ...current, location: { ...current.location, stale: true } },
  );
}

export async function updatePreferences(changes) {
  for (const [key, value] of Object.entries(changes)) {
    const isValid = PREFERENCE_VALIDATORS[key];
    if (!isValid) throw new Error(`Unknown preference: ${key}`);
    if (!isValid(value)) {
      throw new Error(`Invalid value for ${key}: ${JSON.stringify(value)}`);
    }
  }
  return await mutate((current) => ({
    ...current,
    preferences: { ...current.preferences, ...changes },
  }));
}
