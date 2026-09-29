"""D13 unified namespace reads: direct-child entries and whole-path lookup.

The module deliberately returns compact payloads instead of reusing the rich
folder/artifact documents. ``entries`` orders by immutable ``(created_at, id)``
and performs one UNION query over the shared namespace. ``lookup`` resolves all
segments and authorizes the final target in one recursive database statement.
"""

from __future__ import annotations

from typing import Any

from . import v0_authz
from .timestamps import to_rfc3339
from .v0_drives import DriveNotFoundError
from .v0_folders import FolderNotFoundError, _ensure_drive, _live_folder


class NavigationNotFoundError(LookupError):
    """A parent or whole path is absent or not visible (one 404 contract)."""


def _entry_payload(row: Any) -> dict[str, Any]:
    deleted_at = row["deleted_at"]
    payload: dict[str, Any] = {
        "type": row["type"],
        "id": row["id"],
        "name": row["name"],
        "revision": row["revision"],
        "updated_at": to_rfc3339(row["updated_at"]),
        "state": "deleted" if deleted_at else "active",
        "deleted_at": to_rfc3339(deleted_at) if deleted_at else None,
    }
    if row["type"] == "artifact":
        payload.update(
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            head_version_id=row["head_version_id"],
        )
    return payload


async def list_entries(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    parent_id: str,
    entry_type: str | None,
    name: str | None,
    label: str | None,
    content_type: str | None,
    updated_after: Any,
    updated_before: Any,
    state: str,
    limit: int,
    after_ts: Any = None,
    after_id: str | None = None,
) -> dict[str, Any]:
    """List visible direct children in one stable, cross-kind page."""
    try:
        await _ensure_drive(c, actor, drive_id)
        parent = await _live_folder(c, drive_id, parent_id, for_update=False)
        if parent is None:
            raise FolderNotFoundError("folder not found")
        await v0_authz.require(
            c,
            actor=actor,
            drive_id=drive_id,
            resource_type="folder",
            resource_id=parent_id,
            minimum="viewer",
        )
    except (DriveNotFoundError, FolderNotFoundError, v0_authz.NotAuthorizedError):
        raise NavigationNotFoundError("resource not found") from None

    params: list[Any] = [drive_id, parent_id]
    folder_where = ["f.drive_id = $1", "f.parent_id = $2"]
    artifact_where = ["a.drive_id = $1", "a.parent_id = $2"]

    if state == "active":
        folder_where.append("f.deleted_at IS NULL")
        artifact_where.append("a.deleted_at IS NULL")
    elif state == "deleted":
        folder_where.append("f.deleted_at IS NOT NULL")
        artifact_where.append("a.deleted_at IS NOT NULL")

    if name is not None:
        params.append(name)
        placeholder = f"${len(params)}"
        folder_where.append(f'f.name = {placeholder} COLLATE "C"')
        artifact_where.append(f'a.name = {placeholder} COLLATE "C"')

    if content_type is not None:
        params.append(content_type)
        artifact_where.append(f'a.content_type = ${len(params)} COLLATE "C"')
    if label is not None:
        params.append(label)
        artifact_where.append(f"a.labels @> ARRAY[${len(params)}]::text[]")
    if updated_after is not None:
        params.append(updated_after)
        artifact_where.append(f"a.updated_at >= ${len(params)}")
    if updated_before is not None:
        params.append(updated_before)
        artifact_where.append(f"a.updated_at <= ${len(params)}")

    if after_ts is not None:
        params.extend([after_ts, after_id])
        ts_param = len(params) - 1
        id_param = len(params)
        folder_where.append(f"(f.created_at, f.id) < (${ts_param}, ${id_param})")
        artifact_where.append(f"(a.created_at, a.id) < (${ts_param}, ${id_param})")

    # Artifact-only filters imply the artifact member of the union.
    include_folders = entry_type in (None, "folder")
    include_artifacts = entry_type in (None, "artifact")
    if any(value is not None for value in (label, content_type, updated_after, updated_before)):
        include_folders = False

    selects: list[str] = []
    if include_folders:
        selects.append(
            "SELECT 'folder'::text AS type, f.id, f.name, f.revision, "
            "f.created_at, f.updated_at, f.deleted_at, "
            "NULL::bigint AS size_bytes, NULL::text AS content_type, "
            "NULL::text AS head_version_id FROM folders f WHERE "
            + " AND ".join(folder_where)
        )
    if include_artifacts:
        selects.append(
            "SELECT 'artifact'::text AS type, a.id, a.name, a.revision, "
            "a.created_at, a.updated_at, a.deleted_at, "
            "COALESCE(v.size_bytes, 0)::bigint AS size_bytes, a.content_type, "
            "a.head_version_id FROM artifacts a "
            "LEFT JOIN artifact_versions v ON v.id = a.head_version_id "
            "WHERE "
            + " AND ".join(artifact_where)
        )

    params.append(limit + 1)
    sql = (
        "SELECT * FROM ("
        + " UNION ALL ".join(selects)
        + f") entries ORDER BY created_at DESC, id DESC LIMIT ${len(params)}"
    )
    rows = await c.fetch(sql, *params)
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = {
            "created_at": to_rfc3339(last["created_at"]),
            "id": last["id"],
        }
    return {"entries": [_entry_payload(row) for row in rows], "next_cursor": next_cursor}


