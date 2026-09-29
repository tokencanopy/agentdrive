"""Artifact + version verticals (slice 6): artifact CRUD, lifecycle, copy,
and the immutable version trail over the day-0 parent/name namespace.

Artifacts hang off a live folder (``parent_id`` NOT NULL) with one name
segment. Sibling artifact and folder names share ONE collision domain (§6.2):
the cross-kind trigger + partial unique index hold it, surfaced as
``asyncpg.UniqueViolationError`` translated to ``409 ARTIFACT_PATH_CONFLICT``
at the boundary.

**Content lives on the version, identity lives on the artifact.** Day-0
``artifacts`` denormalizes ``content_type`` / ``content_preview`` / ``labels``
from the head version for listing; the authoritative bytes + size + checksum
live on ``artifact_versions``. The artifact ``head_version_id`` points at the
latest immutable version; appends and restores rotate it.

**Versions are immutable** (enforced by the ``artifact_versions_immutable``
trigger). History is never rewound: ``restore_version`` appends a NEW head
version whose ``parent_version_id`` is the current head and whose bytes
reference the historical CAS object (no byte copy). ``ordinal`` is computed
server-side as ``max(ordinal)+1`` (day-0 has no auto-ordinal/head trigger).

**Copy.** Copy is same-drive-only in v0 (cross-drive copy is rejected at the
route layer — the data-transfer path is out of scope). Materializes the
artifact + its selected version synchronously in one transaction, reusing
the source version's CAS object (no byte copy).

Scope/authz note: like the folders vertical, this slice is token-scope only
(``content:read`` / ``content:write``); local grant administration lands in
the grants slice (Task 7). Workspace scoping is enforced here: a drive in
another workspace reads as absent (``DriveNotFoundError``), so existence is
not disclosed.

Layer rule: never import ``agentdrive.api``. Precondition failures and
cursor payloads are handed back as exceptions / plain data and translated at
the route layer. ``If-Match``/ETag and 404 helpers are shared with the
drives/folders verticals.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import asyncpg

from .. import storage
from . import v0_changes as changes
from . import v0_content_commit as content_commit
from .ids import new_id
from .timestamps import to_rfc3339
from .v0_content_commit import VERSION_COLUMNS as _VERSION_COLUMNS
from .v0_drives import (
    DriveNotFoundError,
    PreconditionError,
    _lock_drive_namespace,
    precondition,
)
from .v0_folders import (
    InvalidFolderNameError,
    _name_is_occupied,
)
from .v0_folders import (
    validate_name as _validate_name,
)
from .version_reads import read_coordinates

# Inline (multipart) content ceiling for create / version append. Larger
# content goes through the upload-session substrate (a later slice). 15 MiB is
# also comfortably under Cloud Run's 32 MiB HTTP/1 request cap, so the app cap
# is reachable in prod (the old 50 MiB ceiling could never be hit there).
MAX_BUFFERED_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MiB

_ARTIFACT_COLUMNS = (
    "id, drive_id, parent_id, name, content_type, content_preview, labels, "
    "metadata, head_version_id, revision, created_at, updated_at, deleted_at"
)
# The version column list (including the B3 storage_bucket/storage_generation
# coordinates) is owned by the commit seam so SELECT-for-propagation and the
# one INSERT can never drift apart.
_VERSION_COLUMNS_V = ", ".join(f"v.{c.strip()}" for c in _VERSION_COLUMNS.split(","))


class ArtifactNotFoundError(LookupError):
    """The artifact does not exist in the drive (or is soft-deleted).

    Always 404 ARTIFACT_NOT_FOUND — including the cross-workspace case (via
    the drive check), so existence is not disclosed.
    """


class ArtifactNameConflictError(ValueError):
    """A live sibling — artifact or folder — already owns the name.
    409 ARTIFACT_PATH_CONFLICT."""


class InvalidArtifactNameError(InvalidFolderNameError):
    """An artifact name failed segment validation. 400 INVALID_ARGUMENT."""


def validate_name(value: str) -> str:
    """Use the shared item-name contract with artifact-specific failures."""
    try:
        return _validate_name(value)
    except InvalidFolderNameError as exc:
        raise InvalidArtifactNameError(str(exc)) from None


class InvalidArtifactMoveError(ValueError):
    """An artifact move targets a missing/soft-deleted parent. 404."""


class ArtifactNotDeletedError(ValueError):
    """restore was called on an artifact that is not soft-deleted.
    409 CONFLICT."""


class ArtifactParentNotLiveError(ValueError):
    """restore's target parent folder is itself soft-deleted. 409 CONFLICT.

    Distinct from `InvalidArtifactMoveError` on purpose. A MOVE to a dead
    parent answers 404 as-if-absent, because the caller is naming a
    destination they may not be allowed to know about. A RESTORE names no
    destination — the parent is wherever the artifact already was — so 404
    would claim the artifact does not exist seconds after `?state=deleted`
    listed it. The folder equivalent has always answered 409; this makes the
    artifact case agree."""


class ArtifactTooLargeError(ValueError):
    """Inline content exceeds the buffered ceiling. 413 ARTIFACT_TOO_LARGE."""


class InvalidVersionError(ValueError):
    """A version reference is missing or not owned by the artifact.
    404 NOT_FOUND."""


class ContentEmptyError(ValueError):
    """A content-create/append carried no ``content`` part. 422 VALIDATION."""


class ChecksumMismatchError(ValueError):
    """A declared sha256 does not match the content bytes. 400 INVALID_ARGUMENT."""


def _decode_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _optional_timestamp(value: Any) -> str | None:
    return to_rfc3339(value) if value is not None else None


def artifact_payload(
    row: Any, *, effective_visibility: str = "private"
) -> dict[str, Any]:
    """The wire shape of one artifact row.

    ``effective_visibility`` is not a column — it is the server-computed
    exposure summary from ``v0_authz.artifact_visibility``. It defaults to
    ``private`` so the fail-safe answer is "nobody else can reach this":
    a caller that forgets to compute it under-reports exposure rather than
    inventing a share that does not exist.
    """
    deleted_at = row["deleted_at"]
    return {
        "effective_visibility": effective_visibility,
        "id": row["id"],
        "drive_id": row["drive_id"],
        "parent_id": row["parent_id"],
        "name": row["name"],
        "content_type": row["content_type"],
        "content_preview": row["content_preview"],
        "labels": list(row["labels"] or []),
        "metadata": _decode_json(row["metadata"], {}),
        "head_version_id": row["head_version_id"],
        "revision": row["revision"],
        "state": "deleted" if deleted_at else "active",
        "created_at": to_rfc3339(row["created_at"]),
        "updated_at": to_rfc3339(row["updated_at"]),
        "deleted_at": _optional_timestamp(deleted_at),
    }


async def _payload(c: Any, row: Any) -> dict[str, Any]:
    """``artifact_payload`` with the row's exposure summary resolved.

    Costs ONE extra statement per artifact-returning operation. Lists go
    through ``_payload_page`` instead, which resolves the whole page in a
    single statement — the field must never become a per-row lookup inside a
    loop. The drive comes off the row itself, so a cross-drive copy is
    classified in the drive the artifact actually landed in.
    """
    from . import v0_authz

    visibility = await v0_authz.artifact_visibility_one(
        c, row["drive_id"], row["id"]
    )
    return artifact_payload(row, effective_visibility=visibility)


async def _payload_page(c: Any, rows: list[Any]) -> list[dict[str, Any]]:
    """``artifact_payload`` for a whole drive-scoped page — one statement."""
    from . import v0_authz

    if not rows:
        return []
    visibility = await v0_authz.artifact_visibility(
        c, rows[0]["drive_id"], [r["id"] for r in rows]
    )
    return [
        artifact_payload(
            r, effective_visibility=visibility.get(r["id"], "private")
        )
        for r in rows
    ]


# The wire shape of one immutable version row is single-sourced from the
# commit seam so read projections can never drift from the commit projection.
version_payload = content_commit.version_payload


def validate_artifact_metadata(metadata: dict[str, Any]) -> None:
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object")


def _cas_object(drive_id: str, body: bytes) -> str:
    """Content-addressed GCS object name (sha256 hex under a per-drive dir)."""
    digest = hashlib.sha256(body).hexdigest()
    return f"cas/{drive_id}/{digest}"


def _matches_sha256(content: bytes, sha256: str) -> bool:
    value = sha256.strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:"):]
    return len(value) == 64 and hashlib.sha256(content).hexdigest() == value


# Search preview cap (§6.11): the first N bytes of text content, decoded
# lossily. Kept well under the search_tsv content-preview arm's 64 KiB cap so
# the generated index is bounded. Binary content yields an empty preview and
# matches only on name/metadata/labels.
_PREVIEW_CHARS = 16 * 1024


def _derive_preview(
    content: bytes, *, content_type: str | None = None, name: str = ""
) -> str | None:
    """A searchable text preview from content bytes, or None for binary.

    A spreadsheet is binary to a UTF-8 decode, so without the branch below
    every workbook is invisible to search — findable by filename and by
    nothing inside it. `content_type`/`name` are optional so the many callers
    that pass neither keep their existing behaviour exactly.
    """
    if content_type or name:
        from ..sheets.workbook import Unparseable, detect_format, extract_text

        try:
            detect_format(content_type or "", name)
        except Unparseable:
            pass
        else:
            return extract_text(
                content,
                content_type=content_type or "",
                name=name,
                limit=_PREVIEW_CHARS,
            )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # NUL is valid UTF-8 (U+0000) but ILLEGAL in a Postgres text value, so a
    # successful decode is NOT proof the result can be stored. Without this
    # strip the preview reached the driver's bind and took the WHOLE write down
    # with a 400 out-of-range — not merely an empty preview — and every body it
    # refused was an ordinary file: an MP4's box headers begin `00 00 00 xx`,
    # an empty ZIP is its 22-byte end-of-central-directory record plus NUL
    # padding, ASCII-range UTF-16 interleaves a NUL after every character.
    #
    # Dropped rather than treated as binary: the surrounding text is still
    # worth indexing, and the preview is only a search aid. The emptiness test
    # below therefore has to run AFTER the strip, or all-NUL content would
    # store an empty preview instead of none.
    text = text.replace("\x00", "")
    if not text.strip():
        return None
    return text[:_PREVIEW_CHARS]


def _trim_partial_utf8(prefix: bytes) -> bytes:
    """Trim a trailing partial UTF-8 code point from a truncated byte prefix.

    ``_derive_preview`` decodes strictly, so a preview window that cuts a
    multibyte character mid-sequence would otherwise read as binary (None).
    """
    try:
        prefix.decode("utf-8")
        return prefix
    except UnicodeDecodeError as exc:
        return prefix[: exc.start]


async def _preview_from_cas_object(
    object_name: str,
    *,
    bucket: str | None = None,
    generation: int | None = None,
) -> str | None:
    """A text preview from a version's bytes, derived exactly as the write
    path does (`_derive_preview`) but reading only the preview window — never
    the whole body, which can be far larger than the 16 KiB a preview needs.

    Takes coordinates for the same reason every other read does: a copy of a
    historical DIRECT-UPLOADED version reads from the transfer bucket, and
    assuming the artifact bucket here would 404 the fetch inside a mutation."""
    def _read_window() -> bytes:
        window = _PREVIEW_CHARS + 4  # 4 = longest UTF-8 code point
        buf = bytearray()
        for chunk in storage.stream(
            object_name, chunk_size=8 * 1024, bucket=bucket, generation=generation
        ):
            buf.extend(chunk)
            if len(buf) >= window:
                break
        return bytes(buf[:window])

    prefix = await asyncio.to_thread(_read_window)
    return _derive_preview(_trim_partial_utf8(prefix))


async def _ensure_drive(c: Any, actor: Any, drive_id: str) -> None:
    row = await c.fetchrow(
        "SELECT workspace_id FROM drives WHERE id = $1 AND deleted_at IS NULL",
        drive_id,
    )
    if row is None or row["workspace_id"] != actor.workspace_id:
        raise DriveNotFoundError(drive_id)


def _artifact_or_404(row: Any | None) -> Any:
    if row is None:
        raise ArtifactNotFoundError("artifact not found")
    return row


async def _live_artifact(
    c: Any, drive_id: str, artifact_id: str, *, for_update: bool
) -> Any | None:
    sql = (
        f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts "
        "WHERE drive_id = $1 AND id = $2 AND deleted_at IS NULL"
    )
    if for_update:
        sql += " FOR UPDATE"
    return await c.fetchrow(sql, drive_id, artifact_id)


async def _any_artifact(c: Any, drive_id: str, artifact_id: str) -> Any | None:
    """Include soft-deleted rows (used by restore and by deleted-only reads)."""
    return await c.fetchrow(
        f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id,
    )


async def get_artifact(c: Any, actor: Any, drive_id: str, artifact_id: str) -> dict[str, Any]:
    """Read one active artifact (404 for missing / soft-deleted, or a drive
    outside the actor's workspace)."""
    await _ensure_drive(c, actor, drive_id)
    row = await _live_artifact(c, drive_id, artifact_id, for_update=False)
    return await _payload(c, _artifact_or_404(row))


async def list_artifacts(
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
    content_type: str | None = None,
    label: str | None = None,
    updated_after: Any = None,
    updated_before: Any = None,
) -> dict[str, Any]:
    """Active/deleted/all listing, newest-first with a stable (created_at, id)
    keyset anchor; exact-match filters for ``parent_id`` / ``name`` /
    ``content_type``, label membership, and inclusive ``updated_*`` bounds.
    Returns ``{"items", "next_cursor"}`` for the route to encode.

    Rows are filtered to those the actor can see (grant visibility, §8):
    drive-level grants expose the whole drive; a folder grant exposes that
    folder's whole subtree; a direct artifact grant exposes that artifact;
    a workspace owner/admin sees every row (the workspace-admin overlay —
    `_ensure_drive` above has already pinned the drive to the actor's own
    workspace)."""
    await _ensure_drive(c, actor, drive_id)
    from . import v0_authz

    # actor params are appended AFTER the drive id ($1): subject_type, subject,
    # workspace_id, then the workspace-admin overlay flag.
    principal_type_param = len([drive_id]) + 1
    principal_id_param = principal_type_param + 1
    workspace_param = principal_id_param + 1
    overlay_param = workspace_param + 1
    params: list[Any] = [drive_id]
    sql = f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts AS art"
    sql += v0_authz.visibility_lateral(
        "art",
        start_parent_expr="art.parent_id",
        include_direct_artifact=True,
        principal_type_param=principal_type_param,
        principal_id_param=principal_id_param,
        workspace_param=workspace_param,
        overlay_param=overlay_param,
    )
    sql += " WHERE art.drive_id = $1"
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
    if content_type is not None:
        params.append(content_type)
        sql += " AND content_type = $" + str(len(params)) + " COLLATE \"C\""
    if label is not None:
        params.append(label)
        sql += " AND labels @> ARRAY[$" + str(len(params)) + "]::text[]"
    if updated_after is not None:
        params.append(updated_after)
        sql += " AND updated_at >= $" + str(len(params))
    if updated_before is not None:
        params.append(updated_before)
        sql += " AND updated_at <= $" + str(len(params))
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
    return {"items": await _payload_page(c, rows), "next_cursor": next_cursor}


async def _resolve_parent(c: Any, drive_id: str, parent_id: str) -> Any:
    return await c.fetchrow(
        "SELECT id FROM folders WHERE drive_id = $1 AND id = $2 AND deleted_at IS NULL",
        drive_id, parent_id,
    )


async def create_artifact(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    parent_id: str,
    name: str,
    metadata: dict[str, Any],
    content: bytes,
    content_type: str,
    sha256: str | None,
    labels: list[str] | None = None,
    content_preview: str | None = None,
) -> dict[str, Any]:
    """Create one artifact + its first immutable version in one transaction.
    ``content`` is the multipart body. Must run inside a transaction."""
    name = validate_name(name)
    validate_artifact_metadata(metadata)
    if not content:
        raise ContentEmptyError("create requires a non-empty content part")
    if sha256 is not None and not _matches_sha256(content, sha256):
        raise ChecksumMismatchError("declared sha256 does not match the content")
    if len(content) > MAX_BUFFERED_UPLOAD_BYTES:
        raise ArtifactTooLargeError("inline content exceeds the buffered ceiling")

    await _lock_drive_namespace(c, drive_id)
    await _ensure_drive(c, actor, drive_id)
    if await _resolve_parent(c, drive_id, parent_id) is None:
        raise InvalidArtifactMoveError("no such live folder")
    if await _name_is_occupied(c, drive_id, parent_id, name):
        raise ArtifactNameConflictError(
            f"a sibling under {parent_id} already uses the name {name!r}"
        )

    artifact_id = new_id("art")
    version_id = new_id("ver")
    revision = new_id("rev")
    preview = (
        content_preview
        if content_preview is not None
        else _derive_preview(content, content_type=content_type, name=name)
    )
    try:
        row = await c.fetchrow(
            f"INSERT INTO artifacts "
            f"(id, drive_id, parent_id, name, content_type, content_preview, "
            f"metadata, labels, revision) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text[], $9) "
            f"RETURNING {_ARTIFACT_COLUMNS}",
            artifact_id, drive_id, parent_id, name, content_type,
            preview, json.dumps(metadata), labels or [], revision,
        )
    except asyncpg.UniqueViolationError:
        raise ArtifactNameConflictError(
            f"a sibling under {parent_id} already uses the name {name!r}"
        ) from None

    await _insert_version(
        c,
        actor=actor,
        artifact_id=artifact_id,
        version_id=version_id,
        parent_version_id=None,
        ordinal=1,
        content=content,
        content_type=content_type,
        drive_id=drive_id,
    )
    await c.execute(
        "UPDATE artifacts SET head_version_id = $2 WHERE id = $1",
        artifact_id, version_id,
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.created", resource_type="artifact", resource_id=artifact_id,
        revision=revision,
        data={"name": name},
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.version.created", resource_type="artifact", resource_id=artifact_id,
        revision=revision, data={"name": name, "version_id": version_id},
    )
    row = await c.fetchrow(
        f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id,
    )
    return await _payload(c, row)


async def update_artifact(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    name: str | None,
    parent_id: str | None,
    metadata: dict[str, Any] | None,
    labels: list[str] | None,
    changed: frozenset[str],
    if_match: str | None,
) -> dict[str, Any]:
    """Rename / move / update an artifact's metadata or labels in one
    mutation. Must run inside a transaction."""
    await _lock_drive_namespace(c, drive_id)
    await _ensure_drive(c, actor, drive_id)
    artifact = await _live_artifact(c, drive_id, artifact_id, for_update=True)
    _artifact_or_404(artifact)
    precondition(if_match, artifact["revision"])

    destination_parent_id = parent_id if parent_id is not None else artifact["parent_id"]
    destination_name = validate_name(name) if name is not None else artifact["name"]
    if await _resolve_parent(c, drive_id, destination_parent_id) is None:
        raise InvalidArtifactMoveError("no such live folder")

    moving = (
        destination_parent_id != artifact["parent_id"]
        or destination_name != artifact["name"]
    )
    if moving and await _name_is_occupied(
        c, drive_id, destination_parent_id, destination_name
    ):
        raise ArtifactNameConflictError("a live sibling already uses the destination name")

    current_metadata = _decode_json(artifact["metadata"], {})
    target_metadata = metadata if metadata is not None else current_metadata
    target_labels = labels if labels is not None else list(artifact["labels"] or [])
    current_labels = list(artifact["labels"] or [])
    actual_change = (
        moving
        or target_metadata != current_metadata
        or target_labels != current_labels
    )
    if not actual_change:
        return await _payload(c, artifact)

    revision = new_id("rev")
    sets = [
        "parent_id = $3",
        "name = $4",
        "metadata = $5::jsonb",
        "labels = $6::text[]",
        "revision = $7",
        "updated_at = now()",
    ]
    await c.execute(
        "UPDATE artifacts SET " + ", ".join(sets) + " WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id, destination_parent_id, destination_name,
        json.dumps(target_metadata), target_labels, revision,
    )
    data: dict[str, Any] = {"name": destination_name}
    if destination_name != artifact["name"]:
        data["name_before"] = artifact["name"]
    if destination_parent_id != artifact["parent_id"]:
        data["previous_parent_id"] = artifact["parent_id"]
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.updated", resource_type="artifact", resource_id=artifact_id,
        previous_revision=artifact["revision"], revision=revision,
        data=data,
    )
    row = await c.fetchrow(
        f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id,
    )
    return await _payload(c, row)


