"""The direct-transfer rate windows, now that they live in Postgres.

These pin the properties the in-memory dict could not have and the ones it
did have and must not lose. The API-level behaviour (429 shape, which
dimension is charged where) stays in `test_v0_uploads.py` /
`test_v0_download_capabilities.py`; this module is about the counter itself.
"""

from __future__ import annotations

import asyncio

import pytest

from agentdrive.core.v0_transfer_rate import charge, reset
from agentdrive.db import conn, pool

pytestmark = pytest.mark.asyncio

# A fixed epoch minute, so nothing here can fail by straddling a real
# minute boundary mid-test.
MINUTE = 29_000_000
NOW = MINUTE * 60


async def _count(c, dimension: str, ident: str, window: int) -> int | None:
    return await c.fetchval(
        "SELECT count FROM transfer_rate_windows "
        "WHERE dimension = $1 AND ident = $2 AND window_start = $3",
        dimension, ident, window,
    )


async def test_charges_accumulate_and_then_refuse(app_with_lifespan):
    async with conn() as c:
        for expected in (1, 2, 3):
            assert await charge(c, [("principal", "w/p", 3)], now=NOW) is None
            assert await _count(c, "principal", "w/p", MINUTE) == expected
        # The fourth crosses the limit and is refused BY NAME, so the API
        # layer can report which configured limit was hit.
        assert await charge(c, [("principal", "w/p", 3)], now=NOW) == "principal"
        # …and the refusal did not increment anything.
        assert await _count(c, "principal", "w/p", MINUTE) == 3


async def test_the_window_is_per_minute(app_with_lifespan):
    async with conn() as c:
        assert await charge(c, [("drive", "w/d", 1)], now=NOW) is None
        assert await charge(c, [("drive", "w/d", 1)], now=NOW) == "drive"
        # The next minute is a different row, so the budget is fresh.
        assert await charge(c, [("drive", "w/d", 1)], now=NOW + 60) is None


async def test_a_refused_dimension_leaves_no_phantom_charge_on_its_siblings(
    app_with_lifespan,
):
    """The property the in-memory version got by evaluating everything before
    committing anything, and the reason the whole charge is one transaction:
    a caller refused on its workspace window must not have silently spent its
    principal window on the way there."""
    async with conn() as c:
        # Exhaust the workspace window only.
        assert await charge(c, [("workspace", "w", 1)], now=NOW) is None
        before = await _count(c, "principal", "w/p", MINUTE)
        assert before is None

        refused = await charge(
            c,
            [("principal", "w/p", 10), ("workspace", "w", 1)],
            now=NOW,
        )
        assert refused == "workspace"
        # The principal charge that ran FIRST was rolled back with it.
        assert await _count(c, "principal", "w/p", MINUTE) is None


async def test_all_or_nothing_holds_inside_a_callers_transaction(
    app_with_lifespan,
):
    """The upload call sites charge on their own open transaction, so the
    rollback has to be a SAVEPOINT rather than a real one — otherwise a
    refusal would abort the caller's transaction instead of returning."""
    async with conn() as c, c.transaction():
        await c.execute(
            "INSERT INTO transfer_rate_windows (dimension, ident, window_start, count) "
            "VALUES ('workspace', 'w', $1, 1)",
            MINUTE,
        )
        refused = await charge(
            c, [("principal", "w/p", 10), ("workspace", "w", 1)], now=NOW
        )
        assert refused == "workspace"
        assert await _count(c, "principal", "w/p", MINUTE) is None
        # The caller's own transaction is still usable — a refusal is a
        # return value, not a poisoned transaction.
        assert await c.fetchval("SELECT 1") == 1


async def test_the_limit_is_shared_across_connections(app_with_lifespan):
    """The whole point of the move. Two connections are what two Cloud Run
    instances look like from the database's side, and the ceiling has to hold
    across them — that is what `api_max_instances = 1` was standing in for."""
    async with conn() as first, conn() as second:
        assert first is not second
        assert await charge(first, [("workspace", "shared", 2)], now=NOW) is None
        assert await charge(second, [("workspace", "shared", 2)], now=NOW) is None
        # Third request on EITHER connection is over the shared ceiling.
        assert await charge(second, [("workspace", "shared", 2)], now=NOW) == "workspace"
        assert await charge(first, [("workspace", "shared", 2)], now=NOW) == "workspace"


async def test_concurrent_charges_cannot_both_take_the_last_slot(
    app_with_lifespan,
):
    """Check-and-act is one statement under a row lock, so a race for the
    final slot has exactly one winner. A read-then-write would let both
    callers see the same pre-limit count and both commit."""
    if pool().get_max_size() < 4:  # pragma: no cover - configuration guard
        pytest.skip("needs a few pooled connections to race")

    async def attempt() -> str | None:
        async with conn() as c:
            return await charge(c, [("principal", "race", 4)], now=NOW)

    results = await asyncio.gather(*[attempt() for _ in range(8)])
    granted = [r for r in results if r is None]
    refused = [r for r in results if r == "principal"]
    assert len(granted) == 4, results
    assert len(refused) == 4, results
    async with conn() as c:
        assert await _count(c, "principal", "race", MINUTE) == 4


async def test_an_unconfigured_dimension_is_skipped(app_with_lifespan):
    async with conn() as c:
        assert await charge(c, [("drive", "w/d", None)], now=NOW) is None
        assert await _count(c, "drive", "w/d", MINUTE) is None


async def test_a_zero_limit_admits_nothing(app_with_lifespan):
    """Refused before the upsert on purpose: the insert branch would
    otherwise grant a first request that a limit of zero never allowed."""
    async with conn() as c:
        assert await charge(c, [("drive", "w/d", 0)], now=NOW) == "drive"
        assert await _count(c, "drive", "w/d", MINUTE) is None


async def test_charging_prunes_that_keys_expired_windows(app_with_lifespan):
    """Residue is bounded by tenancy rather than traffic — which is why this
    table needs no sweeper phase. A key that keeps transferring carries at
    most its current window."""
    async with conn() as c:
        for old in (MINUTE - 5, MINUTE - 2, MINUTE - 1):
            await c.execute(
                "INSERT INTO transfer_rate_windows "
                "(dimension, ident, window_start, count) VALUES ($1, $2, $3, 7)",
                "drive", "w/d", old,
            )
        assert await charge(c, [("drive", "w/d", 5)], now=NOW) is None
        rows = await c.fetch(
            "SELECT window_start FROM transfer_rate_windows "
            "WHERE dimension = 'drive' AND ident = 'w/d' ORDER BY window_start"
        )
        assert [r["window_start"] for r in rows] == [MINUTE]


async def test_pruning_is_scoped_to_the_key_being_charged(app_with_lifespan):
    """The prune is an indexed range delete on this key's prefix. A busy
    workspace must not be able to clear an idle one's window as a side
    effect of its own traffic."""
    async with conn() as c:
        await c.execute(
            "INSERT INTO transfer_rate_windows "
            "(dimension, ident, window_start, count) VALUES ('drive', 'other', $1, 3)",
            MINUTE - 9,
        )
        assert await charge(c, [("drive", "w/d", 5)], now=NOW) is None
        assert await _count(c, "drive", "other", MINUTE - 9) == 3


async def test_reset_clears_every_window(app_with_lifespan):
    async with conn() as c:
        await charge(c, [("workspace", "w", 5)], now=NOW)
        await reset(c)
        assert await c.fetchval("SELECT count(*) FROM transfer_rate_windows") == 0
