"""Grants vertical's HTTP surface (slice 8): 5 operations.

Wire semantics mirror the drives/folders verticals (§6.2/§6.3): mutations
require ``Idempotency-Key``; mutation-of-existing ops (update/revoke)
require ``If-Match`` on the grant's state (428 absent, 412 stale); reads
carry ETag and honor ``If-None-Match`` → 304; drives outside the actor's
workspace read as absent (404). ``sharing:read`` / ``sharing:write`` gate
the token scope; local manager authority is the local-capability half.

Every failure renders the single top-level ``{"error": {code, message,
details}}`` envelope (§6.3).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import settings
from ..core import idempotency, ids, v0_drives
from ..core import v0_grants as core
from ..db import conn
from ..identity.actor import V0ActorContext
from .cursors import clamp_limit, cursor_str
from .v0_authz import require_any_grant_in_drive
from .v0_cursors import seal as _seal_cursor
from .v0_cursors import unseal as _unseal_cursor
from .v0_deps import known_params, precondition_http, v0_actor
from .v0_errors import V0ApiError
from .v0_models import GrantListOut, GrantOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["grants"], dependencies=[Depends(enforce_v0_rate_limit)]
)

_SCOPE_READ = "sharing:read"
_SCOPE_WRITE = "sharing:write"

_GRANT_ID_PATTERN = r"^grn_[a-f0-9]{16}$"
_DRIVE_ID_PATTERN = r"^drv_[a-f0-9]{16}$"
_FOLDER_ID_PATTERN = r"^fld_[a-f0-9]{16}$"
_ARTIFACT_ID_PATTERN = r"^art_[a-f0-9]{16}$"

_AGENT_ID_PATTERN = r"^tcagt_"
_USER_ID_PATTERN = r"^tcusr_"
_SERVICE_ID_PATTERN = r"^tcsvc_"


class GrantCreateIn(BaseModel):
    """POST /v0/drives/{id}/grants body."""

    principal_type: Literal["agent", "user", "service", "workspace", "public"]
    principal_id: str | None = Field(
        default=None,
        description=(
            "Required for `agent`, `user`, `service`, and `workspace`; "
            "omitted only for `public`. For `agent`, `user`, and `service` it "
            "is checked against that type's id prefix (`tcagt_` / `tcusr_` / "
            "`tcsvc_`) and a mismatch is "
            "`422 VALIDATION_ERROR`. The prefix is all AgentDrive asserts: "
            "these ids are minted by Hub, so their full shape is not "
            "AgentDrive's to enforce, and a well-formed id naming a principal "
            "that does not exist — or belongs to another workspace — is "
            "accepted here and simply never matches a token. The rule is "
            "conditional on `principal_type`, so it is enforced at the "
            "boundary rather than expressible as one JSON Schema `pattern`."
        ),
    )
    resource_type: Literal["drive", "folder", "artifact"]
    resource_id: str = Field(min_length=1)
    role: Literal["viewer", "editor", "manager"]
    expires_at: datetime | None = None
    model_config = ConfigDict(extra="forbid")

    @field_validator("expires_at")
    @classmethod
    def aware_expires(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def principal_id_matches_its_type(self) -> GrantCreateIn:
        """`agent` and `user` principals must carry a well-formed id.

        `_AGENT_ID_PATTERN` and `_USER_ID_PATTERN` were declared and never
        used, so a typo'd or foreign id returned `201 Created` and produced a
        grant that could never match a token — permanently inert, with nothing
        to tell the caller. `workspace` and `public` carry no id by design.
        """
        expected = {
            "agent": _AGENT_ID_PATTERN,
            "user": _USER_ID_PATTERN,
            "service": _SERVICE_ID_PATTERN,
        }.get(self.principal_type)
        if expected is None:
            return self
        if not self.principal_id or not re.match(expected, self.principal_id):
            raise ValueError(
                f"principal_id must be a {self.principal_type} id "
                f"matching {expected}"
            )
        return self


class GrantUpdateIn(BaseModel):
    """PATCH /v0/drives/{id}/grants/{grant_id} body — at least one field is
    required. An explicit ``expires_at: null`` clears the expiry; omitting it
    leaves it unchanged."""

    role: Literal["viewer", "editor", "manager"] | None = None
    expires_at: datetime | None = None
    model_config = ConfigDict(extra="forbid")

    @field_validator("expires_at")
    @classmethod
    def aware_expires(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def _require_change(self) -> GrantUpdateIn:
        if not self.model_fields_set:
            raise ValueError("provide at least one of role or expires_at")
        self._clear_expires = "expires_at" in self.model_fields_set and self.expires_at is None
        return self

    @property
    def clear_expires(self) -> bool:
        return getattr(self, "_clear_expires", False)


def _require_scope(actor: V0ActorContext, scope: str) -> None:
    if not actor.can(scope):
        raise V0ApiError(
            403, "PERMISSION_DENIED", f"the token does not carry the {scope} scope"
        )


def _check_drive_id(drive_id: str) -> None:
    if not ids.is_valid(drive_id, "drv"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")


def _check_grant_id(grant_id: str) -> None:
    if not ids.is_valid(grant_id, "grn"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed grant id")


def _check_grant_resource_id(resource_type: str, resource_id: str) -> None:
    """Validate a ``resource_id`` filter against its declared kind.

    Same id shapes ``grants_create`` enforces, so the filter cannot name a
    resource the create path would have rejected. Shape only — whether the
    resource exists is not disclosed; a well-formed id nobody granted on
    simply pages empty.
    """
    prefix = {"drive": "drv", "folder": "fld", "artifact": "art"}[resource_type]
    if not ids.is_valid(resource_id, prefix):
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "malformed resource_id for "
            f"{'an' if resource_type == 'artifact' else 'a'} {resource_type} grant"
        )


def _etag(grant_id: str) -> str:
    return f'"{grant_id}"'


def _grant_location(drive_id: str, grant_id: str) -> str:
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    return f"{origin}/v0/drives/{drive_id}/grants/{grant_id}"


def _body_hash(model: BaseModel | None) -> str:
    payload = model.model_dump(mode="json") if model is not None else {}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _mapping_error(exc: Exception) -> V0ApiError:
    if isinstance(exc, v0_drives.PreconditionError):
        return precondition_http(exc)
    if isinstance(exc, v0_drives.DriveNotFoundError):
        return V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace")
    if isinstance(exc, core.GrantNotFoundError):
        return V0ApiError(404, "GRANT_NOT_FOUND", str(exc))
    if isinstance(exc, core.BadGrantError):
        return V0ApiError(400, "INVALID_ARGUMENT", str(exc))
    if isinstance(exc, core.GrantConflictError):
        return V0ApiError(409, "GRANT_CONFLICT", str(exc))
    if isinstance(exc, core.GrantNotRevokedError):
        return V0ApiError(409, "CONFLICT", str(exc))
    if isinstance(exc, core.GrantPermanentError):
        return V0ApiError(409, "GRANT_PERMANENT", str(exc))
    if isinstance(exc, asyncpg.CheckViolationError):
        # `schema.sql`'s `grants_principal_id_shape` has enforced the principal
        # prefixes since day zero, and nothing mapped the violation — so a
        # malformed id surfaced as a 500. The boundary validator above catches
        # the common case first; this is the backstop for every other path into
        # the table, and for constraints the model does not mirror.
        return V0ApiError(
            422, "VALIDATION_ERROR", "the grant does not satisfy a stored constraint",
            details={"constraint": getattr(exc, "constraint_name", None)},
        )
    raise exc


async def _run_mutation(
    actor: V0ActorContext,
    *,
    key: str | None,
    method: str,
    path: str,
    request_hash: str,
    execute: Any,
    replay_guard: Callable[..., Any] | None = None,
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
        if replay_guard is not None:
            async with conn() as c:
                await replay_guard(c)
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
                await idempotency.complete(
                    c, owner_id=owner_id, status=status, body=body, headers=headers
                )
        except Exception:
            with suppress(Exception):
                await idempotency.abandon(c, owner_id=owner_id)
            raise
    return (status, headers, body)


def _target_manager_guard(
    actor: V0ActorContext,
    drive_id: str,
    resource_type: str,
    resource_id: str,
) -> Callable[..., Any]:
    """Replay-time authorization for grants_create (target in the body).

    The manager check lives inside `execute`, which idempotent replay skips
    (the body is not re-parsed). This guard re-runs it on replay so a
    principal whose manager grant was revoked cannot replay a stored 201
    (§6.2). Maps to the surface's uniform 404.
    """

    async def _guard(c: Any) -> None:
        try:
            await core._require_manager(
                c, actor, drive_id, resource_type, resource_id
            )
        except Exception as exc:
            raise _mapping_error(exc) from None

    return _guard


def _grant_row_manager_guard(
    actor: V0ActorContext, drive_id: str, grant_id: str
) -> Callable[..., Any]:
    """Replay-time authorization for grants_update/revoke.

    The grant's target resource lives in the STORED row (not the request
    body), so the guard fetches the grant by its path id and re-checks
    manager on its resource. A grant row that no longer exists reads as the
    same uniform 404 the surface uses.
    """

    async def _guard(c: Any) -> None:
        try:
            row = await c.fetchrow(
                "SELECT resource_type, resource_id FROM grants "
                "WHERE drive_id=$1 AND id=$2",
                drive_id, grant_id,
            )
            if row is None:
                raise core.GrantNotFoundError("no such grant in this drive")
            await core._require_manager(
                c, actor, drive_id, row["resource_type"], row["resource_id"]
            )
        except Exception as exc:
            raise _mapping_error(exc) from None

    return _guard


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    values = v0_drives.etag_values(if_none_match)
    if values == "*":
        return True
    current = etag[1:-1] if etag.startswith('"') and etag.endswith('"') else etag
    return current in (values or [])


@router.get(
    "/drives/{drive_id}/grants",
    response_model=GrantListOut,
    operation_id="grants_list",
    dependencies=[
        Depends(require_any_grant_in_drive()),
        Depends(known_params(
            "state", "limit", "cursor",
            "resource_type", "resource_id", "principal_type",
        )),
    ],
)
async def list_grants(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    state: str = "active",
    limit: int | None = None,
    cursor: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    principal_type: str | None = None,
    response: Response = ...,
) -> GrantListOut:
    """List explicit grants in the drive, keyset paginated.

    **What you see depends on your role (contract change).** A caller holding
    ``manager`` on the drive lists EVERY grant in it. Any other caller lists
    only the grants that name them — their own agent/user rows, ``workspace``
    grants covering them, and ``public`` grants (which already expose the
    resource to them anyway). Previously any drive ``viewer`` could page out
    every principal id, role and expiry in the drive; that was an
    access-graph disclosure, not a feature.

    The operation is refused (404) only for a caller holding no live grant
    anywhere in the drive — never for lack of ``manager``, because seeing
    your own access is not a privilege. That admits folder-scoped
    principals, who previously 404'd here despite having access to show. A
    folder ``manager`` still sees only their own rows, not the roster of the
    subtree they administer; scoping the listing by per-resource
    administration authority is a follow-up this change does not claim.

    ``resource_id`` filters to one resource's grants and REQUIRES
    ``resource_type`` alongside it — a bare resource id is ambiguous across
    the three resource kinds, and guessing the kind from the id prefix would
    make the filter's meaning depend on an id format the contract does not
    promise to keep. ``resource_type`` on its own remains a valid (and
    pre-existing) filter.
    """
    _require_scope(actor, _SCOPE_READ)
    if state not in ("active", "revoked", "all"):
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "state must be one of active, revoked, all"
        )
    _check_drive_id(drive_id)
    if resource_type is not None and resource_type not in ("drive", "folder", "artifact"):
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "resource_type must be drive, folder, or artifact"
        )
    if resource_id is not None and resource_type is None:
        raise V0ApiError(
            400,
            "INVALID_PARAMETER",
            "resource_id requires resource_type; a bare resource id is "
            "ambiguous across drive, folder, and artifact",
        )
    if resource_id is not None:
        _check_grant_resource_id(resource_type, resource_id)
    # The ONE list, shared with the writer, so the filter and the create
    # path cannot enumerate different principal kinds again.
    if principal_type is not None and principal_type not in core.PRINCIPAL_TYPES:
        raise V0ApiError(
            400,
            "INVALID_ARGUMENT",
            "principal_type must be agent, user, service, workspace, or public",
        )
    page_size = clamp_limit(limit)

    # A grants cursor is bound to the list's filters — a state/resource
    # swap between pages must fail closed rather than resume a foreign set.
    # The pre-existing keys stay present unconditionally and `resource_id` is
    # added ONLY when set, so an unfiltered cursor hashes exactly as it did
    # before this change. Adding a key unconditionally would have invalidated
    # every in-flight cursor at deploy — and under the candidate/flip rollout
    # a client paging this list would flap between 200 and 400 INVALID_CURSOR
    # depending on which revision served it.
    # Sealed-cursor key pinned to its original spelling (see `v0_drives`).
    bound = {
        "lifecycle": state,
        "resource_type": resource_type,
        "principal_type": principal_type,
    }
    if resource_id is not None:
        bound["resource_id"] = resource_id
    position = _unseal_cursor("grants", drive_id, cursor, bound=bound)
    after_id = cursor_str(position, "id") if position else None

    async with conn() as c:
        try:
            page = await core.list_grants(
                c, actor, drive_id,
                state=state, limit=page_size, after_id=after_id,
                resource_type=resource_type, resource_id=resource_id,
                principal_type=principal_type,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
    response.headers["Cache-Control"] = "private"
    return {
        "items": page["items"],
        "next_cursor": _seal_cursor(
            "grants", drive_id, page["next_cursor"], bound=bound
        ),
    }


@router.post(
    "/drives/{drive_id}/grants",
    status_code=201,
    response_model=GrantOut,
    operation_id="grants_create",
    dependencies=[Depends(known_params())],
)
async def create_grant(
    drive_id: str,
    request: Request,
    body: GrantCreateIn,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    response: Response = ...,
) -> GrantOut:
    """Grant one principal a role on a drive, folder, or artifact."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    if body.resource_type == "folder" and not ids.is_valid(body.resource_id, "fld"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed resource_id for a folder grant")
    if body.resource_type == "artifact" and not ids.is_valid(body.resource_id, "art"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed resource_id for an artifact grant")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.create_grant(
                c, actor, drive_id,
                principal_type=body.principal_type, principal_id=body.principal_id,
                resource_type=body.resource_type, resource_id=body.resource_id,
                role=body.role, expires_at=body.expires_at,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            201,
            {"ETag": _etag(result["revision"]),
             "Location": _grant_location(drive_id, result["id"]),
             "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/grants",
        request_hash=_body_hash(body),
        execute=execute,
        replay_guard=_target_manager_guard(
            actor, drive_id, body.resource_type, body.resource_id
        ),
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.get(
    "/drives/{drive_id}/grants/{grant_id}",
    response_model=GrantOut,
    responses={304: {"description": "If-None-Match matched."}},
    operation_id="grants_read",
    dependencies=[
        Depends(require_any_grant_in_drive()),
        Depends(known_params()),
    ],
)
async def read_grant(
    drive_id: str,
    grant_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> GrantOut:
    """Read one grant in the drive."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_grant_id(grant_id)
    async with conn() as c:
        try:
            result = await core.get_grant(c, actor, drive_id, grant_id)
        except Exception as exc:
            raise _mapping_error(exc) from None
    etag = _etag(result["revision"])
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private"})
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    return result


@router.patch(
    "/drives/{drive_id}/grants/{grant_id}",
    response_model=GrantOut,
    operation_id="grants_update",
    dependencies=[Depends(known_params())],
)
async def update_grant(
    drive_id: str,
    grant_id: str,
    request: Request,
    body: GrantUpdateIn,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> GrantOut:
    """Change a grant's role or expiry under If-Match."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_grant_id(grant_id)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.update_grant(
                c, actor, drive_id, grant_id,
                role=body.role, expires_at=body.expires_at,
                clear_expires_at=body.clear_expires, if_match=if_match,
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
        path=f"/v0/drives/{drive_id}/grants/{grant_id}",
        request_hash=_body_hash(body),
        execute=execute,
        replay_guard=_grant_row_manager_guard(actor, drive_id, grant_id),
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.delete(
    "/drives/{drive_id}/grants/{grant_id}",
    response_model=GrantOut,
    operation_id="grants_revoke",
    dependencies=[Depends(known_params())],
)
async def revoke_grant(
    drive_id: str,
    grant_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> GrantOut:
    """Revoke a grant (soft, sets revoked_at) under If-Match."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_grant_id(grant_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.revoke_grant(
                c, actor, drive_id, grant_id, if_match=if_match
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
        path=f"/v0/drives/{drive_id}/grants/{grant_id}",
        request_hash=_body_hash(None),
        execute=execute,
        replay_guard=_grant_row_manager_guard(actor, drive_id, grant_id),
    )
    response.status_code = status
    response.headers.update(headers)
    return payload
