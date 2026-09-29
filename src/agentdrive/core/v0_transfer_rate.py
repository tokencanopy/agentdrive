"""Fixed-window rate counters for the direct-transfer control surface (§9).

Shared across instances, because the in-memory dict this replaces was exact
only at ``api_max_instances = 1`` — the pin that leaves the API tier with no
redundancy at all. See ``migrations/0058_transfer_rate_windows.sql`` for why
this is Postgres and not Redis.

Two properties the caller depends on:

  * **All-or-nothing across dimensions.** A rejected dimension must not leave
    a phantom charge on a sibling window, which the in-memory version got by
    evaluating every dimension before committing any. Here it comes from a
    ``c.transaction()`` around the whole charge: nested inside a caller's
    open transaction asyncpg makes that a SAVEPOINT, and standalone it is an
    ordinary transaction, so the guarantee holds either way.

  * **Check-and-act is atomic.** The upsert's ``WHERE count < limit`` is
    evaluated by Postgres under the row lock ``ON CONFLICT DO UPDATE``
    already takes, so two concurrent chargers on one key serialize and
    exactly one of them can be the request that crosses the limit. A
    read-then-write would let both read the same pre-limit count.

This module takes a connection rather than opening one. That is deliberate
and load-bearing: ``_charge_drive_rate`` is called at
``api/v0_uploads.py`` inside ``async with conn() as c, c.transaction()``
while a workspace advisory lock is held, and acquiring a SECOND pooled
connection there deadlocks — the pool is 10 connections against a request
concurrency of 80, so eleven requests each holding one and waiting for
another never resolve. The cost is that a charge made on a caller's
transaction rolls back with it; see the API layer for which windows that
applies to and why it is acceptable there.

Layer rule: no HTTP concepts here. Exhaustion is a returned dimension name;
turning it into a 429 is the API layer's job.
"""

from __future__ import annotations

import time

from ..db import DBConn

# Insert-or-increment under the limit, in one statement. Returns the new
# count when the charge is granted and NO ROW when the window is already at
# the limit — the `WHERE` suppresses the update rather than raising, so
# "denied" is the absence of a row, never an error to parse.
_CHARGE = """
INSERT INTO transfer_rate_windows (dimension, ident, window_start, count)
VALUES ($1, $2, $3, 1)
ON CONFLICT (dimension, ident, window_start)
DO UPDATE SET count = transfer_rate_windows.count + 1
  WHERE transfer_rate_windows.count < $4
RETURNING count
"""

# Drop this key's expired windows while we are already here. An indexed
# range delete on the primary key's `(dimension, ident)` prefix, so it reads
# only rows it deletes. Bounded by tenancy: a key that stops transferring
# leaves at most one stale row behind, so the table tracks how many
# principals/workspaces/drives have EVER transferred, not how much traffic
# they sent — which is what makes a separate sweeper phase unnecessary.
_PRUNE = """
DELETE FROM transfer_rate_windows
 WHERE dimension = $1 AND ident = $2 AND window_start < $3
"""


class _Exhausted(Exception):
    """Internal: unwinds the transaction so sibling charges roll back."""

    def __init__(self, dimension: str) -> None:
        super().__init__(dimension)
        self.dimension = dimension


async def charge(
    c: DBConn,
    dimensions: list[tuple[str, str, int | None]],
    *,
    now: float | None = None,
) -> str | None:
    """Charge one request against every given ``(dimension, ident, limit)``.

    Returns ``None`` when every dimension had room and all counters
    committed, or the NAME of the first exhausted dimension — in which case
    nothing was charged anywhere.

    A ``None`` limit means the dimension is unconfigured and is skipped,
    matching the in-memory behaviour. A limit of zero or less admits nothing
    at all: it is refused before the upsert, because the insert branch would
    otherwise grant a first request the limit never intended to allow.
    """
    minute = int((time.time() if now is None else now) // 60)
    try:
        async with c.transaction():
            for dimension, ident, limit in dimensions:
                if limit is None:
                    continue
                if limit <= 0:
                    raise _Exhausted(dimension)
                granted = await c.fetchval(_CHARGE, dimension, ident, minute, limit)
                if granted is None:
                    raise _Exhausted(dimension)
                await c.execute(_PRUNE, dimension, ident, minute)
    except _Exhausted as exhausted:
        return exhausted.dimension
    return None


async def reset(c: DBConn) -> None:
    """Test seam: drop every window. Never called on a serving path."""
    await c.execute("TRUNCATE transfer_rate_windows")
