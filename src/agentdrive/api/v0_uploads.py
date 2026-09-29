"""Direct-upload session controls (B3 packet 3): 4 operations.

Governing contract: TokenCanopy
``docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md``
§5/§6/§7/§8, as amended by the 2026-08-20 browser-initiated-transfer
amendment. The external GCS byte transfer is NOT an AgentDrive operation;
there is no byte endpoint here — begin returns the one V4-SIGNED initiation
target (the CLIENT performs the GCS initiation POST itself and reads the
session URI from `Location`; that browser-context initiation is what makes
GCS bless the session for CORS) and complete adopts the finalized object.

Wire discipline specific to this surface (§5.1), deliberately stricter
than the older verticals where the spec pins it:

  * Strict JSON: duplicate keys, unknown fields/discriminators/queries, and
    malformed ids are ``400 INVALID_REQUEST``; ``Accept`` must include JSON
    (``406 NOT_ACCEPTABLE``); a non-JSON begin body is ``415``.
  * Fail closed while disabled: every control answers exactly
    ``503 TRANSFER_DISABLED`` (no ``Retry-After`` — operator enablement has
    no honest client retry time), with no inline or proxy fallback.
  * The resumable target is disclosed ONCE, from request memory, only in
    the first successful begin response. It is never persisted, logged,
    stored in the idempotency ledger (a filtered completion guard makes
    that structurally loud), or reissued by any replay/status path.
  * Every control reauthorizes: token scope ∩ local editor capability on
    the target folder/artifact, plus the session's initiating-principal
    binding; every miss is the same anti-enumerating 404.

The provider seam is packet 2's ``XmlTransferStorage`` behind the
module-level ``transfer_storage()`` factory (tests inject a fake). All
transition/recovery semantics live in ``core.v0_uploads``; this module owns
HTTP mapping and the idempotency choreography, which is deliberately NOT
``_run_mutation``: the sagas span several transactions and their replay
rules are operation-specific (§6).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
)

from ..config import settings
from ..core import idempotency, ids
from ..core import v0_authz as authz
from ..core import v0_transfer_rate as core_transfer_rate
from ..core import v0_uploads as core
from ..core.timestamps import to_rfc3339
from ..core.usage.gate import LimitExceeded, usage_gate
from ..db import DBConn, conn
from ..identity.actor import V0ActorContext
from ..storage_transfers import (
    InvalidChecksumError,
    XmlResumableRequest,
    XmlTransferStorage,
    canonical_crc32c,
)
from .v0_deps import v0_actor
from .v0_errors import V0ApiError, public_validation_details
from .v0_models import UploadBeginOut, UploadSessionOut
from .v0_rate_limit import enforce_v0_rate_limit

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v0", tags=["uploads"], dependencies=[Depends(enforce_v0_rate_limit)]
)

_SCOPE_WRITE = "content:write"

# Bound on the begin control body — a strict JSON envelope a few hundred
# bytes long in practice; anything near this bound is not a valid request.
MAX_BEGIN_BODY_BYTES = 64 * 1024

# Bounded leases for the one-owner transition fences (§6). Module constants
# so tests can shrink them to exercise stale-lease attachment.
INITIATION_LEASE_SECONDS = 60
COMPLETE_LEASE_SECONDS = 60
CANCEL_LEASE_SECONDS = 30

# Bounded client retry hints (seconds). Real launch values are B8's.
INCOMPLETE_RETRY_AFTER = 5
UNAVAILABLE_RETRY_AFTER = 10
BUSY_RETRY_AFTER = 5

# Bare type/subtype only (RFC 7230 tokens): no parameters, so a declared
# media type can never smuggle caller-controlled disposition parameters.
_MEDIA_TYPE = re.compile(
    r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+$"
)

_FOLDER_ID_PATTERN = r"^fld_[a-f0-9]{16}$"
_ARTIFACT_ID_PATTERN = r"^art_[a-f0-9]{16}$"


# ── request models (strict) ─────────────────────────────────────────────────


class _ChecksumIn(BaseModel):
    algorithm: str
    value: str
    model_config = ConfigDict(extra="forbid", strict=True)


class _ContentIn(BaseModel):
    size_bytes: StrictInt
    media_type: str
    checksum: _ChecksumIn
    model_config = ConfigDict(extra="forbid", strict=True)


class _TargetArtifactIn(BaseModel):
    kind: str = Field(pattern=r"^artifact$")
    parent_folder_id: str = Field(pattern=_FOLDER_ID_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("name", mode="before")
    @classmethod
    def canonical_name(cls, value: str) -> str:
        from ..core.v0_folders import validate_name

        return validate_name(value)


class _TargetVersionIn(BaseModel):
    kind: str = Field(pattern=r"^version$")
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    model_config = ConfigDict(extra="forbid", strict=True)


class _BeginIn(BaseModel):
    target: _TargetArtifactIn | _TargetVersionIn
    content: _ContentIn
    model_config = ConfigDict(extra="forbid", strict=True)


# ── provider adapter factory ────────────────────────────────────────────────

_storage_singleton: XmlTransferStorage | None = None


def transfer_storage() -> XmlTransferStorage:
    """The configured transfer adapter. Module-level and monkeypatchable —
    the test seam the design's fake-provider evidence rides on. Only
    reachable when the transfer configuration is complete (readiness gate),
    so the constructor's exact-origin validation always has real values."""
    global _storage_singleton
    if _storage_singleton is None:
        from ..storage_transfers import build_transfer_storage

        _storage_singleton = build_transfer_storage()
    return _storage_singleton


def reset_transfer_storage() -> None:
    """Test seam: drop the cached adapter so settings changes take."""
    global _storage_singleton
    _storage_singleton = None


# ── transfer-control rate windows (§9) ──────────────────────────────────────

# Fixed one-minute windows per (dimension, id), held in POSTGRES — shared
# across instances, unlike the general per-IP v0 limiter in `ratelimit.py`,
# which stays in process memory on purpose (it fires as middleware before any
# DB lookup, so backing it with the database would make every REJECTED
# request cost a write — a limiter that amplifies the flood it exists to
# stop). Values come from the B8-owned settings; unreachable while transfer
# is disabled. See `core/v0_transfer_rate.py` and migration 0058.


async def reset_transfer_rate_limits() -> None:
    """Test seam: clear the transfer rate windows."""
    async with conn() as c:
        await core_transfer_rate.reset(c)


async def _charge_transfer_rate(
    dimensions: list[tuple[str, str, int | None]],
    c: DBConn | None = None,
) -> None:
    """Fixed-window charge for the given dimensions. Every dimension is
    EVALUATED before any counter commits, so a rejected dimension never
    poisons a sibling window with a phantom charge.

    `c` is the caller's connection when the call site already holds one.
    Passing it is not an optimization — opening a second pooled connection
    underneath an open transaction deadlocks a 10-connection pool at
    concurrency 80 (see `core/v0_transfer_rate`). Call sites that hold no
    connection pass nothing and get their own.
    """
    if c is None:
        async with conn() as own:
            exhausted = await core_transfer_rate.charge(own, dimensions)
    else:
        exhausted = await core_transfer_rate.charge(c, dimensions)
    if exhausted is not None:
        raise V0ApiError(
            429, "RATE_LIMITED",
            "the configured transfer-control rate is exhausted",
            details={"limit_name": f"direct_transfer_rate_{exhausted}"},
            headers={"Retry-After": "60"},
        )