async def soft_delete_artifact(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    if_match: str | None,
) -> dict[str, Any]:
    """Soft-delete one artifact (its versions stay, hidden behind the flag).
    Must run inside a transaction."""
    await _ensure_drive(c, actor, drive_id)
    artifact = await _live_artifact(c, drive_id, artifact_id, for_update=True)
    _artifact_or_404(artifact)
    precondition(if_match, artifact["revision"])

    revision = new_id("rev")
    await c.execute(
        "UPDATE artifacts SET deleted_at = now(), deleted_cohort_id = $4, "
        "revision = $3, updated_at = now() "
        "WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id, revision, new_id("cset"),
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.deleted", resource_type="artifact", resource_id=artifact_id,
        previous_revision=artifact["revision"], revision=revision,
        data={"name": artifact["name"]},
    )
    row = await c.fetchrow(
        f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id,
    )
    return await _payload(c, row)


async def restore_artifact(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    if_match: str | None,
) -> dict[str, Any]:
    """Atomically restore a soft-deleted artifact. Must run inside a
    transaction."""
    await _lock_drive_namespace(c, drive_id)
    await _ensure_drive(c, actor, drive_id)
    artifact = await _any_artifact(c, drive_id, artifact_id)
    _artifact_or_404(artifact)
    if artifact["deleted_at"] is None:
        raise ArtifactNotDeletedError("the artifact is not soft-deleted")
    precondition(if_match, artifact["revision"])

    if await _resolve_parent(c, drive_id, artifact["parent_id"]) is None:
        raise ArtifactParentNotLiveError(
            "the artifact's parent folder is no longer live; restore it first"
        )
    if await _name_is_occupied(c, drive_id, artifact["parent_id"], artifact["name"]):
        raise ArtifactNameConflictError("a live sibling now owns the artifact's name")

    revision = new_id("rev")
    try:
        await c.execute(
            "UPDATE artifacts SET deleted_at = NULL, deleted_cohort_id = NULL, "
            "revision = $3, updated_at = now() "
            "WHERE drive_id = $1 AND id = $2",
            drive_id, artifact_id, revision,
        )
    except asyncpg.UniqueViolationError:
        raise ArtifactNameConflictError("a live sibling now owns the artifact's name") from None
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.restored", resource_type="artifact", resource_id=artifact_id,
        previous_revision=artifact["revision"], revision=revision,
        data={"name": artifact["name"]},
    )
    row = await c.fetchrow(
        f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id,
    )
    return await _payload(c, row)


