"""Drive vertical: workspace-scoped drive CRUD, lifecycle, and usage (slice 4).

Drives belong to a Hub workspace named by ``V0ActorContext.workspace_id``;
there are no local ``organizations``/``users`` tables on this branch, so
workspace scoping is the single authorization axis here. A drive is created
with its structural root folder and a creator-manager grant in ONE
transaction (§6.1, §4.2).

**Drive-limit cap.** The design's "tiers-style" drive limit is read from an
entitlement service in the full cutover; there is no ``tiers`` table on this
branch, so the cap is a documented module-level constant,
``MAX_DRIVES_PER_WORKSPACE``. It counts ACTIVE (non-soft-deleted) drives and
is enforced under a transaction-scoped advisory lock keyed on the workspace,
because a plain ``SELECT ... FOR UPDATE`` over the workspace's drive rows has
nothing to lock when the workspace is empty — exactly the case two concurrent
first-creates race on. ``pg_advisory_xact_lock`` serializes creates for one
workspace regardless of row count and releases on commit/rollback.

**Usage.** ``storage_bytes`` is the LOCKED authoritative per-drive committed
logical counter (B3 accounting, migration 0049): every version producer
maintains it through the shared `v0_content_commit` seam, and
`storage_bytes_parity` asserts agreement with
``sum(artifact_versions.size_bytes)``. ``retrieval_bytes`` has no recording
table on this branch — it is surfaced from the ``drives.retrieval_bytes``
counter that the future content-read slice maintains.

**No change-feed writes.** The ``drive_changes`` ledger is sequenced per-drive
via ``drive_change_heads``; wiring it is the changes vertical's job, so this
slice mutates rows only.

Layer rule: this module never imports ``agentdrive.api`` (api depends on core).
Precondition failures and cursor payloads are handed back as exceptions /
plain data and translated to wire statuses and opaque cursors at the route
layer.
"""

from __future__ import annotations

import json
from typing import Any

from agentdrive.config import settings

from ..core.ids import new_id
from ..core.timestamps import to_rfc3339
from . import v0_changes as changes
from .usage.gate import usage_gate
from .usage.models import Metric, Period, ScopeType
from .usage.policy import for_actor

# Documented config-free cap (see module docstring). Tests monkeypatch this;
# reads happen at call time so a patch is honored.
MAX_DRIVES_PER_WORKSPACE = 100


class DriveNotFoundError(LookupError):
    """The drive does not exist in the actor's workspace (or is soft-deleted).

    Always a 404 DRIVE_NOT_FOUND at the boundary — including the
    cross-workspace case, so a drive's existence is not disclosed (§6.1).
    """


class DriveLimitReachedError(RuntimeError):
    """The workspace is already at its active-drive cap."""


class DriveNotDeletedError(RuntimeError):
    """restore was called on a drive that is not soft-deleted."""


