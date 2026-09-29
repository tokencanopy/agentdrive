"""The always-direct download-capability mint (B3 packet 4): 1 operation.

Governing contract: TokenCanopy
``docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md``
§5.7/§5.8/§8. An explicit, narrow C3 mint — it does NOT reuse the
conditional ``GET .../content`` (which may stream bytes), and it has no
redirect, proxy-stream, viewer, or null-target fallback: the response is a
fresh signed GCS GET target or a fail-closed error.

Wire discipline, shared with the packet-3 upload surface (§5.1) whose
helpers this module deliberately reuses so the two transfer surfaces cannot
drift apart:

  * Strict JSON: duplicate keys, unknown fields/discriminators/queries, and
    malformed ids are ``400 INVALID_REQUEST``; ``Accept`` must include JSON
    (``406``); a non-JSON body is ``415``.
  * Fail closed while disabled/unready: exactly ``503 TRANSFER_DISABLED``.
  * ``Idempotency-Key`` is FORBIDDEN (manifest ``idempotency_class:
    forbidden``): a supplied key is ``400 INVALID_REQUEST`` and no
    idempotency record is ever created — safely replaying a bearer target
    would require storing it, so every retry reauthorizes and re-mints.
  * Every miss that could enumerate (wrong workspace/drive, unknown
    artifact/version, cross-artifact version, local denial) is the same 404.
  * The signed URL is validated semantically before disclosure and appears
    in no log, error body, or durable state; ``expires_at`` derives from the
    URL's own signing time + expiry. The response's ``nosniff`` header
    governs THIS JSON response only — the signed ``response-content-type``/
    ``response-content-disposition`` query parameters are the download
    content-safety control, and the disclosed URL is a bearer capability
    (no one-time-use or audience enforcement after disclosure).
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import settings
from ..core import v0_download_capabilities as core
from ..core.timestamps import to_rfc3339
from ..core.usage.gate import LimitExceeded, usage_gate
from ..db import conn
from ..identity.actor import V0ActorContext
from ..storage_transfers import (
    DownloadSigningUnavailableError,
    GenerationDownloadSigner,
)
from .v0_deps import v0_actor
from .v0_errors import V0ApiError, public_validation_details
from .v0_models import DownloadCapabilityOut
from .v0_rate_limit import enforce_v0_rate_limit

# Shared §5.1 wire plumbing from the packet-3 surface — one implementation
# for both transfer surfaces (accept/query/id/scope checks, the uniform
# 404, the disabled/readiness gate, and the transfer-control rate windows).
from .v0_uploads import (
    _charge_actor_rate,
    _charge_drive_rate,
    _check_accept,
    _check_ids,
    _check_no_query,
    _invalid,
    _json_response,
    _not_found,
    _parse_strict_json,
    _require_ready,
    _require_scope,
    _usage_limit_error,
)

router = APIRouter(
    prefix="/v0",
    tags=["downloads"],
    dependencies=[Depends(enforce_v0_rate_limit)],
)

_SCOPE_READ = "content:read"

# Bound on the mint control body — a strict JSON envelope well under 1 KiB
# in practice; anything near this bound is not a valid request.
MAX_MINT_BODY_BYTES = 16 * 1024

_ARTIFACT_ID_PATTERN = r"^art_[a-f0-9]{16}$"
_VERSION_ID_PATTERN = r"^ver_[a-f0-9]{16}$"


# ── request models (strict) ─────────────────────────────────────────────────


class _TargetArtifactIn(BaseModel):
    kind: str = Field(pattern=r"^artifact$")
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    model_config = ConfigDict(extra="forbid", strict=True)


class _TargetVersionIn(BaseModel):
    kind: str = Field(pattern=r"^version$")
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    version_id: str = Field(pattern=_VERSION_ID_PATTERN)
    model_config = ConfigDict(extra="forbid", strict=True)


class _MintIn(BaseModel):
    target: _TargetArtifactIn | _TargetVersionIn
    model_config = ConfigDict(extra="forbid", strict=True)


# ── signer factory (test seam) ──────────────────────────────────────────────

_signer_singleton: GenerationDownloadSigner | None = None


def capability_signer() -> GenerationDownloadSigner:
    """The configured capability signer. Module-level and monkeypatchable —
    the test seam the design's fake-signer evidence rides on. Only reachable
    when the transfer configuration is complete (readiness gate), so the
    constructor's exact-origin validation always has real values."""
    global _signer_singleton
    if _signer_singleton is None:
        from ..storage_transfers import build_download_signer

        _signer_singleton = build_download_signer()
    return _signer_singleton


def reset_capability_signer() -> None:
    """Test seam: drop the cached signer so settings changes take."""
    global _signer_singleton
    _signer_singleton = None


# ── the mint ────────────────────────────────────────────────────────────────


def _signing_unavailable() -> V0ApiError:
    # Coordinate-free on purpose (§8): no bucket, object, generation, or
    # provider/exception text may reach the envelope. Deliberately NO
    # Retry-After: B8 owns every retry hint, and no configured value
    # exists for this refusal (review should-fix).
    return V0ApiError(
        503, "DOWNLOAD_SIGNING_UNAVAILABLE",
        "the direct download signer is unavailable; there is no fallback",
    )


