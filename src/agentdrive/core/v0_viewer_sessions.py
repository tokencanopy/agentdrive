"""Viewer sessions: short-lived, hashed credentials for the private viewer.

Minted on `/v0` (product token + `content:read` scope + a live local viewer
grant), redeemed on the isolated viewer host with an `Authorization` header.
The shares pattern, deliberately: `secrets.token_urlsafe(32)` plaintext shown
once, SHA-256 hash in the row, unique index on the hash, and a resolver that
collapses every failure into one `None`.

NOT a share. A viewer session never appears in share management, never lists,
never rotates, and never grants anything to a principal other than the one
that minted it — resolution re-checks that principal's CURRENT viewer grant,
so a grant revoked after minting stops the credential within one fetch.

The session is pinned to one immutable `ver_*` at mint time (the head is
resolved during minting when no explicit version is given). The composite FK
in the schema makes "version belongs to this artifact" structural; nothing
here ever re-reads the head, so a session can never silently render newer
bytes than the ones it was minted for.
"""

from __future__ import annotations

from typing import Any

from ..identity.actor import WORKSPACE_ADMIN_ROLES
from . import paths
from . import v0_authz as authz
from .ids import hash_key, new_id
from .timestamps import to_rfc3339
from .v0_drives import DriveNotFoundError
from .v0_shares import new_share_secret
from .version_reads import read_coordinates

# How many expired rows one mint-time sweep may lock. Small on purpose —
# see `sweep_expired`; the daily GC job owns bulk cleanup.
_SWEEP_LIMIT = 200


class ViewerSessionNotFoundError(LookupError):
    """The mint target is missing or unauthorized. Uniform 404."""


class ViewerVersionNotFoundError(LookupError):
    """An explicit version_id is missing or belongs to another artifact."""


def new_viewer_credential() -> str:
    """A fresh high-entropy bearer credential (URL-safe base64).

    The same construction as a share secret — one credential recipe on this
    codebase, one review surface."""
    return new_share_secret()


def session_payload(row: Any) -> dict[str, Any]:
    """The mint-response representation — NEVER carries credential material."""
    return {
        "id": row["id"],
        "drive_id": row["drive_id"],
        "artifact_id": row["artifact_id"],
        "version_id": row["version_id"],
        "expires_at": to_rfc3339(row["expires_at"]),
        "created_at": to_rfc3339(row["created_at"]),
    }


async def _ensure_drive(c: Any, actor: Any, drive_id: str) -> None:
    row = await c.fetchrow(
        "SELECT workspace_id FROM drives WHERE id=$1 AND deleted_at IS NULL",
        drive_id,
    )
    if row is None or row["workspace_id"] != actor.workspace_id:
        raise DriveNotFoundError(drive_id)


