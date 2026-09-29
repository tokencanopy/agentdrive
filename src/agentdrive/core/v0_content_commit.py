"""The one immutable-version commit and logical-byte accounting seam (B3).

Governing contract: TokenCanopy
``docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md``
§4/§7/§9. The committed quota unit is **logical version bytes**: every
committed version row counts its content size, even when several rows share
one physical object. ``drives.storage_bytes`` is the authoritative per-drive
committed counter and ``workspace_storage`` carries the additive workspace
committed/reserved totals.

Every version producer — inline artifact create, inline version append,
artifact copy, each artifact materialized by folder copy, version restore,
and (from packet 3) direct-upload completion — crosses this seam:

    reserve_version_bytes(...)      before the first object write
    commit_immutable_version(...)   in the producer's own transaction
    release_version_reservation(...) on every non-commit terminal path

``INSERT INTO artifact_versions`` exists ONLY in this module; a structural
test (`tests/test_v0_logical_accounting.py`) enforces that. Artifact-head
rotation and change-feed events stay with the producers — they are namespace
concerns with per-operation shapes — but they run in the same caller-owned
transaction as the commit, so accounting, version row, head, and feed commit
or roll back together.

Ceiling enforcement has a compatibility phase (§9): accounting is always
exact, but the hard logical ceilings apply only when
``direct_transfer_enabled`` is true — and an enabled-but-partial numeric
policy fails at boot (`config.Settings`), never silently weakens here.

Layer rule: never import ``agentdrive.api``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ids import new_id
from .timestamps import to_rfc3339

# The full version-row column list, including the B3 object coordinates.
# Producers that SELECT version rows for propagation import this so the
# column set cannot drift from the INSERT below.
VERSION_COLUMNS = (
    "id, artifact_id, parent_version_id, checksum, content_type, size_bytes, "
    "storage_object, storage_bucket, storage_generation, actor_type, "
    "actor_id, ordinal, created_at, origin_session_id, origin_message"
)


class QuotaExceededError(ValueError):
    """Promised plus committed logical bytes would exceed a configured hard
    ceiling. Only raised while ``direct_transfer_enabled`` is true."""

    def __init__(
        self,
        *,
        scope: str,
        used: int,
        reserved: int,
        limit: int,
        requested: int,
    ) -> None:
        super().__init__("logical storage limit exceeded")
        self.scope = scope
        self.used = int(used)
        self.reserved = int(reserved)
        self.limit = int(limit)
        self.requested = int(requested)


class AccountingError(RuntimeError):
    """The accounting invariant would break: unknown drive, a reservation
    consumed twice, or a commit whose size disagrees with its reservation.
    Never a user error — this is the double-count/double-release signature."""


@dataclass(frozen=True)
class ImmutableVersionCommit:
    """One verified, ready-to-commit immutable version.

    ``checksum`` is algorithm-qualified (``sha256:<hex>`` for inline CAS
    content, ``crc32c:<canonical-base64>`` for adopted direct content).
    ``storage_bucket``/``storage_generation`` are the exact observed object
    coordinates when known; NULL rows are resolved later by the guarded
    ``reconcile_generations`` job. ``reservation_id`` is required: every
    producer reserves before it commits.
    """

    drive_id: str
    workspace_id: str
    artifact_id: str
    version_id: str
    parent_version_id: str | None
    ordinal: int
    checksum: str
    content_type: str
    size_bytes: int
    storage_object: str
    storage_bucket: str | None
    storage_generation: int | None
    actor_type: str
    actor_id: str | None
    reservation_id: str
    # Provenance (migration 0053). Optional because most producers have none
    # to give: a plain upload came from nowhere in particular. A session-born
    # version sets both so the fact survives the session's own GC.
    origin_session_id: str | None = None
    origin_message: str | None = None


async def store_cas_object(
    c: Any, *, object_name: str, data: bytes, content_type: str
):
    """Land CAS content create-only and return its stable coordinates.

    Wraps `storage.put` (exclusive create, adopt-on-412) with the
    reference-aware refresh rule (adversarial review I-1): adopting an AGED
    existing object that NO version row references gets a conditional
    same-content generation refresh, so a mark-sweep whose listing predates
    the adopting commit can never delete it with a matching generation pin
    (and the new generation is young for the age gate). A REFERENCED object
    is never refreshed — existing rows persist its exact generation, and
    its name is in every sweep's live set. Lives on the seam because the
    decision needs the version table; every CAS producer must land bytes
    through here."""
    from .. import storage

    # Serialize CAS adopters of ONE object name (advisory xact lock, held to
    # the caller's commit): without it, a writer adopting the object just
    # UNDER the age threshold could still be uncommitted while a second
    # writer just OVER it sees "unreferenced" and refreshes the generation
    # beneath the first writer's row (review round-4 M-2 boundary straddle).
    await c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('cas_object:' || $1, 0))",
        object_name,
    )
    write = await storage.put(object_name, data, content_type)
    if (
        write.adopted_existing
        and write.generation is not None
        and write.adopted_age_seconds is not None
        and write.adopted_age_seconds >= storage.CAS_REFRESH_AGE.total_seconds()
    ):
        referenced = await c.fetchval(
            "SELECT EXISTS (SELECT 1 FROM artifact_versions "
            "WHERE storage_object = $1)",
            object_name,
        )
        if not referenced:
            write = await storage.refresh_object_generation(
                object_name, data, content_type,
                if_generation_match=write.generation,
            )
    return write


async def _upsert_workspace_row(c: Any, workspace_id: str) -> None:
    await c.execute(
        "INSERT INTO workspace_storage (workspace_id) VALUES ($1) "
        "ON CONFLICT (workspace_id) DO NOTHING",
        workspace_id,
    )


async def reserve_version_bytes(
    c: Any,
    *,
    workspace_id: str,
    drive_id: str,
    principal_id: str,
    upload_id: str | None,
    size_bytes: int,
    workspace_limit_bytes: int | None = None,
    drive_limit_bytes: int | None = None,
) -> str:
    """Atomically reserve one would-be version's logical bytes.

    Lock discipline (reviewed): the shipped-behavior path (ceilings off)
    NEVER writes or locks `workspace_storage` — an inline producer's
    reservation row lives and dies inside its own transaction, so the shared
    workspace row is not held across the GCS object write. The workspace row
    moves only (a) here for direct-upload sessions (whose begin transaction
    is short and commits before any provider call) and (b) at commit time,
    after the object write.

    While ``direct_transfer_enabled`` is true (unreachable in packet 1 —
    `config.Settings` refuses enablement until the full §9 surface lands;
    tests patch the runtime attribute), the workspace row is locked FOR
    UPDATE to serialize the ceiling check: ``committed + live reservations
    + size`` against the configured hard ceilings, where the live-reservation
    sum sees this transaction's own earlier reservations (folder copy) and
    the lock ordering is always workspace → drive.
    """
    if size_bytes < 0:
        raise AccountingError("a reservation cannot promise negative bytes")

    enforce = workspace_limit_bytes is not None or drive_limit_bytes is not None
    if enforce:
        ws_ceiling = workspace_limit_bytes
        drive_ceiling = drive_limit_bytes
        if ws_ceiling is None or drive_ceiling is None:
            # Boot validation makes this unreachable; if reached, fail closed.
            raise AccountingError(
                "storage reservation received an incomplete ceiling policy"
            )
        await _upsert_workspace_row(c, workspace_id)
        ws = await c.fetchrow(
            "SELECT committed_bytes FROM workspace_storage "
            "WHERE workspace_id = $1 FOR UPDATE",
            workspace_id,
        )
        drive = await c.fetchrow(
            "SELECT storage_bytes, workspace_id FROM drives "
            "WHERE id = $1 FOR UPDATE",
            drive_id,
        )
        if drive is None:
            raise AccountingError(f"reservation against unknown drive {drive_id}")
        if drive["workspace_id"] != workspace_id:
            # Ownership is derived from the drive row, never trusted from a
            # duplicated caller input (packet-1 correction).
            raise AccountingError(
                f"drive {drive_id} does not belong to the named workspace"
            )
        ws_reserved = await c.fetchval(
            "SELECT COALESCE(sum(size_bytes), 0) FROM storage_reservations "
            "WHERE workspace_id = $1 AND released_at IS NULL",
            workspace_id,
        )
        if ws["committed_bytes"] + ws_reserved + size_bytes > ws_ceiling:
            raise QuotaExceededError(
                scope="workspace",
                used=ws["committed_bytes"],
                reserved=ws_reserved,
                limit=ws_ceiling,
                requested=size_bytes,
            )
        drive_reserved = await c.fetchval(
            "SELECT COALESCE(sum(size_bytes), 0) FROM storage_reservations "
            "WHERE drive_id = $1 AND released_at IS NULL",
            drive_id,
        )
        if drive["storage_bytes"] + drive_reserved + size_bytes > drive_ceiling:
            raise QuotaExceededError(
                scope="drive",
                used=drive["storage_bytes"],
                reserved=drive_reserved,
                limit=drive_ceiling,
                requested=size_bytes,
            )
    else:
        owner = await c.fetchval(
            "SELECT workspace_id FROM drives WHERE id = $1", drive_id
        )
        if owner is None:
            raise AccountingError(f"reservation against unknown drive {drive_id}")
        if owner != workspace_id:
            # Ownership is derived from the drive row, never trusted from a
            # duplicated caller input (packet-1 correction).
            raise AccountingError(
                f"drive {drive_id} does not belong to the named workspace"
            )

    reservation_id = new_id("rsv")
    await c.execute(
        "INSERT INTO storage_reservations "
        "(id, workspace_id, drive_id, principal_id, upload_id, size_bytes) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        reservation_id, workspace_id, drive_id, principal_id, upload_id,
        size_bytes,
    )
    if upload_id is not None:
        # Sessions materialize the workspace reserved gauge (their
        # reservations outlive this transaction); inline reservations do not
        # — their reserved phase is never observable outside their own
        # transaction, so the gauge would net to zero anyway.
        await _upsert_workspace_row(c, workspace_id)
        await c.execute(
            "UPDATE workspace_storage SET reserved_bytes = reserved_bytes + $2, "
            "updated_at = now() WHERE workspace_id = $1",
            workspace_id, size_bytes,
        )
    return reservation_id


async def release_version_reservation(
    c: Any,
    *,
    reservation_id: str | None = None,
    upload_id: str | None = None,
) -> bool:
    """Release a live reservation exactly once.

    Every non-commit terminal path — rejection, cancellation, expiry, crash
    recovery, GC — runs this same conditional ``released_at IS NULL`` update;
    only one caller succeeds. Returns True when THIS call performed the
    release, False when the reservation was already released/converted (or
    does not exist) — a detected no-op, never a second decrement.
    """
    if (reservation_id is None) == (upload_id is None):
        raise AccountingError("release needs exactly one of reservation/upload id")
    async with c.transaction():
        return await _release_inner(c, reservation_id=reservation_id,
                                    upload_id=upload_id)


async def _release_inner(
    c: Any, *, reservation_id: str | None, upload_id: str | None
) -> bool:
    if reservation_id is not None:
        row = await c.fetchrow(
            "UPDATE storage_reservations "
            "SET released_at = now(), release_kind = 'released' "
            "WHERE id = $1 AND released_at IS NULL "
            "RETURNING workspace_id, size_bytes, upload_id",
            reservation_id,
        )
    else:
        row = await c.fetchrow(
            "UPDATE storage_reservations "
            "SET released_at = now(), release_kind = 'released' "
            "WHERE upload_id = $1 AND released_at IS NULL "
            "RETURNING workspace_id, size_bytes, upload_id",
            upload_id,
        )
    if row is None:
        return False
    if row["upload_id"] is not None:
        # Only session reservations materialized the workspace gauge.
        await c.execute(
            "UPDATE workspace_storage SET reserved_bytes = reserved_bytes - $2, "
            "updated_at = now() WHERE workspace_id = $1",
            row["workspace_id"], row["size_bytes"],
        )
    return True


async def commit_immutable_version(c: Any, request: ImmutableVersionCommit) -> dict[str, Any]:
    """Insert ONE immutable version row and convert its reservation to
    committed usage, atomically in the caller's transaction.

    The conditional conversion is the exactly-once guard: a reservation that
    was already converted or released raises ``AccountingError`` rather than
    double-counting. The committed counters and the version row commit or
    roll back together — accounting can never exist without the version or
    vice versa.
    """
    if (request.storage_bucket is None) != (request.storage_generation is None):
        raise AccountingError(
            "storage_bucket and storage_generation are all-or-none"
        )
    if request.storage_bucket is not None and not request.storage_bucket:
        raise AccountingError("storage_bucket cannot be empty")

    converted = await c.fetchrow(
        "UPDATE storage_reservations "
        "SET released_at = now(), release_kind = 'converted' "
        "WHERE id = $1 AND released_at IS NULL "
        "RETURNING workspace_id, drive_id, size_bytes, upload_id",
        request.reservation_id,
    )
    if converted is None:
        raise AccountingError(
            f"reservation {request.reservation_id} was already consumed"
        )
    if converted["size_bytes"] != request.size_bytes:
        raise AccountingError(
            f"reserved {converted['size_bytes']} bytes but committing "
            f"{request.size_bytes}"
        )
    if (
        converted["drive_id"] != request.drive_id
        or converted["workspace_id"] != request.workspace_id
    ):
        raise AccountingError("reservation belongs to a different drive/workspace")

    row = await c.fetchrow(
        "INSERT INTO artifact_versions "
        "(id, artifact_id, parent_version_id, checksum, content_type, "
        " size_bytes, storage_object, storage_bucket, storage_generation, "
        " actor_type, actor_id, ordinal, origin_session_id, origin_message) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) "
        "RETURNING " + VERSION_COLUMNS,
        request.version_id, request.artifact_id, request.parent_version_id,
        request.checksum, request.content_type, request.size_bytes,
        request.storage_object, request.storage_bucket,
        request.storage_generation, request.actor_type, request.actor_id,
        request.ordinal, request.origin_session_id, request.origin_message,
    )
    # Lock order is workspace → drive everywhere (reserve's enabled path,
    # this commit tail, and GC's purge), so a producer and a concurrent GC
    # purge in the same workspace cannot deadlock. The `reserved_bytes`
    # gauge moves only for session reservations (see reserve/release).
    await _upsert_workspace_row(c, request.workspace_id)
    if converted["upload_id"] is not None:
        await c.execute(
            "UPDATE workspace_storage "
            "SET committed_bytes = committed_bytes + $2, "
            "    reserved_bytes = reserved_bytes - $2, updated_at = now() "
            "WHERE workspace_id = $1",
            request.workspace_id, request.size_bytes,
        )
    else:
        await c.execute(
            "UPDATE workspace_storage "
            "SET committed_bytes = committed_bytes + $2, updated_at = now() "
            "WHERE workspace_id = $1",
            request.workspace_id, request.size_bytes,
        )
    await c.execute(
        "UPDATE drives SET storage_bytes = storage_bytes + $2 WHERE id = $1",
        request.drive_id, request.size_bytes,
    )
    return version_payload(row)


def version_payload(row: Any) -> dict[str, Any]:
    """The wire shape of one immutable version row. Storage coordinates are
    deliberately NOT part of the payload — they never cross the API."""
    return {
        "id": row["id"],
        "artifact_id": row["artifact_id"],
        "version_number": row["ordinal"],
        "parent_version_id": row["parent_version_id"],
        "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
        "hash": row["checksum"],
        "created_by": row["actor_id"],
        "created_at": to_rfc3339(row["created_at"]),
        # Provenance (migration 0053). Both null for a version that came
        # from a plain upload. `origin_session_id` is opaque and may name a
        # session that has since been swept — the message is what survives.
        "origin_session_id": _column(row, "origin_session_id"),
        "origin_message": _column(row, "origin_message"),
    }


def _column(row: Any, name: str) -> Any:
    """Tolerate a row selected before migration 0053 added the column.

    Version rows are read through several SELECTs, not all of which use
    `VERSION_COLUMNS`; a missing key here would turn a provenance decoration
    into a KeyError on the artifact page."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


async def storage_bytes_parity(c: Any, drive_id: str) -> tuple[int, int]:
    """(locked counter, live sum) for one drive — the parity assertion the
    accounting design requires. Equal in a healthy database; the GC job
    checks and reports divergence without 'fixing' it silently."""
    counter = await c.fetchval(
        "SELECT storage_bytes FROM drives WHERE id = $1", drive_id
    )
    live_sum = await c.fetchval(
        "SELECT COALESCE(sum(v.size_bytes), 0) FROM artifact_versions v "
        "JOIN artifacts a ON a.id = v.artifact_id WHERE a.drive_id = $1",
        drive_id,
    )
    return int(counter or 0), int(live_sum or 0)
