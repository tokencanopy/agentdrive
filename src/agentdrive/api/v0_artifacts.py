"""Artifact vertical's HTTP surface (slice 6): 8 operations.

Wire semantics mirror the folders vertical (§6.2/§6.3):

  * create and copy are idempotent under an ``Idempotency-Key`` — create is
    multipart/form-data (a JSON POST is 415). The content bytes are CAS-put
    before the DB transaction; the artifact + first version land atomically;
  * update / delete / restore are mutation-of-existing: they require both an
    ``Idempotency-Key`` and ``If-Match`` (428 absent, 412 stale);
  * read carries the artifact ETag; ``If-None-Match`` → 304;
  * content reads (head only) stream directly at/under
    ``DOWNLOAD_SIGNED_MIN_BYTES``, redirect (307) to a short-lived signed
    GCS URL above it — for a version in EITHER bucket, whenever its
    persisted ``(bucket, object, generation)`` triple can be signed; ETag is
    the head version id;
  * soft-deleted artifacts are reachable through ``?state=deleted|all``
    listing; reads 404;
  * copy completes synchronously (201); cross-drive copy is out of v0 scope
    and rejected with 400 INVALID_ARGUMENT.

Shared handlers (envelope, idempotency, multipart parsing, content
streaming) are exported so the versions router can reuse them.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..config import settings
from ..core import idempotency, ids, v0_drives
from ..core import v0_artifacts as core
from ..core import v0_authz as authz
from ..core.kinds import safe_content_type
from ..core.usage.gate import LimitExceeded, usage_gate
from ..core.v0_content_commit import QuotaExceededError
from ..core.v0_folders import InvalidFolderNameError
from ..db import conn
from ..identity.actor import V0ActorContext
from .cursors import clamp_limit, cursor_str, cursor_ts
from .v0_authz import require_local
from .v0_cursors import seal as _seal_cursor
from .v0_cursors import unseal as _unseal_cursor
from .v0_deps import known_params, precondition_http, v0_actor
from .v0_errors import V0ApiError, public_validation_details
from .v0_models import ArtifactListOut, ArtifactOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["artifacts"], dependencies=[Depends(enforce_v0_rate_limit)]
)

_SCOPE_READ = "content:read"
_SCOPE_WRITE = "content:write"

# Multipart framing allowance above the content ceiling: the raw request body
# carries boundaries + field parts on top of the content part, so the
# Content-Length early-reject threshold sits one MiB above the content cap.
_MULTIPART_FRAMING_SLACK = 1024 * 1024
_MULTIPART_READ_CHUNK_BYTES = 1024 * 1024

# Ceiling for a NON-content multipart part read as text (see
# `_parse_multipart_create`). Scalar parts are ids, names and a small JSON
# metadata object; anything larger is a caller mistake, and the bound keeps an
# oversized part from being buffered just to be rejected by validation later.
_MULTIPART_SCALAR_PART_MAX_BYTES = 128 * 1024

_DRIVE_ID_PATTERN = r"^drv_[a-f0-9]{16}$"
_FOLDER_ID_PATTERN = r"^fld_[a-f0-9]{16}$"
_VERSION_ID_PATTERN = r"^ver_[a-f0-9]{16}$"


class ArtifactCreateIn(BaseModel):
    """POST /v0/drives/{id}/artifacts fields (multipart) — validated after
    the form is parsed, since multipart bodies are not auto-bound."""

    parent_id: str = Field(pattern=_FOLDER_ID_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return core.validate_name(value)


class ArtifactUpdateIn(BaseModel):
    """PATCH /v0/drives/{id}/artifacts/{artifact_id} body — at least one
    field is required."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: str | None = Field(default=None, pattern=_FOLDER_ID_PATTERN)
    metadata: dict[str, Any] | None = None
    labels: list[str] | None = None
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return core.validate_name(value)

    @model_validator(mode="after")
    def _set_changed(self) -> ArtifactUpdateIn:
        fields = ("name", "parent_id", "metadata", "labels")
        if all(getattr(self, f) is None for f in fields):
            raise ValueError("provide at least one of name, parent_id, metadata, labels")
        self._changed = frozenset(f for f in fields if getattr(self, f) is not None)
        return self

    @property
    def changed(self) -> frozenset[str]:
        return getattr(self, "_changed", frozenset())


