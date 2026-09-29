"""Folder vertical: workspace-scoped folder CRUD, lifecycle, and subtree
copy over the day-0 parent/name namespace (slice 5).

Folders hang off a drive's single structural root (``parent_id IS NULL``,
``name IS NULL``, pinned by ``folders_one_root``). Every other folder has
exactly one parent and one name segment. Sibling artifact and folder names
share ONE collision domain (§6.2): within each table a partial unique index
holds it, and ``reject_cross_kind_name_collision`` extends it across the two
tables — so a folder create/rename/restore/copy that collides surfaces as an
``asyncpg.UniqueViolationError`` at INSERT/UPDATE time, translated to
``409 FOLDER_PATH_CONFLICT`` at the boundary.

**Root immutability.** The structural root can never be patched, deleted, or
copied-as-a-child; the mutators raise ``RootFolderError`` (409 CONFLICT).

**Recursive soft-delete with exact cohorts.** Deleting a folder sets
``deleted_at`` on the folder and its ENTIRE live subtree (every descendant
folder plus every artifact whose ``parent_id`` sits in that subtree) in one
transaction, bumping each row's revision and stamping every row with ONE
shared ``deleted_cohort_id`` (``cset_*``). An individual soft-delete — one
artifact, or a non-recursive folder delete — stamps its single row with its
own fresh cohort (a cohort of one), so the model is uniform. Restore clears
``deleted_at`` (and the cohort) on exactly the rows sharing the ROOT
folder's cohort id — the rows that were LIVE at the moment of that recursive
delete and were deleted BY it. Rows deleted earlier for unrelated reasons
(an individually-deleted artifact that a same-named replacement then
collided with, an independently-deleted subtree) keep their own cohorts and
stay deleted. A deleted row with a NULL cohort predates cohort tracking and
is not restorable (no backfill — see the migration comment). Restore also
refuses to run while the folder's parent is deleted and refuses to resurrect
a name a live sibling now owns (any ``asyncpg.UniqueViolationError`` during
restore surfaces as the name-conflict 409, never a 500).

 **Subtree copy.** Copy is same-drive-only in v0 (cross-drive copy is
 rejected at the route layer — the data-transfer path is out of scope).
 Materializes the subtree synchronously in one transaction, up to 5,000 live
 resources (the copied root takes the destination name; grant rows are NOT
 cloned, grants stay on the source, so the copy inherits whatever the
 destination's ancestry grants).

Layer rule: this module never imports ``agentdrive.api`` (api depends on
core). Precondition failures and cursor payloads are handed back as
exceptions / plain data and translated to wire statuses and opaque cursors at
the route layer. The ``If-Match``/ETag primitives are shared with the drives
vertical (``v0_drives.precondition`` / ``etag_values``).
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

import asyncpg

from ..core.ids import new_id
from ..core.timestamps import to_rfc3339
from . import v0_changes as changes
from .v0_drives import DriveNotFoundError, _lock_drive_namespace, precondition

# Synchronous-copy ceiling: a subtree above this many resources is refused
# rather than copied inline; cross-drive jobs are not available in v0.
MAX_SYNCHRONOUS_COPY_RESOURCES = 5_000

MAX_NAME_LENGTH = 255
_BIDI_CONTROLS = frozenset(
    chr(codepoint)
    for start, end in ((0x202A, 0x202E), (0x2066, 0x2069))
    for codepoint in range(start, end + 1)
)
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})

_FOLDER_COLUMNS = (
    "id, drive_id, parent_id, name, metadata, revision, "
    "created_at, updated_at, deleted_at"
)


class FolderNotFoundError(LookupError):
    """The folder does not exist in the drive (or is soft-deleted).

    Always a 404 FOLDER_NOT_FOUND at the boundary — including the
    cross-workspace case (via the drive check), so existence is not
    disclosed.
    """


class RootFolderError(ValueError):
    """The structural root folder cannot be changed. 409 CONFLICT."""


class FolderNameConflictError(ValueError):
    """A live sibling — folder or artifact — already owns the name.
    409 FOLDER_PATH_CONFLICT."""


class InvalidMoveError(ValueError):
    """A move/copy would create a cycle, or a restore has an unavailable
    parent. 409 CONFLICT."""


class FolderNotDeletedError(ValueError):
    """restore was called on a folder that is not soft-deleted. 409 CONFLICT."""


class RecursiveRequiredError(ValueError):
    """The folder is not empty and `recursive` was not set.
    409 FOLDER_RECURSIVE_REQUIRED."""


class SubtreeTooLargeError(ValueError):
    """The synchronous-copy ceiling was exceeded. 409 SUBTREE_TOO_LARGE."""


class InvalidFolderNameError(ValueError):
    """A folder name failed segment validation. 400 INVALID_ARGUMENT."""


def validate_name(value: str) -> str:
    """Return the canonical NFC form of one safe item-name segment."""
    if not isinstance(value, str) or not value:
        raise InvalidFolderNameError("item name must be a non-empty string")
    canonical = unicodedata.normalize("NFC", value)
    if not canonical or len(canonical) > MAX_NAME_LENGTH:
        raise InvalidFolderNameError(
            f"item name must contain 1 to {MAX_NAME_LENGTH} Unicode code points"
        )
    if canonical[0].isspace() or canonical[-1].isspace():
        raise InvalidFolderNameError("item name cannot have edge whitespace")
    if "/" in canonical or "\\" in canonical:
        raise InvalidFolderNameError("item name cannot contain path separators")
    if canonical.strip(".") == "":
        raise InvalidFolderNameError("item name cannot be a dot-only segment")
    if any(
        ch == "\ufeff"
        or ch in _BIDI_CONTROLS
        or unicodedata.category(ch) in _FORBIDDEN_CATEGORIES
        for ch in canonical
    ):
        raise InvalidFolderNameError("item name contains a forbidden control character")
    return canonical


def folder_payload(row: Any) -> dict[str, Any]:
    """The wire shape of one folder row."""
    metadata = row["metadata"]
    deleted_at = row["deleted_at"]
    return {
        "id": row["id"],
        "drive_id": row["drive_id"],
        "parent_id": row["parent_id"],
        "name": row["name"],
        "metadata": json.loads(metadata) if isinstance(metadata, str) else metadata,
        "revision": row["revision"],
        "state": "deleted" if deleted_at else "active",
        "created_at": to_rfc3339(row["created_at"]),
        "updated_at": to_rfc3339(row["updated_at"]),
        "deleted_at": to_rfc3339(deleted_at) if deleted_at else None,
    }


async def _drive_root(c: Any, drive_id: str) -> str | None:
    return await c.fetchval(
        "SELECT root_folder_id FROM drives WHERE id = $1 AND deleted_at IS NULL",
        drive_id,
    )


async def _ensure_drive(c: Any, actor: Any, drive_id: str) -> None:
    """404 semantics for the drive a folder op is scoped to."""
    row = await c.fetchrow(
        "SELECT workspace_id, root_folder_id FROM drives WHERE id = $1 AND deleted_at IS NULL",
        drive_id,
    )
    if row is None or row["workspace_id"] != actor.workspace_id:
        raise DriveNotFoundError(drive_id)


def _folder_or_404(row: Any | None) -> Any:
    if row is None:
        raise FolderNotFoundError("folder not found")
    return row


async def _live_folder(c: Any, drive_id: str, folder_id: str, *, for_update: bool) -> Any | None:
    sql = (
        f"SELECT {_FOLDER_COLUMNS} FROM folders "
        "WHERE drive_id = $1 AND id = $2 AND deleted_at IS NULL"
    )
    if for_update:
        sql += " FOR UPDATE"
    return await c.fetchrow(sql, drive_id, folder_id)


async def get_folder(c: Any, actor: Any, drive_id: str, folder_id: str) -> dict[str, Any]:
    """Read one active folder in the actor's drive (404 for a missing /
    soft-deleted folder or a drive outside the workspace)."""
    await _ensure_drive(c, actor, drive_id)
    row = await _live_folder(c, drive_id, folder_id, for_update=False)
    return folder_payload(_folder_or_404(row))


async def list_folders(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    state: str,
    limit: int,
    after_ts: Any = None,
    after_id: str | None = None,
    parent_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Active/deleted/all listing, newest-first with a stable (created_at, id)
    keyset anchor; ``parent_id`` / ``name`` are exact-match filters. Returns
    ``{"items", "next_cursor"}`` for the route to encode.

    Rows are filtered to those the actor can see (grant visibility, §8):
    drive-level grants expose the whole drive; a folder grant exposes that
    folder's whole subtree; a workspace owner/admin sees every row (the
    workspace-admin overlay — `_ensure_drive` above has already pinned the
    drive to the actor's own workspace)."""
    await _ensure_drive(c, actor, drive_id)
    from . import v0_authz

    # actor params are appended AFTER the drive id ($1): subject_type, subject,
    # workspace_id, then the workspace-admin overlay flag.
    principal_type_param = len([drive_id]) + 1
    principal_id_param = principal_type_param + 1
    workspace_param = principal_id_param + 1
    overlay_param = workspace_param + 1
    params: list[Any] = [drive_id]
    sql = f"SELECT {_FOLDER_COLUMNS} FROM folders AS fld"
    sql += v0_authz.visibility_lateral(
        "fld",
        start_parent_expr="fld.id",
        principal_type_param=principal_type_param,
        principal_id_param=principal_id_param,
        workspace_param=workspace_param,
        overlay_param=overlay_param,
    )
    sql += " WHERE fld.drive_id = $1"
    params.extend([
        actor.subject_type, actor.subject, actor.workspace_id,
        v0_authz.workspace_admin_overlay(actor),
    ])
    if state == "active":
        sql += " AND deleted_at IS NULL"
    elif state == "deleted":
        sql += " AND deleted_at IS NOT NULL"
    if parent_id is not None:
        params.append(parent_id)
        sql += " AND parent_id = $" + str(len(params))
    if name is not None:
        params.append(name)
        sql += " AND name = $" + str(len(params)) + " COLLATE \"C\""
    if after_ts is not None:
        params.extend([after_ts, after_id])
        sql += " AND (created_at, id) < ($" + str(len(params) - 1) + ", $" + str(len(params)) + ")"
    params.append(limit + 1)
    sql += " ORDER BY created_at DESC, id DESC LIMIT $" + str(len(params))
    rows = await c.fetch(sql, *params)

    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = {
            "state": state,
            "created_at": to_rfc3339(last["created_at"]),
            "id": last["id"],
        }
    return {"items": [folder_payload(r) for r in rows], "next_cursor": next_cursor}


