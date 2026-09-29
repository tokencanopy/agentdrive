"""The drives vertical's HTTP surface (slice 4): 7 operations.

Wire semantics (design doc §5/§6):

  * create is idempotent under an ``Idempotency-Key`` (claim → mutate →
    complete; a rejection before execution abandons the claim so the key stays
    usable) and returns 201 + ``Location``;
  * patch/delete/restore are mutation-of-existing: they require both an
    ``Idempotency-Key`` and ``If-Match`` (428 absent, 412 stale), and bump the
    drive revision — the new ETag on the response is the sanctioned source for
    the next mutation;
  * read returns the drive's ETag and honors ``If-None-Match`` → 304;
  * soft-deleted drives are only reachable through ``?state=deleted|all``
    listing (which carries the post-delete revision for restore) — reads 404;
  * drives are scoped to ``V0ActorContext.workspace_id``; a drive in another
    workspace is indistinguishable from absence (404 DRIVE_NOT_FOUND, §6.1).

Every failure renders the single top-level ``{"error": {code, message,
details}}`` envelope (§6.3) via ``v0_api_error_handler``; statuses are
explicit per call, including the preconditions 428/412.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import settings
from ..core import idempotency, ids, v0_drives
from ..db import conn
from ..identity.actor import V0ActorContext
from .cursors import clamp_limit, cursor_str, cursor_ts
from .v0_authz import require_local
from .v0_cursors import seal as _seal_cursor
from .v0_cursors import unseal as _unseal_cursor
from .v0_deps import known_params, precondition_http, v0_actor
from .v0_errors import V0ApiError
from .v0_models import DriveListOut, DriveOut, DriveUsageOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["drives"], dependencies=[Depends(enforce_v0_rate_limit)]
)

# Scope gate per operation (target-manifest `scopes`). Token scope is half the
# authorization check; local capability is layered in later slices.
_SCOPE_READ = "drives:read"
_SCOPE_WRITE = "drives:write"
_SCOPE_USAGE = "usage:read"


class DriveCreateIn(BaseModel):
    """POST /v0/drives body."""

    name: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


class DriveUpdateIn(BaseModel):
    """PATCH /v0/drives/{id} body — at least one field is required."""

    name: str | None = Field(default=None, min_length=1)
    metadata: dict[str, Any] | None = None
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _at_least_one_field(self) -> DriveUpdateIn:
        if self.name is None and self.metadata is None:
            raise ValueError("provide at least one of name or metadata")
        return self


def _require_scope(actor: V0ActorContext, scope: str) -> None:
    if not actor.can(scope):
        raise V0ApiError(
            403, "PERMISSION_DENIED", f"the token does not carry the {scope} scope"
        )


def _check_drive_id(drive_id: str) -> None:
    if not ids.is_valid(drive_id, "drv"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")


def _etag(revision: str) -> str:
    return f'"{revision}"'


def _drive_location(drive_id: str) -> str:
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    return f"{origin}/v0/drives/{drive_id}"


def _body_hash(model: BaseModel | None) -> str:
    """Stable request identity for the idempotency ledger: method+path+hash.

    Built from the parsed request (never raw bytes) so semantically identical
    requests — whitespace/ordering differences — replay the same result.
    """
    payload = model.model_dump(mode="json") if model is not None else {}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


async def _not_found(awaitable: Any) -> Any:
    """Map DriveNotFoundError (404 semantics) to the uniform envelope."""
    try:
        return await awaitable
    except v0_drives.DriveNotFoundError:
        raise V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace") from None


async def _run_mutation(
    actor: V0ActorContext,
    *,
    key: str | None,
    method: str,
    path: str,
    request_hash: str,
    execute: Any,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Claim → execute-in-transaction → complete (or abandon on rejection).

    §7.2: only executed mutations create records. `execute(c)` returns
    (status, headers, body); on ANY exception the transaction rolls back and
    the claim is abandoned on the same (now autocommitting) connection so the
    key remains usable for a corrected retry — a 428/412/404/limit rejection
    must not burn it until expiry.

    Returns the ``(status, headers, body)`` triple for the route to apply to
    its injected ``Response`` and return as the validated body — the route's
    ``response_model`` guards the shape at runtime.
    """
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

    from contextlib import suppress

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


@router.get(
    "/drives",
    response_model=DriveListOut,
    operation_id="drives_list",
    dependencies=[Depends(known_params("state", "limit", "cursor"))],
)
async def list_drives(
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    state: str = "active",
    limit: int | None = None,
    cursor: str | None = None,
    response: Response = ...,
) -> dict[str, Any]:
    """List the actor's workspace drives, newest-first (keyset paginated).

    ``state`` (active|deleted|all) exposes soft-deleted drives so a
    manager can read the post-delete revision as the If-Match source for a
    restore. Unknown query parameters are rejected (§6.3)."""
    _require_scope(actor, _SCOPE_READ)
    if state not in ("active", "deleted", "all"):
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "state must be one of active, deleted, all"
        )
    page_size = clamp_limit(limit)

    # The sealed cursor fingerprint keeps its ORIGINAL key spelling. It is
    # internal and never leaves the sealed token, and renaming it would make
    # every in-flight cursor fail closed on the deploy that renamed the query
    # parameter -- the exact churn `v0_shares` and `v0_grants` engineer
    # against in their own cursor comments.
    bound = {"lifecycle": state}
    position = _unseal_cursor("drives", actor.workspace_id, cursor, bound=bound)
    after_ts = cursor_ts(position, "created_at") if position else None
    after_id = cursor_str(position, "id") if position else None

    async with conn() as c:
        result = await v0_drives.list_drives(
            c,
            actor,
            state=state,
            limit=page_size,
            after_ts=after_ts,
            after_id=after_id,
        )
    response.headers["Cache-Control"] = "private"
    return {
        "items": result["items"],
        "next_cursor": _seal_cursor(
            "drives", actor.workspace_id, result["next_cursor"], bound=bound
        ),
    }


