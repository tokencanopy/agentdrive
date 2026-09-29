"""Sheet edit sessions: the ledger, its lifecycle, and completion.

Two tables and no materialized grid (design §6). The base grid is derivable
from an immutable content-addressed version, so storing it would put
cache-class data in the transactional database; it is parsed on demand and
memoized under the version id instead.

A range write is therefore **one insert and one counter update**. It reads no
object storage and parses no workbook, so its cost is independent of workbook
size — the property the whole design exists to buy.

Layer rule: never import ``agentdrive.api``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import settings
from ..identity.product_token import subject_type_for_subject
from ..sheets import cache
from ..sheets.a1 import Rect, cell_count, format_range, parse_range
from ..sheets.workbook import Edit, Value, apply_edits, read_grid, read_index
from . import cursors, ids, v0_artifacts
from .timestamps import to_rfc3339
from .v0_drives import precondition
from .v0_sheets import (
    SheetNotFound,
    _bytes,
    _require_spreadsheet,
    _source,
    resolve_sheet,
    slice_grid,
)

# ── limits (design §8) ─────────────────────────────────────────────────────
# Proposed defaults; final launch values are configuration. The workbook cap
# is a PARSE-MEMORY bound, not a storage bound — nothing is materialized — and
# it is whole-workbook, so a forty-tab model reaches it at 5,000 cells a tab.
# Limits are read from `settings` at CALL time rather than bound at import:
# a deployment tunes them without a code change, and a test can monkeypatch
# one without reloading the module.

_COLUMNS = (
    "id, drive_id, artifact_id, base_version_id, base_revision, "
    "actor_subject_type, actor_subject, actor_workspace, state, revision, "
    "format, sheet_index, sheets_touched, edit_count, cells_written, "
    "lease_expires_at, completed_version_id, created_at, updated_at"
)


class SessionNotFound(ValueError):
    """Unknown, discarded, or belonging to another drive.

    One code for every case, deliberately: distinguishing "never existed"
    from "not yours" is an enumeration oracle. Mirrors the viewer session's
    rule.
    """


class SessionExpired(ValueError):
    """The lease elapsed. Pending edits are gone."""


class SessionAlreadyCompleted(ValueError):
    """A terminal session cannot be written to or completed again."""


class WorkbookNotEditable(ValueError):
    """Formulas, unsupported features, or not a spreadsheet."""


class WorkbookTooLarge(ValueError):
    """Above the session cell cap."""


class EditLimitExceeded(ValueError):
    """The per-session edit or cell budget is exhausted."""


class RangeTooLarge(ValueError):
    """One write exceeds the per-request cell cap."""


def _now() -> datetime:
    return datetime.now(UTC)


def _payload(row: Any, *, other_open: int | None = None) -> dict[str, Any]:
    out = {
        "session_id": row["id"],
        "drive_id": row["drive_id"],
        "artifact_id": row["artifact_id"],
        "state": row["state"],
        "base_version_id": row["base_version_id"],
        "base_revision": row["base_revision"],
        "revision": row["revision"],
        "format": row["format"],
        "actor": {
            "subject_type": row["actor_subject_type"],
            "subject": row["actor_subject"],
        },
        "sheets": json.loads(row["sheet_index"]) if isinstance(row["sheet_index"], str)
        else row["sheet_index"],
        "sheets_touched": json.loads(row["sheets_touched"])
        if isinstance(row["sheets_touched"], str)
        else row["sheets_touched"],
        "edit_count": row["edit_count"],
        "cells_written": row["cells_written"],
        "lease_expires_at": to_rfc3339(row["lease_expires_at"]),
        "completed_version_id": row["completed_version_id"],
        "created_at": to_rfc3339(row["created_at"]),
    }
    if other_open is not None:
        out["other_open_sessions"] = other_open
    return out


async def _row(
    c: Any,
    drive_id: str,
    artifact_id: str,
    session_id: str,
    *,
    for_update: bool = False,
) -> Any:
    """One session, addressed by the artifact that owns it.

    The artifact is part of the WHERE clause, not an assertion after the
    fact: a session id paired with the wrong artifact is `SessionNotFound`,
    which the API renders as the same 404 an unknown id gets. That is the
    as-if-absent rule the rest of the surface follows, and it is what makes
    the nested path honest — `/artifacts/{a}/sheet-sessions/{s}` cannot be
    used to reach a session belonging to some other artifact whose grants
    the caller does not hold.
    """
    row = await c.fetchrow(
        f"SELECT {_COLUMNS} FROM sheet_sessions "
        "WHERE drive_id = $1 AND artifact_id = $2 AND id = $3"
        + (" FOR UPDATE" if for_update else ""),
        drive_id,
        artifact_id,
        session_id,
    )
    if row is None:
        raise SessionNotFound("no such sheet session on this artifact")
    return row


async def _expire_if_due(c: Any, row: Any) -> Any:
    """Expiry is enforced at USE, not by a timer.

    A session whose lease has elapsed transitions here, on the next call that
    touches it, so there is no sweeper race and no window where a dead
    session still accepts writes.
    """
    if row["state"] != "open" or row["lease_expires_at"] > _now():
        return row
    await c.execute(
        "UPDATE sheet_sessions SET state = 'expired', revision = $2, "
        "updated_at = now() WHERE id = $1 AND state = 'open'",
        row["id"],
        ids.new_id("rev"),
    )
    raise SessionExpired("the session lease elapsed; its edits were not saved")


def _require_open(row: Any) -> None:
    if row["state"] == "expired":
        raise SessionExpired("the session lease elapsed; its edits were not saved")
    if row["state"] in ("completed", "discarded"):
        raise SessionAlreadyCompleted(f"the session is {row['state']}")


# ── create ────────────────────────────────────────────────────────────────


async def create_session(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    if_match: str | None,
    lease_seconds: int | None,
) -> dict[str, Any]:
    """Open a session against the artifact's current head.

    `If-Match` is REQUIRED and is captured as `base_revision`; completion
    enforces it. That is the ratified upload-session pattern — the
    precondition is taken once, at the start, so `complete` needs no
    request-time header and a stale head surfaces as 412 at the end.
    """
    lease = lease_seconds if lease_seconds is not None else settings.sheet_lease_seconds_default
    if lease < 1 or lease > settings.sheet_lease_seconds_max:
        raise ValueError(f"lease_seconds must be between 1 and {settings.sheet_lease_seconds_max}")

    source = await _source(c, actor, drive_id, artifact_id)
    precondition(if_match, source["revision"])

    fmt = _require_spreadsheet(source["content_type"], source["artifact_name"])
    index = read_index(
        await _bytes(source),
        content_type=source["content_type"] or "",
        name=source["artifact_name"],
    )
    if index.editability.status != "ok":
        raise WorkbookNotEditable(index.editability.reason or "not editable")
    if index.cell_count > settings.sheet_max_workbook_cells:
        raise WorkbookTooLarge(
            f"workbook has {index.cell_count} cells; the limit is "
            f"{settings.sheet_max_workbook_cells}"
        )

    open_count = await c.fetchval(
        "SELECT count(*) FROM sheet_sessions WHERE drive_id = $1 AND state = 'open'",
        drive_id,
    )
    if open_count >= settings.sheet_max_open_sessions_per_drive:
        raise EditLimitExceeded(
            f"{settings.sheet_max_open_sessions_per_drive} sessions are already open in this drive"
        )
    other_open = await c.fetchval(
        "SELECT count(*) FROM sheet_sessions "
        "WHERE artifact_id = $1 AND state = 'open'",
        artifact_id,
    )

    sheet_index = [
        {"name": s.name, "index": s.index, "rows": s.rows, "columns": s.columns}
        for s in index.sheets
    ]
    row = await c.fetchrow(
        "INSERT INTO sheet_sessions (id, drive_id, artifact_id, base_version_id, "
        "base_revision, actor_subject_type, actor_subject, actor_workspace, state, "
        "revision, format, sheet_index, lease_expires_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'open',$9,$10,$11::jsonb, now() + $12) "
        f"RETURNING {_COLUMNS}",
        ids.new_id("shs"),
        drive_id,
        artifact_id,
        source["version_id"],
        source["revision"],
        actor.subject_type,
        actor.subject,
        actor.workspace_id,
        ids.new_id("rev"),
        fmt,
        json.dumps(sheet_index),
        timedelta(seconds=lease),
    )
    return _payload(row, other_open=other_open)


# ── read / list / discard ─────────────────────────────────────────────────


async def read_session(
    c: Any, actor: Any, drive_id: str, artifact_id: str, session_id: str
) -> dict:
    await v0_artifacts._ensure_drive(c, actor, drive_id)
    row = await _row(c, drive_id, artifact_id, session_id)
    if row["state"] == "open" and row["lease_expires_at"] <= _now():
        try:
            await _expire_if_due(c, row)
        except SessionExpired:
            row = await _row(c, drive_id, artifact_id, session_id)
    return _payload(row)


async def list_sessions(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    state: str | None,
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    """Sessions in the drive, newest first.

    Gated by the same grant predicate as every other collection: if you may
    read the artifact, you may see the sessions open on it (O5, decided
    2026-08-23). An agent that cannot enumerate a rival session cannot act on
    the `other_open_sessions` warning it was given at create.
    """
    await v0_artifacts._ensure_drive(c, actor, drive_id)
    bound = {"artifact_id": artifact_id, "state": state}
    after = None
    if cursor is not None:
        after = cursors.unseal("sheet-sessions", drive_id, cursor, bound=bound)

    rows = await c.fetch(
        f"SELECT {_COLUMNS} FROM sheet_sessions "
        # The artifact is required, not an optional filter: it comes from
        # the path and carries the grant check.
        "WHERE drive_id = $1 "
        "  AND artifact_id = $2 "
        "  AND ($3::text IS NULL OR state = $3) "
        "  AND ($4::timestamptz IS NULL OR (created_at, id) < ($4, $5)) "
        "ORDER BY created_at DESC, id DESC LIMIT $6",
        drive_id,
        artifact_id,
        state,
        datetime.fromisoformat(after["created_at"]) if after else None,
        after["id"] if after else None,
        limit + 1,
    )
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        cursors.seal(
            "sheet-sessions",
            drive_id,
            {"created_at": rows[-1]["created_at"].isoformat(), "id": rows[-1]["id"]},
            bound=bound,
        )
        if more and rows
        else None
    )
    return {"items": [_payload(r) for r in rows], "next_cursor": next_cursor}


async def assert_pairing(
    c: Any, drive_id: str, artifact_id: str, session_id: str
) -> None:
    """Raise `SessionNotFound` unless this artifact owns this session.

    The same `_row` predicate the mutators use, exposed so a route can run it
    BEFORE claiming an idempotency key. `prepare_completion` already had this
    shape; `delete` and the cell write did not, so a mispaired call reached
    the ledger first and answered `409 IDEMPOTENCY_CONFLICT` where the rest of
    the surface answers the as-if-absent 404. Same predicate, one place.
    """
    await _row(c, drive_id, artifact_id, session_id)


async def discard_session(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    session_id: str,
    *,
    if_match: str | None,
) -> dict[str, Any]:
    await v0_artifacts._ensure_drive(c, actor, drive_id)
    row = await _row(c, drive_id, artifact_id, session_id, for_update=True)
    precondition(if_match, row["revision"])
    _require_open(row)
    updated = await c.fetchrow(
        "UPDATE sheet_sessions SET state = 'discarded', revision = $2, "
        f"updated_at = now() WHERE id = $1 RETURNING {_COLUMNS}",
        session_id,
        ids.new_id("rev"),
    )
    return _payload(updated)


# ── writes (design §5.6) ──────────────────────────────────────────────────


def _parse_writes(writes: list[dict[str, Any]], sheet_names: set[str]) -> list[Edit]:
    """Body -> edits, fully validated before anything is written.

    All-or-nothing: a batch that cannot apply must not half-apply, because
    `seq` is replay order and a partially-recorded batch would replay as
    something the caller never asked for.
    """
    if not writes:
        raise ValueError("writes must not be empty")
    out: list[Edit] = []
    total = 0
    for w in writes:
        rect = parse_range(w["range"])
        sheet = w["sheet"]
        if sheet not in sheet_names:
            raise SheetNotFound(f"no such sheet: {sheet!r}")
        rows = rect.row1 - rect.row0 + 1
        cols = rect.col1 - rect.col0 + 1
        values = w["values"]
        if len(values) != rows or any(len(r) != cols for r in values):
            raise ValueError(
                f"values shape does not match {w['range']}: expected {rows}x{cols}"
            )
        total += cell_count(rect)
        out.append(Edit(sheet=sheet, rect=rect, values=values))
    if total > settings.sheet_max_write_cells:
        raise RangeTooLarge(
            f"the batch writes {total} cells; the per-request limit is "
            f"{settings.sheet_max_write_cells}"
        )
    return out


def _roll_up(existing: list[dict], sheet_order: dict[str, int], edits: list[Edit]) -> list[dict]:
    """Maintain `sheets_touched`, ordered by sheet index.

    Ordered by the WORKBOOK's own index rather than by when the agent got to
    it, because that is the reader's mental model — the tab order they see in
    Excel (component design §5.5).
    """
    by_name = {e["name"]: dict(e) for e in existing}
    for edit in edits:
        entry = by_name.setdefault(
            edit.sheet,
            {
                "name": edit.sheet,
                "index": sheet_order.get(edit.sheet, 0),
                "edit_count": 0,
                "cells_written": 0,
            },
        )
        entry["edit_count"] += 1
        entry["cells_written"] += cell_count(edit.rect)
    return sorted(by_name.values(), key=lambda e: e["index"])


async def write_cells(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    session_id: str,
    *,
    writes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Append range writes to the edit log.

    Touches ONLY Postgres: no object read, no workbook parse. Its cost is the
    same on a forty-tab model as on a three-row csv.
    """
    await v0_artifacts._ensure_drive(c, actor, drive_id)
    row = await _row(c, drive_id, artifact_id, session_id, for_update=True)
    await _expire_if_due(c, row)
    _require_open(row)

    sheet_index = _payload(row)["sheets"]
    order = {s["name"]: s["index"] for s in sheet_index}
    edits = _parse_writes(writes, set(order))

    cells = sum(cell_count(e.rect) for e in edits)
    if row["edit_count"] + len(edits) > settings.sheet_max_edits_per_session:
        raise EditLimitExceeded(
            f"a session may hold {settings.sheet_max_edits_per_session} edits"
        )
    if row["cells_written"] + cells > settings.sheet_max_cells_per_session:
        raise EditLimitExceeded(
            f"a session may write {settings.sheet_max_cells_per_session} cells"
        )

    seq = row["edit_count"]
    for edit in edits:
        seq += 1
        await c.execute(
            "INSERT INTO sheet_session_edits "
            "(session_id, seq, sheet, range_a1, values, actor_subject_type, "
            "actor_subject) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)",
            session_id,
            seq,
            edit.sheet,
            format_range(edit.rect),
            json.dumps(edit.values),
            actor.subject_type,
            actor.subject,
        )

    touched = _roll_up(_payload(row)["sheets_touched"], order, edits)
    updated = await c.fetchrow(
        "UPDATE sheet_sessions SET edit_count = $2, cells_written = $3, "
        "sheets_touched = $4::jsonb, revision = $5, "
        # Each write extends the lease; a READ deliberately does not, so a
        # polling console cannot keep a dead agent's session alive.
        "lease_expires_at = now() + $6, updated_at = now() "
        f"WHERE id = $1 RETURNING {_COLUMNS}",
        session_id,
        seq,
        row["cells_written"] + cells,
        json.dumps(touched),
        ids.new_id("rev"),
        timedelta(seconds=settings.sheet_lease_seconds_default),
    )
    return {
        **_payload(updated),
        "edit_seq": seq,
        "cells_written_now": cells,
    }