async def copy_artifact(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    destination_drive_id: str,
    destination_parent_id: str,
    destination_name: str,
    version_id: str | None,
    destination_etag: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Copy one artifact within the same drive.

    Only same-drive copy is in v0 scope (cross-drive copy is rejected at the
    route layer). Materializes the artifact + its selected version
    synchronously, reusing the source version's CAS object.
    """
    destination_name = validate_name(destination_name)
    await _lock_drive_namespace(c, drive_id)
    await _ensure_drive(c, actor, drive_id)
    source = await _live_artifact(c, drive_id, artifact_id, for_update=True)
    _artifact_or_404(source)
    if destination_etag is not None:
        precondition(destination_etag, source["revision"])

    version = await _resolve_version(c, drive_id, artifact_id, version_id)
    if version is None:
        raise InvalidVersionError("no such version of this artifact")

    await _ensure_drive(c, actor, destination_drive_id)
    if await _resolve_parent(c, destination_drive_id, destination_parent_id) is None:
        raise InvalidArtifactMoveError("no such live destination folder")
    if await _name_is_occupied(
        c, destination_drive_id, destination_parent_id, destination_name
    ):
        raise ArtifactNameConflictError("a live sibling already uses the destination name")

    return await _materialize_artifact_copy(
        c,
        actor=actor,
        drive_id=drive_id,
        destination_parent_id=destination_parent_id,
        destination_name=destination_name,
        source=source,
        version=version,
    )


async def _materialize_artifact_copy(
    c: Any,
    *,
    actor: Any,
    drive_id: str,
    destination_parent_id: str,
    destination_name: str,
    source: Any,
    version: Any,
) -> dict[str, Any]:
    """Same-drive synchronous copy: new artifact row whose head version
    reuses the source version's CAS object (no byte copy), with its own
    version row referencing the source's ``storage_object``."""
    artifact_id = new_id("art")
    version_id = new_id("ver")
    revision = new_id("rev")
    # A head-version copy mirrors the source ARTIFACT's denormalized preview
    # (the authoritative one the write path derived); a copy of a historical
    # version derives the preview from THAT version's bytes — the version row
    # has no content_preview column (asyncpg Record.get on a missing column
    # silently yields None, which would blank every copy's preview).
    if version["id"] == source["head_version_id"]:
        preview = source["content_preview"]
    else:
        # A historical version may live outside the artifact bucket; the
        # copy is refused rather than reading from the wrong place.
        coordinates = read_coordinates(version)
        if coordinates is None:
            raise InvalidVersionError("no such version of this artifact")
        preview = await _preview_from_cas_object(
            version["storage_object"],
            bucket=coordinates[0],
            generation=coordinates[1],
        )
    row = await c.fetchrow(
        f"INSERT INTO artifacts "
        f"(id, drive_id, parent_id, name, content_type, content_preview, "
        f"metadata, labels, revision) "
        f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text[], $9) "
        f"RETURNING {_ARTIFACT_COLUMNS}",
        artifact_id, drive_id, destination_parent_id, destination_name,
        version["content_type"], preview,
        json.dumps(_decode_json(source["metadata"], {})),
        list(source["labels"] or []), revision,
    )
    # Copy reuses the source version's physical object but commits its own
    # LOGICAL version bytes through the shared seam (B3 §7): reserve, then
    # convert with the new version row, propagating the source's exact
    # storage coordinates.
    reservation_id = await content_commit.reserve_version_bytes(
        c, workspace_id=actor.workspace_id, drive_id=drive_id,
        principal_id=actor.subject, upload_id=None,
        size_bytes=version["size_bytes"],
        workspace_limit_bytes=actor.drive_limits.storage_bytes_workspace,
        drive_limit_bytes=actor.drive_limits.storage_bytes_drive,
    )
    await content_commit.commit_immutable_version(
        c,
        content_commit.ImmutableVersionCommit(
            drive_id=drive_id,
            workspace_id=actor.workspace_id,
            artifact_id=artifact_id,
            version_id=version_id,
            parent_version_id=None,
            ordinal=1,
            checksum=version["checksum"],
            content_type=version["content_type"],
            size_bytes=version["size_bytes"],
            storage_object=version["storage_object"],
            storage_bucket=version["storage_bucket"],
            storage_generation=version["storage_generation"],
            # The ACTOR's kind, not a constant. Pairing a literal "agent"
            # with `actor.subject` recorded a human's version as an agent's
            # and would now record a Service's the same way — provenance that
            # names the wrong kind of principal is worse than none, because
            # the change feed and the version history are read as authority.
            actor_type=actor.subject_type,
            actor_id=actor.subject,
            reservation_id=reservation_id,
        ),
    )
    await c.execute(
        "UPDATE artifacts SET head_version_id = $2 WHERE id = $1",
        artifact_id, version_id,
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.created", resource_type="artifact", resource_id=artifact_id,
        revision=revision, data={"name": destination_name, "copy_of": source["id"]},
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.version.created", resource_type="artifact", resource_id=artifact_id,
        revision=revision, data={
            "name": destination_name,
            "version_id": version_id,
            "copy_of_version": version["id"],
        },
    )
    return await _payload(c, row)