class PreconditionError(Exception):
    """A mutation's If-Match failed. The route maps this to the exact wire
    status (428 when the header is absent, 412 when stale), which the
    V0ApiError status map deliberately does not carry.

    ``current_revision`` is the resource's revision at the time of the
    failure, carried on 412 so the route can surface it as
    ``details.current_revision`` (the If-Match value for a corrected retry).
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        current_revision: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.current_revision = current_revision


_DRIVE_COLUMNS = (
    "id, workspace_id, created_by_principal_id, name, metadata, revision, "
    "root_folder_id, storage_bytes, retrieval_bytes, created_at, updated_at, deleted_at"
)


def drive_payload(row: Any) -> dict[str, Any]:
    """The wire shape of one drive row."""
    metadata = row["metadata"]
    deleted_at = row["deleted_at"]
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "created_by": row["created_by_principal_id"],
        "name": row["name"],
        "metadata": json.loads(metadata) if isinstance(metadata, str) else metadata,
        "revision": row["revision"],
        "root_folder_id": row["root_folder_id"],
        "storage_bytes": row["storage_bytes"],
        "retrieval_bytes": row["retrieval_bytes"],
        "created_at": to_rfc3339(row["created_at"]),
        "updated_at": to_rfc3339(row["updated_at"]),
        "deleted_at": to_rfc3339(deleted_at) if deleted_at else None,
        "state": "deleted" if deleted_at else "active",
    }


async def _lock_drive_namespace(c: Any, drive_id: str) -> None:
    """Serialize sibling namespace mutations for one drive.

    §12A's "app transaction" fallback: the cross-table collision trigger does
    not close the concurrent one-row-per-table race, so writers serialize on a
    drive-scoped advisory lock. The key is namespaced `v0_drive_namespace:` —
    the single lock every folder, artifact, and drive namespace writer takes,
    so a drive soft-delete serializes against content writes.
    """
    await c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('v0_drive_namespace:' || $1, 0))",
        drive_id,
    )


async def create_drive(
    c: Any, actor: Any, *, name: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Create a drive with its structural root folder and creator-manager
    grant(s), atomically. Must run inside a transaction; raises
    `DriveLimitReachedError` when the workspace is at its active-drive cap."""
    await c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        actor.workspace_id,
    )
    active = await c.fetchval(
        "SELECT count(*) FROM drives WHERE workspace_id = $1 AND deleted_at IS NULL",
        actor.workspace_id,
    )
    if active >= MAX_DRIVES_PER_WORKSPACE:
        raise DriveLimitReachedError(
            f"workspace {actor.workspace_id} already has "
            f"{MAX_DRIVES_PER_WORKSPACE} active drives"
        )

    drive_id = new_id("drv")
    folder_id = new_id("fld")
    revision = new_id("rev")

    # Insert order respects the day-0 FK graph. `folders.drive_id` → `drives`
    # is immediate, so the drive row must precede the folder; the reverse FK
    # `drives(id, root_folder_id)` → `folders` is DEFERRABLE and files the
    # root back onto the drive last (§4.2: a drive + its root are one txn).
    await c.execute(
        "INSERT INTO drives "
        "  (id, workspace_id, created_by_principal_id, name, metadata, revision, root_folder_id) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6, NULL)",
        drive_id, actor.workspace_id, actor.subject, name, json.dumps(metadata), revision,
    )
    await c.execute(
        "INSERT INTO folders (id, drive_id, parent_id, name, revision) "
        "VALUES ($1, $2, NULL, NULL, $3)",
        folder_id, drive_id, revision,
    )
    await c.execute(
        "UPDATE drives SET root_folder_id = $2 WHERE id = $1",
        drive_id, folder_id,
    )

    # Imported HERE, not at module scope: `v0_grants` imports this module
    # (`DriveNotFoundError`, `precondition`), so a top-level import would be
    # circular and fail at startup. `v0_uploads` reaches for `v0_changes` the
    # same way and for the same reason.
    from .v0_grants import _GRANT_COLUMNS, append_grant_change

    grants = [(actor.subject_type, actor.subject)]
    if actor.subject_type == "agent" and actor.sponsor_id:
        grants.append(("user", actor.sponsor_id))
    grant_rows = []
    for principal_type, principal_id in grants:
        row = await c.fetchrow(
            "INSERT INTO grants "
            "  (id, drive_id, resource_type, resource_id, principal_type, "
            "   principal_id, role, revision) "
            "VALUES ($1, $2, 'drive', $3, $4, $5, 'manager', $6) "
            "RETURNING " + _GRANT_COLUMNS,
            new_id("grn"), drive_id, drive_id, principal_type, principal_id,
            new_id("rev"),
        )
        grant_rows.append(row)

    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="drive.updated", resource_type="drive", resource_id=drive_id,
        revision=revision,
        data={"name": name},
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="folder.created", resource_type="folder", resource_id=folder_id,
        revision=revision,
        data={"name": None},
    )
    for grow in grant_rows:
        await append_grant_change(c, actor, grow, type="grant.created")

    row = await c.fetchrow(
        f"SELECT {_DRIVE_COLUMNS} FROM drives WHERE id = $1", drive_id
    )
    return drive_payload(row)


