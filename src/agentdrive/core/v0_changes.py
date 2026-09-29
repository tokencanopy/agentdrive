"""Changes vertical (slice 9): the drive change feed (§6.7, §9).

**Append (write).** Every domain mutation appends a ``drive_changes`` row on
the caller's own transaction, allocating a dense per-drive ``sequence`` by
``UPDATE drive_change_heads SET last_sequence = last_sequence + 1 RETURNING``
(the per-drive head serializes the feed without a global sequence). Recursive
operations share one ``change_set_id``.

**Read (the pull feed).** ``GET /v0/drives/{drive_id}/changes`` accepts
exactly one of ``start=now|beginning`` or ``cursor=...``. The cursor is a D14
SEALED token (``core.cursors``, kind ``changes``) carrying the position
``{after_sequence, high_water_sequence}`` — deliberately NOT a ``change_cursors``
table, so re-presenting a cursor re-delivers the same page (at-least-once) and
a dropped response is not lost. The cursor is bound to the drive and the
unfiltered fingerprint; a cursor replayed against another drive fails closed.
``next_cursor`` is returned even on empty pages; ``has_more`` is true while
the successor still sits below the cycle's captured high-water mark.

**Two walks, one feed.** ``order`` selects the direction, and the two are for
different jobs:

* ``order=oldest`` (the default, and the ONLY resumable one) is the sync feed
  above. A consumer walks forward, keeps its cursor, and a drained cursor
  re-presented later picks up whatever committed since.
* ``order=newest`` is a BROWSE walk for a human reading a history screen. It
  captures the head once and walks DOWN toward the retention floor. It is not
  a sync position: new events land above the captured head, so a drained
  descending cursor stays drained forever, and "check for new" means capturing
  the head again, not re-presenting the cursor. Because history only grows
  upward, there is no high-water mark to carry and no re-read of the head —
  descending is the simpler of the two.

The direction rides IN the sealed cursor (ascending carries ``{a, h}``,
descending ``{b, f}``), so a caller never repeats it and cannot flip a cursor
into the other walk. Sealed cursors minted before ``order`` existed carry
``{a, h}`` and keep working unchanged.

Expired/behind-retention cursors surface as ``410 CHANGE_CURSOR_EXPIRED``
with recovery ``full_sync``.
"""

from __future__ import annotations

import json
from typing import Any

from . import cursors
from .ids import new_id
from .timestamps import to_rfc3339

_CHANGE_COLUMNS = (
    "id, drive_id, sequence, change_set_id, type, actor_type, actor_id, "
    "resource_type, resource_id, previous_revision, revision, data, occurred_at"
)

# Permission (sharing) events. These re-expose the drive's access graph —
# principal ids, roles, expiries, share targets — so the page read filters
# them out for anyone who is not a drive MANAGER (§9; mirrors #430's
# manager-only grant enumeration). Kept as its own set so the read predicate
# and the "who may see this" rule reference one list.
PERMISSION_CHANGE_TYPES = frozenset({
    "grant.created", "grant.updated", "grant.revoked",
    "share.created", "share.revoked", "share.rotated",
})

# Content events — every domain mutation that is not a permission change.
_CONTENT_CHANGE_TYPES = frozenset({
    "drive.updated", "drive.deleted", "drive.restored",
    "folder.created", "folder.updated", "folder.deleted", "folder.restored",
    "artifact.created", "artifact.updated", "artifact.deleted", "artifact.restored",
    "artifact.purged",
    "artifact.version.created",
})

# Change types are server-controlled dotted names (no client input).
_CHANGE_TYPES = _CONTENT_CHANGE_TYPES | PERMISSION_CHANGE_TYPES

UNFILTERED_FINGERPRINT = "unfiltered"


class ChangeFeedError(ValueError):
    """A change-feed write failed."""


class ChangeCursorGoneError(ValueError):
    """The cursor is expired or behind retained history. 410 + full_sync."""


class ChangeCursorMismatchError(ValueError):
    """The cursor is bound to another collection. 400 INVALID_CURSOR."""


def change_document(row: Any) -> dict[str, Any]:
    """The wire shape of one change (exactly the §9 field list)."""
    return {
        "id": row["id"],
        "change_set_id": row["change_set_id"],
        "type": row["type"],
        "drive_id": row["drive_id"],
        "actor": {"type": row["actor_type"], "id": row["actor_id"]},
        "resource": {"type": row["resource_type"], "id": row["resource_id"]},
        "previous_revision": row["previous_revision"],
        "revision": row["revision"],
        "occurred_at": to_rfc3339(row["occurred_at"]),
        # JSONB comes back from asyncpg as a str (no codec registered); decode
        # so clients receive an object, matching every other vertical.
        "data": json.loads(row["data"]) if isinstance(row["data"], str) else (row["data"] or {}),
    }