# ---------------------------------------------------------------------------
# Version vertical
# ---------------------------------------------------------------------------


async def _insert_version(
    c: Any,
    *,
    actor: Any,
    artifact_id: str,
    version_id: str,
    parent_version_id: str | None,
    ordinal: int,
    content: bytes,
    content_type: str,
    drive_id: str,
    origin_session_id: str | None = None,
    origin_message: str | None = None,
) -> dict[str, Any]:
    """CAS-address the content and commit the immutable version through the
    shared seam (`v0_content_commit`): reserve the exact logical size, land
    the bytes, then convert reservation to committed usage with the version
    row in the caller's transaction. ``content`` must already be validated
    (size ceiling / checksum) by the caller."""
    object_name = _cas_object(drive_id, content)
    checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
    # Reserve BEFORE the first object write (B3 §9): the promised logical
    # bytes are counted from the moment the producer commits to landing them.
    reservation_id = await content_commit.reserve_version_bytes(
        c, workspace_id=actor.workspace_id, drive_id=drive_id,
        principal_id=actor.subject, upload_id=None, size_bytes=len(content),
        workspace_limit_bytes=actor.drive_limits.storage_bytes_workspace,
        drive_limit_bytes=actor.drive_limits.storage_bytes_drive,
    )
    # The bytes land in GCS BEFORE the DB insert: the object is content
    # addressed, so a later DB failure leaves at worst a GC-collectable
    # orphan (the same trade the reference makes). The reservation rolls
    # back with the transaction in that case.
    write = await content_commit.store_cas_object(
        c, object_name=object_name, data=content, content_type=content_type,
    )
    return await content_commit.commit_immutable_version(
        c,
        content_commit.ImmutableVersionCommit(
            drive_id=drive_id,
            workspace_id=actor.workspace_id,
            artifact_id=artifact_id,
            version_id=version_id,
            parent_version_id=parent_version_id,
            ordinal=ordinal,
            origin_session_id=origin_session_id,
            origin_message=origin_message,
            checksum=checksum,
            content_type=content_type,
            size_bytes=len(content),
            storage_object=object_name,
            storage_bucket=write.bucket,
            storage_generation=write.generation,
            # The ACTOR's kind, not a constant. Pairing a literal "agent"
            # with `actor.subject` recorded a human's version as an agent's
            # and would now record a Service's the same way — provenance that
            # names the wrong kind of principal is worse than none, because
            # the change feed and the version history are read as authority.
            actor_type=actor.subject_type,
            actor_id=actor.subject,
            reservation_id=reservation_id,
        ),
    )