async def create_session(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    version_id: str | None,
    ttl_seconds: int,
) -> dict[str, Any]:
    """Mint a viewer session pinned to one immutable version.

    The route layer has already established scope (`content:read`) and the
    local viewer grant (`require_local`). This function re-anchors the
    resources — live drive in the actor's workspace, live artifact in that
    drive, version belonging to that artifact — resolves the head when no
    version is named, and mints the credential.

    Returns the session payload plus the plaintext ``credential`` — the only
    place it ever exists outside the caller's memory.
    """
    await _ensure_drive(c, actor, drive_id)

    if version_id is None:
        row = await c.fetchrow(
            "SELECT a.head_version_id AS version_id FROM artifacts a "
            "WHERE a.id=$1 AND a.drive_id=$2 AND a.deleted_at IS NULL",
            artifact_id, drive_id,
        )
        if row is None or row["version_id"] is None:
            raise ViewerSessionNotFoundError("no such artifact in this drive")
        pinned = row["version_id"]
    else:
        exists = await c.fetchval(
            "SELECT 1 FROM artifact_versions v "
            "JOIN artifacts a ON a.id = v.artifact_id "
            "WHERE v.id=$1 AND a.id=$2 AND a.drive_id=$3 AND a.deleted_at IS NULL",
            version_id, artifact_id, drive_id,
        )
        if not exists:
            raise ViewerVersionNotFoundError(
                "no such version on this artifact"
            )
        pinned = version_id

    # Opportunistic sweep: expired rows are dead weight under a unique
    # index. Row-BOUNDED, not merely index-assisted — this runs inside the
    # mint's transaction, and an unbounded DELETE would lock a whole
    # backlog while two concurrent mints take those locks in nondeterministic
    # order. The daily GC job is what actually guarantees the table stays
    # trimmed; this only keeps up with steady-state traffic.
    await sweep_expired(c, limit=_SWEEP_LIMIT)

    # No collision retry. A 256-bit credential and a 64-bit id make a
    # unique violation unreachable, and the retry that used to sit here
    # could not have worked anyway: it re-ran statements on a connection
    # whose transaction the violation had already aborted, so it would
    # raise InFailedSQLTransactionError rather than insert. A genuinely
    # impossible error should surface as one.
    # A private viewer session is a narrow BROWSER console capability minted
    # by the Human BFF path (service account design §7.1). A Service Account
    # has no browser, and the table's CHECK already refuses one — but reaching
    # that constraint turns an intentional denial into a database error and a
    # 500. Refuse here, in the vocabulary the route already speaks.
    # The module's existing uniform denial, not a new code: an unauthorized
    # mint already answers 404 here, and a Service learning "you specifically
    # are excluded" is a distinction nothing needs to draw.
    if actor.subject_type not in ("agent", "user"):
        raise ViewerSessionNotFoundError(artifact_id)

    credential = new_viewer_credential()
    # `principal_workspace_role` snapshots the minting token's claim so the
    # token-less resolution re-check can honor the workspace-admin overlay —
    # without it, a session an owner/admin mints on a drive they hold no
    # grant row in could never resolve (the public-grant trap, again).
    row = await c.fetchrow(
        "INSERT INTO viewer_sessions "
        "(id, drive_id, artifact_id, version_id, workspace_id, "
        " principal_type, principal_id, principal_workspace_role, "
        " credential_hash, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, "
        "        now() + make_interval(secs => $10)) "
        "RETURNING id, drive_id, artifact_id, version_id, "
        "          created_at, expires_at",
        new_id("vwr"), drive_id, artifact_id, pinned,
        actor.workspace_id, actor.subject_type, actor.subject,
        getattr(actor, "workspace_role", None),
        hash_key(credential), ttl_seconds,
    )
    payload = session_payload(row)
    payload["expires_in"] = ttl_seconds
    payload["credential"] = credential
    return payload


async def resolve_credential(c: Any, credential: str) -> dict[str, Any] | None:
    """Resolve a viewer credential to a render/byte descriptor, or None.

    One `None` for every refusal — unknown credential, expired session,
    deleted drive/artifact, vanished version, and a viewer grant that has
    been revoked since minting — so the route cannot leak which it was.

    The grant re-check is the revocation story: the credential proves the
    mint happened; the CURRENT grant decides whether the bytes still flow.
    Token scope is not re-checked (there is no token here); it was enforced
    at mint, and the sub-five-minute expiry bounds the gap.

    The descriptor is the exact shape `v0_shares.resolve_secret` and
    `public_reads` produce, so the shared renderer and byte path cannot tell
    the surfaces apart — plus the session ``binding`` the shell echoes back
    to the embedding console.
    """
    row = await c.fetchrow(
        "SELECT s.id, s.drive_id, s.artifact_id, s.version_id, "
        "       s.workspace_id, s.principal_type, s.principal_id, "
        "       s.principal_workspace_role "
        "FROM viewer_sessions s "
        "WHERE s.credential_hash = $1 "
        "AND s.expires_at > clock_timestamp() "
        "AND EXISTS ("
        "  SELECT 1 FROM drives WHERE id = s.drive_id AND deleted_at IS NULL"
        ")",
        hash_key(credential),
    )
    if row is None:
        return None

    version = await c.fetchrow(
        "SELECT v.storage_object, v.storage_bucket, v.storage_generation, "
        "       v.size_bytes, v.content_type, v.id, "
        "       a.name, a.id AS artifact_id, a.updated_at "
        "FROM artifact_versions v "
        "JOIN artifacts a ON a.id = v.artifact_id "
        "WHERE v.id = $1 AND a.id = $2 AND a.drive_id = $3 "
        "AND a.deleted_at IS NULL",
        row["version_id"], row["artifact_id"], row["drive_id"],
    )
    if version is None:
        return None

    principal = _StoredPrincipal(
        subject_type=row["principal_type"],
        subject=row["principal_id"],
        workspace_id=row["workspace_id"],
        workspace_role=row["principal_workspace_role"],
    )
    try:
        authorized = await authz.has_role(
            c,
            actor=principal,
            drive_id=row["drive_id"],
            resource_type="artifact",
            resource_id=row["artifact_id"],
            minimum="viewer",
            # The re-check is strictly about the MINTING PRINCIPAL's own
            # standing. Counting a `public` grant here would mean a
            # revoked principal keeps resolving merely because the
            # artifact happens to be published — which is not what
            # "revocation takes effect within one fetch" says. A public
            # artifact is still readable, through the public permalink
            # that publishing created.
            include_public=False,
        )
    except DriveNotFoundError:
        return None
    if not authorized:
        return None

    coordinates = read_coordinates(version)
    if coordinates is None:
        return None
    return {
        "kind": "artifact",
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
        "binding": {
            "drive_id": row["drive_id"],
            "artifact_id": row["artifact_id"],
            "version_id": row["version_id"],
        },
    }


