"""Shares vertical (slice 8): expiring read-only bearer links (§6.9).

A share is a token-as-credential link over one of three targets, described
polymorphically by ``resource_type`` + ``resource_id``:

  * ``artifact`` — the live artifact head;
  * ``artifact_version`` — an immutable version snapshot (``resource_id`` is
    the ``ver_*`` id);
  * ``folder`` — the live folder subtree.

The secret is returned ONCE at create/rotate, stored only as a SHA-256 hash
(``shares.secret_hash``), never present in list/get responses, and excluded
from logs. Rotate replaces the hash in place (same ``shr_*`` id, same
transaction — no grace window). Revoke sets ``revoked_at``.

Manager authority mirrors grants: a drive manager may mint shares on any
drive resource (drive grants are authoritative throughout the drive).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from . import paths
from . import v0_authz as authz
from . import v0_changes as changes
from .ids import new_id
from .timestamps import to_rfc3339
from .v0_drives import DriveNotFoundError, precondition
from .version_reads import read_coordinates

RESOURCE_TYPES = ("artifact", "artifact_version", "folder")

_SHARE_COLUMNS = (
    "id, drive_id, resource_type, resource_id, secret_hash, "
    "created_by_principal_type, created_by_principal_id, "
    "revision, expires_at, daily_byte_limit, created_at, rotated_at, revoked_at"
)


class ShareNotFoundError(LookupError):
    """The share does not exist in the drive (or is revoked). 404."""


class BadShareError(ValueError):
    """A share target or shape is invalid. 400 INVALID_ARGUMENT."""


class ShareRevokedError(ValueError):
    """An operation targets an already-revoked share. 409 CONFLICT."""


def hash_secret(secret: str) -> str:
    """The only representation of a share secret that may touch storage."""
    return hashlib.sha256(secret.encode()).hexdigest()


def new_share_secret() -> str:
    """A fresh high-entropy bearer secret (URL-safe base64)."""
    return secrets.token_urlsafe(32)


def _now():
    return datetime.now(UTC)


def validate_share_expiry(expires_at: datetime | None, *, now: datetime) -> datetime:
    from ..config import settings

    resolved = expires_at or now + timedelta(seconds=settings.share_default_ttl_seconds)
    if resolved <= now:
        raise BadShareError("expires_at must be in the future")
    if resolved > now + timedelta(seconds=settings.share_max_ttl_seconds):
        raise BadShareError("expires_at cannot exceed 30 days")
    return resolved


async def require_share_capacity(
    c: Any,
    *,
    workspace_id: str,
    resource_type: str,
    resource_id: str,
    workspace_limit: int,
    resource_limit: int,
) -> None:
    workspace_count = await c.fetchval(
        "SELECT count(*) FROM shares s JOIN drives d ON d.id=s.drive_id "
        "WHERE d.workspace_id=$1 AND s.revoked_at IS NULL AND s.expires_at>now()",
        workspace_id,
    )
    resource_count = await c.fetchval(
        "SELECT count(*) FROM shares s JOIN drives d ON d.id=s.drive_id "
        "WHERE d.workspace_id=$1 AND s.resource_type=$2 AND s.resource_id=$3 "
        "AND s.revoked_at IS NULL AND s.expires_at>now()",
        workspace_id,
        resource_type,
        resource_id,
    )
    if workspace_count >= workspace_limit or resource_count >= resource_limit:
        raise BadShareError("the active share limit is reached")


def share_payload(row: Any) -> dict[str, Any]:
    """The management representation — NEVER carries secret material."""
    expires_at = row["expires_at"]
    revoked_at = row["revoked_at"]
    return {
        "id": row["id"],
        "drive_id": row["drive_id"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "created_by": row["created_by_principal_id"],
        "revision": row["revision"],
        # Redemption has always honoured `expires_at`; only this field did
        # not, so an expired link reported `active` and `state=active`
        # listed it. Grants have computed expiry into `state` since they
        # shipped — same shape here.
        "state": (
            "revoked" if revoked_at is not None
            else "expired" if expires_at is not None and expires_at <= _now()
            else "active"
        ),
        "expires_at": to_rfc3339(expires_at) if expires_at else None,
        "revoked_at": to_rfc3339(revoked_at) if revoked_at else None,
        "created_at": to_rfc3339(row["created_at"]),
        "rotated_at": to_rfc3339(row["rotated_at"]) if row["rotated_at"] else None,
    }


async def _change_resource(
    c: Any, resource_type: str, resource_id: str
) -> tuple[str, str]:
    """Map a share target onto the change feed's (drive|folder|artifact)
    resource vocabulary (``drive_changes.resource_type`` is CHECK-bounded to
    those three). An ``artifact_version`` share resolves to its parent artifact
    — the watchable resource — with the exact version kept in the event
    ``data``. Everything else maps 1:1."""
    if resource_type == "artifact_version":
        artifact_id = await c.fetchval(
            "SELECT artifact_id FROM artifact_versions WHERE id=$1", resource_id,
        )
        return "artifact", artifact_id or resource_id
    return resource_type, resource_id


async def _append_share_change(
    c: Any, actor: Any, row: Any, *, type: str, previous_revision: str | None = None
) -> None:
    """Append a share change on the SAME transaction as the mutation (§6.7),
    on the share's target resource.

    HARD RULE: a share event references the share by ``shr_*`` id and its
    target only — the plaintext ``secret`` and its ``secret_hash`` NEVER enter
    a change payload."""
    change_type, change_id = await _change_resource(
        c, row["resource_type"], row["resource_id"]
    )
    await changes.append(
        c,
        drive_id=row["drive_id"],
        actor=actor,
        type=type,
        resource_type=change_type,
        resource_id=change_id,
        previous_revision=previous_revision,
        revision=row["revision"],
        data={
            "share_id": row["id"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "expires_at": to_rfc3339(row["expires_at"]) if row["expires_at"] else None,
        },
    )


async def _ensure_drive(c: Any, actor: Any, drive_id: str) -> None:
    row = await c.fetchrow(
        "SELECT workspace_id FROM drives WHERE id=$1 AND deleted_at IS NULL",
        drive_id,
    )
    if row is None or row["workspace_id"] != actor.workspace_id:
        raise DriveNotFoundError(drive_id)


async def _validate_resource(
    c: Any, drive_id: str, resource_type: str, resource_id: str
) -> None:
    """Confirm the share target exists in the drive (404 semantics)."""
    if resource_type == "folder":
        exists = await c.fetchval(
            "SELECT 1 FROM folders WHERE drive_id=$1 AND id=$2 LIMIT 1",
            drive_id, resource_id,
        )
    elif resource_type == "artifact":
        exists = await c.fetchval(
            "SELECT 1 FROM artifacts WHERE drive_id=$1 AND id=$2 LIMIT 1",
            drive_id, resource_id,
        )
    else:  # artifact_version — its drive is its artifact's, via the join
        exists = await c.fetchval(
            "SELECT 1 FROM artifact_versions v "
            "JOIN artifacts a ON a.id = v.artifact_id "
            "WHERE a.drive_id=$1 AND v.id=$2 LIMIT 1",
            drive_id, resource_id,
        )
    if not exists:
        raise ShareNotFoundError("no such share target resource")


async def create_share(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    resource_type: str,
    resource_id: str,
    expires_at: Any,
) -> dict[str, Any]:
    """Mint a read-only bearer link over a snapshot, head, or subtree.

    Returns the management payload plus the plaintext ``secret`` (create and
    rotate are the ONLY responses that carry it)."""
    await _ensure_drive(c, actor, drive_id)
    if resource_type not in RESOURCE_TYPES:
        raise BadShareError("resource_type must be artifact, artifact_version, or folder")
    await _validate_resource(c, drive_id, resource_type, resource_id)
    await c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('v0_share_create:' || $1, 0))",
        actor.workspace_id,
    )
    from ..config import settings

    now = await c.fetchval("SELECT transaction_timestamp()")
    expires_at = validate_share_expiry(expires_at, now=now)
    await require_share_capacity(
        c,
        workspace_id=actor.workspace_id,
        resource_type=resource_type,
        resource_id=resource_id,
        workspace_limit=settings.share_max_active_workspace,
        resource_limit=settings.share_max_active_resource,
    )

    if not await authz.has_role(
        c, actor=actor, drive_id=drive_id,
        resource_type="drive", resource_id=drive_id, minimum="manager",
    ):
        raise ShareNotFoundError("no such share target resource or no manager authority")

    secret = new_share_secret()
    try:
        row = await c.fetchrow(
            "INSERT INTO shares "
            "(id, drive_id, resource_type, resource_id, secret_hash, "
            "created_by_principal_type, created_by_principal_id, revision, "
            "expires_at, daily_byte_limit) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
            "RETURNING " + _SHARE_COLUMNS,
            new_id("shr"), drive_id, resource_type, resource_id,
            hash_secret(secret), actor.subject_type, actor.subject,
            new_id("rev"), expires_at, actor.drive_limits.public_share_bytes_day,
        )
    except asyncpg.UniqueViolationError:
        # Hash collision on the secret — effectively impossible; retry once.
        return await create_share(
            c, actor, drive_id,
            resource_type=resource_type, resource_id=resource_id, expires_at=expires_at,
        )
    await _append_share_change(c, actor, row, type="share.created")
    payload = share_payload(row)
    payload["secret"] = secret
    return payload


async def list_shares(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    state: str,
    limit: int,
    after_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> dict[str, Any]:
    """List the drive's shares with an ``active|revoked|all`` filter and a
    stable (id) keyset anchor. Returns ``{"items", "next_cursor"}``.

    ``resource_type`` / ``resource_id`` narrow the page to one resource's
    links — "what links exist on THIS artifact", the only query shape the
    pre-reset surface had. No extra authorization is layered on them: the
    route already requires drive ``manager`` for any share listing, so the
    filter cannot reach a row the unfiltered listing would have withheld.
    """
    await _ensure_drive(c, actor, drive_id)
    params: list[Any] = [drive_id]
    sql = f"SELECT {_SHARE_COLUMNS} FROM shares WHERE drive_id = $1"
        # `active` means redeemable/effective, so it must exclude an expired
        # row as well as a revoked one — the authorization resolver has always
        # applied the same predicate (`v0_authz._folder_level_only`), so
        # without this the listing disagreed with the access it describes.
        # An expired-but-unrevoked row is neither `active` nor `revoked`; it
        # appears under `all`, matching its computed `state`.
    if state == "active":
        sql += (
            " AND revoked_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > clock_timestamp())"
        )
    elif state == "revoked":
        sql += " AND revoked_at IS NOT NULL"
    if resource_type is not None:
        params.append(resource_type)
        sql += " AND resource_type = $" + str(len(params))
    if resource_id is not None:
        params.append(resource_id)
        sql += " AND resource_id = $" + str(len(params))
    if after_id is not None:
        params.append(after_id)
        sql += " AND id > $" + str(len(params)) + " COLLATE \"C\""
    params.append(limit + 1)
    sql += " ORDER BY id COLLATE \"C\" LIMIT $" + str(len(params))
    rows = await c.fetch(sql, *params)

    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more and rows:
        next_cursor = {"id": rows[-1]["id"]}
    return {"items": [share_payload(r) for r in rows], "next_cursor": next_cursor}


async def get_share(c: Any, actor: Any, drive_id: str, share_id: str) -> dict[str, Any]:
    """Read one share's management representation (no secret)."""
    await _ensure_drive(c, actor, drive_id)
    row = await c.fetchrow(
        f"SELECT {_SHARE_COLUMNS} FROM shares WHERE drive_id=$1 AND id=$2",
        drive_id, share_id,
    )
    if row is None:
        raise ShareNotFoundError("no such share in this drive")
    return share_payload(row)


