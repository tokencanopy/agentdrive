"""D13 drive-scoped namespace navigation endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response

from ..core import ids
from ..core import v0_folders as folders_core
from ..core import v0_navigation as core
from ..db import conn
from ..identity.actor import V0ActorContext
from .cursors import clamp_limit, cursor_str, cursor_ts
from .v0_cursors import seal as _seal_cursor
from .v0_cursors import unseal as _unseal_cursor
from .v0_deps import known_params, v0_actor
from .v0_errors import V0ApiError
from .v0_models import EntryListOut, LookupOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["navigation"], dependencies=[Depends(enforce_v0_rate_limit)]
)

_SCOPE_READ = "content:read"
_MAX_PATH_SEGMENTS = 256


def _require_scope(actor: V0ActorContext) -> None:
    if not actor.can(_SCOPE_READ):
        raise V0ApiError(
            403,
            "PERMISSION_DENIED",
            f"the token does not carry the {_SCOPE_READ} scope",
        )


def _check_drive_id(drive_id: str) -> None:
    if not ids.is_valid(drive_id, "drv"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")


def _check_folder_id(folder_id: str) -> None:
    if not ids.is_valid(folder_id, "fld"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed parent_id")


def _entry_type(value: str | None) -> Literal["folder", "artifact"] | None:
    if value not in (None, "folder", "artifact"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "type must be folder or artifact")
    return value


def _not_found() -> V0ApiError:
    return V0ApiError(404, "NOT_FOUND", "resource not found")


def _cursor_bound(
    *,
    parent_id: str,
    entry_type: str | None,
    name: str | None,
    label: str | None,
    content_type: str | None,
    updated_after: datetime | None,
    updated_before: datetime | None,
    state: str,
) -> dict[str, str | None]:
    return {
        "parent_id": parent_id,
        "type": entry_type,
        "name": name,
        "label": label,
        "content_type": content_type,
        "updated_after": updated_after.isoformat() if updated_after else None,
        "updated_before": updated_before.isoformat() if updated_before else None,
        "state": state,
    }


@router.get(
    "/drives/{drive_id}/entries",
    response_model=EntryListOut,
    operation_id="entries_list",
    dependencies=[Depends(known_params(
        "parent_id", "type", "name", "label", "content_type",
        "updated_after", "updated_before", "state", "limit", "cursor",
    ))],
)
async def list_entries(
    drive_id: str,
    parent_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    type: str | None = None,
    name: str | None = None,
    label: str | None = None,
    content_type: str | None = None,
    updated_after: datetime | None = None,
    updated_before: datetime | None = None,
    state: str = "active",
    limit: int | None = None,
    cursor: str | None = None,
    response: Response = ...,
) -> EntryListOut:
    """List one folder's direct children across the shared namespace.

    ``state`` (active|deleted|all) exposes soft-deleted entries, matching
    every other v0 collection. Unknown query parameters are rejected."""
    _require_scope(actor)
    _check_drive_id(drive_id)
    _check_folder_id(parent_id)
    requested_type = _entry_type(type)
    if state not in ("active", "deleted", "all"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "state must be one of active, deleted, all")
    if name is not None:
        try:
            folders_core.validate_name(name)
        except folders_core.InvalidFolderNameError as exc:
            raise V0ApiError(400, "INVALID_ARGUMENT", str(exc)) from None
    if any(value is not None for value in (label, content_type, updated_after, updated_before)):
        if requested_type == "folder":
            raise V0ApiError(
                400,
                "INVALID_ARGUMENT",
                "artifact filters cannot be combined with type=folder",
            )
        requested_type = "artifact"
    if (
        updated_after is not None
        and updated_before is not None
        and updated_after > updated_before
    ):
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "updated_after must not exceed updated_before"
        )

    page_size = clamp_limit(limit)
    bound = _cursor_bound(
        parent_id=parent_id,
        entry_type=requested_type,
        name=name,
        label=label,
        content_type=content_type,
        updated_after=updated_after,
        updated_before=updated_before,
        state=state,
    )
    position = _unseal_cursor("entries", drive_id, cursor, bound=bound)
    after_ts = cursor_ts(position, "created_at") if position else None
    after_id = cursor_str(position, "id") if position else None

    async with conn() as c:
        try:
            page = await core.list_entries(
                c,
                actor,
                drive_id,
                parent_id=parent_id,
                entry_type=requested_type,
                name=name,
                label=label,
                content_type=content_type,
                updated_after=updated_after,
                updated_before=updated_before,
                state=state,
                limit=page_size,
                after_ts=after_ts,
                after_id=after_id,
            )
        except core.NavigationNotFoundError:
            raise _not_found() from None
    response.headers["Cache-Control"] = "private"
    return {
        "entries": page["entries"],
        "next_cursor": _seal_cursor(
            "entries", drive_id, page["next_cursor"], bound=bound
        ),
    }


def _path_segments(path: str) -> list[str]:
    if not path or path.startswith("/") or path.endswith("/") or "\\" in path:
        raise V0ApiError(400, "INVALID_ARGUMENT", "path must be a relative canonical path")
    segments = path.split("/")
    if len(segments) > _MAX_PATH_SEGMENTS:
        raise V0ApiError(400, "INVALID_ARGUMENT", "path has too many segments")
    try:
        return [folders_core.validate_name(segment) for segment in segments]
    except folders_core.InvalidFolderNameError:
        raise V0ApiError(400, "INVALID_ARGUMENT", "path contains an invalid segment") from None


@router.get(
    "/drives/{drive_id}/lookup",
    response_model=LookupOut,
    operation_id="lookup",
    dependencies=[Depends(known_params("path", "type"))],
)
async def lookup(
    drive_id: str,
    path: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    type: str | None = None,
    response: Response = ...,
) -> LookupOut:
    """Resolve a complete root-relative path to one stable resource id."""
    _require_scope(actor)
    _check_drive_id(drive_id)
    requested_type = _entry_type(type)
    segments = _path_segments(path)
    async with conn() as c:
        try:
            result = await core.lookup(
                c,
                actor,
                drive_id,
                segments=segments,
                entry_type=requested_type,
            )
        except core.NavigationNotFoundError:
            raise _not_found() from None
    response.headers["Cache-Control"] = "private"
    return result