async def _name_is_occupied(
    c: Any,
    drive_id: str,
    parent_id: str,
    name: str,
    *,
    exclude_folder_id: str | None = None,
) -> bool:
    """Live folder OR artifact sibling under `parent_id` owns `name`."""
    return bool(
        await c.fetchval(
            """
            SELECT EXISTS (
              SELECT 1 FROM folders
               WHERE drive_id = $1 AND parent_id = $2 AND name = $3
                 AND deleted_at IS NULL
                 AND ($4::text IS NULL OR id <> $4)
              UNION ALL
              SELECT 1 FROM artifacts
               WHERE drive_id = $1 AND parent_id = $2 AND name = $3
                 AND deleted_at IS NULL
            )
            """,
            drive_id, parent_id, name, exclude_folder_id,
        )
    )


async def _is_structural_root(c: Any, drive_id: str, folder_id: str) -> bool:
    return (await _drive_root(c, drive_id)) == folder_id


async def create_folder(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    parent_id: str,
    name: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Create one folder under `parent_id`. Must run inside a transaction."""
    name = validate_name(name)
    await _lock_drive_namespace(c, drive_id)
    await _ensure_drive(c, actor, drive_id)
    parent = await _live_folder(c, drive_id, parent_id, for_update=True)
    _folder_or_404(parent)

    if await _name_is_occupied(c, drive_id, parent_id, name):
        raise FolderNameConflictError(
            f"a sibling under {parent_id} already uses the name {name!r}"
        )

    folder_id = new_id("fld")
    revision = new_id("rev")
    try:
        row = await c.fetchrow(
            f"INSERT INTO folders (id, drive_id, parent_id, name, "
            f"metadata, revision) "
            f"VALUES ($1, $2, $3, $4, $5::jsonb, $6) RETURNING {_FOLDER_COLUMNS}",
            folder_id, drive_id, parent_id, name,
            json.dumps(metadata), revision,
        )
    except asyncpg.UniqueViolationError:
        raise FolderNameConflictError(
            f"a sibling under {parent_id} already uses the name {name!r}"
        ) from None
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="folder.created", resource_type="folder", resource_id=folder_id,
        revision=revision,
        data={"name": name},
    )
    return folder_payload(row)


async def patch_folder(
    c: Any,
    actor: Any,
    drive_id: str,
    folder_id: str,
    *,
    name: str | None,
    parent_id: str | None,
    metadata: dict[str, Any] | None,
    changed: frozenset[str],
    if_match: str | None,
) -> dict[str, Any]:
    """Rename / move / update a folder's metadata in one mutation. Must run
    inside a transaction."""
    await _lock_drive_namespace(c, drive_id)
    await _ensure_drive(c, actor, drive_id)
    folder = await _live_folder(c, drive_id, folder_id, for_update=True)
    _folder_or_404(folder)
    if await _is_structural_root(c, drive_id, folder_id):
        raise RootFolderError("the drive's root folder cannot be changed")
    precondition(if_match, folder["revision"])

    destination_parent_id = parent_id if parent_id is not None else folder["parent_id"]
    destination_name = validate_name(name) if name is not None else folder["name"]
    if destination_parent_id is None or destination_name is None:
        # A move that would un-parent the folder, or a rename of the root,
        # cannot be represented. (Root is already rejected above.)
        raise InvalidFolderNameError("a folder must keep one parent and one name")

    parent = await _live_folder(c, drive_id, destination_parent_id, for_update=True)
    _folder_or_404(parent)

    moving = (
        destination_parent_id != folder["parent_id"]
        or destination_name != folder["name"]
    )
    if moving:
        if destination_parent_id == folder_id or await _is_descendant(
            c, drive_id, ancestor=folder_id, folder=destination_parent_id
        ):
            raise InvalidMoveError("a folder cannot be moved into itself or its descendants")
        if await _name_is_occupied(
            c,
            drive_id,
            destination_parent_id,
            destination_name,
            exclude_folder_id=folder_id,
        ):
            raise FolderNameConflictError("a live sibling already uses the destination name")

    current_metadata = folder["metadata"]
    if isinstance(current_metadata, str):
        current_metadata = json.loads(current_metadata)
    target_metadata = metadata if metadata is not None else current_metadata

    actual_change = moving or target_metadata != current_metadata
    if not actual_change:
        return folder_payload(folder)

    revision = new_id("rev")
    sets = [
        "parent_id = $3",
        "name = $4",
        "metadata = $5::jsonb",
        "revision = $6",
        "updated_at = now()",
    ]
    await c.execute(
        "UPDATE folders SET " + ", ".join(sets) + " WHERE drive_id = $1 AND id = $2",
        drive_id, folder_id, destination_parent_id, destination_name,
        json.dumps(target_metadata), revision,
    )
    data: dict[str, Any] = {"name": destination_name}
    if destination_name != folder["name"]:
        data["name_before"] = folder["name"]
    if destination_parent_id != folder["parent_id"]:
        data["previous_parent_id"] = folder["parent_id"]
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="folder.updated", resource_type="folder", resource_id=folder_id,
        previous_revision=folder["revision"], revision=revision,
        data=data,
    )
    row = await c.fetchrow(
        f"SELECT {_FOLDER_COLUMNS} FROM folders WHERE drive_id = $1 AND id = $2",
        drive_id, folder_id,
    )
    return folder_payload(row)


async def _is_descendant(c: Any, drive_id: str, *, ancestor: str, folder: str) -> bool:
    """True when `folder` lies inside `ancestor`'s subtree (cycle guard)."""
    return bool(
        await c.fetchval(
            """
            WITH RECURSIVE subtree AS (
              SELECT id FROM folders
               WHERE drive_id = $1 AND id = $2 AND deleted_at IS NULL
              UNION ALL
              SELECT child.id FROM folders child
                JOIN subtree ON child.parent_id = subtree.id
               WHERE child.drive_id = $1 AND child.deleted_at IS NULL
            )
            SELECT EXISTS (SELECT 1 FROM subtree WHERE id = $3)
            """,
            drive_id, ancestor, folder,
        )
    )


async def soft_delete_folder(
    c: Any,
    actor: Any,
    drive_id: str,
    folder_id: str,
    *,
    recursive: bool,
    if_match: str | None,
) -> dict[str, Any]:
    """Soft-delete a folder and its full live subtree in one transaction.
    A non-empty subtree requires ``recursive=True`` (409 otherwise).
    Returns the deleted root representation plus exact cascade counts."""
    await _lock_drive_namespace(c, drive_id)
    await _ensure_drive(c, actor, drive_id)
    root = await _live_folder(c, drive_id, folder_id, for_update=True)
    _folder_or_404(root)
    if await _is_structural_root(c, drive_id, folder_id):
        raise RootFolderError("the drive's root folder cannot be deleted")
    precondition(if_match, root["revision"])

    folder_rows = await c.fetch(
        """
        WITH RECURSIVE subtree AS (
          SELECT f.id, f.parent_id, 0::integer AS depth
            FROM folders f
           WHERE f.drive_id = $1 AND f.id = $2 AND f.deleted_at IS NULL
          UNION ALL
          SELECT child.id, child.parent_id, subtree.depth + 1
            FROM folders child
            JOIN subtree ON child.parent_id = subtree.id
           WHERE child.drive_id = $1 AND child.deleted_at IS NULL
        )
        SELECT f.*, subtree.depth
          FROM subtree
          JOIN folders f ON f.id = subtree.id
         WHERE f.drive_id = $1
         ORDER BY subtree.depth, f.id COLLATE "C"
         FOR UPDATE OF f
        """,
        drive_id, folder_id,
    )
    folder_ids = [row["id"] for row in folder_rows]
    artifact_rows = await c.fetch(
        "SELECT a.id, a.name, a.revision FROM artifacts a "
        "WHERE a.drive_id = $1 AND a.parent_id = ANY($2::text[]) AND a.deleted_at IS NULL "
        "ORDER BY a.id COLLATE \"C\" FOR UPDATE",
        drive_id, folder_ids,
    )

    descendant_count = (len(folder_rows) - 1) + len(artifact_rows)
    if descendant_count and not recursive:
        raise RecursiveRequiredError(
            "The folder is not empty; pass recursive=true to delete it."
        )

    revision = new_id("rev")
    cohort_id = new_id("cset")
    await c.execute(
        "UPDATE folders SET deleted_at = now(), deleted_cohort_id = $4, "
        "revision = $3, updated_at = now() "
        "WHERE drive_id = $1 AND id = ANY($2::text[])",
        drive_id, folder_ids, revision, cohort_id,
    )
    artifact_ids = [row["id"] for row in artifact_rows]
    if artifact_ids:
        await c.execute(
            "UPDATE artifacts SET deleted_at = now(), deleted_cohort_id = $4, "
            "revision = $3, updated_at = now() "
            "WHERE drive_id = $1 AND id = ANY($2::text[])",
            drive_id, artifact_ids, revision, cohort_id,
        )

    deleted_root = await c.fetchrow(
        f"SELECT {_FOLDER_COLUMNS} FROM folders WHERE drive_id = $1 AND id = $2",
        drive_id, folder_id,
    )
    # One change row PER affected resource, all sharing the deletion cohort as
    # the change set id (§6.7): a feed-driven mirror replays the whole cascade
    # atomically instead of learning only that the root went away. The root's
    # row keeps its cascade-count payload; members append in a deterministic
    # order (folders by depth/id, then artifacts by id).
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="folder.deleted", resource_type="folder", resource_id=folder_id,
        previous_revision=root["revision"], revision=revision,
        change_set_id=cohort_id,
        data={
            "name": root["name"],
            "cascade": {"folders": len(folder_rows) - 1, "artifacts": len(artifact_rows)},
        },
    )
    for f in folder_rows[1:]:
        await changes.append(
            c, drive_id=drive_id, actor=actor,
            type="folder.deleted", resource_type="folder", resource_id=f["id"],
            previous_revision=f["revision"], revision=revision,
            change_set_id=cohort_id,
            data={"name": f["name"]},
        )
    for a in artifact_rows:
        await changes.append(
            c, drive_id=drive_id, actor=actor,
            type="artifact.deleted", resource_type="artifact", resource_id=a["id"],
            previous_revision=a["revision"], revision=revision,
            change_set_id=cohort_id,
            data={"name": a["name"]},
        )
    return {
        "folder": folder_payload(deleted_root),
        "cascade": {"folders": len(folder_rows) - 1, "artifacts": len(artifact_rows)},
    }