async def rotate_share(
    c: Any,
    actor: Any,
    drive_id: str,
    share_id: str,
    *,
    if_match: str | None,
) -> dict[str, Any]:
    """Rotate the secret in place: new hash, same id, same transaction (no
    grace window). Requires If-Match on the share's state (428/412)."""
    await _ensure_drive(c, actor, drive_id)
    row = await c.fetchrow(
        f"SELECT {_SHARE_COLUMNS} FROM shares WHERE drive_id=$1 AND id=$2 FOR UPDATE",
        drive_id, share_id,
    )
    if row is None:
        raise ShareNotFoundError("no such share in this drive")
    precondition(if_match, row["revision"])
    if not await authz.has_role(
        c, actor=actor, drive_id=drive_id,
        resource_type="drive", resource_id=drive_id, minimum="manager",
    ):
        raise ShareNotFoundError("no manager authority on this share")
    if row["revoked_at"] is not None:
        raise ShareRevokedError("the share is already revoked")

    secret = new_share_secret()
    revision = new_id("rev")
    await c.execute(
        "UPDATE shares SET secret_hash=$3, rotated_at=now(), revision=$4 "
        "WHERE drive_id=$1 AND id=$2",
        drive_id, share_id, hash_secret(secret), revision,
    )
    updated = await c.fetchrow(
        f"SELECT {_SHARE_COLUMNS} FROM shares WHERE drive_id=$1 AND id=$2",
        drive_id, share_id,
    )
    await _append_share_change(
        c, actor, updated, type="share.rotated", previous_revision=row["revision"]
    )
    payload = share_payload(updated)
    payload["secret"] = secret
    return payload


