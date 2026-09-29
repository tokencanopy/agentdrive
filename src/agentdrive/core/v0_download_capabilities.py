"""Download-capability target resolution (B3 packet 4, §5.7).

Governing contract: TokenCanopy
``docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md``.

One job: turn an authenticated mint request's strict target union into the
exact persisted object coordinates of ONE committed immutable version —
reauthorizing on every call and collapsing every miss into the same
anti-enumerating not-found. The wire surface signs the result; nothing here
touches the provider, mints a URL, or writes any state.

Anti-enumeration (§4/§8): a wrong-workspace drive, unknown/foreign-drive
artifact, unknown version, cross-artifact version, deleted artifact, and a
caller without local viewer capability all raise the same
``DownloadTargetNotFoundError`` — existence is never distinguishable.

Fail closed (§7): a resolved version whose object coordinates are not the
complete persisted (bucket, object, generation) triple raises
``DownloadCoordinatesUnavailableError`` (503 at the wire). The readiness
gate makes this unreachable in a ready deployment; this is the second,
row-level fence under it, and it never fabricates a generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import v0_authz as authz

# The signed response-content-type for every minted download (§5.7): the
# configured safe attachment type. A constant, not per-version metadata —
# forcing octet-stream is the download content-safety control, so the
# version's own (caller-declared) media type must not steer the signed
# response headers.
SAFE_ATTACHMENT_MEDIA_TYPE = "application/octet-stream"


class DownloadTargetNotFoundError(LookupError):
    """The uniform anti-enumerating miss (404 NOT_FOUND at the wire)."""


class DownloadCoordinatesUnavailableError(Exception):
    """The resolved version cannot be signed: its persisted object
    coordinates are incomplete. Deliberately coordinate-free — carries no
    bucket, object key, or generation (§8 redaction)."""


@dataclass(frozen=True)
class DownloadSource:
    """The exact persisted coordinates the mint signs — never caller input."""

    artifact_id: str
    version_id: str
    bucket: str
    object_name: str
    generation: int
    filename: str
    size_bytes: int


async def resolve_download_source(
    c: Any,
    *,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    version_id: str | None,
) -> DownloadSource:
    """Reauthorize and resolve one mint target to signed-object coordinates.

    ``version_id=None`` is the artifact target: the CURRENT immutable head
    at mint time. A supplied ``version_id`` must belong to the supplied
    artifact (and therefore drive) — a cross-artifact version is the same
    uniform miss as an unknown one.
    """
    workspace = await c.fetchval(
        "SELECT workspace_id FROM drives WHERE id = $1 AND deleted_at IS NULL",
        drive_id,
    )
    if workspace is None or workspace != actor.workspace_id:
        raise DownloadTargetNotFoundError()

    artifact = await c.fetchrow(
        "SELECT id, name, head_version_id FROM artifacts "
        "WHERE id = $1 AND drive_id = $2 AND deleted_at IS NULL",
        artifact_id, drive_id,
    )
    if artifact is None:
        raise DownloadTargetNotFoundError()

    try:
        await authz.require(
            c, actor=actor, drive_id=drive_id,
            resource_type="artifact", resource_id=artifact_id,
            minimum="viewer",
        )
    except authz.NotAuthorizedError:
        raise DownloadTargetNotFoundError() from None

    target_version_id = version_id or artifact["head_version_id"]
    if target_version_id is None:
        raise DownloadTargetNotFoundError()

    version = await c.fetchrow(
        "SELECT id, storage_object, storage_bucket, storage_generation, size_bytes "
        "FROM artifact_versions WHERE id = $1 AND artifact_id = $2",
        target_version_id, artifact_id,
    )
    if version is None:
        raise DownloadTargetNotFoundError()

    bucket = version["storage_bucket"]
    generation = version["storage_generation"]
    object_name = version["storage_object"]
    if not bucket or not object_name or generation is None or generation <= 0:
        raise DownloadCoordinatesUnavailableError()

    return DownloadSource(
        artifact_id=artifact_id,
        version_id=version["id"],
        bucket=bucket,
        object_name=object_name,
        generation=generation,
        filename=artifact["name"],
        size_bytes=version["size_bytes"],
    )