async def restore_folder(
    c: Any,
    actor: Any,
    drive_id: str,
    folder_id: str,
    *,
    if_match: str | None,
) -> dict[str, Any]:
    """Restore the deletion cohort rooted at `folder_id` atomically. Returns
    the restored root representation plus exact cascade counts.

    Exactly the rows stamped with the ROOT folder's ``deleted_cohort_id`` are
    restored — the cohort one recursive delete removed. Rows deleted
    beforehand for unrelated reasons (an independently-deleted artifact whose
    name a replacement now owns, an independently-deleted subtree) keep their
    own cohorts and stay deleted. A deleted row whose cohort is NULL predates
    cohort tracking and is not restorable (404)."""
    await _lock_drive_namespace(c, drive_id)
    await _ensure_drive(c, actor, drive_id)
    row = await c.fetchrow(
        f"SELECT {_FOLDER_COLUMNS}, deleted_cohort_id FROM folders "
        "WHERE drive_id = $1 AND id = $2 FOR UPDATE",
        drive_id, folder_id,
    )
    _folder_or_404(row)
    if row["deleted_at"] is None:
        raise FolderNotDeletedError(folder_id)
    if await _is_structural_root(c, drive_id, folder_id):
        raise RootFolderError("the drive's root folder cannot be restored independently")
    precondition(if_match, row["revision"])

    cohort_id = row["deleted_cohort_id"]
    if cohort_id is None:
        # Pre-cohort deleted rows are permanently gone by design (no
        # backfill — see the migration comment); there is no cohort to bring
        # back, so the folder is not restorable.
        raise FolderNotFoundError(folder_id)

    parent = await c.fetchrow(
        "SELECT id FROM folders WHERE drive_id = $1 AND id = $2 AND deleted_at IS NULL",
        drive_id, row["parent_id"],
    )
    if parent is None:
        raise InvalidMoveError("the folder cannot be restored until its parent is active")

    folder_rows = await c.fetch(
        "SELECT id, parent_id, name, revision FROM folders "
        "WHERE drive_id = $1 AND deleted_cohort_id = $2 "
        "ORDER BY id COLLATE \"C\" FOR UPDATE OF folders",
        drive_id, cohort_id,
    )
    if not folder_rows:
        raise FolderNotFoundError(folder_id)
    artifact_rows = await c.fetch(
        "SELECT id, parent_id, name, revision FROM artifacts "
        "WHERE drive_id = $1 AND deleted_cohort_id = $2 "
        "ORDER BY id COLLATE \"C\" FOR UPDATE",
        drive_id, cohort_id,
    )

    for f in folder_rows:
        if await _name_is_occupied(c, drive_id, f["parent_id"], f["name"]):
            raise FolderNameConflictError(
                "a live sibling already uses a name required by this restore"
            )
    for a in artifact_rows:
        if await _name_is_occupied(c, drive_id, a["parent_id"], a["name"]):
            raise FolderNameConflictError(
                "a live sibling already uses a name required by this restore"
            )

    revision = new_id("rev")
    try:
        await c.execute(
            "UPDATE folders SET deleted_at = NULL, deleted_cohort_id = NULL, "
            "revision = $3, updated_at = now() "
            "WHERE drive_id = $1 AND deleted_cohort_id = $2",
            drive_id, cohort_id, revision,
        )
        if artifact_rows:
            await c.execute(
                "UPDATE artifacts SET deleted_at = NULL, deleted_cohort_id = NULL, "
                "revision = $3, updated_at = now() "
                "WHERE drive_id = $1 AND deleted_cohort_id = $2",
                drive_id, cohort_id, revision,
            )
    except asyncpg.UniqueViolationError:
        raise FolderNameConflictError(
            "a live sibling already uses a name required by this restore"
        ) from None

    restored_root = await c.fetchrow(
        f"SELECT {_FOLDER_COLUMNS} FROM folders WHERE drive_id = $1 AND id = $2",
        drive_id, folder_id,
    )
    # One change row PER restored resource, all sharing the cohort as the
    # change set id so a feed-driven mirror replays the whole restore
    # atomically. The root's row keeps its cascade-count payload; members
    # append in a deterministic order (folders by id, then artifacts by id).
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="folder.restored", resource_type="folder", resource_id=folder_id,
        previous_revision=row["revision"], revision=revision,
        change_set_id=cohort_id,
        data={
            "name": row["name"],
            "cascade": {"folders": len(folder_rows) - 1, "artifacts": len(artifact_rows)},
        },
    )
    for f in folder_rows:
        if f["id"] == folder_id:
            continue
        await changes.append(
            c, drive_id=drive_id, actor=actor,
            type="folder.restored", resource_type="folder", resource_id=f["id"],
            previous_revision=f["revision"], revision=revision,
            change_set_id=cohort_id,
            data={"name": f["name"]},
        )
    for a in artifact_rows:
        await changes.append(
            c, drive_id=drive_id, actor=actor,
            type="artifact.restored", resource_type="artifact", resource_id=a["id"],
            previous_revision=a["revision"], revision=revision,
            change_set_id=cohort_id,
            data={"name": a["name"]},
        )
    return {
        "folder": folder_payload(restored_root),
        "cascade": {"folders": len(folder_rows) - 1, "artifacts": len(artifact_rows)},
    }