async def _charge_actor_rate(
    actor: V0ActorContext, c: DBConn | None = None
) -> None:
    """The caller-identity dimensions: chargeable before target resolution
    because they are keyed by the AUTHENTICATED workspace/principal — a
    caller can only ever spend its own windows here.

    Every call site charges these BEFORE opening a transaction, so they
    commit on their own connection and stick even when the operation they
    guard then fails. That is the property that makes them the abuse fence:
    a caller cannot buy unlimited attempts by making them all fail.
    """
    await _charge_transfer_rate([
        ("principal", f"{actor.workspace_id}/{actor.subject}",
         settings.direct_transfer_rate_principal),
        ("workspace", actor.workspace_id,
         settings.direct_transfer_rate_workspace),
    ], c)


async def _charge_drive_rate(
    actor: V0ActorContext, drive_id: str, c: DBConn | None = None
) -> None:
    """The drive dimension. MUST be called only after the drive is resolved
    inside the caller's workspace and locally authorized — an unauthorized
    caller must never be able to spend a foreign drive's window. The key is
    workspace-scoped as a second fence.

    Unlike the actor windows, this one is charged on the caller's connection
    at the three upload call sites, because proving the drive is in-workspace
    requires being inside that transaction already. Where that transaction is
    explicit (`begin`), a later failure rolls this charge back with it. That
    asymmetry is accepted rather than fixed: making it autonomous needs a
    second pooled connection under an advisory lock, which deadlocks, and the
    windows that actually bound an abusive caller — principal and workspace —
    are unconditional above.
    """
    await _charge_transfer_rate([
        ("drive", f"{actor.workspace_id}/{drive_id}",
         settings.direct_transfer_rate_drive),
    ], c)


# ── shared wire plumbing ────────────────────────────────────────────────────


def _disabled() -> V0ApiError:
    # No Retry-After: operator enablement has no honest client retry time.
    return V0ApiError(
        503, "TRANSFER_DISABLED",
        "direct transfer is not enabled on this deployment",
    )


async def _require_ready() -> None:
    """Fail closed unless transfer is enabled AND runtime-ready (§9): the
    flag, the boot-validated policy, and zero unresolved generation rows.

    Deliberately NOT cached: the exact zero-unresolved-rows gate holds per
    request (review round 3). The disabled flag short-circuits before any
    database work, and the unresolved-rows predicate is backed by the
    partial index `artifact_versions_unresolved_coordinates` (migration
    0050) so the uncached gate stays O(unresolved)."""
    if not settings.direct_transfer_enabled:
        raise _disabled()
    async with conn() as c:
        ready, _reasons = await core.transfer_readiness(c)
    if not ready:
        raise _disabled()


def _require_scope(actor: V0ActorContext, scope: str) -> None:
    if not actor.can(scope):
        raise V0ApiError(
            403, "PERMISSION_DENIED", f"the token does not carry the {scope} scope"
        )


def _invalid(message: str, *, details: Any = None) -> V0ApiError:
    return V0ApiError(400, "INVALID_REQUEST", message, details=details)


def _not_found() -> V0ApiError:
    """The ONE anti-enumerating miss for this surface (§4/§5.8/§8): wrong
    workspace, wrong drive, unknown upload/target, foreign principal, and
    local denial all answer this exact envelope."""
    return V0ApiError(404, "NOT_FOUND", "no such resource in this drive")


def _check_ids(drive_id: str, upload_id: str | None = None) -> None:
    # §5.1: malformed ids on this surface are INVALID_REQUEST.
    if not ids.is_valid(drive_id, "drv"):
        raise _invalid("malformed drive id")
    if upload_id is not None and not ids.is_valid(upload_id, "upld"):
        raise _invalid("malformed upload id")


def _check_accept(request: Request) -> None:
    accept = request.headers.get("accept")
    if not accept:
        return
    for member in accept.split(","):
        parts = member.split(";")
        media = parts[0].strip().lower()
        if media not in ("*/*", "application/*", "application/json"):
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if quality > 0:
            return
    raise V0ApiError(406, "NOT_ACCEPTABLE", "Accept must include application/json")


def _check_no_query(request: Request) -> None:
    if request.query_params:
        raise _invalid(
            f"unknown query parameter(s): {sorted(set(request.query_params))}"
        )


