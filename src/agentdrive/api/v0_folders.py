"""The folders vertical's HTTP surface (slice 5): 7 operations.

Wire semantics mirror the drives vertical (§6.2/§6.3):

  * create and copy are idempotent under an ``Idempotency-Key`` (claim →
    mutate → complete; a rejection before execution abandons the claim) —
    create returns 201 + ``Location``; same-drive copy returns 201 with the
    materialized folder (cross-drive copy is out of v0 scope and rejected
    with 400 INVALID_ARGUMENT);
  * patch / delete / restore are mutation-of-existing: they require both an
    ``Idempotency-Key`` and ``If-Match`` (428 absent, 412 stale), and bump the
    folder revision — the response ETag is the sanctioned source for the next
    mutation;
  * read returns the folder's ETag and honors ``If-None-Match`` → 304;
  * soft-deleted folders are only reachable through ``?state=deleted|all``
    listing; reads 404;
  * folders are scoped to the drive's workspace — a drive in another
    workspace is indistinguishable from absence (404 DRIVE_NOT_FOUND), and an
    unknown/soft-deleted folder is 404 FOLDER_NOT_FOUND.

Sibling folder AND artifact names share one collision domain, surfaced as
``409 FOLDER_PATH_CONFLICT`` by the core mutators.

Every failure renders the single top-level ``{"error": {code, message,
details}}`` envelope (§6.3) via ``v0_api_error_handler``; statuses are
explicit per call, including the preconditions 428/412.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import settings
from ..core import idempotency, ids, v0_drives
from ..core import v0_authz as authz
from ..core import v0_folders as core
from ..core.v0_content_commit import QuotaExceededError
from ..db import conn
from ..identity.actor import V0ActorContext
from .cursors import clamp_limit, cursor_str, cursor_ts
from .v0_authz import require_local
from .v0_cursors import seal as _seal_cursor
from .v0_cursors import unseal as _unseal_cursor
from .v0_deps import known_params, precondition_http, v0_actor
from .v0_errors import V0ApiError
from .v0_models import FolderCascadeOut, FolderListOut, FolderOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["folders"], dependencies=[Depends(enforce_v0_rate_limit)]
)

# Scope gate per operation. Token scope is half the authorization check;
# local capability is layered in later slices.
_SCOPE_READ = "content:read"
_SCOPE_WRITE = "content:write"

_FOLDER_ID_PATTERN = r"^fld_[a-f0-9]{16}$"
_DRIVE_ID_PATTERN = r"^drv_[a-f0-9]{16}$"


class FolderCreateIn(BaseModel):
    """POST /v0/drives/{id}/folders body."""

    parent_id: str = Field(pattern=_FOLDER_ID_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return core.validate_name(value)


class FolderUpdateIn(BaseModel):
    """PATCH /v0/drives/{id}/folders/{folder_id} body — at least one field is
    required."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: str | None = Field(default=None, pattern=_FOLDER_ID_PATTERN)
    metadata: dict[str, Any] | None = None
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return core.validate_name(value)

    @model_validator(mode="after")
    def _set_changed(self) -> FolderUpdateIn:
        fields = ("name", "parent_id", "metadata")
        if all(getattr(self, f) is None for f in fields):
            raise ValueError("provide at least one of name, parent_id, metadata")
        self._changed = frozenset(f for f in fields if getattr(self, f) is not None)
        return self

    @property
    def changed(self) -> frozenset[str]:
        return getattr(self, "_changed", frozenset())


class FolderCopyIn(BaseModel):
    """POST /v0/drives/{id}/folders/{folder_id}/copy body.

    ``destination_drive_id`` must equal the source drive (or be absent) —
    cross-drive copy is out of v0 scope and rejected."""

    destination_drive_id: str | None = Field(default=None, pattern=_DRIVE_ID_PATTERN)
    destination_parent_id: str = Field(pattern=_FOLDER_ID_PATTERN)
    destination_name: str = Field(min_length=1, max_length=255)
    model_config = ConfigDict(extra="forbid")

    @field_validator("destination_name", mode="before")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return core.validate_name(value)


def _require_scope(actor: V0ActorContext, scope: str) -> None:
    if not actor.can(scope):
        raise V0ApiError(
            403, "PERMISSION_DENIED", f"the token does not carry the {scope} scope"
        )


