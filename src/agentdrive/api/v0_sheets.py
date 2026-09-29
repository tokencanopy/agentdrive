"""Sheet reads: the workbook index and one rectangle of committed values.

Two operations, both reads, both artifact-scoped. Sessions are a separate
surface; nothing here mutates.

Shared handlers come from ``v0_artifacts`` — one source of truth for the
reset surface's cross-cutting behaviour (scope checks, id validation, ETag
comparison, error mapping). Reimplementing any of them here is how two
surfaces answer the same malformed request differently.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from ..core import cursors as core_cursors
from ..core import ids
from ..core import v0_sheets as core
from ..db import conn
from ..identity.actor import V0ActorContext
from ..sheets.a1 import InvalidRange, Rect, parse_range
from .v0_artifacts import (
    _check_artifact_id,
    _check_drive_id,
    _etag,
    _etag_matches,
    _mapping_error,
    _require_scope,
)
from .v0_authz import require_local
from .v0_deps import known_params, v0_actor
from .v0_errors import V0ApiError
from .v0_models import RFC3339
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["sheets"], dependencies=[Depends(enforce_v0_rate_limit)]
)
sheet_sessions_router = APIRouter(
    prefix="/v0", tags=["sheets"], dependencies=[Depends(enforce_v0_rate_limit)]
)

_SCOPE_READ = "content:read"

# The whole used range, when a caller omits `range`. Still bounded by
# MAX_READ_CELLS, so a huge sheet is refused rather than silently truncated.
_WHOLE_SHEET = "A1"


class EditabilityOut(BaseModel):
    """Discriminated, not a boolean plus an optional reason — a client
    branches on `status` and never reasons about a sometimes-absent field."""

    status: str
    reason: str | None = None


class SheetOut(BaseModel):
    name: str
    index: int
    rows: int
    columns: int
    has_formulas: bool


class WorkbookOut(BaseModel):
    artifact_id: str
    revision: str
    format: str
    cell_count: int
    editability: EditabilityOut


class SheetIndexOut(BaseModel):
    """A representation of the workbook, deliberately NOT a paginated
    collection: sheets are not independently addressable resources and their
    number is bounded by the file itself. The contract's paginate-everything
    rule guards collections that grow without limit; this is closer to an
    artifact's metadata than to a listing."""

    workbook: WorkbookOut
    sheets: list[SheetOut]


class CellRangeOut(BaseModel):
    sheet: str
    range: str
    revision: str
    values: list[list[Any]]
    model_config = ConfigDict(extra="forbid")


def _map_sheet_error(exc: Exception) -> V0ApiError:
    """Sheet-specific failures first, then the shared artifact mapping.

    `NotASpreadsheet` is deliberately NOT a 404: the artifact exists and the
    caller can see it in a listing, so claiming it is gone would be a lie
    about a resource they hold a grant on.
    """
    if isinstance(exc, core.NotASpreadsheet):
        return V0ApiError(409, "WORKBOOK_NOT_EDITABLE", str(exc))
    if isinstance(exc, core.SheetNotFound):
        return V0ApiError(404, "SHEET_NOT_FOUND", str(exc))
    if isinstance(exc, core.RangeTooLarge):
        return V0ApiError(413, "PAYLOAD_TOO_LARGE", str(exc))
    if isinstance(exc, core.AmbiguousSheet):
        return V0ApiError(400, "INVALID_ARGUMENT", str(exc))
    from ..sheets.workbook import Unparseable

    if isinstance(exc, Unparseable):
        return V0ApiError(422, "WORKBOOK_UNPARSEABLE", str(exc))
    return _mapping_error(exc)


@router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/sheets",
    response_model=SheetIndexOut,
    operation_id="sheets_list",
    responses={304: {"description": "If-None-Match matched."}},
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def sheets_list(
    drive_id: str,
    artifact_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> Any:
    """The workbook's sheets and whether it can be edited.

    One call carries three things a caller needs before opening a session:
    the structure, the editability verdict, and — as the ETag — the revision
    that becomes the session's `If-Match`. So opening a session costs one
    read, not two.
    """
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    async with conn() as c:
        try:
            index, revision = await core.read_workbook_index(
                c, actor, drive_id, artifact_id
            )
        except Exception as exc:
            raise _map_sheet_error(exc) from None

    etag = _etag(revision)
    if _etag_matches(if_none_match, etag):
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": "private"}
        )
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    return {
        "workbook": {
            "artifact_id": artifact_id,
            "revision": revision,
            "format": index.format,
            "cell_count": index.cell_count,
            "editability": {
                "status": index.editability.status,
                "reason": index.editability.reason,
            },
        },
        "sheets": [
            {
                "name": s.name,
                "index": s.index,
                "rows": s.rows,
                "columns": s.columns,
                "has_formulas": s.has_formulas,
            }
            for s in index.sheets
        ],
    }


