// What SnipIt suggests the first time somebody signs in.
//
// One rule: propose only when there is nothing to choose between. With a
// single workspace holding a single drive, "Screenshots at the root" is the
// answer anybody would give, and asking is friction. With more than one of
// either there is no safe guess — and a wrong guess is quiet, because the
// capture succeeds and lands somewhere the person will not think to look.
//
// So this returns `null` far more often than it returns a proposal, and the
// options page turns that `null` into a question.

/** The folder a first-run proposal offers to create. */
export const PROPOSED_FOLDER_NAME = "Screenshots";

function nonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}

/**
 * @param {{workspaces?: unknown, drives?: unknown}} args
 * @returns {{workspace_id: string, workspace_name: string, drive_id: string,
 *   drive_name: string, parent_folder_id: string, folder_name: string} | null}
 */
export function proposeLocation({ workspaces, drives } = {}) {
  if (!Array.isArray(workspaces) || workspaces.length !== 1) return null;
  if (!Array.isArray(drives) || drives.length !== 1) return null;

  const [workspace] = workspaces;
  const [drive] = drives;
  if (
    !nonEmptyString(workspace?.id) ||
    !nonEmptyString(workspace?.display_name) ||
    !nonEmptyString(drive?.id) ||
    !nonEmptyString(drive?.name) ||
    // Without a root folder there is nowhere to put the proposal, and
    // inventing a parent id would 404 at the first capture.
    !nonEmptyString(drive?.root_folder_id)
  ) {
    return null;
  }

  return {
    workspace_id: workspace.id,
    workspace_name: workspace.display_name,
    drive_id: drive.id,
    drive_name: drive.name,
    parent_folder_id: drive.root_folder_id,
    folder_name: PROPOSED_FOLDER_NAME,
  };
}
