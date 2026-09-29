"""Shares vertical's HTTP surface (slice 8): 5 operations.

Wire semantics mirror the drives/folders/grant verticals (§6.2/§6.3):
mutations require ``Idempotency-Key``; revoke/rotate require ``If-Match`` on
the share's state (428 absent, 412 stale); reads carry ETag and honor
``If-None-Match`` → 304; drives outside the workspace read as absent (404).
The create/rotate responses are the ONLY ones carrying the plaintext
``secret`` — list/get never include it.

Every failure renders the single top-level ``{"error": {code, message,
details}}`` envelope (§6.3).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import settings
from ..core import idempotency, ids, urls, v0_drives
from ..core import v0_shares as core
from ..db import conn
from ..identity.actor import V0ActorContext
from .cursors import clamp_limit, cursor_str
from .v0_authz import require_local
from .v0_cursors import seal as _seal_cursor
from .v0_cursors import unseal as _unseal_cursor
from .v0_deps import known_params, precondition_http, v0_actor
from .v0_errors import V0ApiError
from .v0_models import ShareCreateOut, ShareListOut, ShareOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["shares"], dependencies=[Depends(enforce_v0_rate_limit)]
)

_SCOPE_READ = "sharing:read"
_SCOPE_WRITE = "sharing:write"

_SHARE_ID_PATTERN = r"^shr_[a-f0-9]{16}$"
_DRIVE_ID_PATTERN = r"^drv_[a-f0-9]{16}$"
_FOLDER_ID_PATTERN = r"^fld_[a-f0-9]{16}$"
_ARTIFACT_ID_PATTERN = r"^art_[a-f0-9]{16}$"
_VERSION_ID_PATTERN = r"^ver_[a-f0-9]{16}$"


class ShareCreateIn(BaseModel):
    """POST /v0/drives/{id}/shares body."""

    resource_type: Literal["artifact", "artifact_version", "folder"]
    resource_id: str = Field(min_length=1)
    expires_at: datetime | None = None
    model_config = ConfigDict(extra="forbid")

    @field_validator("expires_at")
    @classmethod
    def aware_expires(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone offset")
        return value


def _require_scope(actor: V0ActorContext, scope: str) -> None:
    if not actor.can(scope):
        raise V0ApiError(
            403, "PERMISSION_DENIED", f"the token does not carry the {scope} scope"
        )


def _check_drive_id(drive_id: str) -> None:
    if not ids.is_valid(drive_id, "drv"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")


def _check_share_id(share_id: str) -> None:
    if not ids.is_valid(share_id, "shr"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed share id")


def _etag(share_id: str) -> str:
    return f'"{share_id}"'


def _share_location(drive_id: str, share_id: str) -> str:
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    return f"{origin}/v0/drives/{drive_id}/shares/{share_id}"


def _with_share_url(result: dict[str, Any]) -> dict[str, Any]:
    """Attach the public redemption URL when this response carries a secret.

    `Location` above is the MANAGEMENT url (`/v0/.../shares/{id}`) — useful to
    an operator, useless to whoever should open the link. The redemption URL
    lives on the public share origin and embeds the secret, and the origin is
    deployment configuration, so a client genuinely cannot compose it. Minting
    a share without returning it left the tool unable to do the one thing its
    name promises.

    Gated on `secret` so an idempotent replay — which deliberately withholds
    the secret — does not emit a URL either. `core.urls.share_url` already
    resolves the correct origin (share host when configured, public base
    otherwise); it simply had no caller until now.
    """
    secret = result.get("secret")
    if not secret:
        return result
    return {**result, "url": urls.share_url(secret)}


def _body_hash(model: BaseModel | None) -> str:
    payload = model.model_dump(mode="json") if model is not None else {}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _mapping_error(exc: Exception) -> V0ApiError:
    if isinstance(exc, v0_drives.PreconditionError):
        return precondition_http(exc)
    if isinstance(exc, v0_drives.DriveNotFoundError):
        return V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace")
    if isinstance(exc, core.ShareNotFoundError):
        return V0ApiError(404, "SHARE_NOT_FOUND", str(exc))
    if isinstance(exc, core.BadShareError):
        return V0ApiError(400, "INVALID_ARGUMENT", str(exc))
    if isinstance(exc, core.ShareRevokedError):
        return V0ApiError(409, "CONFLICT", str(exc))
    raise exc


async def _run_mutation(
    actor: V0ActorContext,
    *,
    key: str | None,
    method: str,
    path: str,
    request_hash: str,
    execute: Any,
    store_body: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
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
        return (stored.status, stored.headers, stored.body)
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
                # Secret-bearing responses (share create/rotate) are stored in
                # the idempotency ledger WITHOUT the plaintext secret, so a DB
                # backup/leak never yields share credentials (§6.2). The live
                # response still carries the secret to the caller.
                ledger_body = store_body(body) if store_body else body
                await idempotency.complete(
                    c, owner_id=owner_id, status=status, body=ledger_body,
                    headers=headers,
                )
        except Exception:
            with suppress(Exception):
                await idempotency.abandon(c, owner_id=owner_id)
            raise
    return (status, headers, body)


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    values = v0_drives.etag_values(if_none_match)
    if values == "*":
        return True
    current = etag[1:-1] if etag.startswith('"') and etag.endswith('"') else etag
    return current in (values or [])


def _check_resource_id(resource_type: str, resource_id: str) -> None:
    """Validate resource_id shape per the polymorphic resource_type."""
    if resource_type == "folder" and not ids.is_valid(resource_id, "fld"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed resource_id for a folder share")
    if resource_type == "artifact" and not ids.is_valid(resource_id, "art"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed resource_id for an artifact share")
    if resource_type == "artifact_version" and not ids.is_valid(resource_id, "ver"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed resource_id for a version share")


@router.get(
    "/drives/{drive_id}/shares",
    response_model=ShareListOut,
    operation_id="shares_list",
    dependencies=[
        Depends(require_local("manager", "drive", "drive_id")),
        Depends(known_params(
            "state", "limit", "cursor", "resource_type", "resource_id",
        )),
    ],
)
async def list_shares(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    state: str = "active",
    limit: int | None = None,
    cursor: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    response: Response = ...,
) -> ShareListOut:
    """List the drive's shares (no secrets), keyset paginated.

    ``resource_id`` narrows the page to one resource's links and REQUIRES
    ``resource_type`` alongside it — a bare resource id is ambiguous across
    ``artifact`` / ``artifact_version`` / ``folder``, and inferring the kind
    from the id prefix would tie the filter's meaning to an id format the
    contract does not promise to keep. ``resource_type`` alone is a valid
    filter. Listing shares already requires drive ``manager``, so these
    filters only narrow a page the caller could already read in full.
    """
    _require_scope(actor, _SCOPE_READ)
    if state not in ("active", "revoked", "all"):
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "state must be one of active, revoked, all"
        )
    _check_drive_id(drive_id)
    if resource_type is not None and resource_type not in core.RESOURCE_TYPES:
        raise V0ApiError(
            400,
            "INVALID_ARGUMENT",
            "resource_type must be artifact, artifact_version, or folder",
        )
    if resource_id is not None and resource_type is None:
        raise V0ApiError(
            400,
            "INVALID_PARAMETER",
            "resource_id requires resource_type; a bare resource id is "
            "ambiguous across artifact, artifact_version, and folder",
        )
    if resource_id is not None:
        _check_resource_id(resource_type, resource_id)
    page_size = clamp_limit(limit)

    # Only non-None filters enter the fingerprint, so an unfiltered cursor
    # hashes exactly as it did before these params existed — an in-flight
    # cursor survives the deploy instead of 400-ing on whichever revision
    # serves the next page.
    # Sealed-cursor key pinned to its original spelling (see `v0_drives`).
    bound: dict[str, str] = {"lifecycle": state}
    if resource_type is not None:
        bound["resource_type"] = resource_type
    if resource_id is not None:
        bound["resource_id"] = resource_id
    position = _unseal_cursor("shares", drive_id, cursor, bound=bound)
    after_id = cursor_str(position, "id") if position else None

    async with conn() as c:
        try:
            page = await core.list_shares(
                c, actor, drive_id, state=state, limit=page_size,
                after_id=after_id,
                resource_type=resource_type, resource_id=resource_id,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
    response.headers["Cache-Control"] = "private"
    return {
        "items": page["items"],
        "next_cursor": _seal_cursor(
            "shares", drive_id, page["next_cursor"], bound=bound
        ),
    }


@router.post(
    "/drives/{drive_id}/shares",
    status_code=201,
    response_model=ShareCreateOut,
    operation_id="shares_create",
    dependencies=[
        Depends(require_local("manager", "drive", "drive_id")),
        Depends(known_params()),
    ],
)
async def create_share(
    drive_id: str,
    request: Request,
    body: ShareCreateIn,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    response: Response = ...,
) -> ShareCreateOut:
    """Mint a read-only bearer link. The response carries the plaintext
    secret — the only response that does."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_resource_id(body.resource_type, body.resource_id)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.create_share(
                c, actor, drive_id,
                resource_type=body.resource_type, resource_id=body.resource_id,
                expires_at=body.expires_at,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            201,
            {"ETag": _etag(result["revision"]),
             "Location": _share_location(drive_id, result["id"]),
             "Cache-Control": "private"},
            _with_share_url(result),
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/shares",
        request_hash=_body_hash(body),
        execute=execute,
        store_body=lambda b: {
            k: v for k, v in b.items() if k not in ("secret", "url")
        },
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.get(
    "/drives/{drive_id}/shares/{share_id}",
    response_model=ShareOut,
    responses={304: {"description": "If-None-Match matched."}},
    operation_id="shares_read",
    dependencies=[
        Depends(require_local("manager", "drive", "drive_id")),
        Depends(known_params()),
    ],
)
async def read_share(
    drive_id: str,
    share_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> ShareOut:
    """Read one share's management representation (no secret)."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_share_id(share_id)
    async with conn() as c:
        try:
            result = await core.get_share(c, actor, drive_id, share_id)
        except Exception as exc:
            raise _mapping_error(exc) from None
    etag = _etag(result["revision"])
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private"})
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    return result


@router.delete(
    "/drives/{drive_id}/shares/{share_id}",
    response_model=ShareOut,
    operation_id="shares_revoke",
    dependencies=[
        Depends(require_local("manager", "drive", "drive_id")),
        Depends(known_params()),
    ],
)
async def revoke_share(
    drive_id: str,
    share_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> ShareOut:
    """Revoke a share (soft, sets revoked_at) under If-Match."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_share_id(share_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.revoke_share(
                c, actor, drive_id, share_id, if_match=if_match
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
        path=f"/v0/drives/{drive_id}/shares/{share_id}",
        request_hash=_body_hash(None),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.post(
    "/drives/{drive_id}/shares/{share_id}/rotate",
    response_model=ShareCreateOut,
    operation_id="shares_rotate",
    dependencies=[
        Depends(require_local("manager", "drive", "drive_id")),
        Depends(known_params()),
    ],
)
async def rotate_share(
    drive_id: str,
    share_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> ShareCreateOut:
    """Rotate the secret in place (same id, no grace window). The response
    carries the new plaintext secret."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_share_id(share_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.rotate_share(
                c, actor, drive_id, share_id, if_match=if_match
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            200,
            {"ETag": _etag(result["revision"]), "Cache-Control": "private"},
            _with_share_url(result),
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/shares/{share_id}/rotate",
        request_hash=_body_hash(None),
        execute=execute,
        store_body=lambda b: {
            k: v for k, v in b.items() if k not in ("secret", "url")
        },
    )
    response.status_code = status
    response.headers.update(headers)
    return payload
