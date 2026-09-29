// The first-run location proposal.
//
// The whole rule is "propose only when there is nothing to choose between".
// Guessing among several workspaces or drives files someone's screenshots
// somewhere they did not pick and will not think to look.

import { test } from "node:test";
import assert from "node:assert/strict";

import { proposeLocation, PROPOSED_FOLDER_NAME } from "../../src/lib/first-run.js";

const WORKSPACE = { id: "tcws_1", display_name: "Acme", role: "owner" };
const DRIVE = { id: "drv_1", name: "Design drive", root_folder_id: "fld_root" };

test("proposes when there is exactly one workspace and one drive", () => {
  const proposal = proposeLocation({
    workspaces: [WORKSPACE],
    drives: [DRIVE],
  });

  assert.deepEqual(proposal, {
    workspace_id: "tcws_1",
    workspace_name: "Acme",
    drive_id: "drv_1",
    drive_name: "Design drive",
    parent_folder_id: "fld_root",
    folder_name: PROPOSED_FOLDER_NAME,
  });
  assert.equal(PROPOSED_FOLDER_NAME, "Screenshots");
});

test("refuses to guess between several workspaces", () => {
  assert.equal(
    proposeLocation({
      workspaces: [WORKSPACE, { id: "tcws_2", display_name: "Other", role: "member" }],
      drives: [DRIVE],
    }),
    null,
  );
});

test("refuses to guess between several drives", () => {
  assert.equal(
    proposeLocation({
      workspaces: [WORKSPACE],
      drives: [DRIVE, { id: "drv_2", name: "Other drive", root_folder_id: "fld_2" }],
    }),
    null,
  );
});

test("refuses when there is nothing to propose", () => {
  assert.equal(proposeLocation({ workspaces: [], drives: [] }), null);
  assert.equal(proposeLocation({ workspaces: [WORKSPACE], drives: [] }), null);
  assert.equal(proposeLocation({ workspaces: [], drives: [DRIVE] }), null);
});

test("refuses a drive with no readable root folder", () => {
  // Without a root there is nowhere to put the proposed folder, and
  // inventing a parent id would 404 at the first capture.
  for (const drive of [
    { id: "drv_1", name: "D" },
    { id: "drv_1", name: "D", root_folder_id: null },
    { id: "drv_1", name: "D", root_folder_id: "" },
  ]) {
    assert.equal(
      proposeLocation({ workspaces: [WORKSPACE], drives: [drive] }),
      null,
    );
  }
});

test("refuses malformed input rather than half-filling a location", () => {
  for (const args of [
    { workspaces: null, drives: [DRIVE] },
    { workspaces: [WORKSPACE], drives: null },
    { workspaces: [{ id: "tcws_1" }], drives: [DRIVE] },
    { workspaces: [{ display_name: "Acme" }], drives: [DRIVE] },
    {},
  ]) {
    assert.equal(proposeLocation(args), null, JSON.stringify(args));
  }
});