def _check_version_id(version_id: str) -> None:
    if not ids.is_valid(version_id, "ver"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed version id")


def _rect(range_a1: str | None, used: str | None = None) -> Rect:
    try:
        return parse_range(range_a1 or used or _WHOLE_SHEET)
    except InvalidRange as exc:
        raise V0ApiError(400, "INVALID_ARGUMENT", str(exc)) from None


@router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/cells",
    response_model=CellRangeOut,
    operation_id="sheet_cells_read",
    responses={304: {"description": "If-None-Match matched."}},
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
        Depends(known_params("sheet", "range")),
    ],
)
async def sheet_cells_read(
    drive_id: str,
    artifact_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    sheet: str | None = None,
    range: str | None = None,  # noqa: A002 - the wire name is `range`
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> Any:
    """One rectangle of committed values.

    `range` IS the pagination here: the caller chooses the window and the
    server caps it, so this needs no cursor. The response is always exactly
    the rectangle asked for, padded with nulls outside the used range —
    ragged arrays would put the padding logic in every client instead.
    """
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    rect = _rect(range)
    async with conn() as c:
        try:
            result = await core.read_cells(
                c, actor, drive_id, artifact_id, sheet=sheet, rect=rect
            )
        except Exception as exc:
            raise _map_sheet_error(exc) from None

    etag = _etag(result["revision"])
    if _etag_matches(if_none_match, etag):
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": "private"}
        )
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    from ..sheets.a1 import format_range

    return {
        "sheet": result["sheet"],
        "range": format_range(rect),
        "revision": result["revision"],
        "values": result["values"],
    }


# ── edit sessions (design §5.5–§5.8) ──────────────────────────────────────

from ..core import v0_sheet_sessions as sessions  # noqa: E402
from ..core.v0_drives import PreconditionError  # noqa: E402
from .cursors import clamp_limit  # noqa: E402
from .v0_artifacts import _body_hash, _run_mutation  # noqa: E402
from .v0_deps import precondition_http  # noqa: E402

# Sessions are content-WRITE objects, but looking at one is a read.
#
# All four session GETs demanded `content:write` at first, which made every
# caller that only wants to SEE what an agent is doing — the console's
# awareness panel above all — hold a token that could also write. The BFF
# rule asks for minimally scoped delegated tokens, and a read-only view
# minting a write-capable one is exactly what it is trying to prevent.
#
# The local grant predicate is unchanged and is still the real gate: if you
# may read the artifact you may see its sessions (O5). This only stops the
# TOKEN from carrying an authority the operation never uses.
_SCOPE_WRITE = "content:write"


class SessionCreateIn(BaseModel):
    """POST …/sheet-sessions body."""

    lease_seconds: int | None = Field(default=None, ge=1)
    model_config = ConfigDict(extra="forbid")


class CellWriteIn(BaseModel):
    sheet: str = Field(min_length=1)
    range: str = Field(min_length=1)
    values: list[list[Any]]
    model_config = ConfigDict(extra="forbid")


class WriteCellsIn(BaseModel):
    """Batched by design: chatty per-range writes to one workbook are the
    main source of throttling and unrecoverable partial state (Microsoft's
    documented Excel-API lesson), so one request carries the whole batch and
    it applies atomically or not at all."""

    writes: list[CellWriteIn] = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class CompleteIn(BaseModel):
    message: str | None = Field(default=None, max_length=500)
    model_config = ConfigDict(extra="forbid")


class SheetSessionResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SheetSessionActorOut(SheetSessionResponseModel):
    subject_type: Literal["agent", "user", "service"]
    subject: str


class SheetSessionSheetOut(SheetSessionResponseModel):
    name: str
    index: int
    rows: int = Field(ge=0)
    columns: int = Field(ge=0)


class SheetSessionTouchedOut(SheetSessionResponseModel):
    name: str
    index: int
    edit_count: int = Field(ge=0)
    cells_written: int = Field(ge=0)