async def revoke_share(
    c: Any,
    actor: Any,
    drive_id: str,
    share_id: str,
    *,
    if_match: str | None,
) -> dict[str, Any]:
    """Revoke a share (sets revoked_at). Requires If-Match on the share's
    state (428/412)."""
    await _ensure_drive(c, actor, drive_id)
    row = await c.fetchrow(
        f"SELECT {_SHARE_COLUMNS} FROM shares WHERE drive_id=$1 AND id=$2 FOR UPDATE",
        drive_id, share_id,
    )
    if row is None:
        raise ShareNotFoundError("no such share in this drive")
    precondition(if_match, row["revision"])
    if not await authz.has_role(
        c, actor=actor, drive_id=drive_id,
        resource_type="drive", resource_id=drive_id, minimum="manager",
    ):
        raise ShareNotFoundError("no manager authority on this share")
    if row["revoked_at"] is not None:
        raise ShareRevokedError("the share is already revoked")

    revision = new_id("rev")
    await c.execute(
        "UPDATE shares SET revoked_at=now(), revision=$3 WHERE drive_id=$1 AND id=$2",
        drive_id, share_id, revision,
    )
    updated = await c.fetchrow(
        f"SELECT {_SHARE_COLUMNS} FROM shares WHERE drive_id=$1 AND id=$2",
        drive_id, share_id,
    )
    await _append_share_change(
        c, actor, updated, type="share.revoked", previous_revision=row["revision"]
    )
    return share_payload(updated)