@router.post(
    "/drives",
    status_code=201,
    response_model=DriveOut,
    operation_id="drives_create",
    dependencies=[Depends(known_params())],
)
async def create_drive(
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    body: DriveCreateIn = ...,
    response: Response = ...,
) -> DriveOut:
    """Create a drive with its structural root folder and creator-manager
    grant(s) in one transaction; idempotent under the ``Idempotency-Key``."""
    _require_scope(actor, _SCOPE_WRITE)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            drive = await v0_drives.create_drive(
                c, actor, name=body.name, metadata=body.metadata
            )
        except v0_drives.DriveLimitReachedError:
            raise V0ApiError(
                409,
                "DRIVE_LIMIT_EXCEEDED",
                "The workspace has reached its current AgentDrive limit.",
            ) from None
        return (
            201,
            {
                "ETag": _etag(drive["revision"]),
                "Location": _drive_location(drive["id"]),
                "Cache-Control": "private",
            },
            drive,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path="/v0/drives",
        request_hash=_body_hash(body),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.get(
    "/drives/{drive_id}",
    response_model=DriveOut,
    responses={304: {"description": "If-None-Match matched."}},
    operation_id="drives_read",
    dependencies=[
        Depends(require_local("viewer", "drive", "drive_id")),
        Depends(known_params()),
    ],
)
async def read_drive(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> DriveOut:
    """Read one active drive. ETag = quoted revision; matching
    ``If-None-Match`` → 304. Deleted and cross-workspace drives are 404."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    async with conn() as c:
        drive = await _not_found(v0_drives.get_drive(c, actor, drive_id))
    etag = _etag(drive["revision"])
    values = v0_drives.etag_values(if_none_match) if if_none_match else None
    if values == "*" or (values and drive["revision"] in values):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private"})
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    return drive


@router.patch(
    "/drives/{drive_id}",
    response_model=DriveOut,
    operation_id="drives_update",
    dependencies=[
        Depends(require_local("editor", "drive", "drive_id")),
        Depends(known_params()),
    ],
)
async def update_drive(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    body: DriveUpdateIn = ...,
    response: Response = ...,
) -> DriveOut:
    """Rename / update a drive's metadata. Requires ``Idempotency-Key`` and
    ``If-Match`` (428 absent, 412 stale); bumps the revision."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            drive = await v0_drives.patch_drive(
                c,
                actor,
                drive_id,
                name=body.name,
                metadata=body.metadata,
                if_match=if_match,
            )
        except v0_drives.DriveNotFoundError:
            raise V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace") from None
        except v0_drives.PreconditionError as exc:
            raise precondition_http(exc) from None
        return (
            200,
            {"ETag": _etag(drive["revision"]), "Cache-Control": "private"},
            drive,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="PATCH",
        path=f"/v0/drives/{drive_id}",
        request_hash=_body_hash(body),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.delete(
    "/drives/{drive_id}",
    response_model=DriveOut,
    operation_id="drives_delete",
    dependencies=[
        Depends(require_local("manager", "drive", "drive_id")),
        Depends(known_params()),
    ],
)
async def delete_drive(
    drive_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> DriveOut:
    """Soft-delete a drive. Returns 200 with the deleted representation so the
    client has the post-delete revision/ETag for a restore."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            drive = await v0_drives.delete_drive(c, actor, drive_id, if_match=if_match)
        except v0_drives.DriveNotFoundError:
            raise V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace") from None
        except v0_drives.PreconditionError as exc:
            raise precondition_http(exc) from None
        return (
            200,
            {"ETag": _etag(drive["revision"]), "Cache-Control": "private"},
            drive,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="DELETE",
        path=f"/v0/drives/{drive_id}",
        request_hash=_body_hash(None),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.post(
    "/drives/{drive_id}/restore",
    response_model=DriveOut,
    operation_id="drives_restore",
    dependencies=[
        Depends(require_local("manager", "drive", "drive_id", include_deleted=True)),
        Depends(known_params()),
    ],
)
async def restore_drive(
    drive_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> DriveOut:
    """Restore a soft-deleted drive. If-Match must carry the post-delete
    revision; restoring an already-active drive is 409 CONFLICT."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            drive = await v0_drives.restore_drive(c, actor, drive_id, if_match=if_match)
        except v0_drives.DriveNotFoundError:
            raise V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace") from None
        except v0_drives.DriveNotDeletedError:
            raise V0ApiError(409, "CONFLICT", "drive is not soft-deleted") from None
        except v0_drives.PreconditionError as exc:
            raise precondition_http(exc) from None
        return (
            200,
            {"ETag": _etag(drive["revision"]), "Cache-Control": "private"},
            drive,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/restore",
        request_hash=_body_hash(None),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.get(
    "/drives/{drive_id}/usage",
    response_model=DriveUsageOut,
    operation_id="drives_usage",
    dependencies=[
        Depends(require_local("viewer", "drive", "drive_id")),
        Depends(known_params()),
    ],
)
async def drive_usage(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    response: Response = ...,
) -> DriveUsageOut:
    """Usage counters, current storage and download meters, and the effective
    file/share limits for one active drive. The snapshot includes committed
    and reserved bytes so clients can show capacity already in flight."""
    _require_scope(actor, _SCOPE_USAGE)
    _check_drive_id(drive_id)
    response.headers["Cache-Control"] = "private"
    async with conn() as c:
        return await _not_found(v0_drives.drive_usage(c, actor, drive_id))