def _require_key(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise V0ApiError(
            400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required"
        )
    return idempotency_key


async def _require_empty_body(request: Request) -> None:
    if await request.body():
        raise _invalid("this operation accepts no request body")


class _DuplicateJsonKeyError(ValueError):
    pass


def _parse_strict_json(raw: bytes) -> Any:
    def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise _DuplicateJsonKeyError("duplicate JSON key")
        return dict(pairs)

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except _DuplicateJsonKeyError:
        raise _invalid("duplicate JSON keys are not accepted") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid("the request body is not valid JSON") from None


_SESSION_ETAG_VALUE = re.compile(r"^(upld_[a-f0-9]{16})\.([0-9]{1,18})$")


def _parse_session_if_match(header: str, upload_id: str) -> int:
    """Parse cancel's If-Match into the exact pinned session revision.

    The cancel fence requires THE session's current strong ETag (§5.1), so
    this is deliberately stricter than RFC 9110 list matching: `*` and
    multi-member lists cannot pin a revision (400 INVALID_REQUEST); a weak
    tag or an ETag naming another session can never match (412, with no
    current ETag disclosed)."""
    value = header.strip()
    if value == "*":
        raise _invalid(
            "If-Match must carry the session's current strong ETag, not *"
        )
    if "," in value:
        raise _invalid(
            "If-Match must carry exactly one session ETag on this operation"
        )
    if value.startswith("W/"):
        raise V0ApiError(
            412, "PRECONDITION_FAILED",
            "the upload session changed after it was read",
        )
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    matched = _SESSION_ETAG_VALUE.fullmatch(value)
    if matched is None or matched.group(1) != upload_id:
        raise V0ApiError(
            412, "PRECONDITION_FAILED",
            "the upload session changed after it was read",
        )
    return int(matched.group(2))


def _location(path: str) -> str:
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    return f"{origin}{path}"


def _base_headers(row: Any, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {
        "ETag": core.session_etag(row),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        **(extra or {}),
    }


def _session_payload(row: Any, *, transfer: dict[str, Any] | None = None) -> dict:
    """The §5.3 non-secret representation. ``transfer`` appears only when
    the caller passes the one in-memory target (the fresh 201 begin)."""
    from ..core.timestamps import to_rfc3339

    if row["target_kind"] == "artifact":
        target: dict[str, Any] = {
            "kind": "artifact",
            "parent_folder_id": row["parent_folder_id"],
            "name": row["artifact_name"],
        }
    else:
        target = {"kind": "version", "artifact_id": row["artifact_id"]}
    state = row["state"]
    result = None
    if state == "completed":
        result = {
            "kind": row["target_kind"],
            "artifact_id": row["result_artifact_id"],
            "version_id": row["result_version_id"],
            "revision": row["result_revision"],
        }
    upload: dict[str, Any] = {
        "id": row["id"],
        "drive_id": row["drive_id"],
        "state": state,
        "target": target,
        "content": {
            "size_bytes": row["declared_size_bytes"],
            "media_type": row["declared_media_type"],
            "checksum": {"algorithm": "crc32c", "value": row["declared_crc32c"]},
        },
        "expires_at": to_rfc3339(row["expires_at"]),
        "target_disclosed": row["target_disclosed"],
        # §5.3: a pure function of response class and publication state —
        # false only on the one target-carrying begin response and on
        # completing/cancelling/terminal states.
        "restart_required": state in ("preparing", "active") and transfer is None,
        "result": result,
        "failure": {"code": row["failure_code"]} if row["failure_code"] else None,
        "cleanup": {"state": row["cleanup_state"]},
    }
    if transfer is not None:
        upload["transfer"] = transfer
        return UploadBeginOut.model_validate({"upload": upload}).model_dump(mode="json")
    return UploadSessionOut.model_validate({"upload": upload}).model_dump(mode="json")


def _json_response(status: int, body: dict, headers: dict[str, str]) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=body, headers=headers,
        media_type="application/json; charset=utf-8",
    )


def _error_body(code: str, message: str, details: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


def _error_response(
    status: int, code: str, message: str,
    *, details: Any = None, retry_after: int | None = None,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return _json_response(status, _error_body(code, message, details), headers)


# ── filtered idempotency storage (§7) ───────────────────────────────────────

_ALLOWED_STORED_HEADERS = frozenset(
    {"ETag", "Location", "Cache-Control", "X-Content-Type-Options", "Retry-After"}
)
_FORBIDDEN_BODY_KEYS = frozenset(
    {"url", "transfer", "authorization", "cookie", "token", "required_headers",
     "signature", "set-cookie"}
)


def _assert_storable(value: Any) -> None:
    """Refuse to store anything that could carry the bearer target: a body
    with a forbidden field name, a value naming the provider endpoint, or a
    header outside the fixed safe set. Fail closed — a violation is a bug,
    never something to silently strip."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if str(key).lower() in _FORBIDDEN_BODY_KEYS:
                raise RuntimeError(
                    "refusing to store a secret-capable field in the "
                    "idempotency ledger"
                )
            _assert_storable(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            _assert_storable(inner)
    elif isinstance(value, str):
        endpoint = settings.direct_transfer_upload_endpoint
        if endpoint and endpoint in value:
            raise RuntimeError(
                "refusing to store a provider endpoint value in the "
                "idempotency ledger"
            )


async def _store_result(
    c: Any, *, owner_id: str, status: int, body: dict, headers: dict[str, str]
) -> None:
    """Filtered ledger store. A VANISHED claim (reaped by the idempotency
    crash lease under a long-running saga) is tolerated: the surrounding
    product transaction must still commit — the terminal session state
    answers later same-key retries — and a correct publication must never
    roll back over bookkeeping (review round 4)."""
    _assert_storable(body)
    unknown = set(headers) - _ALLOWED_STORED_HEADERS
    if unknown:
        raise RuntimeError(f"refusing to store non-allowlisted headers: {sorted(unknown)}")
    try:
        await idempotency.complete(
            c, owner_id=owner_id, status=status, body=body, headers=headers
        )
    except idempotency.ClaimVanishedError:
        log.warning(
            "at=upload.claim_vanished owner=%s status=%s", owner_id, status
        )


async def _abandon(owner_id: str) -> None:
    try:
        async with conn() as c:
            await idempotency.abandon(c, owner_id=owner_id)
    except idempotency.ClaimVanishedError:
        # Benign: already stored, already abandoned by an earlier layer, or
        # reaped by the crash lease — the key self-heals either way.
        log.debug("at=upload.claim_already_settled owner=%s", owner_id)
    except Exception as exc:
        # Best effort by design (the claim self-heals via its crash lease),
        # but never silent: a persistent failure here burns keys for the
        # lease window. Only the safe record id is logged.
        log.warning(
            "at=upload.abandon_failed owner=%s error_class=%s",
            owner_id, type(exc).__name__,
        )


def _replay(stored: idempotency.StoredResponse) -> JSONResponse:
    # §5.5: a same-key COMPLETED replay answers 200, not the stored 201 —
    # the publication is no longer fresh. Every other stored status replays
    # verbatim (the durable terminal 422/412/409 results).
    status = 200 if stored.status == 201 else stored.status
    headers = {**stored.headers, "Idempotent-Replay": "true"}
    return _json_response(status, stored.body, headers)


# ── shared authorization/resolution ─────────────────────────────────────────


async def _resolve_session(
    c: Any, actor: V0ActorContext, drive_id: str, upload_id: str,
    *, for_update: bool = False,
) -> Any:
    """Reauthorize and fetch one session, anti-enumeration preserved (§8):
    wrong-workspace drive, unknown upload, foreign principal, and revoked
    local capability all collapse to the same 404."""
    workspace = await c.fetchval(
        "SELECT workspace_id FROM drives WHERE id = $1", drive_id
    )
    if workspace is None or workspace != actor.workspace_id:
        raise _not_found()
    try:
        if for_update:
            row = await core.get_session_locked(c, drive_id=drive_id, upload_id=upload_id)
        else:
            row = await core.get_session(c, drive_id=drive_id, upload_id=upload_id)
    except core.UploadSessionNotFoundError:
        raise _not_found() from None
    if (
        row["principal_id"] != actor.subject
        or row["principal_type"] != actor.subject_type
    ):
        # The session is bound to its initiating principal (§8): a status
        # read must not turn an upload id into a shared capability.
        raise _not_found()
    try:
        if row["target_kind"] == "artifact":
            await authz.require(
                c, actor=actor, drive_id=drive_id,
                resource_type="folder", resource_id=row["parent_folder_id"],
                minimum="editor", include_deleted=True,
            )
        else:
            await authz.require(
                c, actor=actor, drive_id=drive_id,
                resource_type="artifact", resource_id=row["artifact_id"],
                minimum="editor", include_deleted=True,
            )
    except authz.NotAuthorizedError:
        raise _not_found() from None
    return row


# ── begin ───────────────────────────────────────────────────────────────────


def _validated_begin_body(raw: bytes) -> _BeginIn:
    parsed = _parse_strict_json(raw)
    if not isinstance(parsed, dict):
        raise _invalid("the request body must be a JSON object")
    try:
        body = _BeginIn.model_validate(parsed)
    except ValidationError as exc:
        raise _invalid(
            "The request does not match the upload-session contract.",
            details=public_validation_details(exc.errors()),
        ) from None
    content = body.content
    if not _MEDIA_TYPE.fullmatch(content.media_type):
        raise _invalid("media_type must be a bare IANA type/subtype")
    if content.checksum.algorithm != "crc32c":
        raise _invalid("checksum.algorithm must be crc32c")
    try:
        canonical_crc32c(content.checksum.value)
    except InvalidChecksumError as exc:
        raise _invalid(str(exc)) from None
    size = content.size_bytes
    if size < 0:
        raise _invalid("size_bytes must not be negative")
    ceilings = {
        "direct_transfer_max_bytes": settings.direct_transfer_max_bytes,
        "max_file_bytes": settings.max_file_bytes,
    }
    limit_name, maximum = min(
        ((name, value) for name, value in ceilings.items() if value is not None),
        key=lambda item: item[1],
    )
    if maximum is not None and size > maximum:
        raise V0ApiError(
            413, "PAYLOAD_TOO_LARGE",
            "the declared size exceeds the direct-transfer ceiling",
            details={
                "limit_name": limit_name,
                "used": 0,
                "reserved": 0,
                "limit": maximum,
                "requested": size,
                "remaining": maximum,
                "reset_at": None,
            },
        )
    if size == 0 and not settings.direct_transfer_allow_zero_bytes:
        raise _invalid("zero-byte uploads are not enabled")
    minimum = settings.direct_transfer_min_bytes
    if minimum is not None and size < minimum:
        raise _invalid("the declared size is below the direct-transfer minimum")
    return body


def _usage_limit_error(exc: LimitExceeded) -> V0ApiError:
    decision = exc.decision
    reset_at = decision.reset_at
    remaining = max(decision.limit - decision.used - decision.reserved, 0)
    details = {
        "limit_name": "_".join(
            part
            for part in (
                decision.metric.value,
                decision.period,
                decision.scope_type,
            )
            if part
        ),
        "used": decision.used,
        "reserved": decision.reserved,
        "limit": decision.limit,
        "requested": decision.requested,
        "remaining": remaining,
        "reset_at": to_rfc3339(reset_at) if reset_at else None,
    }
    retry_after = (
        max(1, int((reset_at - datetime.now(UTC)).total_seconds()))
        if reset_at
        else 1
    )
    return V0ApiError(
        429,
        "BANDWIDTH_LIMIT_EXCEEDED",
        "the workspace transfer allowance is exhausted",
        details=details,
        headers={"Retry-After": str(retry_after)},
    )


def _begin_request_hash(body: _BeginIn, if_match: str | None) -> str:
    payload = {"body": body.model_dump(mode="json"), "if_match": if_match}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _new_object_keys(upload_id: str) -> tuple[str, str, str]:
    """Server-selected scratch/final keys and the opaque adoption marker.
    Never caller input; opaque suffixes so a key is not guessable from the
    upload id alone."""
    import secrets

    scratch = (
        f"{settings.direct_transfer_scratch_prefix}"
        f"{upload_id}-{secrets.token_hex(8)}"
    )
    final = (
        f"{settings.direct_transfer_immutable_prefix}"
        f"{upload_id}-{secrets.token_hex(8)}"
    )
    marker = secrets.token_hex(16)
    return scratch, final, marker


async def _run_initiation(drive_id: str, row: Any) -> JSONResponse:
    """Begin saga steps 2–4 (§6, amended 2026-08-20): sign the initiation
    target FIRST (no provider state — a failure here leaves the session
    preparing/retryable with the marker unburned), then the one disclosure
    lease, CAS to active, and the single disclosure from memory. The
    CLIENT performs the actual GCS initiation POST from its own context —
    which is what makes GCS bless the session for browser CORS."""
    from ..core.timestamps import to_rfc3339

    upload_id = row["id"]
    storage = transfer_storage()
    try:
        signed = await storage.sign_resumable_initiation(
            XmlResumableRequest(
                object_name=row["scratch_object"],
                content_type=row["declared_media_type"],
                adoption_marker=row["adoption_marker"],
            )
        )
    except Exception:
        # Signing mints no provider-side state, so this is PROVABLY not a
        # credential loss: the row stays preparing with the marker unset
        # and the same key retries the whole initiation later.
        raise V0ApiError(
            503, "TRANSFER_UNAVAILABLE",
            "the upload session could not be initiated; retry",
            headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER)},
        ) from None

    try:
        async with conn() as c:
            leased = await core.acquire_initiation_lease(
                c, upload_id=upload_id, lease_seconds=INITIATION_LEASE_SECONDS
            )
    except Exception:
        # Failure BEFORE the durable disclosure marker proves nothing was
        # disclosed: the row stays preparing/retryable and the same key may
        # take a later lease (§6 crash rule). The signed URL dies with this
        # frame.
        raise V0ApiError(
            503, "TRANSFER_UNAVAILABLE",
            "the upload session could not be initiated; retry",
            headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER)},
        ) from None
    if leased is None:
        # Someone else owns (or already burned) the one disclosure: answer
        # the non-secret state (restart_required says everything; this may
        # be a first execution, so no replay label).
        async with conn() as c:
            current = await core.get_session(c, drive_id=drive_id, upload_id=upload_id)
        return _json_response(
            200, _session_payload(current), _base_headers(current)
        )

    try:
        async with conn() as c:
            active = await core.activate_session(c, upload_id=upload_id)
    except Exception:
        # The disclosure marker is set but `active` did not commit: the
        # disclosure outcome is uncertain and this session may never
        # disclose again (§6). The signed URL stays in this frame only.
        return await _fail_initiation(upload_id, "UPLOAD_INITIATION_UNCERTAIN")

    transfer = {
        "chunk_protocol": "gcs-xml-resumable",
        "initiation": {
            "url": signed.url,
            "method": "POST",
            "required_headers": dict(signed.required_headers),
            "expires_at": to_rfc3339(signed.expires_at),
        },
        "chunks": {
            "method": "PUT",
            "required_headers": {"Content-Type": active["declared_media_type"]},
        },
    }
    return _json_response(
        201,
        _session_payload(active, transfer=transfer),
        _base_headers(
            active,
            {"Location": _location(f"/v0/drives/{drive_id}/uploads/{upload_id}")},
        ),
    )


async def _fail_initiation(upload_id: str, failure_code: str) -> JSONResponse:
    try:
        async with conn() as c:
            await core.fail_initiation(
                c, upload_id=upload_id, failure_code=failure_code
            )
    except Exception as exc:
        # The row stays for GC's stale-preparing reconciliation; log the
        # safe ids so the wedge is visible, never silent.
        log.warning(
            "at=upload.terminalize_failed upload_id=%s failure_code=%s "
            "error_class=%s",
            upload_id, failure_code, type(exc).__name__,
        )
    return _error_response(
        503, "TRANSFER_UNAVAILABLE",
        "the provider could not initiate the transfer session",
        retry_after=UNAVAILABLE_RETRY_AFTER,
    )


@router.post(
    "/drives/{drive_id}/uploads",
    status_code=201,
    response_model=UploadBeginOut,
    responses={200: {"description": "Idempotent replay without transfer target."}},
    operation_id="uploads_create",
)
async def begin_upload(
    drive_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    """Begin one direct-upload session; the 201 response carries the one
    external GCS XML resumable target, disclosed exactly once."""
    await _require_ready()
    _require_scope(actor, _SCOPE_WRITE)
    _check_accept(request)
    _check_no_query(request)
    _check_ids(drive_id)
    await _charge_actor_rate(actor)
    key = _require_key(idempotency_key)

    media = request.headers.get("content-type", "")
    if media.split(";", 1)[0].strip().lower() != "application/json":
        raise V0ApiError(
            415, "UNSUPPORTED_MEDIA_TYPE",
            "This operation requires an application/json body.",
        )
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > MAX_BEGIN_BODY_BYTES:
                raise _invalid("the request body exceeds the control-body bound")
        except ValueError:
            pass  # malformed header; the read-side bound below still holds
    raw = await request.body()
    if len(raw) > MAX_BEGIN_BODY_BYTES:
        raise _invalid("the request body exceeds the control-body bound")
    body = _validated_begin_body(raw)
    target = body.target
    if isinstance(target, _TargetArtifactIn):
        if if_match is not None:
            # §5.2: the artifact target union has NO If-Match member.
            raise _invalid("If-Match is not accepted for an artifact target")
    elif if_match is None:
        raise V0ApiError(
            428, "PRECONDITION_REQUIRED",
            "a version target requires If-Match with the artifact head ETag",
        )

    outcome = await idempotency.claim(
        principal_id=actor.subject,
        key=key,
        method="POST",
        path=f"/v0/drives/{drive_id}/uploads",
        request_hash=_begin_request_hash(body, if_match),
    )
    if outcome.state == "conflict":
        raise V0ApiError(
            409, "IDEMPOTENCY_CONFLICT",
            "idempotency key was already used for a different request",
        )
    if outcome.state == "in_flight":
        raise V0ApiError(
            409, "IDEMPOTENCY_IN_PROGRESS",
            "idempotency key is already being processed; retry",
            headers={"Retry-After": str(BUSY_RETRY_AFTER)},
        )
    if outcome.state == "replayed":
        return await _begin_replay(actor, drive_id, outcome.stored)

    owner_id = outcome.owner_id
    assert owner_id is not None
    try:
        row = await _begin_first_transaction(
            actor, drive_id, body, if_match, owner_id
        )
    except Exception:
        await _abandon(owner_id)
        raise
    return await _run_initiation(drive_id, row)


async def _begin_first_transaction(
    actor: V0ActorContext,
    drive_id: str,
    body: _BeginIn,
    if_match: str | None,
    owner_id: str,
) -> Any:
    """Begin saga step 1 (§6): authorize the destination, refuse an
    equivalent live session, enforce the §9 ceilings, choose the object
    keys, insert `preparing` + the one reservation, and record the
    idempotency LINKAGE (upload id only, never a response body) — all
    committed before any provider contact."""
    from ..core.v0_artifacts import _ensure_drive
    from ..core.v0_content_commit import QuotaExceededError
    from ..core.v0_drives import (
        DriveNotFoundError,
        PreconditionError,
        _lock_drive_namespace,
    )
    from ..core.v0_folders import InvalidFolderNameError, validate_name

    target = body.target
    async with conn() as c, c.transaction():
        try:
            await _ensure_drive(c, actor, drive_id)
        except DriveNotFoundError:
            raise _not_found() from None
        # The drive dimension charges only now — the drive is proven to be
        # inside the caller's own workspace, so a foreign caller can never
        # spend another tenant's window.
        await _charge_drive_rate(actor, drive_id, c)
        # The workspace-scoped advisory lock serializes the CROSS-DRIVE §9
        # count ceilings (workspace/principal): two begins targeting
        # different drives must not both read the pre-insert count.
        # Ordering is workspace → drive, consistent with the accounting
        # seam's lock discipline.
        await c.execute(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('v0_workspace_sessions:' || $1, 0))",
            actor.workspace_id,
        )
        # Serializes equivalent-target checks and the per-drive ceiling for
        # this drive (the same advisory lock every namespace mutation
        # takes).
        await _lock_drive_namespace(c, drive_id)

        if isinstance(target, _TargetArtifactIn):
            try:
                name = validate_name(target.name)
            except InvalidFolderNameError as exc:
                raise _invalid(str(exc)) from None
            folder = await c.fetchrow(
                "SELECT id FROM folders WHERE drive_id = $1 AND id = $2 "
                "AND deleted_at IS NULL",
                drive_id, target.parent_folder_id,
            )
            if folder is None:
                raise _not_found()
            try:
                await authz.require(
                    c, actor=actor, drive_id=drive_id,
                    resource_type="folder",
                    resource_id=target.parent_folder_id, minimum="editor",
                )
            except authz.NotAuthorizedError:
                raise _not_found() from None
            target_kind, parent_folder_id = "artifact", target.parent_folder_id
            artifact_name, artifact_id, expected_revision = name, None, None
        else:
            artifact = await c.fetchrow(
                "SELECT id, revision FROM artifacts "
                "WHERE drive_id = $1 AND id = $2 AND deleted_at IS NULL",
                drive_id, target.artifact_id,
            )
            if artifact is None:
                raise _not_found()
            try:
                await authz.require(
                    c, actor=actor, drive_id=drive_id,
                    resource_type="artifact",
                    resource_id=target.artifact_id, minimum="editor",
                )
            except authz.NotAuthorizedError:
                raise _not_found() from None
            from ..core.v0_drives import precondition

            try:
                precondition(if_match, artifact["revision"])
            except PreconditionError as exc:
                from .v0_deps import precondition_http

                raise precondition_http(exc) from None
            target_kind, parent_folder_id = "version", None
            artifact_name = None
            artifact_id = target.artifact_id
            expected_revision = artifact["revision"]

        equivalent = await core.find_equivalent_live_session(
            c, drive_id=drive_id,
            principal_type=actor.subject_type, principal_id=actor.subject,
            target_kind=target_kind, parent_folder_id=parent_folder_id,
            artifact_name=artifact_name, artifact_id=artifact_id,
        )
        if equivalent is not None:
            raise V0ApiError(
                409, "CONFLICT",
                "an equivalent live upload session already exists for "
                "this target; complete or cancel it first",
                details={"upload_id": equivalent},
            )

        by_principal, by_workspace, by_drive = await core.count_live_sessions(
            c, workspace_id=actor.workspace_id, drive_id=drive_id,
            principal_id=actor.subject,
        )
        # The per-principal bound governs ONE agent or person. A SERVICE
        # principal is not that: a first-party service fronts every member of
        # a workspace under a single subject, so counting its sessions per
        # principal silently caps the whole workspace at one user's budget.
        # Chat's attachment lane hit exactly that — three concurrent uploads
        # for an entire workspace, and a 422 the moment a fourth arrived.
        #
        # Exempt, NOT unbounded: the drive and workspace bounds below still
        # apply and are the axes that mean something for a shared caller. A
        # genuine per-USER bound here is not possible today — `upload_sessions`
        # records `principal_id` only, with no acting-user column — so this
        # does not pretend to offer one.
        applicable = (
            ()
            if actor.subject_type == "service"
            else (
                (by_principal,
                 settings.direct_transfer_max_active_sessions_principal,
                 "direct_transfer_max_active_sessions_principal"),
            )
        )
        for count, limit, limit_name in (
            *applicable,
            (by_workspace, settings.direct_transfer_max_active_sessions_workspace,
             "direct_transfer_max_active_sessions_workspace"),
            (by_drive, settings.direct_transfer_max_active_sessions_drive,
             "direct_transfer_max_active_sessions_drive"),
        ):
            if limit is not None and count >= limit:
                raise V0ApiError(
                    422, "TRANSFER_LIMIT_EXCEEDED",
                    "the active-session bound cannot be acquired",
                    details={"limit_name": limit_name},
                )

        upload_id = ids.new_id("upld")
        scratch, final, marker = _new_object_keys(upload_id)
        ttl = settings.direct_transfer_session_ttl_seconds
        if ttl is None:
            # Boot validation guarantees this when enabled; if reached,
            # fail closed rather than inventing a B8-owned number.
            raise _disabled()
        try:
            await usage_gate.charge_upload_authorized_bytes(
                c,
                actor=actor,
                operation_key=f"upload-begin:{upload_id}",
                size_bytes=body.content.size_bytes,
            )
            row = await core.create_session(
                c, upload_id=upload_id,
                workspace_id=actor.workspace_id, drive_id=drive_id,
                principal_type=actor.subject_type, principal_id=actor.subject,
                principal_workspace_role=actor.workspace_role,
                target_kind=target_kind, parent_folder_id=parent_folder_id,
                artifact_name=artifact_name, artifact_id=artifact_id,
                expected_artifact_revision=expected_revision,
                declared_size_bytes=body.content.size_bytes,
                declared_media_type=body.content.media_type,
                declared_crc32c=body.content.checksum.value,
                adoption_marker=marker, scratch_object=scratch,
                final_object=final, expires_in_seconds=ttl,
                workspace_limit_bytes=min(
                    actor.drive_limits.storage_bytes_workspace,
                    settings.direct_transfer_hard_logical_version_bytes_workspace
                    or actor.drive_limits.storage_bytes_workspace,
                ),
                drive_limit_bytes=min(
                    actor.drive_limits.storage_bytes_drive,
                    settings.direct_transfer_hard_logical_version_bytes_drive
                    or actor.drive_limits.storage_bytes_drive,
                ),
            )
        except LimitExceeded as exc:
            raise _usage_limit_error(exc) from None
        except QuotaExceededError as exc:
            raise V0ApiError(
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
            ) from None
        # The begin record stores ONLY the request identity linkage —
        # replay reads fresh state; target reissue is unrepresentable.
        await _store_result(
            c, owner_id=owner_id, status=200,
            body={"upload_id": upload_id}, headers={},
        )
    return row


async def _begin_replay(
    actor: V0ActorContext, drive_id: str, stored: idempotency.StoredResponse
) -> JSONResponse:
    upload_id = (stored.body or {}).get("upload_id")
    if not isinstance(upload_id, str):
        raise V0ApiError(
            503, "TRANSFER_UNAVAILABLE",
            "the upload session record is unreadable; retry later",
            headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER)},
        )
    async with conn() as c:
        row = await _resolve_session(c, actor, drive_id, upload_id)
    if row["state"] == "preparing" and row["provider_attempted_at"] is None:
        # Proven no outbound attempt: the same key resumes the saga and may
        # receive the one (first) disclosure (§6 crash rule).
        return await _run_initiation(drive_id, row)
    return _json_response(
        200, _session_payload(row),
        {**_base_headers(row), "Idempotent-Replay": "true"},
    )


# ── status ──────────────────────────────────────────────────────────────────


@router.get(
    "/drives/{drive_id}/uploads/{upload_id}",
    response_model=UploadSessionOut,
    responses={304: {"description": "If-None-Match matched."}},
    operation_id="uploads_read",
)
async def read_upload(
    drive_id: str,
    upload_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    """Non-secret recovery state (§5.3). Never a target, coordinate,
    principal, reservation, continuation, or provider diagnostic."""
    await _require_ready()
    _require_scope(actor, _SCOPE_WRITE)
    _check_accept(request)
    _check_no_query(request)
    _check_ids(drive_id, upload_id)
    async with conn() as c:
        row = await _resolve_session(c, actor, drive_id, upload_id)
    etag = core.session_etag(row)
    if _etag_matches_weak(if_none_match, etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "no-store"},
        )
    return _json_response(200, _session_payload(row), _base_headers(row))


def _etag_matches_weak(if_none_match: str | None, etag: str) -> bool:
    from ..core.v0_drives import etag_values

    if not if_none_match:
        return False
    current = etag[1:-1] if etag.startswith('"') and etag.endswith('"') else etag
    values = etag_values(if_none_match)
    return values == "*" or current in (values or [])


# ── cancel ──────────────────────────────────────────────────────────────────


@router.delete(
    "/drives/{drive_id}/uploads/{upload_id}",
    response_model=UploadSessionOut,
    operation_id="uploads_delete",
)
async def cancel_upload(
    drive_id: str,
    upload_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    """Close publication permanently and release the reservation exactly
    once (§5.4); cleanup continues independently."""
    await _require_ready()
    _require_scope(actor, _SCOPE_WRITE)
    _check_accept(request)
    _check_no_query(request)
    _check_ids(drive_id, upload_id)
    await _charge_actor_rate(actor)
    await _require_empty_body(request)
    key = _require_key(idempotency_key)

    # The claim runs BEFORE the If-Match requirement: §5.1 exempts the
    # exact same-key replay from the precondition — it reauthorizes and
    # returns the stored response. Only a NEWLY CLAIMED execution requires
    # (and atomically enforces) the current session ETag.
    outcome = await idempotency.claim(
        principal_id=actor.subject,
        key=key,
        method="DELETE",
        path=f"/v0/drives/{drive_id}/uploads/{upload_id}",
        # The hash deliberately excludes If-Match: a same-key retry replays
        # the original response even after the cancel rotated the ETag.
        request_hash="sha256:" + hashlib.sha256(b"{}").hexdigest(),
    )
    if outcome.state == "replayed":
        # Replays reauthorize but charge no drive window — only fresh
        # executions spend the target dimension (uniform across the three
        # mutations).
        async with conn() as c:
            await _resolve_session(c, actor, drive_id, upload_id)
        return _replay(outcome.stored)
    if outcome.state == "conflict":
        raise V0ApiError(
            409, "IDEMPOTENCY_CONFLICT",
            "idempotency key was already used for a different request",
        )
    if outcome.state == "in_flight":
        raise V0ApiError(
            409, "IDEMPOTENCY_IN_PROGRESS",
            "idempotency key is already being processed; retry",
            headers={"Retry-After": str(BUSY_RETRY_AFTER)},
        )
    owner_id = outcome.owner_id
    assert owner_id is not None
    try:
        if if_match is None:
            raise V0ApiError(
                428, "PRECONDITION_REQUIRED", "If-Match header is required"
            )
        return await _execute_cancel(actor, drive_id, upload_id, if_match, owner_id)
    except Exception:
        await _abandon(owner_id)
        raise


async def _execute_cancel(
    actor: V0ActorContext, drive_id: str, upload_id: str,
    if_match: str, owner_id: str,
) -> Response:
    async with conn() as c:
        row = await _resolve_session(c, actor, drive_id, upload_id)
        await _charge_drive_rate(actor, drive_id, c)
        expected_revision = _parse_session_if_match(if_match, upload_id)
        state = row["state"]
        if state in ("completed", "cancelled", "expired", "rejected"):
            # Terminal rows never transition, so a read-compare suffices:
            # the caller must still prove the CURRENT ETag (§5.1: no
            # current ETag is disclosed on the 412).
            if expected_revision != row["session_revision"]:
                raise V0ApiError(
                    412, "PRECONDITION_FAILED",
                    "the upload session changed after it was read",
                )
            if state == "completed":
                # A state that precludes the operation leaves the key usable.
                raise V0ApiError(
                    409, "UPLOAD_ALREADY_COMPLETED",
                    "the upload already published; cancel cannot revert it",
                )
            # Terminal-failed states: answer the terminal status without
            # reviving anything.
            body = _session_payload(row)
            headers = _base_headers(row)
            async with c.transaction():
                await _store_result(
                    c, owner_id=owner_id, status=200, body=body, headers=headers
                )
            return _json_response(200, body, headers)
        try:
            # The pinned revision rides INSIDE the transition CAS: the
            # comparison and the active → cancelling change are one atomic
            # statement (blocker 2), so a stale ETag can never cancel a
            # session that changed after it was read.
            await core.acquire_transition(
                c, upload_id=upload_id, action="cancel",
                lease_seconds=CANCEL_LEASE_SECONDS,
                expected_revision=expected_revision,
            )
        except core.StaleSessionRevisionError:
            raise V0ApiError(
                412, "PRECONDITION_FAILED",
                "the upload session changed after it was read",
            ) from None
        except core.UploadSessionNotFoundError:
            raise _not_found() from None
        except core.UploadBusyError:
            raise V0ApiError(
                409, "UPLOAD_BUSY",
                "another request owns this session's transition",
                headers={"Retry-After": str(BUSY_RETRY_AFTER)},
            ) from None
        except core.InvalidUploadTransitionError as exc:
            if exc.state == "completed":
                raise V0ApiError(
                    409, "UPLOAD_ALREADY_COMPLETED",
                    "the upload already published; cancel cannot revert it",
                ) from None
            raise V0ApiError(
                409, "UPLOAD_BUSY",
                "the session cannot be cancelled in its current state",
                headers={"Retry-After": str(BUSY_RETRY_AFTER)},
            ) from None
        async with c.transaction():
            cancelled = await core.finalize_cancel(c, upload_id=upload_id)
            body = _session_payload(cancelled)
            headers = _base_headers(cancelled)
            await _store_result(
                c, owner_id=owner_id, status=200, body=body, headers=headers
            )
    return _json_response(200, body, headers)


# ── complete ────────────────────────────────────────────────────────────────


_COMPLETE_HASH = "sha256:" + hashlib.sha256(b"complete").hexdigest()


def _complete_request_hash() -> str:
    return _COMPLETE_HASH


@router.post(
    "/drives/{drive_id}/uploads/{upload_id}/complete",
    status_code=201,
    response_model=UploadSessionOut,
    responses={200: {"description": "Idempotent replay of completion."}},
    operation_id="uploads_complete",
)
async def complete_upload(
    drive_id: str,
    upload_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    """Adopt the finalized scratch object and publish exactly one immutable
    artifact/version (§5.5/§6). Empty body; If-Match is not accepted — the
    version precondition was captured at begin, and the transition fence
    plus idempotency serializes the session itself."""
    await _require_ready()
    _require_scope(actor, _SCOPE_WRITE)
    _check_accept(request)
    _check_no_query(request)
    _check_ids(drive_id, upload_id)
    await _charge_actor_rate(actor)
    if request.headers.get("if-match") is not None:
        raise _invalid("If-Match is not accepted by complete")
    if await request.body():
        raise _invalid("complete requires an empty request body")
    key = _require_key(idempotency_key)

    outcome = await idempotency.claim(
        principal_id=actor.subject,
        key=key,
        method="POST",
        path=f"/v0/drives/{drive_id}/uploads/{upload_id}/complete",
        request_hash=_complete_request_hash(),
    )
    if outcome.state == "replayed":
        async with conn() as c:
            await _resolve_session(c, actor, drive_id, upload_id)
        return _replay(outcome.stored)
    if outcome.state == "conflict":
        raise V0ApiError(
            409, "IDEMPOTENCY_CONFLICT",
            "idempotency key was already used for a different request",
        )
    if outcome.state == "in_flight":
        # The durable completion action (or a concurrent same-key request)
        # is still resolving: the current retryable answer, never
        # independent provider work (§5.5 transient rule).
        return _error_response(
            503, "TRANSFER_UNAVAILABLE",
            "the completion is still resolving; retry",
            retry_after=UNAVAILABLE_RETRY_AFTER,
        )
    owner_id = outcome.owner_id
    assert owner_id is not None
    try:
        return await _execute_complete(actor, drive_id, upload_id, owner_id)
    except V0ApiError:
        await _abandon(owner_id)
        raise
    except _CompleteOutcome as final:
        return final.response
    except Exception:
        await _abandon(owner_id)
        raise


class _CompleteOutcome(Exception):
    """Control-flow carrier for completion outcomes that already resolved
    their own idempotency bookkeeping (stored or deliberately left
    in-flight)."""

    def __init__(self, response: Response) -> None:
        self.response = response


async def _store_error_result(
    owner_id: str, status: int, code: str, message: str,
    *, details: Any = None, terminal=None,
) -> JSONResponse:
    """Durably record a deterministic completion outcome (§5.5): the
    terminal transition and the stored replayable error commit together."""
    body = _error_body(code, message, details)
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    async with conn() as c, c.transaction():
        if terminal is not None:
            await terminal(c)
        await _store_result(
            c, owner_id=owner_id, status=status, body=body, headers=headers
        )
    return _json_response(status, body, headers)


async def _terminal_complete_response(
    owner_id: str, row: Any,
) -> JSONResponse:
    """A claimed key meeting an already-terminal session (§5.5 different-key
    rules): completed replays the durable success as 200; failed terminals
    answer 409 UPLOAD_NOT_COMPLETABLE with only the safe failure code."""
    if row["state"] == "completed":
        body = _session_payload(row)
        headers = _base_headers(row)
        async with conn() as c, c.transaction():
            await _store_result(
                c, owner_id=owner_id, status=200, body=body, headers=headers
            )
        return _json_response(200, body, headers)
    details = (
        {"failure": {"code": row["failure_code"]}} if row["failure_code"] else None
    )
    return await _store_error_result(
        owner_id, 409, "UPLOAD_NOT_COMPLETABLE",
        "the upload session is terminal and cannot publish",
        details=details,
    )


async def _execute_complete(
    actor: V0ActorContext, drive_id: str, upload_id: str, owner_id: str
) -> Response:
    # Reauthorize + fast terminal paths, then take the completion fence
    # (which enforces the deadline before provider inspection).
    async with conn() as c:
        row = await _resolve_session(c, actor, drive_id, upload_id)
        await _charge_drive_rate(actor, drive_id, c)
        if row["state"] in ("completed", "cancelled", "expired", "rejected"):
            raise _CompleteOutcome(await _terminal_complete_response(owner_id, row))

    # Fence acquisition. The FIRST deadline outcome commits atomically with
    # its exact replayable 422 (review round 3 blocker): acquire's
    # terminalization is a subtransaction of this transaction, and the
    # ledger record joins it — a crash in between rolls BOTH back, so the
    # same key re-executes into the same 422 instead of degrading to the
    # different-key 409.
    async with conn() as c:
        expired_response: Response | None = None
        try:
            async with c.transaction():
                try:
                    row = await core.acquire_transition(
                        c, upload_id=upload_id, action="complete",
                        lease_seconds=COMPLETE_LEASE_SECONDS,
                    )
                except core.UploadExpiredError:
                    body = _error_body(
                        "UPLOAD_EXPIRED",
                        "the publication deadline elapsed before completion",
                    )
                    headers = {
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    }
                    await _store_result(
                        c, owner_id=owner_id, status=422, body=body,
                        headers=headers,
                    )
                    expired_response = _json_response(422, body, headers)
        except core.UploadSessionNotFoundError:
            raise _not_found() from None
        except core.UploadBusyError:
            await _abandon(owner_id)
            raise V0ApiError(
                409, "UPLOAD_BUSY",
                "another completion or cancel owns this session",
                headers={"Retry-After": str(BUSY_RETRY_AFTER)},
            ) from None
        except core.InvalidUploadTransitionError:
            async with conn() as c2:
                current = await core.get_session(
                    c2, drive_id=drive_id, upload_id=upload_id
                )
            raise _CompleteOutcome(
                await _terminal_complete_response(owner_id, current)
            ) from None
        if expired_response is not None:
            raise _CompleteOutcome(expired_response)

    lease_id = row["transition_lease_id"]

    # ── the fenced saga (provider phase + publication) lives in core so the
    # background reconciler drives the SAME code path (§6). The publication
    # transaction stores this key's 201 via on_publish, atomically. ──
    published: dict[str, Any] = {}

    async def _on_publish(c: Any, fresh: Any, result: dict[str, Any]) -> None:
        if result["kind"] == "artifact":
            location = _location(
                f"/v0/drives/{drive_id}/artifacts/{result['artifact_id']}"
            )
        else:
            location = _location(
                f"/v0/drives/{drive_id}/artifacts/{result['artifact_id']}"
                f"/versions/{result['version_id']}"
            )
        body = _session_payload(fresh)
        headers = _base_headers(fresh, {"Location": location})
        await _store_result(
            c, owner_id=owner_id, status=201, body=body, headers=headers
        )
        published["body"] = body
        published["headers"] = headers

    outcome = await core.run_fenced_completion(
        transfer_storage(), drive_id=drive_id, upload_id=upload_id,
        lease_id=lease_id, actor=actor, on_publish=_on_publish,
        lease_seconds=COMPLETE_LEASE_SECONDS,
    )

    if outcome.kind == "published":
        return _json_response(201, published["body"], published["headers"])

    if outcome.kind == "incomplete":
        # Incomplete, not corrupt: back to active, key abandoned/reusable.
        # Pre-adoption only — the core saga never returns this once a final
        # object may exist (§6 / review round 3 blocker 7).
        async with conn() as c:
            try:
                await core.return_to_active(
                    c, upload_id=upload_id, lease_id=lease_id
                )
            except (core.LeaseLostError, core.InvalidUploadTransitionError):
                return await _classify_lost_fence(actor, drive_id, upload_id, owner_id)
        await _abandon(owner_id)
        return _error_response(
            409, "UPLOAD_INCOMPLETE",
            "the resumable upload has not finalized an object yet",
            retry_after=INCOMPLETE_RETRY_AFTER,
        )

    if outcome.kind == "unavailable":
        # Transient/ambiguous provider work: keep the durable completing
        # action/lease AND the in-flight idempotency claim, so the same key
        # attaches (503 now, the eventual result later), a different key
        # answers UPLOAD_BUSY, and the GC reconciler resumes the action.
        return _error_response(
            503, "TRANSFER_UNAVAILABLE",
            "the provider is temporarily unavailable; the completion "
            "will be reconciled",
            retry_after=UNAVAILABLE_RETRY_AFTER,
        )

    if outcome.kind == "expired":
        # Deadline fence fired during publication: terminal expiry and the
        # exact replayable 422 commit together.
        async with conn() as c, c.transaction():
            await core.expire_session(c, upload_id=upload_id)
            body = _error_body(
                "UPLOAD_EXPIRED",
                "the publication deadline elapsed during completion",
            )
            headers = {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            }
            await _store_result(
                c, owner_id=owner_id, status=422, body=body, headers=headers
            )
        return _json_response(422, body, headers)

    if outcome.kind == "reject":
        return await _finalize_rejection(
            actor, drive_id, upload_id, owner_id, lease_id, outcome
        )

    # lease_lost (or any unrecognized outcome): classify from a fresh read.
    return await _classify_lost_fence(actor, drive_id, upload_id, owner_id)


# Wire mapping for the deterministic rejection codes (§5.5/§5.8).
_REJECTION_STATUS = {
    "CHECKSUM_MISMATCH": 422,
    "OBJECT_SIZE_MISMATCH": 422,
    "OBJECT_METADATA_MISMATCH": 422,
    "NAME_CONFLICT": 409,
    "PRECONDITION_FAILED": 412,
}


async def _finalize_rejection(
    actor: V0ActorContext, drive_id: str, upload_id: str, owner_id: str,
    lease_id: str, outcome: Any,
) -> Response:
    """Terminalize a deterministic completion rejection atomically with the
    stored replayable result (§5.5). Post-adoption authorization/destination
    misses are anti-enumerating: terminal `rejected` + quarantine, wire 404,
    no failure code (§6 / review round 3 blocker 7)."""
    if outcome.anti_enumerate:
        status, code, message = 404, "NOT_FOUND", "no such resource in this drive"
    else:
        status = _REJECTION_STATUS[outcome.failure_code]
        code = outcome.failure_code
        message = {
            "CHECKSUM_MISMATCH":
                "the finalized object does not match the declaration",
            "OBJECT_SIZE_MISMATCH":
                "the finalized object does not match the declaration",
            "OBJECT_METADATA_MISMATCH":
                "the object does not match this session's adoption identity",
            "NAME_CONFLICT":
                "the artifact name became occupied at publication",
            "PRECONDITION_FAILED":
                "the artifact head changed after the upload began",
        }[code]
    body = _error_body(code, message)
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    try:
        async with conn() as c, c.transaction():
            await core.reject_session(
                c, upload_id=upload_id,
                failure_code=outcome.failure_code,
                cleanup_state=outcome.cleanup_state or "pending",
                lease_id=lease_id,
            )
            await _store_result(
                c, owner_id=owner_id, status=status, body=body, headers=headers
            )
    except (core.LeaseLostError, core.InvalidUploadTransitionError):
        return await _classify_lost_fence(actor, drive_id, upload_id, owner_id)
    return _json_response(status, body, headers)


async def _classify_lost_fence(
    actor: V0ActorContext, drive_id: str, upload_id: str, owner_id: str
) -> Response:
    """This worker no longer owns the session's transition: answer from the
    CURRENT state — a completed session yields the durable result, failed
    terminals the terminal classification, anything else UPLOAD_BUSY. The
    loser performed no writes over the new owner."""
    async with conn() as c:
        try:
            current = await core.get_session(
                c, drive_id=drive_id, upload_id=upload_id
            )
        except core.UploadSessionNotFoundError:
            await _abandon(owner_id)
            raise _not_found() from None
    if current["state"] == "expired":
        # This key ATTEMPTED the completion and the deadline won under it
        # (possibly terminalized by the reconciler): §5.5's first-completion
        # rule applies — the exact replayable 422, not the never-attempted
        # different-key 409 (which the pre-fence fast path answers).
        body = _error_body(
            "UPLOAD_EXPIRED",
            "the publication deadline elapsed during completion",
        )
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        async with conn() as c, c.transaction():
            await _store_result(
                c, owner_id=owner_id, status=422, body=body, headers=headers
            )
        return _json_response(422, body, headers)
    if current["state"] in ("completed", "cancelled", "rejected"):
        return await _terminal_complete_response(owner_id, current)
    await _abandon(owner_id)
    raise V0ApiError(
        409, "UPLOAD_BUSY",
        "another completion or cancel owns this session",
        headers={"Retry-After": str(BUSY_RETRY_AFTER)},
    )