def _check_drive_id(drive_id: str) -> None:
    if not ids.is_valid(drive_id, "drv"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")


def _check_folder_id(folder_id: str) -> None:
    if not ids.is_valid(folder_id, "fld"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed folder id")


def _etag(revision: str) -> str:
    return f'"{revision}"'


def _folder_location(drive_id: str, folder_id: str) -> str:
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    return f"{origin}/v0/drives/{drive_id}/folders/{folder_id}"


def _body_hash(model: BaseModel | None) -> str:
    """Stable request identity for the idempotency ledger: method+path+hash.

    Built from the parsed request (never raw bytes) so semantically identical
    requests — whitespace/ordering differences — replay the same result.
    """
    payload = model.model_dump(mode="json") if model is not None else {}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _body_hash_for_delete(recursive: bool) -> str:
    """Idempotency hash folding the ``recursive`` query flag, which changes
    the semantics of ``folders_delete`` even though the route body is empty."""
    payload: dict[str, Any] = {"recursive": recursive}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


async def _not_found_drive(awaitable: Any) -> Any:
    """Map DriveNotFoundError (404 semantics) to the uniform envelope."""
    try:
        return await awaitable
    except v0_drives.DriveNotFoundError:
        raise V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace") from None


def _destination_editor_guard(
    actor: Any, drive_id: str, parent_id: str | None
) -> Callable[..., Any]:
    """Replay-time authorization for a move's destination.

    The destination check lives inside `execute`, which idempotent replay
    skips, so without this a principal whose editor grant on the destination
    was revoked could replay a stored success and move the folder anyway.
    `artifacts_update` has carried the equivalent guard since it shipped
    (§6.2); this is the folder half.
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


def _mapping_error(exc: Exception) -> V0ApiError:
    """Folders-core exceptions → V0ApiError at explicit status/code/message."""
    if isinstance(exc, v0_drives.PreconditionError):
        return precondition_http(exc)
    if isinstance(exc, v0_drives.DriveNotFoundError):
        return V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace")
    if isinstance(exc, core.FolderNotFoundError):
        return V0ApiError(404, "FOLDER_NOT_FOUND", "no such folder in this drive")
    if isinstance(exc, authz.NotAuthorizedError):
        return V0ApiError(404, "NOT_AUTHORIZED", "not authorized on this resource")
    if isinstance(exc, (core.RootFolderError, core.InvalidMoveError, core.FolderNotDeletedError)):
        return V0ApiError(409, "CONFLICT", str(exc))
    if isinstance(exc, core.FolderNameConflictError):
        return V0ApiError(409, "FOLDER_PATH_CONFLICT", str(exc))
    if isinstance(exc, core.SubtreeTooLargeError):
        return V0ApiError(409, "SUBTREE_TOO_LARGE", str(exc))
    if isinstance(exc, core.RecursiveRequiredError):
        return V0ApiError(409, "FOLDER_RECURSIVE_REQUIRED", str(exc))
    if isinstance(exc, core.InvalidFolderNameError):
        return V0ApiError(400, "INVALID_ARGUMENT", str(exc))
    if isinstance(exc, QuotaExceededError):
        # `folders_copy` reserves logical bytes per copied version, so it hits
        # the same ceiling the uploads path does and must answer the same way
        # rather than surfacing a 500.
        return V0ApiError(
            422, "TRANSFER_LIMIT_EXCEEDED",
            "the storage reservation cannot be acquired",
            details={"limit_name": str(exc)},
        )
    raise exc


def _parent_editor_guard(
    actor: V0ActorContext, drive_id: str, parent_id: str
) -> Callable[..., Any]:
    """Replay-time authorization for create_folder/copy (parent in body).

    The parent-editor check lives inside `execute`, which idempotent replay
    skips (the body is not re-parsed). This guard re-runs it on replay so a
    principal whose editor grant was revoked cannot replay a stored 201
    (§6.2). The workspace check mirrors the first-execution path.
    """

    async def _guard(c: Any) -> None:
        try:
            await core._ensure_drive(c, actor, drive_id)
            await authz.require(
                c, actor=actor, drive_id=drive_id,
                resource_type="folder", resource_id=parent_id, minimum="editor",
            )
        except Exception as exc:
            raise _mapping_error(exc) from None

    return _guard


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
    """Claim → execute-in-transaction → complete (abandon on rejection).

    §7.2: only executed mutations create records. `execute(c)` returns
    (status, headers, body); on ANY exception the transaction rolls back and
    the claim is abandoned so the key stays usable for a corrected retry — a
    428/412/404/limit rejection must not burn it until expiry.

    `replay_guard(c)` (when given) runs on REPLAY before the stored result is
    returned, on a fresh connection. Authorization checks that live INSIDE
    `execute` (parent-editor for body-derived parents) never run on replay —
    the request body is not re-parsed — so a guard re-checks them, honoring
    §6.2's "replay re-checks authorization first" for those paths too.
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


@router.get(
    "/drives/{drive_id}/folders",
    response_model=FolderListOut,
    operation_id="folders_list",
    dependencies=[Depends(known_params("state", "limit", "cursor", "parent_id", "name"))],
)
async def list_folders(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    state: str = "active",
    limit: int | None = None,
    cursor: str | None = None,
    parent_id: str | None = None,
    name: str | None = None,
    response: Response = ...,
) -> FolderListOut:
    """List the drive's folders, newest-first (keyset paginated).

    ``state`` (active|deleted|all) exposes soft-deleted folders so the
    post-delete revision can be read as the If-Match source for a restore.
    ``parent_id`` / ``name`` are exact-match filters. Unknown query parameters
    are rejected (§6.3)."""
    _require_scope(actor, _SCOPE_READ)
    if state not in ("active", "deleted", "all"):
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "state must be one of active, deleted, all"
        )
    _check_drive_id(drive_id)
    if parent_id is not None:
        _check_folder_id(parent_id)
    if name is not None:
        try:
            name = core.validate_name(name)
        except core.InvalidFolderNameError as exc:
            raise V0ApiError(400, "INVALID_ARGUMENT", str(exc)) from None
    page_size = clamp_limit(limit)

    # Sealed-cursor key pinned to its original spelling (see `v0_drives`).
    bound = {"lifecycle": state, "parent_id": parent_id, "name": name}
    position = _unseal_cursor("folders", drive_id, cursor, bound=bound)
    after_ts = cursor_ts(position, "created_at") if position else None
    after_id = cursor_str(position, "id") if position else None

    async with conn() as c:
        result = await _not_found_drive(
            core.list_folders(
                c,
                actor,
                drive_id,
                state=state,
                limit=page_size,
                after_ts=after_ts,
                after_id=after_id,
                parent_id=parent_id,
                name=name,
            )
        )
    response.headers["Cache-Control"] = "private"
    return {
        "items": result["items"],
        "next_cursor": _seal_cursor(
            "folders", drive_id, result["next_cursor"], bound=bound
        ),
    }


@router.post(
    "/drives/{drive_id}/folders",
    status_code=201,
    response_model=FolderOut,
    operation_id="folders_create",
    dependencies=[Depends(known_params())],
)
async def create_folder(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    body: FolderCreateIn = ...,
    response: Response = ...,
) -> FolderOut:
    """Create one folder under `parent_id`; idempotent under the
    ``Idempotency-Key``."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            # Drive workspace membership first (cross-workspace = DRIVE_NOT_FOUND),
            # then editor capability on the parent folder.
            await core._ensure_drive(c, actor, drive_id)
            await authz.require(
                c, actor=actor, drive_id=drive_id,
                resource_type="folder", resource_id=body.parent_id, minimum="editor",
            )
            folder = await core.create_folder(
                c,
                actor,
                drive_id,
                parent_id=body.parent_id,
                name=body.name,
                metadata=body.metadata,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            201,
            {
                "ETag": _etag(folder["revision"]),
                "Location": _folder_location(drive_id, folder["id"]),
                "Cache-Control": "private",
            },
            folder,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/folders",
        request_hash=_body_hash(body),
        execute=execute,
        replay_guard=_parent_editor_guard(actor, drive_id, body.parent_id),
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.get(
    "/drives/{drive_id}/folders/{folder_id}",
    response_model=FolderOut,
    responses={304: {"description": "If-None-Match matched."}},
    operation_id="folders_read",
    dependencies=[
        Depends(require_local("viewer", "folder", "folder_id")),
        Depends(known_params()),
    ],
)
async def read_folder(
    drive_id: str,
    folder_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> FolderOut:
    """Read one active folder. ETag = quoted revision; matching
    ``If-None-Match`` → 304. Deleted and cross-workspace folders are 404."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_folder_id(folder_id)
    async with conn() as c:
        try:
            folder = await core.get_folder(c, actor, drive_id, folder_id)
        except Exception as exc:
            raise _mapping_error(exc) from None
    etag = _etag(folder["revision"])
    values = v0_drives.etag_values(if_none_match) if if_none_match else None
    if values == "*" or (values and folder["revision"] in values):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private"})
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    return folder


@router.patch(
    "/drives/{drive_id}/folders/{folder_id}",
    response_model=FolderOut,
    operation_id="folders_update",
    dependencies=[
        Depends(require_local("editor", "folder", "folder_id")),
        Depends(known_params()),
    ],
)
async def update_folder(
    drive_id: str,
    folder_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    body: FolderUpdateIn = ...,
    response: Response = ...,
) -> FolderOut:
    """Rename / move / update a folder's metadata. Requires
    ``Idempotency-Key`` and ``If-Match`` (428 absent, 412 stale); bumps the
    revision."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_folder_id(folder_id)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            # A move requires editor on the DESTINATION folder — even when it
            # equals the current parent — exactly as `artifacts_update` does.
            # The route gate only proves editor on the folder being moved, so
            # without this an editor could reparent a subtree into a namespace
            # they hold no capability on.
            if body.parent_id is not None:
                await core._ensure_drive(c, actor, drive_id)
                await authz.require(
                    c, actor=actor, drive_id=drive_id,
                    resource_type="folder", resource_id=body.parent_id,
                    minimum="editor",
                )
            folder = await core.patch_folder(
                c,
                actor,
                drive_id,
                folder_id,
                name=body.name,
                parent_id=body.parent_id,
                metadata=body.metadata,
                changed=body.changed,
                if_match=if_match,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            200,
            {"ETag": _etag(folder["revision"]), "Cache-Control": "private"},
            folder,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="PATCH",
        path=f"/v0/drives/{drive_id}/folders/{folder_id}",
        request_hash=_body_hash(body),
        execute=execute,
        replay_guard=_destination_editor_guard(actor, drive_id, body.parent_id),
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.delete(
    "/drives/{drive_id}/folders/{folder_id}",
    response_model=FolderCascadeOut,
    operation_id="folders_delete",
    dependencies=[
        Depends(require_local("manager", "folder", "folder_id")),
        Depends(known_params("recursive")),
    ],
)
async def delete_folder(
    drive_id: str,
    folder_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    recursive: bool = False,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> FolderCascadeOut:
    """Soft-delete a folder and its full live subtree (folders + artifacts) in
    one transaction. A non-empty subtree requires ``recursive=true`` (409
    FOLDER_RECURSIVE_REQUIRED otherwise). Returns the deleted root
    representation plus exact cascade counts and the post-delete
    revision/ETag for a restore."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_folder_id(folder_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.soft_delete_folder(
                c, actor, drive_id, folder_id, recursive=recursive, if_match=if_match
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            200,
            {"ETag": _etag(result["folder"]["revision"]), "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="DELETE",
        path=f"/v0/drives/{drive_id}/folders/{folder_id}",
        request_hash=_body_hash_for_delete(recursive),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.post(
    "/drives/{drive_id}/folders/{folder_id}/restore",
    response_model=FolderCascadeOut,
    operation_id="folders_restore",
    dependencies=[
        Depends(require_local("manager", "folder", "folder_id", include_deleted=True)),
        Depends(known_params()),
    ],
)
async def restore_folder(
    drive_id: str,
    folder_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> FolderCascadeOut:
    """Restore a soft-deleted folder and its deleted subtree atomically.
    If-Match must carry the post-delete revision; restoring an already-active
    folder is 409 CONFLICT."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_folder_id(folder_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.restore_folder(
                c, actor, drive_id, folder_id, if_match=if_match
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            200,
            {"ETag": _etag(result["folder"]["revision"]), "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/folders/{folder_id}/restore",
        request_hash=_body_hash(None),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.post(
    "/drives/{drive_id}/folders/{folder_id}/copy",
    status_code=201,
    response_model=FolderOut,
    operation_id="folders_copy",
    dependencies=[
        Depends(require_local("editor", "folder", "folder_id")),
        Depends(known_params()),
    ],
)
async def copy_folder(
    drive_id: str,
    folder_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    body: FolderCopyIn = ...,
    response: Response = ...,
) -> FolderOut:
    """Copy a folder's subtree within the same drive.

    Cross-drive copy is out of v0 scope and rejected (400 INVALID_ARGUMENT).
    ``destination_drive_id`` must equal the source drive when present.
    Materializes the subtree synchronously → 201 + the copied folder.
    The source root, descendant folders, and artifacts may total at most
    5,000 live resources; larger copies fail with 409 SUBTREE_TOO_LARGE.
    ``If-Match`` is optional; when present it is validated against the source
    revision (412 stale)."""
    _require_scope(actor, _SCOPE_READ)
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_folder_id(folder_id)
    if body.destination_drive_id is not None and body.destination_drive_id != drive_id:
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "cross-drive copy is not available in v0"
        )
    destination_drive_id = body.destination_drive_id or drive_id

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            # Writing into the destination parent requires editor there too.
            await authz.require(
                c, actor=actor, drive_id=destination_drive_id,
                resource_type="folder", resource_id=body.destination_parent_id,
                minimum="editor",
            )
            result = await core.copy_folder(
                c,
                actor,
                drive_id,
                folder_id,
                destination_drive_id=destination_drive_id,
                destination_parent_id=body.destination_parent_id,
                destination_name=body.destination_name,
                destination_etag=if_match,
                idempotency_key=idempotency_key or "",
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        folder = result
        return (
            201,
            {
                "Location": _folder_location(drive_id, folder["id"]),
                "ETag": _etag(folder["revision"]),
                "Cache-Control": "private",
            },
            folder,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/folders/{folder_id}/copy",
        request_hash=_body_hash(body),
        execute=execute,
        replay_guard=_parent_editor_guard(
            actor, destination_drive_id, body.destination_parent_id
        ),
    )
    response.status_code = status
    response.headers.update(headers)
    return payload