async def _resolve_version(
    c: Any, drive_id: str, artifact_id: str, version_id: str | None
) -> Any | None:
    """Resolve ``version_id`` (or the artifact's head) to a version row."""
    if version_id is None:
        return await c.fetchrow(
            f"SELECT {_VERSION_COLUMNS_V} "
            "FROM artifact_versions v "
            "JOIN artifacts a ON a.id = v.artifact_id AND a.head_version_id = v.id "
            "WHERE v.artifact_id = $1",
            artifact_id,
        )
    return await c.fetchrow(
        f"SELECT {_VERSION_COLUMNS_V} "
        "FROM artifact_versions v "
        "JOIN artifacts a ON a.id = v.artifact_id "
        "WHERE v.artifact_id = $1 AND v.id = $2 AND a.drive_id = $3",
        artifact_id, version_id, drive_id,
    )


async def list_versions(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    limit: int,
    after_ordinal: int | None = None,
    after_id: str | None = None,
) -> dict[str, Any]:
    """Version trail, newest-first (ordinal DESC) with a stable (ordinal, id)
    keyset anchor. Returns ``{"items", "next_cursor"}`` for the route."""
    await _ensure_drive(c, actor, drive_id)
    artifact = await _live_artifact(c, drive_id, artifact_id, for_update=False)
    _artifact_or_404(artifact)
    params: list[Any] = [artifact_id]
    sql = (
        f"SELECT {_VERSION_COLUMNS} FROM artifact_versions "
        "WHERE artifact_id = $1"
    )
    if after_ordinal is not None:
        params.extend([after_ordinal, after_id])
        sql += " AND (ordinal, id) < ($" + str(len(params) - 1) + ", $" + str(len(params)) + ")"
    params.append(limit + 1)
    sql += " ORDER BY ordinal DESC, id DESC LIMIT $" + str(len(params))
    rows = await c.fetch(sql, *params)

    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = {"ordinal": last["ordinal"], "id": last["id"]}
    return {"items": [version_payload(r) for r in rows], "next_cursor": next_cursor}