async def copy_folder(
    c: Any,
    actor: Any,
    drive_id: str,
    folder_id: str,
    *,
    destination_drive_id: str,
    destination_parent_id: str,
    destination_name: str,
    destination_etag: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Copy a consistent current subtree synchronously within one drive.

    Only same-drive copy is in v0 scope (cross-drive copy is rejected at the
    route layer). A bounded preflight rejects more than 5,000 live resources
    before the recursive rows are materialized and locked. The accepted
    subtree is copied in one transaction and returns the copied root's payload.
    """
    destination_name = validate_name(destination_name)
    await _lock_drive_namespace(c, drive_id)
    await _ensure_drive(c, actor, drive_id)
    source = await _live_folder(c, drive_id, folder_id, for_update=True)
    _folder_or_404(source)
    if await _is_structural_root(c, drive_id, folder_id):
        raise RootFolderError("the drive's root folder cannot be copied as a child")
    if destination_etag is not None:
        precondition(destination_etag, source["revision"])

    await _ensure_drive(c, actor, destination_drive_id)
    destination_parent = await _live_folder(
        c, destination_drive_id, destination_parent_id, for_update=True
    )
    _folder_or_404(destination_parent)
    if (
        destination_parent_id == folder_id
        or await _is_descendant(c, drive_id, ancestor=folder_id, folder=destination_parent_id)
    ):
        raise InvalidMoveError("a folder cannot be copied into its own subtree")

    if await _name_is_occupied(
        c, destination_drive_id, destination_parent_id, destination_name
    ):
        raise FolderNameConflictError("a live sibling already uses the destination name")

    resource_count = await _bounded_subtree_resource_count(
        c,
        drive_id,
        folder_id,
        max_resources=MAX_SYNCHRONOUS_COPY_RESOURCES,
    )
    if resource_count > MAX_SYNCHRONOUS_COPY_RESOURCES:
        raise SubtreeTooLargeError("folder subtree exceeds the synchronous copy limit")

    folder_rows = await c.fetch(
        """
        WITH RECURSIVE subtree AS (
          SELECT f.id, f.drive_id, 0::integer AS depth
            FROM folders f
           WHERE f.drive_id = $1 AND f.id = $2 AND f.deleted_at IS NULL
          UNION ALL
          SELECT child.id, child.drive_id, subtree.depth + 1
            FROM folders child
            JOIN subtree ON child.parent_id = subtree.id
           WHERE child.drive_id = $1 AND child.deleted_at IS NULL
        )
        SELECT f.*, subtree.depth
          FROM subtree
          JOIN folders f ON f.id = subtree.id
         WHERE f.drive_id = $1
         ORDER BY subtree.depth, f.id COLLATE "C"
         FOR UPDATE OF f
        """,
        drive_id, folder_id,
    )
    artifact_rows = await c.fetch(
        "SELECT a.* FROM artifacts a "
        "WHERE a.drive_id = $1 AND a.parent_id = ANY($2::text[]) AND a.deleted_at IS NULL "
        "ORDER BY a.id COLLATE \"C\" FOR UPDATE",
        drive_id, [row["id"] for row in folder_rows],
    )
    materialized_resource_count = len(folder_rows) + len(artifact_rows)
    if materialized_resource_count > MAX_SYNCHRONOUS_COPY_RESOURCES:
        raise SubtreeTooLargeError("folder subtree exceeds the synchronous copy limit")

    return await _materialize_copy(
        c,
        actor=actor,
        drive_id=drive_id,
        destination_parent_id=destination_parent_id,
        destination_name=destination_name,
        folder_rows=folder_rows,
        artifact_rows=artifact_rows,
    )


async def _bounded_subtree_resource_count(
    c: Any,
    drive_id: str,
    folder_id: str,
    *,
    max_resources: int,
) -> int:
    """Count live subtree resources, stopping at ``max_resources + 1``.

    The caller holds the drive namespace advisory lock, so a passing count
    remains stable until the subsequent row-locking materialization finishes.
    Folder ids are pulled directly from the recursive CTE without sorting, so
    PostgreSQL can stop recursion as soon as the rejecting sentinel is read.
    """
    rejecting_count = max_resources + 1
    folder_rows = await c.fetch(
        """
        WITH RECURSIVE subtree AS (
          SELECT f.id
            FROM folders f
           WHERE f.drive_id = $1 AND f.id = $2 AND f.deleted_at IS NULL
          UNION ALL
          SELECT child.id
            FROM folders child
            JOIN subtree ON child.parent_id = subtree.id
           WHERE child.drive_id = $1 AND child.deleted_at IS NULL
        )
        SELECT id FROM subtree
        LIMIT $3
        """,
        drive_id,
        folder_id,
        rejecting_count,
    )
    folder_count = len(folder_rows)
    if folder_count >= rejecting_count:
        return rejecting_count

    artifact_count = await c.fetchval(
        """
        SELECT count(*)::integer
          FROM (
            SELECT 1
              FROM artifacts a
             WHERE a.drive_id = $1
               AND a.parent_id = ANY($2::text[])
               AND a.deleted_at IS NULL
             LIMIT $3
          ) AS bounded_artifacts
        """,
        drive_id,
        [row["id"] for row in folder_rows],
        rejecting_count - folder_count,
    )
    return folder_count + int(artifact_count)


async def _materialize_copy(
    c: Any,
    *,
    actor: Any,
    drive_id: str,
    destination_parent_id: str,
    destination_name: str,
    folder_rows: list[Any],
    artifact_rows: list[Any],
) -> dict[str, Any]:
    """Insert the duplicate subtree under `destination_parent_id`.

    Grant rows are NOT cloned — grants stay on the source, so the copy is
    reachable by whoever the DESTINATION's ancestry grants (inheritance is
    additive-only: seeing a folder means seeing everything under it). Each
    copied artifact gets one version (ordinal 1) referencing the source head
    version's content (same drive shares the object-store key).
    """
    folder_id_map: dict[str, str] = {}
    created_folders: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(folder_rows):
        new_fid = new_id("fld")
        folder_id_map[source["id"]] = new_fid
        if index == 0:
            parent_id = destination_parent_id
            name = destination_name
        else:
            parent_id = folder_id_map[source["parent_id"]]
            name = source["name"]
        revision = new_id("rev")
        row = await c.fetchrow(
            f"INSERT INTO folders (id, drive_id, parent_id, name, "
            f"metadata, revision) "
            f"VALUES ($1, $2, $3, $4, $5::jsonb, $6) RETURNING {_FOLDER_COLUMNS}",
            new_fid, drive_id, parent_id, name,
            json.dumps(
                json.loads(source["metadata"]) if isinstance(source["metadata"], str)
                else source["metadata"]
            ),
            revision,
        )
        created_folders[source["id"]] = dict(row)

    created_artifacts: list[tuple[str, str]] = []
    for source in artifact_rows:
        new_aid = new_id("art")
        new_vid = new_id("ver")
        parent_id = folder_id_map[source["parent_id"]]
        revision = new_id("rev")
        head = await c.fetchrow(
            "SELECT checksum, content_type, size_bytes, storage_object, "
            "storage_bucket, storage_generation FROM artifact_versions "
            "WHERE artifact_id = $1 AND id = $2",
            source["id"], source["head_version_id"],
        )
        if head is None:
            raise RuntimeError(f"artifact {source['id']} has no head version to copy")
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, content_type, content_preview, "
            "labels, metadata, head_version_id, revision) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::text[], $8::jsonb, $9, $10)",
            new_aid, drive_id, parent_id, source["name"], head["content_type"],
            source["content_preview"], source["labels"], json.dumps(
                json.loads(source["metadata"]) if isinstance(source["metadata"], str)
                else source["metadata"]
            ),
            new_vid, revision,
        )
        # Every artifact a folder copy materializes is a version PRODUCER
        # (B3 §7): its logical version bytes reserve and commit through the
        # shared seam even though the physical object is reused.
        from . import v0_content_commit as content_commit

        reservation_id = await content_commit.reserve_version_bytes(
            c, workspace_id=actor.workspace_id, drive_id=drive_id,
            principal_id=actor.subject, upload_id=None,
            size_bytes=head["size_bytes"],
            workspace_limit_bytes=actor.drive_limits.storage_bytes_workspace,
            drive_limit_bytes=actor.drive_limits.storage_bytes_drive,
        )
        await content_commit.commit_immutable_version(
            c,
            content_commit.ImmutableVersionCommit(
                drive_id=drive_id,
                workspace_id=actor.workspace_id,
                artifact_id=new_aid,
                version_id=new_vid,
                parent_version_id=None,
                ordinal=1,
                checksum=head["checksum"],
                content_type=head["content_type"],
                size_bytes=head["size_bytes"],
                storage_object=head["storage_object"],
                storage_bucket=head["storage_bucket"],
                storage_generation=head["storage_generation"],
                actor_type=actor.subject_type,
                actor_id=actor.subject,
                reservation_id=reservation_id,
            ),
        )
        created_artifacts.append((new_aid, revision, source["name"]))

    copied_root = created_folders[folder_rows[0]["id"]]
    # One change row PER copied resource, all sharing one change set id (§6.7)
    # so a feed-driven mirror learns about every descendant the subtree copy
    # materialized — not just the root. Folders append by depth/id, then
    # artifacts by id, matching the deterministic ordering.
    change_set_id = new_id("cset")
    for source in folder_rows:
        copied = created_folders[source["id"]]
        await changes.append(
            c, drive_id=drive_id, actor=actor,
            type="folder.created", resource_type="folder", resource_id=copied["id"],
            revision=copied["revision"],
            change_set_id=change_set_id,
            data={"name": copied["name"]},
        )
    for artifact_id, artifact_revision, artifact_name in created_artifacts:
        await changes.append(
            c, drive_id=drive_id, actor=actor,
            type="artifact.created", resource_type="artifact", resource_id=artifact_id,
            revision=artifact_revision,
            change_set_id=change_set_id,
            data={"name": artifact_name},
        )
    return folder_payload(copied_root)