_LOOKUP_SQL = """
WITH RECURSIVE
drive_ctx AS (
  SELECT d.id AS drive_id, d.root_folder_id
    FROM drives d
   WHERE d.id = $1
     AND d.workspace_id = $2
     AND d.deleted_at IS NULL
),
walk(depth, type, id, parent_id, revision) AS (
  SELECT 0, 'folder'::text, f.id, f.parent_id, f.revision
    FROM drive_ctx d
    JOIN folders f
      ON f.drive_id = d.drive_id AND f.id = d.root_folder_id
   WHERE f.deleted_at IS NULL
  UNION ALL
  SELECT w.depth + 1, child.type, child.id, child.parent_id, child.revision
    FROM walk w
    JOIN LATERAL (
      SELECT 'folder'::text AS type, f.id, f.parent_id, f.revision
        FROM folders f
       WHERE f.drive_id = $1
         AND f.parent_id = w.id
         AND f.name = ($3::text[])[w.depth + 1] COLLATE "C"
         AND f.deleted_at IS NULL
      UNION ALL
      SELECT 'artifact'::text, a.id, a.parent_id, a.revision
        FROM artifacts a
       WHERE a.drive_id = $1
         AND a.parent_id = w.id
         AND a.name = ($3::text[])[w.depth + 1] COLLATE "C"
         AND a.deleted_at IS NULL
         AND w.depth + 1 = cardinality($3::text[])
    ) child ON w.type = 'folder'
   WHERE w.depth < cardinality($3::text[])
),
target AS (
  SELECT w.type, w.id, w.parent_id, w.revision
    FROM walk w
   WHERE w.depth = cardinality($3::text[])
     AND ($4::text IS NULL OR w.type = $4)
),
ancestry(id, parent_id) AS (
  SELECT f.id, f.parent_id
    FROM target t
    JOIN folders f
      ON f.drive_id = $1
     AND f.id = CASE WHEN t.type = 'folder' THEN t.id ELSE t.parent_id END
  UNION ALL
  SELECT parent.id, parent.parent_id
    FROM folders parent
    JOIN ancestry a ON parent.id = a.parent_id
   WHERE parent.drive_id = $1
),
role AS (
  SELECT max(CASE g.role WHEN 'manager' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END) AS level
    FROM target t
    JOIN grants g ON g.drive_id = $1
    LEFT JOIN ancestry a
      ON g.resource_type = 'folder' AND g.resource_id = a.id
   WHERE g.revoked_at IS NULL
     AND (g.expires_at IS NULL OR g.expires_at > clock_timestamp())
     AND _principal_matches($5, $6, $7, g.principal_type, g.principal_id)
     AND (
       g.resource_type = 'drive' AND g.resource_id = $1
       OR t.type = 'artifact'
          AND g.resource_type = 'artifact' AND g.resource_id = t.id
       OR a.id IS NOT NULL
     )
)
SELECT t.type, t.id, t.parent_id, t.revision
  FROM target t
 CROSS JOIN role r
 WHERE r.level >= 1
"""


async def lookup(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    segments: list[str],
    entry_type: str | None,
) -> dict[str, Any]:
    """Resolve and authorize the complete relative path in one DB operation."""
    row = await c.fetchrow(
        _LOOKUP_SQL,
        drive_id,
        actor.workspace_id,
        segments,
        entry_type,
        actor.subject_type,
        actor.subject,
        actor.workspace_id,
    )
    if row is None:
        raise NavigationNotFoundError("resource not found")
    return {
        "type": row["type"],
        "id": row["id"],
        "parent_id": row["parent_id"],
        "revision": row["revision"],
    }