async def append(
    c: Any,
    *,
    drive_id: str,
    actor: Any,
    type: str,
    resource_type: str,
    resource_id: str,
    previous_revision: str | None = None,
    revision: str | None = None,
    change_set_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one change on the caller's domain-mutation transaction.

    Must run inside the same transaction as the mutation it records, so the
    change and its effect commit atomically (§6.7)."""
    if type not in _CHANGE_TYPES:
        raise ChangeFeedError(f"unknown change type {type!r}")

    await c.execute(
        "INSERT INTO drive_change_heads (drive_id) VALUES ($1) "
        "ON CONFLICT (drive_id) DO NOTHING",
        drive_id,
    )
    sequence = await c.fetchval(
        "UPDATE drive_change_heads "
        "SET last_sequence = last_sequence + 1, updated_at = now() "
        "WHERE drive_id = $1 RETURNING last_sequence",
        drive_id,
    )
    if sequence is None:
        raise ChangeFeedError("drive change head disappeared during allocation")

    row = await c.fetchrow(
        "INSERT INTO drive_changes "
        "(id, drive_id, sequence, change_set_id, type, actor_type, actor_id, "
        "resource_type, resource_id, previous_revision, revision, data) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb) "
        "RETURNING " + _CHANGE_COLUMNS,
        new_id("chg"), drive_id, sequence,
        change_set_id or new_id("cset"),
        type,
        actor.subject_type, actor.subject,
        resource_type, resource_id,
        previous_revision, revision,
        json.dumps(data or {}),
    )
    if row is None:
        raise ChangeFeedError("drive change insert returned no row")
    return dict(row)


def _position_payload(after_sequence: int, high_water_sequence: int) -> dict[str, Any]:
    return {"a": after_sequence, "h": high_water_sequence}


def _descending_payload(before_sequence: int, floor_at_capture: int) -> dict[str, Any]:
    """A backward walk's position: read strictly BELOW ``b``, and the floor as
    it stood at capture so a retention advance mid-walk is detectable."""
    return {"b": before_sequence, "f": floor_at_capture}


async def _head(c: Any, drive_id: str) -> int:
    return await c.fetchval(
        "SELECT last_sequence FROM drive_change_heads WHERE drive_id = $1",
        drive_id,
    ) or 0


async def _floor(c: Any, drive_id: str) -> int:
    """The retention floor: the oldest sequence still retained (1 when the
    drive has no head row yet, i.e. nothing has ever been appended)."""
    return await c.fetchval(
        "SELECT retained_from_sequence FROM drive_change_heads WHERE drive_id = $1",
        drive_id,
    ) or 1


async def capture(
    c: Any, actor: Any, drive_id: str, *, start: str, order: str = "oldest"
) -> dict[str, Any]:
    """Capture a starting position. Returns a sealed cursor.

    ``order=oldest``: ``start=now`` (the current head) or ``start=beginning``
    (the retention floor) — the forward sync walk.

    ``order=newest``: the head, walking down. ``start`` is necessarily ``now``
    (the route rejects ``beginning``, which names the far end of a walk that
    ends there). ``b = head + 1`` because the read is strictly below ``b``, so
    the first page must include the head itself."""
    if order == "newest":
        return cursors.seal(
            "changes", drive_id,
            _descending_payload(await _head(c, drive_id) + 1, await _floor(c, drive_id)),
            bound={"fp": UNFILTERED_FINGERPRINT},
        )
    if start == "now":
        high = await _head(c, drive_id)
        after = high
    else:  # beginning
        after = await _floor(c, drive_id) - 1
        high = await _head(c, drive_id)
    return cursors.seal(
        "changes", drive_id, _position_payload(after, high),
        bound={"fp": UNFILTERED_FINGERPRINT},
    )


async def read_page(
    c: Any,
    *,
    actor: Any,
    drive_id: str,
    cursor: str,
    limit: int,
    include_permission_events: bool,
    types: list[str] | None = None,
) -> dict[str, Any]:
    """Read one bounded page from a sealed cursor and return the successor
    cursor (even on an empty page). Returns ``{"items", "next_cursor",
    "has_more"}``.

    The cursor decides the DIRECTION (see the module header): ``{a, h}`` walks
    forward from ``a`` toward the captured high-water mark, ``{b, f}`` walks
    backward from ``b`` toward the retention floor. Rows come back in the walk
    order, so a descending page is newest-first as read.

    Two READ-time filters are applied as WHERE predicates on the page query
    (never by post-filtering a fetched page), so the ``limit`` counts
    POST-filter and the sealed keyset cursor stays stable and consistent for
    the actor walking the feed:

      * ``include_permission_events`` — when False (every non-manager),
        ``PERMISSION_CHANGE_TYPES`` rows are excluded IN SQL. This is what
        keeps the access graph #430 gated to managers from re-leaking through
        the feed. It is re-evaluated from the caller's LIVE manager status on
        every page, not baked into the cursor — a demotion takes effect
        mid-walk, and the cursor carries no per-viewer secret.
      * ``types`` — an optional exact-match allow-list (the ``type`` query
        param). Intersected with the visibility filter: a non-manager asking
        for a permission type still gets nothing (empty, indistinguishable
        from "no such events"), never an existence oracle.

    ``has_more`` is derived from a ``limit + 1`` probe of the FILTERED rows,
    so a page is short only when it is genuinely the last one — a viewer never
    sees a full-``limit`` page truncated by hidden rows (which would leak, via
    a short page or a trailing empty page, that permission events exist in the
    gap). For a manager (unfiltered, dense sequence) this is identical to the
    previous high-water comparison."""
    try:
        position = cursors.unseal(
            "changes", drive_id, cursor,
            bound={"fp": UNFILTERED_FINGERPRINT},
        )
    except cursors.BadCursor as exc:
        raise ChangeCursorMismatchError("the change cursor is not valid for this drive") from exc

    floor = await _floor(c, drive_id)
    # Which walk this cursor belongs to is a property of the cursor, never of
    # the request — so a descending cursor cannot be replayed as a sync
    # position, and a pre-`order` cursor (which carries only `a`/`h`) still
    # reads as the forward walk it was minted for.
    descending = "b" in position

    if descending:
        before_sequence = position.get("b")
        floor_at_capture = position.get("f")
        if not isinstance(before_sequence, int) or not isinstance(floor_at_capture, int):
            raise ChangeCursorMismatchError("the change cursor is not valid for this drive")
        # Retention advanced under a backward walk. Stopping at the NEW floor
        # would silently swallow the events between the two floors and report
        # a clean end of history — the exact lie 410 exists to prevent. The
        # forward walk's equivalent is `after_sequence < floor - 1` below.
        if floor > floor_at_capture:
            raise ChangeCursorGoneError("change cursor is older than retained history")
        predicates = ["drive_id = $1", "sequence < $2", "sequence >= $3"]
        params: list[Any] = [drive_id, before_sequence, floor]
        walk = "DESC"
    else:
        after_sequence = position.get("a")
        high_water_sequence = position.get("h")
        if not isinstance(after_sequence, int) or not isinstance(high_water_sequence, int):
            raise ChangeCursorMismatchError("the change cursor is not valid for this drive")

        if after_sequence < floor - 1:
            raise ChangeCursorGoneError("change cursor is older than retained history")

        if after_sequence == high_water_sequence:
            high_water_sequence = await _head(c, drive_id)

        predicates = ["drive_id = $1", "sequence > $2", "sequence <= $3"]
        params = [drive_id, after_sequence, high_water_sequence]
        walk = "ASC"
    if not include_permission_events:
        params.append(list(PERMISSION_CHANGE_TYPES))
        predicates.append(f"type <> ALL(${len(params)}::text[])")
    if types is not None:
        params.append(types)
        predicates.append(f"type = ANY(${len(params)}::text[])")
    params.append(limit + 1)
    rows = await c.fetch(
        "SELECT " + _CHANGE_COLUMNS + " FROM drive_changes "
        "WHERE " + " AND ".join(predicates) + " "
        "ORDER BY sequence " + walk + " LIMIT $" + str(len(params)),
        *params,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [change_document(dict(r)) for r in rows]
    if descending:
        # The last row of a descending page is its LOWEST sequence, so the
        # successor reads strictly below it. An empty page leaves the position
        # untouched: history does not grow downward, so re-presenting a drained
        # descending cursor is empty again rather than eventually productive.
        next_position = _descending_payload(
            rows[-1]["sequence"] if rows else before_sequence, floor_at_capture
        )
    else:
        # A full page ends on its last visible row; the final page advances to
        # the captured high water (all visible rows up to it were returned), so
        # the next poll's ``after == high`` refreshes onto newly committed
        # events.
        next_position = _position_payload(
            rows[-1]["sequence"] if has_more else high_water_sequence,
            high_water_sequence,
        )
    next_cursor = cursors.seal(
        "changes", drive_id, next_position,
        bound={"fp": UNFILTERED_FINGERPRINT},
    )
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
