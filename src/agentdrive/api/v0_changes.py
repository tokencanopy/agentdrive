"""Changes vertical's HTTP surface (slice 9): one operation.

``GET /v0/drives/{drive_id}/changes`` — the cursor-resumable pull feed.
Accepts exactly one of ``start=now|beginning`` or ``cursor=...``. Requires
``changes:read`` AND a live local drive grant (checked via the shared authz
primitive) on every capture and page read. Uses the specialized changes
envelope, not the generic ``items``/``next_cursor`` collection envelope.

Every failure renders the single top-level ``{"error": {code, message,
details}}`` envelope (§6.3).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response

from ..core import ids
from ..core import v0_authz as authz
from ..core import v0_changes as core
from ..core import v0_grants as grants_core
from ..db import conn
from ..identity.actor import V0ActorContext
from .cursors import clamp_limit
from .v0_deps import known_params, v0_actor
from .v0_errors import V0ApiError
from .v0_models import ChangePageOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["changes"], dependencies=[Depends(enforce_v0_rate_limit)]
)

_SCOPE_READ = "changes:read"


def _mapping_error(exc: Exception) -> V0ApiError:
    if isinstance(exc, authz.DriveNotFoundError):
        return V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace")
    if isinstance(exc, authz.NotAuthorizedError):
        return V0ApiError(404, "NOT_AUTHORIZED", "not authorized on this resource")
    if isinstance(exc, core.ChangeCursorGoneError):
        return V0ApiError(
            410, "CHANGE_CURSOR_EXPIRED",
            "the change cursor expired or fell behind the retained history",
            details={"recovery": "full_sync"},
        )
    if isinstance(exc, core.ChangeCursorMismatchError):
        return V0ApiError(400, "INVALID_CURSOR", "the change cursor is not valid for this drive")
    raise exc


@router.get(
    "/drives/{drive_id}/changes",
    response_model=ChangePageOut,
    operation_id="changes_list",
    dependencies=[Depends(known_params("limit", "start", "cursor", "type", "order"))],
)
async def list_changes(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    limit: int | None = None,
    start: Literal["now", "beginning"] | None = None,
    cursor: str | None = None,
    type: str | None = None,
    order: Literal["oldest", "newest"] | None = None,
    response: Response = ...,
) -> ChangePageOut:
    """Pull one page of changes. Exactly one of ``start`` or ``cursor``.

    ``order`` selects the direction of the walk and accompanies ``start``, never
    a ``cursor`` — a cursor already carries the direction it was minted for, so
    repeating it could only ever contradict it.

    ``oldest`` (the default) is the resumable sync walk: forward from the
    position, and a drained cursor re-presented later picks up what committed
    since. ``newest`` is a browse walk for a history screen: it captures the
    head and walks down toward the retention floor, newest row first. Because
    new events land ABOVE a captured head, a drained descending cursor stays
    drained — a reader checking for new activity captures the head again. That
    is also why ``order=newest`` takes only ``start=now``: ``beginning`` names
    the far end of a walk that already ends there.

    ``type`` is an optional comma-separated allow-list of exact event-type
    strings (e.g. ``type=folder.created,artifact.updated`` for content only, or
    ``type=grant.created,grant.updated,grant.revoked`` for grant events). A
    comma-list — not a single value or a ``grant.*`` glob — because the useful
    sync queries ("content only", "all permission events") are SETS of exact
    types, and exact-match keeps the filter's meaning independent of the dotted
    naming (§6.3: unknown params are rejected; unknown type VALUES 400 here).
    Permission types requested by a non-manager are silently empty (the
    manager filter still applies), never an existence oracle."""
    if not actor.can(_SCOPE_READ):
        raise V0ApiError(
            403, "PERMISSION_DENIED", f"the token does not carry the {_SCOPE_READ} scope"
        )
    if (start is None) == (cursor is None):
        raise V0ApiError(
            400, "INVALID_REQUEST", "pass exactly one of start or cursor"
        )
    if order is not None and cursor is not None:
        raise V0ApiError(
            400, "INVALID_REQUEST",
            "order applies to start; a cursor already carries its direction",
        )
    if order == "newest" and start == "beginning":
        raise V0ApiError(
            400, "INVALID_REQUEST",
            "order=newest starts at the head; pass start=now",
        )
    if not ids.is_valid(drive_id, "drv"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")

    types = _parse_type_filter(type)

    page_size = clamp_limit(limit)
    async with conn() as c:
        # The drive must be in the caller's workspace (cross-workspace reads as
        # absent — no disclosure), then a live local drive grant is required
        # (a public share does not confer change-feed access, §9).
        workspace = await c.fetchval(
            "SELECT workspace_id FROM drives WHERE id=$1 AND deleted_at IS NULL",
            drive_id,
        )
        if workspace != actor.workspace_id:
            raise V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace")
        try:
            await authz.require(
                c, actor=actor, drive_id=drive_id,
                resource_type="drive", resource_id=drive_id, minimum="viewer",
                include_public=False,  # a public share does not confer feed access (§9)
            )
            # Permission events are manager-only. The decision is the SAME
            # predicate #430 uses for grant enumeration (not a second check),
            # resolved live per request so a demotion takes effect immediately.
            is_manager = await grants_core.is_drive_manager(c, actor, drive_id)
            if start is not None:
                page_cursor = await core.capture(
                    c, actor, drive_id, start=start, order=order or "oldest",
                )
            else:
                page_cursor = cursor
            page = await core.read_page(
                c, actor=actor, drive_id=drive_id,
                cursor=page_cursor, limit=page_size,
                include_permission_events=is_manager,
                types=types,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None

    response.headers["Cache-Control"] = "private"
    return page


def _parse_type_filter(raw: str | None) -> list[str] | None:
    """Parse the ``type`` query param into a de-duplicated exact-match list.

    ``None`` (param absent) means no type filter. An empty value or any
    unknown type string is a 400 — the allow-list is the server-controlled
    ``_CHANGE_TYPES`` vocabulary, so a typo fails loudly rather than silently
    matching nothing."""
    if raw is None:
        return None
    wanted = [t.strip() for t in raw.split(",") if t.strip()]
    if not wanted:
        raise V0ApiError(
            400, "INVALID_ARGUMENT", "type must be a comma-separated list of event types"
        )
    unknown = [t for t in wanted if t not in core._CHANGE_TYPES]
    if unknown:
        raise V0ApiError(
            400, "INVALID_ARGUMENT",
            f"unknown change type(s): {', '.join(sorted(set(unknown)))}",
        )
    # Preserve first-seen order, drop duplicates.
    return list(dict.fromkeys(wanted))