async def get_version(
    c: Any, actor: Any, drive_id: str, artifact_id: str, version_id: str
) -> dict[str, Any]:
    """Read one immutable version (404 for a missing artifact/version or a
    drive outside the workspace)."""
    await _ensure_drive(c, actor, drive_id)
    artifact = await _live_artifact(c, drive_id, artifact_id, for_update=False)
    _artifact_or_404(artifact)
    row = await _resolve_version(c, drive_id, artifact_id, version_id)
    if row is None:
        raise InvalidVersionError("no such version of this artifact")
    return version_payload(row)


async def append_version(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    content: bytes,
    content_type: str,
    sha256: str | None,
    if_match: str | None,
    expect_head_version_id: str | None = None,
    origin_session_id: str | None = None,
    origin_message: str | None = None,
) -> dict[str, Any]:
    """Append one immutable version and rotate the artifact head to it.
    Must run inside a transaction.

    ``origin_session_id``/``origin_message`` record what produced this
    version, for producers that have an answer. They are frozen on the row
    and outlive whatever session is named (migration 0053).

    ``expect_head_version_id`` selects a CONTENT-anchored precondition
    instead of the revision one, for a producer whose work depends on the
    artifact's bytes and nothing else. A sheet edit session is the case:
    its replay reads the pinned ``base_version_id``, so a rename, a move or
    a relabel — each of which rotates the artifact revision (§4.1) —
    cannot invalidate it, and aborting on one stranded an open session that
    could then never complete. A rival VERSION still refuses, because that
    is the change a replay genuinely cannot absorb. Checked under the same
    row lock as the append, so the head cannot move between test and
    write."""
    if not content:
        raise ContentEmptyError("append requires a non-empty content part")
    if sha256 is not None and not _matches_sha256(content, sha256):
        raise ChecksumMismatchError("declared sha256 does not match the content")
    if len(content) > MAX_BUFFERED_UPLOAD_BYTES:
        raise ArtifactTooLargeError("inline content exceeds the buffered ceiling")

    await _ensure_drive(c, actor, drive_id)
    artifact = await _live_artifact(c, drive_id, artifact_id, for_update=True)
    _artifact_or_404(artifact)
    if expect_head_version_id is not None:
        if artifact["head_version_id"] != expect_head_version_id:
            raise PreconditionError(
                412,
                "PRECONDITION_FAILED",
                "the artifact's content changed after it was read",
                current_revision=artifact["revision"],
            )
    else:
        precondition(if_match, artifact["revision"])

    next_ordinal = (
        await c.fetchval(
            "SELECT coalesce(max(ordinal), 0) + 1 FROM artifact_versions "
            "WHERE artifact_id = $1",
            artifact_id,
        )
    )
    version_id_new_ver = new_id("ver")
    version = await _insert_version(
        c,
        actor=actor,
        artifact_id=artifact_id,
        version_id=version_id_new_ver,
        parent_version_id=artifact["head_version_id"],
        ordinal=next_ordinal,
        content=content,
        content_type=content_type,
        drive_id=drive_id,
        origin_session_id=origin_session_id,
        origin_message=origin_message,
    )
    revision = new_id("rev")
    preview = _derive_preview(content, content_type=content_type, name=artifact["name"])
    await c.execute(
        "UPDATE artifacts SET head_version_id = $3, revision = $4, "
        "content_type = $5, content_preview = $6, updated_at = now() "
        "WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id, version["id"], revision, content_type, preview,
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.version.created", resource_type="artifact", resource_id=artifact_id,
        previous_revision=artifact["revision"], revision=revision,
        data={"name": artifact["name"], "version_id": version["id"]},
    )
    version["artifact_revision"] = revision
    return version