class SheetSessionOut(SheetSessionResponseModel):
    session_id: str = Field(pattern=r"^shs_[a-f0-9]{16}$")
    drive_id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{16}$")
    state: Literal["open", "completed", "discarded", "expired"]
    base_version_id: str = Field(pattern=r"^ver_[a-f0-9]{16}$")
    base_revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")
    format: str
    actor: SheetSessionActorOut
    sheets: list[SheetSessionSheetOut]
    sheets_touched: list[SheetSessionTouchedOut]
    edit_count: int = Field(ge=0)
    cells_written: int = Field(ge=0)
    lease_expires_at: RFC3339
    completed_version_id: str | None = Field(
        default=None, pattern=r"^ver_[a-f0-9]{16}$"
    )
    created_at: RFC3339


class SheetSessionCreateOut(SheetSessionOut):
    other_open_sessions: int = Field(ge=0)


class SheetSessionListOut(SheetSessionResponseModel):
    items: list[SheetSessionOut]
    next_cursor: str | None


class SheetSessionWriteOut(SheetSessionOut):
    edit_seq: int = Field(ge=1)
    cells_written_now: int = Field(ge=1)


class SheetSessionEditOut(SheetSessionResponseModel):
    seq: int = Field(ge=1)
    sheet: str
    range: str
    previous: list[list[Any]]
    values: list[list[Any]]
    actor: SheetSessionActorOut
    created_at: RFC3339


class SheetSessionEditListOut(SheetSessionResponseModel):
    items: list[SheetSessionEditOut]
    next_cursor: str | None


class SheetSessionCompleteOut(SheetSessionOut):
    version_id: str | None = Field(default=None, pattern=r"^ver_[a-f0-9]{16}$")
    artifact_revision: str | None = Field(
        default=None, pattern=r"^rev_[a-f0-9]{16}$"
    )


def _map_session_error(exc: Exception) -> V0ApiError:
    if isinstance(exc, core_cursors.BadCursor):
        return V0ApiError(400, "INVALID_CURSOR", str(exc))
    if isinstance(exc, sessions.SessionNotFound):
        return V0ApiError(404, "SHEET_SESSION_NOT_FOUND", str(exc))
    if isinstance(exc, sessions.SessionExpired):
        return V0ApiError(409, "SHEET_SESSION_EXPIRED", str(exc))
    if isinstance(exc, sessions.SessionAlreadyCompleted):
        return V0ApiError(409, "SHEET_SESSION_ALREADY_COMPLETED", str(exc))
    if isinstance(exc, sessions.WorkbookNotEditable):
        return V0ApiError(
            409, "WORKBOOK_NOT_EDITABLE", str(exc), details={"reason": str(exc)}
        )
    if isinstance(exc, sessions.WorkbookTooLarge):
        return V0ApiError(413, "WORKBOOK_TOO_LARGE", str(exc))
    if isinstance(exc, sessions.EditLimitExceeded):
        return V0ApiError(409, "SHEET_EDIT_LIMIT_EXCEEDED", str(exc))
    if isinstance(exc, sessions.RangeTooLarge):
        return V0ApiError(413, "PAYLOAD_TOO_LARGE", str(exc))
    if isinstance(exc, PreconditionError):
        return precondition_http(exc)
    if isinstance(exc, InvalidRange):
        return V0ApiError(400, "INVALID_ARGUMENT", str(exc))
    if isinstance(exc, ValueError) and type(exc) is ValueError:
        return V0ApiError(422, "VALIDATION_ERROR", str(exc))
    return _map_sheet_error(exc)


async def _require_session_pairing(
    drive_id: str, artifact_id: str, session_id: str
) -> None:
    """Refuse a mispaired session BEFORE the idempotency key is claimed.

    `_run_mutation` claims first and calls `execute` second, so a check that
    lives inside `execute` never runs on a replay and, on a first call with a
    reused key, is preceded by the ledger's own answer. Running the pairing
    predicate here keeps `/artifacts/{a}/sheet-sessions/{s}` answering the one
    as-if-absent 404 whatever key the caller presents — the rule
    `core.v0_sheet_sessions._row` documents — and leaves the key unclaimed,
    per §7.2 (only executed mutations create records). `complete` gets the
    same guarantee from `prepare_completion`, which already runs before the
    claim for exactly this reason.
    """
    async with conn() as c:
        try:
            await sessions.assert_pairing(c, drive_id, artifact_id, session_id)
        except Exception as exc:
            raise _map_session_error(exc) from None


