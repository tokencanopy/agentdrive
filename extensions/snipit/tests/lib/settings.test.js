// The saved capture location and the preferences around it.
//
// A wrong default here does not fail loudly — it quietly files someone's
// screenshots in the wrong drive. So the rules pinned below are mostly
// about refusing to guess: no location means no capture, and a location
// that stopped working says so instead of falling back to somewhere else.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

import { installChromeMock } from "../support/chrome-mock.js";

installChromeMock();

const {
  readSettings,
  writeLocation,
  updatePreferences,
  markLocationStale,
  clearLocation,
  DEFAULT_PREFERENCES,
  SETTINGS_KEY,
} = await import("../../src/lib/settings.js");

const LOCATION = {
  workspace_id: "tcws_0000000000000001",
  workspace_name: "Acme",
  drive_id: "drv_00000000000000a1",
  drive_name: "Design drive",
  folder_id: "fld_00000000000000b2",
  folder_path: ["Screenshots"],
};

let state;
beforeEach(() => {
  state = installChromeMock({ installType: "development" });
});

test("starts with no location and the documented defaults", async () => {
  const settings = await readSettings();

  assert.equal(settings.location, null);
  assert.deepEqual(settings.preferences, DEFAULT_PREFERENCES);
  assert.deepEqual(DEFAULT_PREFERENCES, {
    group_by_date: true,
    link: "share",
    open_console: false,
    strip_query: false,
    link_expiry_days: null,
  });
});

test("stores a location and reads it back", async () => {
  await writeLocation(LOCATION);

  const settings = await readSettings();

  assert.deepEqual(settings.location, { ...LOCATION, stale: false });
});

test("writing a location clears a previous stale mark", async () => {
  await writeLocation(LOCATION);
  await markLocationStale();
  assert.equal((await readSettings()).location.stale, true);

  await writeLocation({ ...LOCATION, folder_id: "fld_00000000000000c3" });

  assert.equal((await readSettings()).location.stale, false);
});

test("markLocationStale keeps the ids — the person needs to see what broke", async () => {
  await writeLocation(LOCATION);

  await markLocationStale();

  const { location } = await readSettings();
  assert.equal(location.stale, true);
  assert.equal(location.folder_id, LOCATION.folder_id);
  assert.equal(location.drive_name, "Design drive");
});

test("markLocationStale on no location is a no-op, not a crash", async () => {
  await markLocationStale();

  assert.equal((await readSettings()).location, null);
});

test("clearLocation removes it entirely", async () => {
  await writeLocation(LOCATION);

  await clearLocation();

  assert.equal((await readSettings()).location, null);
});

test("rejects a location missing any required id", async () => {
  for (const key of ["workspace_id", "drive_id", "folder_id"]) {
    const incomplete = { ...LOCATION };
    delete incomplete[key];
    await assert.rejects(() => writeLocation(incomplete), new RegExp(key));
  }
});

test("preferences update one field at a time and keep the rest", async () => {
  await updatePreferences({ link: "console" });
  await updatePreferences({ group_by_date: false });

  const { preferences } = await readSettings();
  assert.equal(preferences.link, "console");
  assert.equal(preferences.group_by_date, false);
  assert.equal(preferences.strip_query, false);
});

test("rejects a preference value outside its documented set", async () => {
  await assert.rejects(() => updatePreferences({ link: "public" }), /link/);
  await assert.rejects(
    () => updatePreferences({ group_by_date: "yes" }),
    /group_by_date/,
  );
  await assert.rejects(
    () => updatePreferences({ link_expiry_days: 5 }),
    /link_expiry_days/,
  );
  await assert.rejects(
    () => updatePreferences({ unknown_thing: true }),
    /unknown_thing/,
  );
});

test("accepts the offered link expiries and 'never'", async () => {
  for (const days of [7, 30, 90, null]) {
    await updatePreferences({ link_expiry_days: days });
    assert.equal((await readSettings()).preferences.link_expiry_days, days);
  }
});

test("stored settings from an unknown schema are discarded, not obeyed", async () => {
  // Reading a future or corrupted shape and acting on it is how a capture
  // lands somewhere nobody chose.
  state.local[SETTINGS_KEY] = {
    schema: 99,
    location: { ...LOCATION, drive_id: "drv_from_the_future" },
  };

  const settings = await readSettings();

  assert.equal(settings.location, null);
  assert.deepEqual(settings.preferences, DEFAULT_PREFERENCES);
});

test("a stored location missing an id is treated as absent", async () => {
  state.local[SETTINGS_KEY] = {
    schema: 1,
    location: { workspace_id: "tcws_1", drive_id: "drv_1" },
    preferences: DEFAULT_PREFERENCES,
  };

  assert.equal((await readSettings()).location, null);
});

test("an unreadable preference falls back to its default rather than failing", async () => {
  state.local[SETTINGS_KEY] = {
    schema: 1,
    location: null,
    preferences: { link: "nonsense", group_by_date: 7 },
  };

  const { preferences } = await readSettings();

  assert.deepEqual(preferences, DEFAULT_PREFERENCES);
});