async def restore_version(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    version_id: str,
    *,
    if_match: str | None,
) -> dict[str, Any]:
    """Restore a historical version as a NEW head version.

    History is never rewound: the restore appends a new immutable version
    whose parent is the current head and whose bytes reference the historical
    version's CAS object (no byte copy). Must run inside a transaction."""
    await _ensure_drive(c, actor, drive_id)
    artifact = await _live_artifact(c, drive_id, artifact_id, for_update=True)
    _artifact_or_404(artifact)
    precondition(if_match, artifact["revision"])

    historical = await _resolve_version(c, drive_id, artifact_id, version_id)
    if historical is None:
        raise InvalidVersionError("no such version of this artifact")

    next_ordinal = (
        await c.fetchval(
            "SELECT coalesce(max(ordinal), 0) + 1 FROM artifact_versions "
            "WHERE artifact_id = $1",
            artifact_id,
        )
    )
    version_id_new = new_id("ver")
    # Version restore is a version PRODUCER (B3 §7): a new logical version
    # row referencing the historical bytes, reserved and committed through
    # the shared seam with the source's exact storage coordinates.
    reservation_id = await content_commit.reserve_version_bytes(
        c, workspace_id=actor.workspace_id, drive_id=drive_id,
        principal_id=actor.subject, upload_id=None,
        size_bytes=historical["size_bytes"],
        workspace_limit_bytes=actor.drive_limits.storage_bytes_workspace,
        drive_limit_bytes=actor.drive_limits.storage_bytes_drive,
    )
    committed = await content_commit.commit_immutable_version(
        c,
        content_commit.ImmutableVersionCommit(
            drive_id=drive_id,
            workspace_id=actor.workspace_id,
            artifact_id=artifact_id,
            version_id=version_id_new,
            parent_version_id=artifact["head_version_id"],
            ordinal=next_ordinal,
            checksum=historical["checksum"],
            content_type=historical["content_type"],
            size_bytes=historical["size_bytes"],
            storage_object=historical["storage_object"],
            storage_bucket=historical["storage_bucket"],
            storage_generation=historical["storage_generation"],
            # The ACTOR's kind, not a constant. Pairing a literal "agent"
            # with `actor.subject` recorded a human's version as an agent's
            # and would now record a Service's the same way — provenance that
            # names the wrong kind of principal is worse than none, because
            # the change feed and the version history are read as authority.
            actor_type=actor.subject_type,
            actor_id=actor.subject,
            reservation_id=reservation_id,
        ),
    )
    revision = new_id("rev")
    await c.execute(
        "UPDATE artifacts SET head_version_id = $3, revision = $4, "
        "content_type = $5, updated_at = now() WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id, version_id_new, revision,
        committed["content_type"],
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.version.created", resource_type="artifact", resource_id=artifact_id,
        previous_revision=artifact["revision"], revision=revision,
        data={"name": artifact["name"], "version_id": version_id_new, "restored_from": version_id},
    )
    committed["artifact_revision"] = revision
    return committed