async def resolve_secret(
    c: Any, secret: str
) -> dict[str, Any] | None:
    """Resolve a bearer secret to its share's content target, or None if the
    secret is unknown, revoked, or expired.

    The share-link consumer (``/s/{share_key}``) looks up by the hashed
    secret and checks expiry/revocation — possession of the secret IS the
    credential, so no principal is required. Returns a content descriptor
    with the storage object (for artifact/version shares) or a folder marker:

    - ``{"kind": "artifact", "storage_object", "size_bytes", "content_type",
       "name", "etag", "artifact_id", "updated_at", "path"}`` for a live
       artifact head or immutable version;
    - ``{"kind": "folder", "resource_id"}`` for a live folder subtree.

    The last three keys are render metadata: the public viewer needs a title,
    a derived display path (§4.1 — path is never stored), and a timestamp to
    put in the page's OpenGraph tags. Byte serving only reads the others.
    """
    row = await c.fetchrow(
        f"SELECT {_SHARE_COLUMNS} FROM shares "
        "WHERE secret_hash=$1 "
        "AND revoked_at IS NULL "
        "AND expires_at > clock_timestamp() "
        "AND EXISTS ("
        "  SELECT 1 FROM drives WHERE id = shares.drive_id AND deleted_at IS NULL"
        ")",
        hash_secret(secret),
    )
    if row is None:
        return None

    workspace_id = await c.fetchval(
        "SELECT workspace_id FROM drives WHERE id=$1",
        row["drive_id"],
    )
    share_metadata = {
        "share_id": row["id"],
        "drive_id": row["drive_id"],
        "workspace_id": workspace_id,
        "daily_byte_limit": row["daily_byte_limit"],
    }

    resource_type = row["resource_type"]
    resource_id = row["resource_id"]

    if resource_type == "folder":
        # A share is a capability over a LIVE subtree: a soft-deleted folder
        # stops serving (its share remains but resolves to nothing).
        live = await c.fetchval(
            "SELECT 1 FROM folders WHERE id=$1 AND drive_id=$2 AND deleted_at IS NULL",
            resource_id, row["drive_id"],
        )
        if not live:
            return None
        return {
            "kind": "folder",
            "resource_id": resource_id,
            **share_metadata,
        }

    if resource_type == "artifact_version":
        version = await c.fetchrow(
            "SELECT v.storage_object, v.storage_bucket, v.storage_generation, "
            "       v.size_bytes, v.content_type, v.id, "
            "       a.name, a.id AS artifact_id, a.updated_at "
            "FROM artifact_versions v "
            "JOIN artifacts a ON a.id = v.artifact_id "
            "WHERE v.id = $1 AND a.deleted_at IS NULL",
            resource_id,
        )
        if version is None:
            return None
        coordinates = read_coordinates(version)
        if coordinates is None:
            return None
        return {
            "kind": "artifact",
            **share_metadata,
            "storage_object": version["storage_object"],
            "storage_bucket": coordinates[0],
            "storage_generation": coordinates[1],
            "size_bytes": version["size_bytes"],
            "content_type": version["content_type"],
            "name": version["name"],
            "etag": version["id"],
            "artifact_id": version["artifact_id"],
            "updated_at": version["updated_at"],
            "path": await paths.artifact_path(c, version["artifact_id"])
            or version["name"],
        }

    # Live artifact head — soft-delete stops the share (§6.9).
    version = await c.fetchrow(
        "SELECT v.storage_object, v.storage_bucket, v.storage_generation, "
        "       v.size_bytes, v.content_type, v.id, "
        "       a.name, a.id AS artifact_id, a.updated_at "
        "FROM artifact_versions v "
        "JOIN artifacts a ON a.id = v.artifact_id AND a.head_version_id = v.id "
        "WHERE a.id = $1 AND a.deleted_at IS NULL",
        resource_id,
    )
    if version is None:
        return None
    coordinates = read_coordinates(version)
    if coordinates is None:
        return None
    return {
        "kind": "artifact",
        **share_metadata,
        "storage_object": version["storage_object"],
        "storage_bucket": coordinates[0],
        "storage_generation": coordinates[1],
        "size_bytes": version["size_bytes"],
        "content_type": version["content_type"],
        "name": version["name"],
        "etag": version["id"],
        "artifact_id": version["artifact_id"],
        "updated_at": version["updated_at"],
        "path": await paths.artifact_path(c, version["artifact_id"]) or version["name"],
    }
