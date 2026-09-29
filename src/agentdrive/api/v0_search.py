"""Search vertical's HTTP surface (slice 9): one operation.

``GET /v0/drives/{drive_id}/search`` — drive-scoped lexical retrieval over
the artifact search index (§11.1). Only ``lexical`` mode is enabled; a
request for a disabled mode fails ``400 SEARCH_MODE_UNAVAILABLE``. Requires
``content:read``; hits are visibility-filtered by local grants (see
``core/v0_search``), so a caller with the scope but no grants gets an empty
page, never a leak.

Every failure renders the single top-level ``{"error": {code, message,
details}}`` envelope (§6.3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response

from ..core import ids
from ..core import v0_search as core
from ..core.cursors import BadCursor
from ..db import conn
from ..identity.actor import V0ActorContext
from .cursors import clamp_limit
from .v0_deps import known_params, v0_actor
from .v0_errors import V0ApiError
from .v0_models import SearchPageOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["search"], dependencies=[Depends(enforce_v0_rate_limit)]
)

_SCOPE_READ = "content:read"
_ENABLED_MODES = ("lexical",)


def _mapping_error(exc: Exception) -> V0ApiError:
    if isinstance(exc, core.SearchDriveNotFoundError):
        return V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace")
    if isinstance(exc, BadCursor):
        return V0ApiError(400, "INVALID_CURSOR", "the cursor is invalid or expired")
    raise exc


@router.get(
    "/drives/{drive_id}/search",
    response_model=SearchPageOut,
    operation_id="drive_search",
    dependencies=[Depends(known_params(
        "q", "mode", "limit", "cursor", "parent_id", "content_type", "label",
        "updated_after", "updated_before",
    ))],
)
async def drive_search(
    drive_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    q: Annotated[str, Query(min_length=1)],
    mode: Literal["lexical", "hybrid", "semantic"] = "lexical",
    limit: int | None = None,
    cursor: str | None = None,
    parent_id: str | None = None,
    content_type: str | None = None,
    label: str | None = None,
    updated_after: datetime | None = None,
    updated_before: datetime | None = None,
    response: Response = ...,
) -> SearchPageOut:
    """Search the drive's live artifacts. ``q`` is required and must be
    non-empty.

    ``mode`` selects the retrieval engine: ``lexical``, ``hybrid``, or
    ``semantic``. This deployment enables ``lexical`` only; requesting a
    disabled mode fails ``400 SEARCH_MODE_UNAVAILABLE``.

    Each hit's ``snippet`` is HTML-safe by contract: artifact content is
    entity-escaped and only the server's own ``<mark>``/``</mark>`` highlight
    pair survives, so a client may render it as HTML."""
    if not actor.can(_SCOPE_READ):
        raise V0ApiError(
            403, "PERMISSION_DENIED", f"the token does not carry the {_SCOPE_READ} scope"
        )
    if mode not in _ENABLED_MODES:
        raise V0ApiError(
            400, "SEARCH_MODE_UNAVAILABLE", f"search mode {mode!r} is not enabled"
        )
    if not ids.is_valid(drive_id, "drv"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")
    if parent_id is not None and not ids.is_valid(parent_id, "fld"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed parent_id")

    page_size = clamp_limit(limit)
    try:
        async with conn() as c:
            page = await core.search_authorized(
                c, actor, drive_id,
                q=q, limit=page_size, cursor=cursor,
                parent_id=parent_id, content_type=content_type, label=label,
                updated_after=updated_after, updated_before=updated_before,
            )
    except Exception as exc:
        raise _mapping_error(exc) from None
    response.headers["Cache-Control"] = "private"
    return page