async def _base_grid(c: Any, row: Any) -> dict[str, list[list[Value]]]:
    """The session's base version, parsed. Memoized on the immutable id."""
    grid = cache.get(row["base_version_id"])
    if grid is not None:
        return grid
    version = await c.fetchrow(
        "SELECT storage_object, storage_bucket, storage_generation, content_type "
        "FROM artifact_versions WHERE id = $1",
        row["base_version_id"],
    )
    artifact_name = await c.fetchval(
        "SELECT name FROM artifacts WHERE id = $1", row["artifact_id"]
    )
    from .. import storage

    data = await storage.get(
        version["storage_object"],
        bucket=version["storage_bucket"],
        generation=version["storage_generation"],
    )
    return cache.store(
        row["base_version_id"],
        read_grid(data, content_type=version["content_type"] or "", name=artifact_name),
    )


async def _edits(c: Any, session_id: str, *, upto: int | None = None) -> list[Any]:
    return await c.fetch(
        "SELECT seq, sheet, range_a1, values, actor_subject_type, "
        "actor_subject, created_at "
        "FROM sheet_session_edits WHERE session_id = $1 "
        "  AND ($2::int IS NULL OR seq < $2) ORDER BY seq",
        session_id,
        upto,
    )


def _overlay(grid: dict[str, list[list[Value]]], rows: list[Any]) -> dict:
    """Base grid + pending edits, as a fresh copy.

    A copy, not a mutation: `grid` is the memoized base and every session
    reading the same version shares it.
    """
    out = {name: [list(r) for r in sheet] for name, sheet in grid.items()}
    for row in rows:
        rect = parse_range(row["range_a1"])
        values = json.loads(row["values"]) if isinstance(row["values"], str) else row["values"]
        sheet = out.setdefault(row["sheet"], [])
        for r, line in enumerate(values):
            target_row = rect.row0 + r
            while len(sheet) <= target_row:
                sheet.append([])
            for cix, value in enumerate(line):
                target_col = rect.col0 + cix
                while len(sheet[target_row]) <= target_col:
                    sheet[target_row].append(None)
                sheet[target_row][target_col] = value
    return out