async def sweep_expired(c: Any, *, limit: int | None = None) -> int:
    """Delete viewer-session rows expired for over an hour. Returns the count.

    Called as an opportunistic bounded sweep at mint (rows are minutes-lived,
    so mint traffic keeps the table trim). B8, not an already-existing daily
    job, owns operational sweep scheduling.
    The one-hour grace means a just-expired credential still resolves to the
    uniform refusal rather than to a vanished row — same observable, kept
    anyway so DELETE never races a concurrent resolver's read window.

    `limit` bounds the rows one call may lock. The mint path always passes
    one: it sweeps inside its own transaction, where an unbounded DELETE
    would hold locks across an arbitrarily large backlog and let two
    concurrent mints deadlock on them. An operational sweeper may omit it.
    """
    if limit is not None:
        result = await c.execute(
            "DELETE FROM viewer_sessions WHERE ctid IN ("
            "  SELECT ctid FROM viewer_sessions "
            "  WHERE expires_at < now() - interval '1 hour' "
            "  LIMIT $1"
            ")",
            limit,
        )
    else:
        result = await c.execute(
            "DELETE FROM viewer_sessions "
            "WHERE expires_at < now() - interval '1 hour'"
        )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):  # pragma: no cover - asyncpg contract
        return 0


class _StoredPrincipal:
    """The minting principal, reconstructed for grant re-resolution.

    Wears the actor shape `v0_authz` expects (`subject_type`, `subject`,
    `workspace_id`, and the `is_workspace_admin` the overlay reads) and
    nothing else — in particular no token scopes, because there is no token
    at resolution time.

    `workspace_role` is the SNAPSHOT recorded at mint. Like the token scope
    the docstring above declines to re-check, it is honored for the
    session's sub-five-minute lifetime rather than re-verified — AgentDrive
    has no live view of Hub workspace roles, and the expiry bounds the gap
    for a demoted admin exactly as it does for a narrowed token.
    """

    __slots__ = ("subject_type", "subject", "workspace_id", "workspace_role")

    def __init__(
        self,
        *,
        subject_type: str,
        subject: str,
        workspace_id: str,
        workspace_role: str | None = None,
    ):
        self.subject_type = subject_type
        self.subject = subject
        self.workspace_id = workspace_id
        self.workspace_role = workspace_role

    @property
    def is_workspace_admin(self) -> bool:
        """Mint-time owner/admin standing, for the workspace-admin overlay."""
        return (
            self.subject_type == "user"
            and self.workspace_role in WORKSPACE_ADMIN_ROLES
        )