async def head_content(
    c: Any, actor: Any, drive_id: str, artifact_id: str
) -> dict[str, Any] | None:
    """Resolve the artifact's head version content row, or None when the
    artifact is missing/deleted. The caller streams or 307s it."""
    await _ensure_drive(c, actor, drive_id)
    artifact = await _live_artifact(c, drive_id, artifact_id, for_update=False)
    if artifact is None:
        return None
    row = await _resolve_version(c, drive_id, artifact_id, None)
    if row is None:
        return None
    coordinates = read_coordinates(row)
    if coordinates is None:
        return None
    return {
        "storage_object": row["storage_object"],
        "storage_bucket": coordinates[0],
        "storage_generation": coordinates[1],
        "size_bytes": row["size_bytes"],
        "content_type": row["content_type"],
        "artifact_name": artifact["name"],
        "version_id": row["id"],
    }


async def version_content(
    c: Any, actor: Any, drive_id: str, artifact_id: str, version_id: str
) -> dict[str, Any] | None:
    """Resolve one version's content row, or None when the artifact/version is
    missing/deleted. The caller streams or 307s it."""
    await _ensure_drive(c, actor, drive_id)
    artifact = await _live_artifact(c, drive_id, artifact_id, for_update=False)
    if artifact is None:
        return None
    row = await _resolve_version(c, drive_id, artifact_id, version_id)
    if row is None:
        return None
    coordinates = read_coordinates(row)
    if coordinates is None:
        return None
    return {
        "storage_object": row["storage_object"],
        "storage_bucket": coordinates[0],
        "storage_generation": coordinates[1],
        "size_bytes": row["size_bytes"],
        "content_type": row["content_type"],
        "artifact_name": artifact["name"],
        "version_id": row["id"],
    }