def _session_idempotency_path(
    drive_id: str, artifact_id: str, session_id: str, suffix: str = ""
) -> str:
    """The canonical idempotency identity for a session mutation (§7.2).

    It is the FULL logical route, artifact segment included, because §7.2
    matches a repeated key on principal + method + path + request hash and
    that path is the only thing naming the resource the stored result belongs
    to. These routes omitted `/artifacts/{artifact_id}`, so one session id was
    enough to name the record — and the route's `require_local(... "artifact",
    "artifact_id")` authorizes the artifact in the PATH while the session /
    artifact pairing check (`core.v0_sheet_sessions._row`) lives inside
    `execute`, which a replay never runs. A principal who kept `editor` on a
    sibling artifact could therefore present the old session id, key and body
    against that sibling and be handed the first artifact's cached success:
    the replay was authorized against a resource the original request never
    named. Building the path here, from both ids, is what keeps a replay
    bound to the exact resource that produced it.
    """
    return (
        f"/v0/drives/{drive_id}/artifacts/{artifact_id}"
        f"/sheet-sessions/{session_id}{suffix}"
    )


def _session_location(drive_id: str, artifact_id: str, session_id: str) -> str:
    """Where the created session actually lives.

    Nested, like every other route that addresses it — a `Location` that
    named the old flat URL would hand a caller a 404 on its first follow.
    """
    from ..config import settings

    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    return (
        f"{origin}/v0/drives/{drive_id}/artifacts/{artifact_id}"
        f"/sheet-sessions/{session_id}"
    )