async def _read_bounded_body(request: Request) -> bytes:
    """Collect the mint's JSON control body under a REAL 16 KiB bound.

    Review blocker 3: ``request.body()`` buffers the whole request before
    any length check, turning the nominal bound into an authenticated
    memory-exhaustion path. This reads incrementally instead: a declared
    over-limit Content-Length is refused before a single body byte is
    consumed; malformed/contradictory declarations are refused
    conservatively; an ABSENT declaration (chunked transfer) falls through
    to the streaming count, which stops within one chunk of the bound and
    stays authoritative even when a declaration lies low."""
    declared = request.headers.getlist("content-length")
    if declared:
        values = {value.strip() for value in declared}
        if len(values) != 1:
            raise _invalid("contradictory Content-Length headers")
        value = next(iter(values))
        # Digit-count bound before int(): the conversion itself raises past
        # 4300 digits, and no honest length needs more than 12.
        if len(value) > 12 or not value.isascii() or not value.isdigit():
            raise _invalid("malformed Content-Length header")
        if int(value) > MAX_MINT_BODY_BYTES:
            raise _invalid("the request body exceeds the control-body bound")
    received = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_MINT_BODY_BYTES:
            # Stop HERE: the remaining request is never read or buffered.
            raise _invalid("the request body exceeds the control-body bound")
        chunks.append(chunk)
    return b"".join(chunks)


def _validated_mint_body(raw: bytes) -> _MintIn:
    parsed = _parse_strict_json(raw)
    if not isinstance(parsed, dict):
        raise _invalid("the request body must be a JSON object")
    try:
        return _MintIn.model_validate(parsed)
    except ValidationError as exc:
        raise _invalid(
            "The request does not match the download-capability contract.",
            details=public_validation_details(exc.errors()),
        ) from None


@router.post(
    "/drives/{drive_id}/download-capabilities",
    status_code=200,
    response_model=DownloadCapabilityOut,
    operation_id="download_capabilities_create",
)
async def create_download_capability(
    drive_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
) -> Response:
    """Mint one fresh, generation-pinned signed GET target for the current
    artifact head or one owned version. 200 only; every call reauthorizes
    and re-mints."""
    await _require_ready()
    _require_scope(actor, _SCOPE_READ)
    _check_accept(request)
    _check_no_query(request)
    _check_ids(drive_id)
    if request.headers.get("Idempotency-Key") is not None:
        # §5.7: idempotency_class is FORBIDDEN — replaying a bearer target
        # would require storing it. No idempotency record is created (this
        # surface never touches the idempotency ledger at all).
        raise _invalid(
            "Idempotency-Key is not accepted on this operation; every "
            "request mints a fresh download target"
        )
    await _charge_actor_rate(actor)

    media_parts = request.headers.get("content-type", "").split(";")
    if media_parts[0].strip().lower() != "application/json":
        raise V0ApiError(
            415, "UNSUPPORTED_MEDIA_TYPE",
            "This operation requires an application/json body.",
        )
    # §5.1: the documented body encoding is JSON in UTF-8. Only the exact
    # forms are accepted: no parameters, or a single charset=utf-8.
    for parameter in media_parts[1:]:
        name, _, value = parameter.partition("=")
        if not name.strip():
            continue
        if (
            name.strip().lower() != "charset"
            or value.strip().strip('"').lower() != "utf-8"
        ):
            raise V0ApiError(
                415, "UNSUPPORTED_MEDIA_TYPE",
                "This operation accepts application/json with at most a "
                "charset=utf-8 parameter.",
            )
    raw = await _read_bounded_body(request)
    body = _validated_mint_body(raw)
    target = body.target
    version_id = (
        target.version_id if isinstance(target, _TargetVersionIn) else None
    )

    async with conn() as c, c.transaction():
        try:
            source = await core.resolve_download_source(
                c, actor=actor, drive_id=drive_id,
                artifact_id=target.artifact_id, version_id=version_id,
            )
        except core.DownloadTargetNotFoundError:
            raise _not_found() from None
        except core.DownloadCoordinatesUnavailableError:
            raise _signing_unavailable() from None
        try:
            await usage_gate.charge_private_download(
                c,
                actor=actor,
                drive_id=drive_id,
                operation_key=f"download-capability:{secrets.token_hex(16)}",
                size_bytes=source.size_bytes,
            )
        except LimitExceeded as exc:
            raise _usage_limit_error(exc) from None
    await _charge_drive_rate(actor, drive_id)

    ttl = settings.direct_download_capability_ttl_seconds
    if ttl is None:
        raise _signing_unavailable()
    try:
        # A signer that cannot even be CONSTRUCTED (a configured namespace
        # violating the constructor's invariants) is configuration
        # unavailability — the typed 503, never an escaping ValueError.
        signer = capability_signer()
    except Exception:
        raise _signing_unavailable() from None
    try:
        capability = await signer.sign_capability(
            bucket=source.bucket,
            object_name=source.object_name,
            generation=source.generation,
            media_type=core.SAFE_ATTACHMENT_MEDIA_TYPE,
            filename=source.filename,
            ttl_seconds=ttl,
        )
    except DownloadSigningUnavailableError:
        raise _signing_unavailable() from None

    payload = DownloadCapabilityOut.model_validate({
        "download": {
            "artifact_id": source.artifact_id,
            "version_id": source.version_id,
            "expires_at": to_rfc3339(capability.expires_at),
            "target": {
                "url": capability.url,
                "method": "GET",
                # No signed request header is required; ordinary extra
                # browser headers are not prohibited (§5.7).
                "required_headers": {},
                "content_disposition": capability.disposition,
            },
        }
    }).model_dump(mode="json")
    return _json_response(
        200,
        payload,
        {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