async def _fetch(
    c: Any, drive_id: str, *, include_deleted: bool, for_update: bool = False
) -> Any | None:
    sql = f"SELECT {_DRIVE_COLUMNS} FROM drives WHERE id = $1"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    if for_update:
        sql += " FOR UPDATE"
    return await c.fetchrow(sql, drive_id)


def _drive_or_404(row: Any | None, actor: Any, drive_id: str) -> Any:
    if row is None or row["workspace_id"] != actor.workspace_id:
        raise DriveNotFoundError(drive_id)
    return row


async def get_drive(c: Any, actor: Any, drive_id: str) -> dict[str, Any]:
    """Read one active drive in the actor's workspace (404 semantics for
    missing / other-workspace / soft-deleted)."""
    row = await _fetch(c, drive_id, include_deleted=False)
    return drive_payload(_drive_or_404(row, actor, drive_id))


async def list_drives(
    c: Any,
    actor: Any,
    *,
    state: str,
    limit: int,
    after_ts: Any = None,
    after_id: str | None = None,
) -> dict[str, Any]:
    """Active/deleted/all listing, newest-first with a stable (created_at, id)
    keyset anchor. Returns ``{"items", "next_cursor"}`` where ``next_cursor``
    is a plain payload dict for the route to encode (or None at the last
    page); the route validates the caller-supplied cursor and encodes the
    next one.

    Rows are filtered to the drives the actor can actually read (grant
    visibility): the same predicate `drives_read` enforces, so the list shows
    EXACTLY the drives for which the identical caller's `GET /v0/drives/{id}`
    would return 200 — a drive with a local grant is visible, one without is
    absent. A workspace-scoped list that showed drives the caller couldn't
    open would contradict the 404 that same read returns. That parity is
    exactly why a workspace owner/admin sees EVERY drive in the workspace
    (the workspace-admin overlay, §8): their `drives_read` answers 200 for
    all of them, so their list does too — the rows are already
    workspace-filtered ($1), which pins the overlay to their own workspace."""
    from . import v0_authz

    # Actor params are appended after the workspace id ($1): subject_type,
    # subject, workspace_id feed the drive-visibility EXISTS; $5 is the
    # workspace-admin overlay flag.
    principal_type_param = 2
    principal_id_param = 3
    workspace_param = 4
    overlay_param = 5
    params: list[Any] = [
        actor.workspace_id, actor.subject_type, actor.subject, actor.workspace_id,
        v0_authz.workspace_admin_overlay(actor),
    ]
    sql = f"SELECT {_DRIVE_COLUMNS} FROM drives WHERE workspace_id = $1"
    sql += " AND" + v0_authz.drive_visibility_exists(
        drive_id_expr="drives.id",
        principal_type_param=principal_type_param,
        principal_id_param=principal_id_param,
        workspace_param=workspace_param,
        overlay_param=overlay_param,
    )
    if state == "active":
        sql += " AND deleted_at IS NULL"
    elif state == "deleted":
        sql += " AND deleted_at IS NOT NULL"
    if after_ts is not None:
        params.extend([after_ts, after_id])
        sql += " AND (created_at, id) < ($6, $7)"
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
    return {"items": [drive_payload(r) for r in rows], "next_cursor": next_cursor}


def precondition(if_match: str | None, current: str) -> None:
    """Enforce If-Match for mutation-of-existing ops: raise PreconditionError
    (428 absent / 412 stale) unless `current` matches the header."""
    if if_match is None:
        raise PreconditionError(
            428, "PRECONDITION_REQUIRED", "If-Match header is required"
        )
    values = etag_values(if_match, weak=False)
    if values == "*" or current in (values or []):
        return
    raise PreconditionError(
        412, "PRECONDITION_FAILED", "the resource changed after it was read",
        current_revision=current,
    )