async def read_session_cells(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    session_id: str,
    *,
    sheet: str | None,
    rect: Rect,
) -> dict[str, Any]:
    """Working state: the base grid overlaid with pending edits.

    A separate resource from the artifact's `cells` read, not a flag on it —
    committed state and session working state are genuinely different things,
    not two ways of asking for one.
    """
    await v0_artifacts._ensure_drive(c, actor, drive_id)
    row = await _row(c, drive_id, artifact_id, session_id)
    working = _overlay(await _base_grid(c, row), await _edits(c, session_id))
    name = resolve_sheet(working, sheet)
    return {
        "sheet": name,
        "revision": row["revision"],
        "values": slice_grid(working[name], rect),
    }


async def list_edits(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    session_id: str,
    *,
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    """The edit log, with `previous` computed rather than stored.

    Reconstructible at any time from the immutable base plus the preceding
    edits, so the write path stays a pure append and this cost lands on the
    rare console-diff request instead of on every write.
    """
    await v0_artifacts._ensure_drive(c, actor, drive_id)
    row = await _row(c, drive_id, artifact_id, session_id)
    after = (
        cursors.unseal("sheet-edits", drive_id, cursor, bound={"session": session_id})
        if cursor
        else None
    )
    rows = await c.fetch(
        "SELECT edit.seq, edit.sheet, edit.range_a1, edit.values, "
        "edit.actor_subject_type, edit.actor_subject, edit.created_at "
        "FROM sheet_session_edits AS edit "
        "WHERE edit.session_id = $1 AND ($2::int IS NULL OR edit.seq > $2) "
        "ORDER BY edit.seq LIMIT $3",
        session_id,
        after["seq"] if after else None,
        limit + 1,
    )
    more = len(rows) > limit
    rows = rows[:limit]

    base = await _base_grid(c, row)
    items = []
    for r in rows:
        prior = _overlay(base, await _edits(c, session_id, upto=r["seq"]))
        rect = parse_range(r["range_a1"])
        sheet_rows = prior.get(r["sheet"], [])
        actor_subject_type = r["actor_subject_type"] or subject_type_for_subject(
            r["actor_subject"]
        )
        if actor_subject_type is None:
            raise RuntimeError("sheet edit has an unrecognized actor subject namespace")
        items.append(
            {
                "seq": r["seq"],
                "sheet": r["sheet"],
                "range": r["range_a1"],
                "previous": slice_grid(sheet_rows, rect),
                "values": json.loads(r["values"])
                if isinstance(r["values"], str)
                else r["values"],
                "actor": {
                    "subject_type": actor_subject_type,
                    "subject": r["actor_subject"],
                },
                "created_at": to_rfc3339(r["created_at"]),
            }
        )
    next_cursor = (
        cursors.seal(
            "sheet-edits", drive_id, {"seq": rows[-1]["seq"]}, bound={"session": session_id}
        )
        if more and rows
        else None
    )
    return {"items": items, "next_cursor": next_cursor}


# ── completion (design §5.8.1) ────────────────────────────────────────────


async def prepare_completion(
    c: Any, actor: Any, drive_id: str, artifact_id: str, session_id: str
) -> dict[str, Any]:
    """Everything expensive, OUTSIDE the publishing transaction.

    Fetch, parse, replay and serialize here so a replay failure costs nothing
    and publishes nothing — the session is not fenced and no bytes are
    written. A cheap pre-check on the head fails fast before any of it.
    """
    await v0_artifacts._ensure_drive(c, actor, drive_id)
    row = await _row(c, drive_id, artifact_id, session_id)

    # A terminal session returns here rather than raising, and this ordering
    # is load-bearing: `prepare` runs BEFORE the idempotency claim, so raising
    # would refuse a legitimate retry with 409 instead of replaying the stored
    # 201. The transactional fence in `finish_completion` owns that refusal —
    # it runs after the claim, where a replay never reaches it.
    if row["state"] != "open":
        return {"row": row, "content": None, "content_type": None, "edit_count": 0}
    if row["lease_expires_at"] <= _now():
        # Expiry still transitions at use; `finish` turns the state into the
        # 409 the caller sees.
        with_suppress = await c.execute(
            "UPDATE sheet_sessions SET state = 'expired', revision = $2, "
            "updated_at = now() WHERE id = $1 AND state = 'open'",
            row["id"],
            ids.new_id("rev"),
        )
        del with_suppress
        return {"row": row, "content": None, "content_type": None, "edit_count": 0}

    edits = await _edits(c, session_id)
    if not edits:
        return {"row": row, "content": None, "content_type": None, "edit_count": 0}

    version = await c.fetchrow(
        "SELECT storage_object, storage_bucket, storage_generation, content_type "
        "FROM artifact_versions WHERE id = $1",
        row["base_version_id"],
    )
    name = await c.fetchval("SELECT name FROM artifacts WHERE id = $1", row["artifact_id"])
    from .. import storage

    data = await storage.get(
        version["storage_object"],
        bucket=version["storage_bucket"],
        generation=version["storage_generation"],
    )
    replayed = apply_edits(
        data,
        [
            Edit(
                sheet=e["sheet"],
                rect=parse_range(e["range_a1"]),
                values=json.loads(e["values"])
                if isinstance(e["values"], str)
                else e["values"],
            )
            for e in edits
        ],
        content_type=version["content_type"] or "",
        name=name,
    )
    return {
        "row": row,
        "content": replayed,
        "content_type": version["content_type"],
        "edit_count": len(edits),
    }


async def finish_completion(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    session_id: str,
    *,
    content: bytes | None,
    content_type: str | None,
    message: str | None = None,
) -> dict[str, Any]:
    """Fence the session and publish one version. Runs INSIDE a transaction.

    The fence is a conditional UPDATE: zero rows affected means another
    completion already won, which is the exactly-once guarantee. It does not
    rest on the serializer producing stable bytes — openpyxl's output is not
    byte-deterministic and nothing here assumes it is.
    """
    row = await _row(c, drive_id, artifact_id, session_id, for_update=True)
    _require_open(row)

    published: dict[str, Any] | None = None
    if content is not None:
        # The precondition captured at create IS the If-Match. A head that
        # moved since then surfaces as 412 here, and nothing is published.
        published = await v0_artifacts.append_version(
            c,
            actor,
            drive_id,
            row["artifact_id"],
            content=content,
            content_type=content_type or "application/octet-stream",
            sha256=None,
            # Anchored on the BYTES, not the revision. The replay reads the
            # pinned base version, so a rename/move/relabel must publish;
            # only a rival version refuses. `base_revision` is kept on the
            # row as the audit record of what was current at create.
            if_match=None,
            expect_head_version_id=row["base_version_id"],
            # Provenance, frozen on the version row. `message` was accepted
            # by the API and then discarded until now; the session id is
            # stored knowing the session itself is swept a day after it
            # terminates (migration 0053).
            origin_session_id=session_id,
            origin_message=message,
        )

    fenced = await c.fetchrow(
        "UPDATE sheet_sessions SET state = 'completed', revision = $2, "
        "completed_version_id = $3, updated_at = now() "
        f"WHERE id = $1 AND state = 'open' RETURNING {_COLUMNS}",
        session_id,
        ids.new_id("rev"),
        published["id"] if published else None,
    )
    if fenced is None:
        raise SessionAlreadyCompleted("another completion already won")

    out = _payload(fenced)
    out["version_id"] = published["id"] if published else None
    out["artifact_revision"] = published["artifact_revision"] if published else None
    return out