class ArtifactCopyIn(BaseModel):
    """POST /v0/drives/{id}/artifacts/{artifact_id}/copy body.

    ``destination_drive_id`` must equal the source drive (or be absent) —
    cross-drive copy is out of v0 scope and rejected."""

    destination_drive_id: str | None = Field(default=None, pattern=_DRIVE_ID_PATTERN)
    destination_parent_id: str = Field(pattern=_FOLDER_ID_PATTERN)
    destination_name: str = Field(min_length=1, max_length=255)
    version_id: str | None = Field(default=None, pattern=_VERSION_ID_PATTERN)
    model_config = ConfigDict(extra="forbid")

    @field_validator("destination_name", mode="before")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return core.validate_name(value)


# ── helpers (shared with versions) ──────────────────────────────────────────

def _require_scope(actor: V0ActorContext, scope: str) -> None:
    if not actor.can(scope):
        raise V0ApiError(
            403, "PERMISSION_DENIED", f"the token does not carry the {scope} scope"
        )


def _check_drive_id(drive_id: str) -> None:
    if not ids.is_valid(drive_id, "drv"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")


def _check_artifact_id(artifact_id: str) -> None:
    if not ids.is_valid(artifact_id, "art"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed artifact id")


def _etag(revision: str) -> str:
    return f'"{revision}"'


def _artifact_location(drive_id: str, artifact_id: str) -> str:
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    return f"{origin}/v0/drives/{drive_id}/artifacts/{artifact_id}"


def _body_hash(model: Any) -> str:
    payload = model.model_dump(mode="json") if model is not None else {}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _multipart_hash(*parts: Any) -> str:
    """Idempotency identity for multipart mutations (§7.2).

    The artifact/version write paths parse multipart by hand, so FastAPI's
    body-model hashing can't see the request. This folds the parsed fields
    (parent/name/metadata/content_type) and a content digest into the request
    hash so two DIFFERENT creates under one Idempotency-Key conflict (409)
    instead of silently replaying the first result. ``content_bytes`` is
    passed by the caller for the digest; the sha256 field (when present) is
    used verbatim so a retry with the same declared hash is stable.
    """
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _artifact_cursor_bound(
    *,
    state: str,
    parent_id: str | None,
    name: str | None,
    content_type: str | None,
    label: str | None,
    updated_after: datetime | None,
    updated_before: datetime | None,
) -> dict[str, Any]:
    """The filter fingerprint a later page must not change (§6.3).

    Built identically at seal and unseal so a cursor minted under one filter
    set is rejected if the filters change between pages. Datetimes normalize
    to ISO strings (``core.cursors`` canonicalizes with ``str`` anyway, but an
    explicit ISO form keeps the bound stable and readable).
    """
    # The key keeps its original spelling: it is internal to the sealed
    # token, so renaming it with the query parameter would fail every
    # in-flight cursor closed on the deploy.
    return {
        "lifecycle": state,
        "parent_id": parent_id,
        "name": name,
        "content_type": content_type,
        "label": label,
        "updated_after": updated_after.isoformat() if updated_after else None,
        "updated_before": updated_before.isoformat() if updated_before else None,
    }


def _mapping_error(exc: Exception) -> V0ApiError:
    if isinstance(exc, v0_drives.PreconditionError):
        return precondition_http(exc)
    if isinstance(exc, v0_drives.DriveNotFoundError):
        return V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace")
    if isinstance(exc, core.ArtifactNotFoundError):
        return V0ApiError(404, "ARTIFACT_NOT_FOUND", "no such artifact in this drive")
    if isinstance(exc, authz.NotAuthorizedError):
        return V0ApiError(404, "NOT_AUTHORIZED", "not authorized on this resource")
    if isinstance(exc, core.ArtifactNameConflictError):
        return V0ApiError(409, "ARTIFACT_PATH_CONFLICT", str(exc))
    if isinstance(exc, (core.InvalidArtifactMoveError,)):
        return V0ApiError(404, "ARTIFACT_NOT_FOUND", str(exc))
    if isinstance(exc, (core.ArtifactNotDeletedError, core.ArtifactParentNotLiveError)):
        return V0ApiError(409, "CONFLICT", str(exc))
    if isinstance(exc, core.InvalidArtifactNameError):
        return V0ApiError(400, "INVALID_ARGUMENT", str(exc))
    if isinstance(exc, core.ArtifactTooLargeError):
        return V0ApiError(413, "ARTIFACT_TOO_LARGE", str(exc))
    if isinstance(exc, core.InvalidVersionError):
        return V0ApiError(404, "VERSION_NOT_FOUND", str(exc))
    if isinstance(exc, core.ContentEmptyError):
        return V0ApiError(422, "VALIDATION_ERROR", str(exc))
    if isinstance(exc, core.ChecksumMismatchError):
        return V0ApiError(409, "CHECKSUM_MISMATCH", str(exc))
    if isinstance(exc, QuotaExceededError):
        # Reached by every inline content write, copy, and version restore once
        # a workspace ceiling is configured. Only the uploads path mapped it,
        # so turning quotas on would have turned each of these into a 500 —
        # the same code and status the direct path already answers.
        return V0ApiError(
            422, "TRANSFER_LIMIT_EXCEEDED",
            "the storage reservation cannot be acquired",
            details={
                "limit_name": f"storage_bytes_{exc.scope}",
                "used": exc.used,
                "reserved": exc.reserved,
                "limit": exc.limit,
                "requested": exc.requested,
                "remaining": max(exc.limit - exc.used - exc.reserved, 0),
                "reset_at": None,
            },
        )
    raise exc


def _folder_editor_guard(
    actor: V0ActorContext, drive_id: str, parent_id: str | None
) -> Callable[..., Any]:
    """Replay-time authorization for create/copy (destination parent in body).

    The parent-editor check lives inside `execute`, which idempotent replay
    skips. This guard re-runs it on replay so a revoked principal cannot
    replay a stored success (§6.2). None parent = create at the root, which
    needs no parent capability beyond drive membership.
    """

    async def _guard(c: Any) -> None:
        try:
            if parent_id is not None:
                await authz.require(
                    c, actor=actor, drive_id=drive_id,
                    resource_type="folder", resource_id=parent_id, minimum="editor",
                )
        except Exception as exc:
            raise _mapping_error(exc) from None

    return _guard


async def _replayable(body: Any) -> Any:
    """Bring a STORED mutation body up to the current response contract.

    An idempotency record is replayed verbatim, so a record written by a
    previous deployment carries that deployment's response shape. When a new
    REQUIRED response field ships, every in-flight key claimed by the old
    image would otherwise fail response-model validation on retry — a 500
    for the whole 24h record lifetime, and continuously while a candidate
    revision and the previous one both serve traffic. The client's only
    escape would be a fresh Idempotency-Key, i.e. a duplicate artifact:
    exactly what idempotency exists to prevent.

    `effective_visibility` is derived, not stored, so it is recomputed from
    live state rather than defaulted — a replay reports the artifact's
    exposure NOW, which is also what a non-replayed response would say.
    """
    if not isinstance(body, dict):
        return body
    if "effective_visibility" in body:
        return body
    artifact_id, drive_id = body.get("id"), body.get("drive_id")
    if not (isinstance(artifact_id, str) and artifact_id.startswith("art_")):
        return body  # not an artifact payload (e.g. a version response)
    if not isinstance(drive_id, str):
        return body
    from ..core import v0_authz

    async with conn() as c:
        visibility = await v0_authz.artifact_visibility_one(c, drive_id, artifact_id)
    return {**body, "effective_visibility": visibility}


async def _run_mutation(
    actor: V0ActorContext,
    *,
    key: str | None,
    method: str,
    path: str,
    request_hash: str,
    execute: Any,
    replay_guard: Callable[..., Any] | None = None,
    replay_status: int | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Claim → execute-in-transaction → complete (abandon on rejection).

    `replay_guard(c)` (when given) runs on REPLAY before the stored result is
    returned, on a fresh connection — re-checking authorization for checks
    that live inside `execute` (body-derived parents) so a revoked principal
    cannot replay a stored success (§6.2).
    """
    from contextlib import suppress

    if not key:
        raise V0ApiError(
            400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required"
        )
    outcome = await idempotency.claim(
        principal_id=actor.subject,
        key=key,
        method=method,
        path=path,
        request_hash=request_hash,
    )
    if outcome.state == "replayed":
        stored = outcome.stored
        if replay_guard is not None:
            async with conn() as c:
                await replay_guard(c)
        status = stored.status if replay_status is None else replay_status
        return (status, stored.headers, await _replayable(stored.body))
    if outcome.state == "conflict":
        raise V0ApiError(
            409,
            "IDEMPOTENCY_CONFLICT",
            "idempotency key was already used for a different request",
        )
    if outcome.state == "in_flight":
        raise V0ApiError(
            409,
            "IDEMPOTENCY_IN_PROGRESS",
            "idempotency key is already being processed; retry",
            headers={"Retry-After": "5"},
        )
    owner_id = outcome.owner_id
    assert owner_id is not None

    async with conn() as c:
        try:
            async with c.transaction():
                status, headers, body = await execute(c)
                await idempotency.complete(
                    c, owner_id=owner_id, status=status, body=body, headers=headers
                )
        except Exception:
            with suppress(Exception):
                await idempotency.abandon(c, owner_id=owner_id)
            raise
    return (status, headers, body)


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """True when ``If-None-Match`` matches the current strong ETag.

    Same parsing as the drives/folders reads (via ``v0_drives.etag_values``):
    supports ``*``, weak tags (``W/"..."``), and comma-separated lists. The
    comparison is on the unquoted current value against the parsed header
    values (weak comparison per RFC 7232 §3.2)."""
    if not if_none_match:
        return False
    current = etag[1:-1] if etag.startswith('"') and etag.endswith('"') else etag
    values = v0_drives.etag_values(if_none_match)
    if values == "*":
        return True
    return current in (values or [])


async def _signed_capability_target(
    *,
    gcs_object: str,
    gcs_bucket: str | None,
    gcs_generation: int | None,
    content_type: str,
    filename: str,
) -> str | None:
    """A validated signed GET for a row carrying the COMPLETE persisted
    ``(bucket, object, generation)`` triple, or ``None`` to stream.

    This is the packet-4 capability signer — the same one the
    ``download-capabilities`` mint uses, and the only signer that may point
    at the transfer bucket: it pins the generation, proves the object is a
    real member of that bucket's configured namespace, validates its own
    output, and fails closed. `storage.signed_download_url` cannot do any of
    that (it signs against the default host with a soft `None`), which is
    why the transfer bucket was excluded from the redirect shortcut rather
    than handed to it.

    Why this matters here and not only at the mint: a direct-uploaded
    version may be up to `DIRECT_TRANSFER_MAX_BYTES` (1 GiB in production),
    and streaming one holds an anyio worker thread for as long as the client
    takes to read it. Production runs `api_max_instances = 1`, so a handful
    of slow readers on large transfer-bucket versions is enough to starve
    every other request on the only instance — `/health` included.

    Returns `None` — never raises — whenever the triple is incomplete
    (legacy CAS rows the reconcile job has not resolved yet), the transfer
    configuration is absent, or the signer fails closed. Every one of those
    is "stream it instead", which is exactly today's behaviour.
    """
    if not gcs_bucket or not gcs_generation or gcs_generation <= 0:
        return None
    ttl = settings.direct_download_capability_ttl_seconds
    if ttl is None:
        return None
    from ..storage_transfers import DownloadSigningUnavailableError
    from .v0_download_capabilities import capability_signer

    try:
        signer = capability_signer()
    except Exception:
        # An unbuildable signer is absent configuration, not a request
        # fault. The stream path is still correct, so it is taken.
        return None
    try:
        capability = await signer.sign_capability(
            bucket=gcs_bucket,
            object_name=gcs_object,
            generation=gcs_generation,
            # Deliberately the version's own sanitized type, NOT the mint's
            # forced `application/octet-stream`. That constant is the mint's
            # policy because a minted URL is disclosed to a browser; this
            # surface has no browser callers (the console origin is off the
            # product `/v0` CORS allowlist) and machine clients rely on the
            # content type they get today on both the stream and the 307.
            # Safety is unchanged either way: `safe_content_type` refuses
            # anything that is not a well-formed media type, and
            # `sign_capability` forces `Content-Disposition: attachment`
            # unconditionally.
            media_type=safe_content_type(content_type),
            filename=filename,
            ttl_seconds=ttl,
        )
    except DownloadSigningUnavailableError:
        return None
    return capability.url


async def _bytes_response(
    *,
    gcs_object: str,
    gcs_bucket: str | None = None,
    gcs_generation: int | None = None,
    size_bytes: int,
    content_type: str,
    filename: str,
    etag: str,
    base_headers: dict[str, str],
    actor: V0ActorContext,
    drive_id: str,
) -> Response:
    """Stream-or-307: small objects stream directly; larger ones redirect to
    a signed GCS URL (fall back to stream when signing is unavailable)."""
    from .. import storage
    from .v0_uploads import _usage_limit_error

    async with conn() as c, c.transaction():
        try:
            await usage_gate.charge_private_download(
                c,
                actor=actor,
                drive_id=drive_id,
                operation_key=f"content-download:{secrets.token_hex(16)}",
                size_bytes=size_bytes,
            )
        except LimitExceeded as exc:
            raise _usage_limit_error(exc) from None

    if size_bytes > settings.download_signed_min_bytes:
        # Preferred: the validated capability signer, which reaches BOTH
        # buckets for any row with a complete persisted coordinate triple.
        signed = await _signed_capability_target(
            gcs_object=gcs_object,
            gcs_bucket=gcs_bucket,
            gcs_generation=gcs_generation,
            content_type=content_type,
            filename=filename,
        )
        # Fallback: a legacy CAS row whose bucket/generation the reconcile
        # job has not backfilled yet has no triple to sign, so it keeps the
        # original CAS-only signer. Transfer-bucket rows never take this
        # branch — `signed_download_url` would sign against the default
        # host with none of the namespace/generation checks.
        if signed is None and gcs_bucket is None:
            signed = await storage.signed_download_url(
                gcs_object,
                content_type=safe_content_type(content_type),
                filename=filename,
                ttl_s=settings.download_url_ttl_s,
                bucket=gcs_bucket,
                generation=gcs_generation,
            )
        if signed is not None:
            return Response(
                status_code=307,
                headers={**base_headers, "Location": signed, "ETag": etag},
            )
    return StreamingResponse(
        storage.stream(gcs_object, bucket=gcs_bucket, generation=gcs_generation),
        status_code=200,
        headers={
            **base_headers,
            "Content-Type": safe_content_type(content_type),
            "Content-Length": str(size_bytes),
            "ETag": etag,
        },
    )


def _check_multipart_content_length(request: Request) -> None:
    """Reject 413 on the Content-Length header ALONE, before the body is
    buffered: a request whose declared length already exceeds the content
    ceiling (plus multipart framing) can never be accepted, so reading it is
    wasted memory and a wasted upload. An absent or non-numeric header is
    left to the chunked cap check inside the parser."""
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        declared = int(raw)
    except ValueError:
        return
    if declared > core.MAX_BUFFERED_UPLOAD_BYTES + _MULTIPART_FRAMING_SLACK:
        raise V0ApiError(413, "ARTIFACT_TOO_LARGE", "inline content exceeds the buffered ceiling")


async def _parse_multipart_create(
    request: Request,
) -> tuple[dict[str, str], bytes, str]:
    """Extract fields + content bytes from a multipart create form.

    The content part is read in bounded chunks with a running total, so an
    over-cap body aborts with 413 as soon as the ceiling is crossed — never
    materializing more than cap + one chunk of content bytes in memory."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise V0ApiError(
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            "This operation requires multipart/form-data.",
        )
    form = await request.form()
    from starlette.datastructures import UploadFile as _UploadFilePart

    # A scalar part may arrive EITHER as a plain form field or as its own body
    # part carrying a content type. OpenAPI 3 says an object-typed multipart
    # property is serialized as a part with `Content-Type: application/json`,
    # and generated clients do exactly that -- the TypeScript SDK sends
    # `metadata` as a JSON blob. Keeping only `isinstance(v, str)` dropped that
    # part on the floor: `metadata` fell back to its `"{}"` default, so an
    # artifact created with metadata silently came back with none and no error
    # was raised, because an empty object is a valid value.
    fields: dict[str, str] = {}
    for key, value in form.items():
        if key == "content":
            continue
        if isinstance(value, str):
            fields[key] = value
            continue
        if isinstance(value, _UploadFilePart):
            raw = await value.read(_MULTIPART_SCALAR_PART_MAX_BYTES + 1)
            if len(raw) > _MULTIPART_SCALAR_PART_MAX_BYTES:
                raise V0ApiError(
                    413, "ARTIFACT_TOO_LARGE",
                    f"the {key} part exceeds the scalar part ceiling",
                    details={"fields": [{"location": key, "reason": "too_large"}]},
                )
            try:
                fields[key] = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise V0ApiError(
                    422, "VALIDATION_ERROR", f"the {key} part must be UTF-8 text",
                    details={"fields": [{"location": key, "reason": "invalid_value"}]},
                ) from None
    content_field = form.get("content")
    if content_field is None:
        raise V0ApiError(
            422, "VALIDATION_ERROR", "the content file part is required",
            details={"fields": [{"location": "content", "reason": "required"}]},
        )
    if isinstance(content_field, _UploadFilePart):
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await content_field.read(_MULTIPART_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > core.MAX_BUFFERED_UPLOAD_BYTES:
                raise V0ApiError(
                    413, "ARTIFACT_TOO_LARGE",
                    "inline content exceeds the buffered ceiling",
                )
            chunks.append(chunk)
        content_bytes = b"".join(chunks)
        part_ct = content_field.content_type or "application/octet-stream"
        return fields, content_bytes, part_ct
    if isinstance(content_field, str):
        return fields, content_field.encode(), "application/octet-stream"
    raise V0ApiError(422, "VALIDATION_ERROR", "content must be a file part")


# ── routes ──────────────────────────────────────────────────────────────────


@router.get(
    "/drives/{drive_id}/artifacts",
    response_model=ArtifactListOut,
    operation_id="artifacts_list",
    dependencies=[Depends(known_params(
        "state", "limit", "cursor", "parent_id", "name", "content_type",
        "label", "updated_after", "updated_before",
    ))],
)
async def list_artifacts(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    state: str = "active",
    limit: int | None = None,
    cursor: str | None = None,
    parent_id: str | None = None,
    name: str | None = None,
    content_type: str | None = None,
    label: str | None = None,
    updated_after: datetime | None = None,
    updated_before: datetime | None = None,
    response: Response = ...,
) -> ArtifactListOut:
    """List the drive's artifacts, newest-first (keyset paginated).

    ``state`` (active|deleted|all) exposes soft-deleted artifacts.
    ``parent_id`` / ``name`` / ``content_type`` / ``label`` are exact-match
    filters; ``updated_after`` / ``updated_before`` are inclusive bounds.
    Unknown query parameters are rejected."""
    _require_scope(actor, _SCOPE_READ)
    if state not in ("active", "deleted", "all"):
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "state must be one of active, deleted, all"
        )
    _check_drive_id(drive_id)
    if parent_id is not None:
        from ..core.ids import is_valid as id_valid
        if not id_valid(parent_id, "fld"):
            raise V0ApiError(400, "INVALID_ARGUMENT", "malformed parent_id")
    if name is not None:
        try:
            name = core.validate_name(name)
        except InvalidFolderNameError as e:
            raise V0ApiError(400, "INVALID_ARGUMENT", str(e)) from None
    page_size = clamp_limit(limit)

    # The filter fingerprint a later page must not change. Built ONCE here and
    # used for both unseal (validate the caller's cursor) and seal (mint the
    # next one), so the two sites cannot drift. Datetimes normalize to ISO
    # strings; absent filters are None.
    bound = _artifact_cursor_bound(
        state=state, parent_id=parent_id, name=name,
        content_type=content_type, label=label,
        updated_after=updated_after, updated_before=updated_before,
    )
    position = _unseal_cursor("artifacts", drive_id, cursor, bound=bound)
    after_ts = cursor_ts(position, "created_at") if position else None
    after_id = cursor_str(position, "id") if position else None

    async with conn() as c:
        try:
            page = await core.list_artifacts(
                c, actor, drive_id,
                state=state, limit=page_size,
                after_ts=after_ts, after_id=after_id,
                parent_id=parent_id, name=name, content_type=content_type,
                label=label, updated_after=updated_after, updated_before=updated_before,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
    response.headers["Cache-Control"] = "private"
    return {
        "items": page["items"],
        "next_cursor": _seal_cursor(
            "artifacts", drive_id, page["next_cursor"], bound=bound
        ),
    }


@router.post(
    "/drives/{drive_id}/artifacts",
    status_code=201,
    response_model=ArtifactOut,
    operation_id="artifacts_create",
    dependencies=[Depends(known_params())],
)
async def create_artifact(
    drive_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    response: Response = ...,
) -> ArtifactOut:
    """Create one artifact with inline content — multipart only."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_multipart_content_length(request)
    fields, content_bytes, part_content_type = await _parse_multipart_create(request)

    metadata_text = fields.get("metadata", "{}")
    try:
        raw_metadata = json.loads(metadata_text)
    except json.JSONDecodeError as exc:
        raise V0ApiError(
            422, "VALIDATION_ERROR", "metadata must be a JSON object",
            details={"fields": [{"location": "metadata", "reason": "invalid_value"}]},
        ) from exc
    try:
        body = ArtifactCreateIn.model_validate(
            {
                "parent_id": fields.get("parent_id"),
                "name": fields.get("name"),
                "metadata": raw_metadata,
            }
        )
    except ValidationError as exc:
        raise V0ApiError(
            422, "VALIDATION_ERROR", "The request does not match the v0 contract.",
            details=public_validation_details(exc.errors()),
        ) from None
    ct = fields.get("content_type") or part_content_type or "application/octet-stream"
    sha256 = fields.get("sha256")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            # Drive workspace membership first, then editor capability on the
            # parent folder (the artifact is created INTO it).
            await core._ensure_drive(c, actor, drive_id)
            if body.parent_id is not None:
                await authz.require(
                    c, actor=actor, drive_id=drive_id,
                    resource_type="folder", resource_id=body.parent_id, minimum="editor",
                )
            result = await core.create_artifact(
                c, actor, drive_id,
                parent_id=body.parent_id, name=body.name, metadata=body.metadata,
                content=content_bytes, content_type=ct, sha256=sha256,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            201,
            {"Location": _artifact_location(drive_id, result["id"]),
             "ETag": _etag(result["revision"]),
             "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/artifacts",
        request_hash=_multipart_hash(
            {
                "parent_id": body.parent_id,
                "name": body.name,
                "metadata": body.metadata,
                "content_type": ct,
                "sha256": sha256,
            },
            {"content_digest": hashlib.sha256(content_bytes).hexdigest()},
        ),
        execute=execute,
        replay_guard=_folder_editor_guard(actor, drive_id, body.parent_id),
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}",
    response_model=ArtifactOut,
    responses={304: {"description": "If-None-Match matched."}},
    operation_id="artifacts_read",
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def read_artifact(
    drive_id: str,
    artifact_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> ArtifactOut:
    """Read one active artifact. ``If-None-Match`` short-circuits to 304."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    async with conn() as c:
        try:
            result = await core.get_artifact(c, actor, drive_id, artifact_id)
        except Exception as exc:
            raise _mapping_error(exc) from None
    etag = _etag(result["revision"])
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private"})
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    return result


@router.patch(
    "/drives/{drive_id}/artifacts/{artifact_id}",
    response_model=ArtifactOut,
    operation_id="artifacts_update",
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def update_artifact(
    drive_id: str,
    artifact_id: str,
    request: Request,
    body: ArtifactUpdateIn,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> ArtifactOut:
    """Rename / move / set metadata or labels. At least one field required."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            # Specifying a destination parent (a move) requires editor on THAT
            # folder — even if it equals the current parent — so a principal
            # holding only a direct artifact grant cannot move the artifact
            # into a namespace they hold no capability on.
            if body.parent_id is not None:
                await core._ensure_drive(c, actor, drive_id)
                await authz.require(
                    c, actor=actor, drive_id=drive_id,
                    resource_type="folder", resource_id=body.parent_id,
                    minimum="editor",
                )
            result = await core.update_artifact(
                c, actor, drive_id, artifact_id,
                name=body.name, parent_id=body.parent_id, metadata=body.metadata,
                labels=body.labels, changed=body.changed, if_match=if_match,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            200,
            {"ETag": _etag(result["revision"]), "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="PATCH",
        path=f"/v0/drives/{drive_id}/artifacts/{artifact_id}",
        request_hash=_body_hash(body),
        execute=execute,
        replay_guard=_folder_editor_guard(actor, drive_id, body.parent_id),
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.delete(
    "/drives/{drive_id}/artifacts/{artifact_id}",
    response_model=ArtifactOut,
    operation_id="artifacts_delete",
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def delete_artifact(
    drive_id: str,
    artifact_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> ArtifactOut:
    """Soft-delete one artifact (its versions stay)."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.soft_delete_artifact(
                c, actor, drive_id, artifact_id, if_match=if_match
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            200,
            {"ETag": _etag(result["revision"]), "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="DELETE",
        path=f"/v0/drives/{drive_id}/artifacts/{artifact_id}",
        request_hash=_body_hash(None),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.post(
    "/drives/{drive_id}/artifacts/{artifact_id}/restore",
    response_model=ArtifactOut,
    operation_id="artifacts_restore",
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id", include_deleted=True)),
        Depends(known_params()),
    ],
)
async def restore_artifact(
    drive_id: str,
    artifact_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> ArtifactOut:
    """Restore a soft-deleted artifact atomically."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.restore_artifact(
                c, actor, drive_id, artifact_id, if_match=if_match
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            200,
            {"ETag": _etag(result["revision"]), "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/artifacts/{artifact_id}/restore",
        request_hash=_body_hash(None),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/content",
    responses={
        304: {"description": "If-None-Match matched."},
        307: {"description": "Redirect to a short-lived signed URL."},
    },
    operation_id="artifacts_content",
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def read_artifact_content(
    drive_id: str,
    artifact_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    """Download the head version's bytes — stream or 307 signed URL."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    async with conn() as c:
        row = await core.head_content(c, actor, drive_id, artifact_id)
    if row is None:
        raise V0ApiError(404, "ARTIFACT_NOT_FOUND", "no such artifact in this drive")
    etag = _etag(row["version_id"])
    if _etag_matches(if_none_match, etag):
        return Response(
            status_code=304,
            headers={"Cache-Control": "private", "ETag": etag},
        )
    # The shared handler, not a second copy of it. This route carried a
    # near-verbatim duplicate of `_bytes_response`, which is how the
    # transfer-bucket gap survived on the artifact HEAD route after the
    # versions route was reasoned about: one rule, two implementations, and
    # only one of them ever gets the fix.
    return await _bytes_response(
        gcs_object=row["storage_object"],
        gcs_bucket=row["storage_bucket"],
        gcs_generation=row["storage_generation"],
        size_bytes=row["size_bytes"],
        content_type=row["content_type"],
        filename=row["artifact_name"],
        etag=etag,
        base_headers={
            "Cache-Control": "private",
            "X-Content-Type-Options": "nosniff",
        },
        actor=actor,
        drive_id=drive_id,
    )


@router.post(
    "/drives/{drive_id}/artifacts/{artifact_id}/copy",
    status_code=201,
    response_model=ArtifactOut,
    operation_id="artifacts_copy",
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def copy_artifact(
    drive_id: str,
    artifact_id: str,
    body: ArtifactCopyIn,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> ArtifactOut:
    """Copy one artifact within the same drive.

    Cross-drive copy is out of v0 scope and rejected (400 INVALID_ARGUMENT).
    ``destination_drive_id`` must equal the source drive when present.
    Materializes the artifact + its selected version synchronously → 201.
    ``If-Match`` is optional; when present it is validated against the source
    revision (412 stale)."""
    _require_scope(actor, _SCOPE_READ)
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)

    destination_drive_id = body.destination_drive_id or drive_id
    if destination_drive_id != drive_id:
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "cross-drive copy is not available in v0"
        )

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            # Writing into the destination parent requires editor there too.
            await core._ensure_drive(c, actor, drive_id)
            if body.destination_parent_id is not None:
                await authz.require(
                    c, actor=actor, drive_id=destination_drive_id,
                    resource_type="folder", resource_id=body.destination_parent_id,
                    minimum="editor",
                )
            result = await core.copy_artifact(
                c, actor, drive_id, artifact_id,
                destination_drive_id=destination_drive_id,
                destination_parent_id=body.destination_parent_id,
                destination_name=body.destination_name,
                version_id=body.version_id,
                destination_etag=if_match,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            201,
            {"Location": _artifact_location(drive_id, result["id"]),
             "ETag": _etag(result["revision"]),
             "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/artifacts/{artifact_id}/copy",
        request_hash=_body_hash(body),
        execute=execute,
        replay_guard=_folder_editor_guard(
            actor, destination_drive_id, body.destination_parent_id
        ),
    )
    response.status_code = status
    response.headers.update(headers)
    return payload