def etag_values(value: str, *, weak: bool = True) -> str | list[str]:
    """Parse a comma-separated ETag header into unquoted members (or ``*``).

    ``weak=True`` (the default, used for ``If-None-Match``) strips a ``W/``
    prefix so weak and strong validators compare equal — the correct
    comparison for conditional GETs. ``weak=False`` (used for ``If-Match``,
    RFC 9110 §13.1.1) preserves ``W/`` members verbatim so a weak validator
    never matches a strong ETag.
    """
    v = value.strip()
    if v == "*":
        return "*"
    parts: list[str] = []
    for member in v.split(","):
        member = member.strip()
        if weak and member.startswith("W/"):
            member = member[2:].strip()
        if len(member) >= 2 and member.startswith('"') and member.endswith('"'):
            member = member[1:-1]
        if member:
            parts.append(member)
    return parts


async def patch_drive(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    name: str | None,
    metadata: dict[str, Any] | None,
    if_match: str | None,
) -> dict[str, Any]:
    """Update a drive's name/metadata, bumping its revision under If-Match."""
    row = _drive_or_404(
        await _fetch(c, drive_id, include_deleted=False, for_update=True), actor, drive_id
    )
    precondition(if_match, row["revision"])

    revision = new_id("rev")
    sets = ["revision = $2", "updated_at = now()"]
    params: list[Any] = [drive_id, revision]
    if name is not None:
        sets.append("name = $" + str(len(params) + 1))
        params.append(name)
    if metadata is not None:
        sets.append("metadata = $" + str(len(params) + 1) + "::jsonb")
        params.append(json.dumps(metadata))
    await c.execute(
        "UPDATE drives SET " + ", ".join(sets) + " WHERE id = $1",
        *params,
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="drive.updated", resource_type="drive", resource_id=drive_id,
        previous_revision=row["revision"], revision=revision,
        data={"name": name if name is not None else row["name"]},
    )
    return drive_payload(await _fetch(c, drive_id, include_deleted=False))


async def delete_drive(
    c: Any, actor: Any, drive_id: str, *, if_match: str | None
) -> dict[str, Any]:
    """Soft-delete a drive (sets deleted_at, bumps revision) under If-Match.

    Serializes on the drive advisory lock so a concurrent content write cannot
    slip a folder/artifact into the drive after it is soft-deleted (the
    liveness check in the content mutators runs under the same lock)."""
    await _lock_drive_namespace(c, drive_id)
    row = _drive_or_404(
        await _fetch(c, drive_id, include_deleted=False, for_update=True), actor, drive_id
    )
    precondition(if_match, row["revision"])

    revision = new_id("rev")
    await c.execute(
        "UPDATE drives SET deleted_at = now(), revision = $2, updated_at = now() "
        "WHERE id = $1",
        drive_id, revision,
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="drive.deleted", resource_type="drive", resource_id=drive_id,
        previous_revision=row["revision"], revision=revision,
        data={"name": row["name"]},
    )
    row = await c.fetchrow(
        f"SELECT {_DRIVE_COLUMNS} FROM drives WHERE id = $1", drive_id
    )
    return drive_payload(row)


async def restore_drive(
    c: Any, actor: Any, drive_id: str, *, if_match: str | None
) -> dict[str, Any]:
    """Restore a soft-deleted drive (clears deleted_at, bumps revision).

    The If-Match is judged against the drive's CURRENT revision — the
    post-delete revision returned by the delete response — so a restore cannot
    clobber a drive that moved since it was read. Restoring an active drive is
    a `DriveNotDeletedError` (409 CONFLICT), not a silent no-op."""
    row = _drive_or_404(
        await _fetch(c, drive_id, include_deleted=True, for_update=True), actor, drive_id
    )
    if row["deleted_at"] is None:
        raise DriveNotDeletedError(drive_id)
    precondition(if_match, row["revision"])

    revision = new_id("rev")
    await c.execute(
        "UPDATE drives SET deleted_at = NULL, revision = $2, updated_at = now() "
        "WHERE id = $1",
        drive_id, revision,
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="drive.restored", resource_type="drive", resource_id=drive_id,
        previous_revision=row["revision"], revision=revision,
        data={"name": row["name"]},
    )
    row = await c.fetchrow(
        f"SELECT {_DRIVE_COLUMNS} FROM drives WHERE id = $1", drive_id
    )
    return drive_payload(row)