@sheet_sessions_router.post(
    "/drives/{drive_id}/artifacts/{artifact_id}/sheet-sessions",
    operation_id="sheet_sessions_create",
    response_model=SheetSessionCreateOut,
    responses={
        200: {
            "model": SheetSessionCreateOut,
            "description": "A replay of the same idempotent session creation.",
        }
    },
    status_code=201,
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def sheet_sessions_create(
    drive_id: str,
    artifact_id: str,
    body: SessionCreateIn,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> Any:
    """Open an edit session. `If-Match` on the artifact head is required and
    is captured for completion."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await sessions.create_session(
                c, actor, drive_id, artifact_id,
                if_match=if_match, lease_seconds=body.lease_seconds,
            )
        except Exception as exc:
            raise _map_session_error(exc) from None
        return (
            201,
            {
                "Location": _session_location(
                    drive_id, artifact_id, result["session_id"]
                ),
                "ETag": _etag(result["revision"]),
            },
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/artifacts/{artifact_id}/sheet-sessions",
        request_hash=_body_hash(body),
        execute=execute,
        replay_status=200,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@sheet_sessions_router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/sheet-sessions",
    operation_id="sheet_sessions_list",
    response_model=SheetSessionListOut,
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
       Depends(known_params("state", "limit", "cursor"))],
)
async def sheet_sessions_list(
    drive_id: str,
    artifact_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    state: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Any:
    """Sessions on this artifact, newest first.

    Artifact-scoped, not drive-scoped. The grant predicate is the
    declarative `require_local` above rather than a filter written by hand
    here — which is the whole reason these routes are nested. If you may
    read the artifact, you may see the sessions open on it (O5).
    """
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    if state is not None and state not in (
        "open", "completed", "discarded", "expired"
    ):
        raise V0ApiError(400, "INVALID_ARGUMENT", "unknown session state")
    async with conn() as c:
        try:
            return await sessions.list_sessions(
                c, actor, drive_id,
                artifact_id=artifact_id, state=state,
                limit=clamp_limit(limit), cursor=cursor,
            )
        except Exception as exc:
            raise _map_session_error(exc) from None


@sheet_sessions_router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/sheet-sessions/{session_id}",
    operation_id="sheet_sessions_read",
    response_model=SheetSessionOut,
    responses={304: {"description": "If-None-Match matched."}},
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
       Depends(known_params())],
)
async def sheet_sessions_read(
    drive_id: str,
    artifact_id: str,
    session_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> Any:
    """One session. Reads deliberately do NOT extend the lease, so a polling
    console can never keep a dead agent's session alive."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    async with conn() as c:
        try:
            result = await sessions.read_session(c, actor, drive_id, artifact_id, session_id)
        except Exception as exc:
            raise _map_session_error(exc) from None
    etag = _etag(result["revision"])
    if _etag_matches(if_none_match, etag):
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": "private"}
        )
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    return result


@sheet_sessions_router.delete(
    "/drives/{drive_id}/artifacts/{artifact_id}/sheet-sessions/{session_id}",
    operation_id="sheet_sessions_delete",
    response_model=SheetSessionOut,
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id")),
       Depends(known_params())],
)
async def sheet_sessions_delete(
    drive_id: str,
    artifact_id: str,
    session_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> Any:
    """Abandon a session. Nothing is published and the edits go with it."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    await _require_session_pairing(drive_id, artifact_id, session_id)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await sessions.discard_session(
                c, actor, drive_id, artifact_id, session_id, if_match=if_match
            )
        except Exception as exc:
            raise _map_session_error(exc) from None
        return (200, {"ETag": _etag(result["revision"])}, result)

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="DELETE",
        path=_session_idempotency_path(drive_id, artifact_id, session_id),
        request_hash=_body_hash(None),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@sheet_sessions_router.post(
    "/drives/{drive_id}/artifacts/{artifact_id}/sheet-sessions/{session_id}/cells",
    operation_id="sheet_sessions_write_cells",
    response_model=SheetSessionWriteOut,
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id")),
       Depends(known_params())],
)
async def sheet_sessions_write_cells(
    drive_id: str,
    artifact_id: str,
    session_id: str,
    body: WriteCellsIn,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    response: Response = ...,
) -> Any:
    """Append range writes to the edit log.

    No `If-Match`: the session's captured `base_revision` is the concurrency
    anchor and completion is where it is enforced. `Idempotency-Key` is what
    makes a retry safe — without it a retried write landing after a later
    overlapping write would silently resurrect stale values.
    """
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    await _require_session_pairing(drive_id, artifact_id, session_id)

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await sessions.write_cells(
                c, actor, drive_id, artifact_id, session_id,
                writes=[w.model_dump() for w in body.writes],
            )
        except Exception as exc:
            raise _map_session_error(exc) from None
        return (200, {"ETag": _etag(result["revision"])}, result)

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=_session_idempotency_path(drive_id, artifact_id, session_id, "/cells"),
        request_hash=_body_hash(body),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@sheet_sessions_router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/sheet-sessions/{session_id}/cells",
    operation_id="sheet_sessions_read_cells",
    response_model=CellRangeOut,
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
       Depends(known_params("sheet", "range"))],
)
async def sheet_sessions_read_cells(
    drive_id: str,
    artifact_id: str,
    session_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    sheet: str | None = None,
    range: str | None = None,  # noqa: A002 - the wire name is `range`
) -> Any:
    """Working state: base plus pending edits. Read-your-writes before any
    completion."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    rect = _rect(range)
    async with conn() as c:
        try:
            result = await sessions.read_session_cells(
                c, actor, drive_id, artifact_id, session_id, sheet=sheet, rect=rect
            )
        except Exception as exc:
            raise _map_session_error(exc) from None
    from ..sheets.a1 import format_range

    return {**result, "range": format_range(rect)}


@sheet_sessions_router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/sheet-sessions/{session_id}/edits",
    operation_id="sheet_sessions_list_edits",
    response_model=SheetSessionEditListOut,
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
       Depends(known_params("limit", "cursor"))],
)
async def sheet_sessions_list_edits(
    drive_id: str,
    artifact_id: str,
    session_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    limit: int | None = None,
    cursor: str | None = None,
) -> Any:
    """The edit log with before-and-after values — the console's diff, and
    the audit record that survives into version history."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    async with conn() as c:
        try:
            return await sessions.list_edits(
                c, actor, drive_id, artifact_id, session_id,
                limit=clamp_limit(limit), cursor=cursor,
            )
        except Exception as exc:
            raise _map_session_error(exc) from None


@sheet_sessions_router.post(
    "/drives/{drive_id}/artifacts/{artifact_id}/sheet-sessions/{session_id}/complete",
    operation_id="sheet_sessions_complete",
    response_model=SheetSessionCompleteOut,
    responses={
        200: {
            "model": SheetSessionCompleteOut,
            "description": (
                "The session had no edits, so no version was published, or "
                "this is a replay of an already completed request. A "
                "byte-identical version would pollute history and consume "
                "the per-tier version cap."
            )
        }
    },
    status_code=201,
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id")),
       Depends(known_params())],
)
async def sheet_sessions_complete(
    drive_id: str,
    artifact_id: str,
    session_id: str,
    body: CompleteIn,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    response: Response = ...,
) -> Any:
    """Replay the edit log and publish exactly one version.

    The expensive half — fetch, parse, replay, serialize — runs OUTSIDE the
    publishing transaction, so a replay failure costs nothing and publishes
    nothing. Only the fence and the version append are transactional.
    """
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)

    async with conn() as c:
        try:
            prepared = await sessions.prepare_completion(
                c, actor, drive_id, artifact_id, session_id
            )
        except Exception as exc:
            raise _map_session_error(exc) from None

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await sessions.finish_completion(
                c, actor, drive_id, artifact_id, session_id,
                content=prepared["content"], content_type=prepared["content_type"],
                message=body.message,
            )
        except Exception as exc:
            raise _map_session_error(exc) from None
        # A zero-edit session publishes NO version: a byte-identical version
        # would pollute history and consume the per-tier version cap. That is
        # also why only the publishing case is a 201 — and a 201 carries
        # Location, pointing at the version that was actually created.
        status = 201 if result["version_id"] else 200
        headers = {"ETag": _etag(result["revision"])}
        if result["version_id"]:
            from ..config import settings

            origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
            headers["Location"] = (
                f"{origin}/v0/drives/{drive_id}/artifacts/"
                f"{result['artifact_id']}/versions/{result['version_id']}"
            )
        return (status, headers, result)

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=_session_idempotency_path(
            drive_id, artifact_id, session_id, "/complete"
        ),
        request_hash=_body_hash(body),
        execute=execute,
        replay_status=200,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


# ── version-scoped reads ────────────────────────────────────────────────
#
# The parsed twins of `/versions/{version_id}/content`. Bytes already had a
# head route and a version-nested route; the parsed views had only the head
# half, which meant an agent whose session lost a race could not read the
# version it BASED on — the very thing it needs in order to work out what
# changed. Same shape as `/content`, one level down, so nothing new is
# invented and `require_local` still reads the artifact from the path.


@router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/versions/{version_id}/sheets",
    response_model=SheetIndexOut,
    operation_id="version_sheets_list",
    responses={304: {"description": "If-None-Match matched."}},
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def version_sheets_list(
    drive_id: str,
    artifact_id: str,
    version_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> Any:
    """One version's sheets and whether that version could be edited.

    The ETag is the VERSION id: a version is immutable, so its parsed
    contents can never change and its own identity is the strongest
    validator there is.
    """
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    _check_version_id(version_id)
    async with conn() as c:
        try:
            index, validator = await core.read_workbook_index(
                c, actor, drive_id, artifact_id, version_id=version_id
            )
        except Exception as exc:
            raise _map_sheet_error(exc) from None

    etag = _etag(validator)
    if _etag_matches(if_none_match, etag):
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": "private"}
        )
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    return {
        "workbook": {
            "artifact_id": artifact_id,
            "revision": validator,
            "format": index.format,
            "cell_count": index.cell_count,
            "editability": {
                "status": index.editability.status,
                "reason": index.editability.reason,
            },
        },
        "sheets": [
            {
                "name": s.name,
                "index": s.index,
                "rows": s.rows,
                "columns": s.columns,
                "has_formulas": s.has_formulas,
            }
            for s in index.sheets
        ],
    }


@router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/versions/{version_id}/cells",
    response_model=CellRangeOut,
    operation_id="version_cells_read",
    responses={304: {"description": "If-None-Match matched."}},
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
        Depends(known_params("sheet", "range")),
    ],
)
async def version_cells_read(
    drive_id: str,
    artifact_id: str,
    version_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    sheet: str | None = None,
    range: str | None = None,  # noqa: A002 - the wire name is `range`
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> Any:
    """One rectangle of one version's values.

    What makes a conflict diffable: read the same range from the session's
    base version and from the new head, and the difference is exactly what
    landed underneath.
    """
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    _check_version_id(version_id)
    rect = _rect(range)
    async with conn() as c:
        try:
            result = await core.read_cells(
                c, actor, drive_id, artifact_id,
                sheet=sheet, rect=rect, version_id=version_id,
            )
        except Exception as exc:
            raise _map_sheet_error(exc) from None

    etag = _etag(result["revision"])
    if _etag_matches(if_none_match, etag):
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": "private"}
        )
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    from ..sheets.a1 import format_range

    return {
        "sheet": result["sheet"],
        "range": format_range(rect),
        "revision": result["revision"],
        "values": result["values"],
    }