def _usage_meter(
    *, scope: str, used: int, reserved: int, limit: int, reset_at: Any = None
) -> dict[str, Any]:
    return {
        "scope": scope,
        "used": int(used),
        "reserved": int(reserved),
        "limit": int(limit),
        "remaining": max(int(limit) - int(used) - int(reserved), 0),
        "reset_at": to_rfc3339(reset_at) if reset_at else None,
    }


async def drive_usage(c: Any, actor: Any, drive_id: str) -> dict[str, Any]:
    """One consistent snapshot of counters and the actor's effective limits.

    ``storage_bytes`` reads the LOCKED authoritative counter maintained by
    the shared commit seam (`v0_content_commit`, B3 §9) — migration 0049
    backfilled it from ``sum(artifact_versions.size_bytes)`` and every
    version producer keeps it exact. Parity against the live sum is asserted
    by `v0_content_commit.storage_bytes_parity` and checked by the GC job.
    """
    from .v0_artifacts import MAX_BUFFERED_UPLOAD_BYTES

    policy = for_actor(actor)
    async with c.transaction(isolation="repeatable_read", readonly=True):
        now = await c.fetchval("SELECT transaction_timestamp()")
        row = _drive_or_404(
            await _fetch(c, drive_id, include_deleted=False), actor, drive_id
        )
        drive_reserved = await c.fetchval(
            "SELECT COALESCE(sum(size_bytes), 0) FROM storage_reservations "
            "WHERE drive_id = $1 AND released_at IS NULL",
            drive_id,
        )
        workspace = await c.fetchrow(
            "SELECT committed_bytes, reserved_bytes FROM workspace_storage "
            "WHERE workspace_id = $1",
            actor.workspace_id,
        )
        downloads = await usage_gate.meter.snapshot(
            c, limits=policy.private_download, now=now
        )

    day = downloads[
        (Metric.DOWNLOAD_BYTES, ScopeType.WORKSPACE.value, actor.workspace_id, Period.DAY)
    ]
    month = downloads[
        (
            Metric.DOWNLOAD_BYTES,
            ScopeType.WORKSPACE.value,
            actor.workspace_id,
            Period.MONTH,
        )
    ]
    workspace_used = int(workspace["committed_bytes"]) if workspace else 0
    workspace_reserved = int(workspace["reserved_bytes"]) if workspace else 0
    storage_bytes = int(row["storage_bytes"])
    return {
        "storage_bytes": storage_bytes,
        "retrieval_bytes": int(row["retrieval_bytes"]),
        "meters": {
            "drive_storage": _usage_meter(
                scope="drive",
                used=storage_bytes,
                reserved=drive_reserved,
                limit=policy.storage_drive,
            ),
            "workspace_storage": _usage_meter(
                scope="workspace",
                used=workspace_used,
                reserved=workspace_reserved,
                limit=policy.storage_workspace,
            ),
            "workspace_download_day": _usage_meter(
                scope="workspace",
                used=day.used,
                reserved=day.reserved,
                limit=day.limit,
                reset_at=day.reset_at,
            ),
            "workspace_download_month": _usage_meter(
                scope="workspace",
                used=month.used,
                reserved=month.reserved,
                limit=month.limit,
                reset_at=month.reset_at,
            ),
        },
        "effective_limits": {
            "max_file_bytes": settings.max_file_bytes,
            "max_inline_file_bytes": MAX_BUFFERED_UPLOAD_BYTES,
            "share_default_ttl_seconds": settings.share_default_ttl_seconds,
            "share_max_ttl_seconds": settings.share_max_ttl_seconds,
        },
    }
