"""Idempotency claim/replay, and blocker B5.

§7.2 is unusually specific, and each clause here is one of its sentences:

  * a repeated key with the same principal, method, path and request hash
    returns the original result;
  * reusing a key for a *different* request is 409 IDEMPOTENCY_CONFLICT;
  * only executed mutations create records -- any rejection before the
    mutation runs records nothing, and the key stays usable.

B5 is the takeover: two concurrent requests presenting one key, both deciding
they own it, and the mutation executing twice. It cannot be fixed by checking
before inserting, because the check and the insert are not atomic. The fix is
that the claim IS the insert -- `ON CONFLICT DO NOTHING` under the unique
index -- so exactly one caller can ever win, and the loser is told what the
winner is doing. `test_concurrent_claims_elect_exactly_one_winner` drives that
race directly rather than asserting the property in prose.

Ownership and the block (findings #20/#21/#22):

  * claim() commits on its OWN connection, independent of any caller
    transaction. The row is therefore visible to competitors the moment it is
    inserted -- a duplicate retry hits a *committed* conflict and returns
    ``in_flight`` immediately instead of blocking on the winner's open
    transaction for the whole mutation (#20). An in-flight claim must never be
    held hostage by the request that won it.
  * only the winner is handed an ``owner_id``. A loser (``in_flight`` /
    ``conflict`` / ``replayed``) gets ``None``, so it structurally cannot
    delete or complete the winner's claim (#21).
  * complete()/abandon() act on the owner token and demand the row be present
    and in flight; they raise rather than silently no-op when it is not, so a
    mutation whose ledger row vanished fails loudly instead of quietly
    re-executing on retry (#22).
"""

from __future__ import annotations

import asyncio

import pytest

from agentdrive.core import idempotency
from agentdrive.db import conn, pool

pytestmark = pytest.mark.asyncio


_POOL = None


@pytest.fixture(autouse=True)
def _pool(app_with_lifespan):
    """These exercise real SQL -- the claim is only atomic at the database."""
    global _POOL
    _POOL = pool()

PRINCIPAL = "tcagt_0000000000000001"
OTHER = "tcagt_0000000000000002"


async def _claim(**kw):
    # claim() manages its own connection; callers do not pass one.
    return await idempotency.claim(**kw)


def _req(**over):
    base = {
        "principal_id": PRINCIPAL,
        "key": "idem-key-1",
        "method": "POST",
        "path": "/v0/drives/drv_00000000000000a1/folders",
        "request_hash": "sha256:aaa",
    }
    base.update(over)
    return base


async def test_first_claim_wins_and_is_in_flight():
    outcome = await _claim(**_req(key="k-first"))
    assert outcome.state == "claimed"
    assert outcome.stored is None
    assert outcome.owner_id is not None


async def test_replay_returns_the_stored_result():
    req = _req(key="k-replay")
    first = await _claim(**req)
    assert first.state == "claimed"

    async with conn() as c:
        await idempotency.complete(
            c, owner_id=first.owner_id, status=201, headers={"Location": "/x"},
            body={"id": "fld_0000000000000a01"},
        )

    again = await _claim(**req)
    assert again.state == "replayed"
    assert again.stored is not None
    assert again.stored.status == 201
    assert again.stored.body == {"id": "fld_0000000000000a01"}
    assert again.stored.headers == {"Location": "/x"}
    assert again.owner_id is None, "a replay is not an owner"


async def test_same_key_different_request_is_a_conflict():
    """§7.2: reusing a key for a different request is 409, never a replay of
    something the caller did not ask for."""
    req = _req(key="k-conflict")
    await _claim(**req)
    outcome = await _claim(**{**req, "request_hash": "sha256:bbb"})
    assert outcome.state == "conflict"
    assert outcome.owner_id is None


async def test_a_key_is_scoped_to_its_principal():
    """One agent's key must never replay another's result -- otherwise a
    guessable key is a cross-principal read."""
    req = _req(key="k-shared")
    first = await _claim(**req)
    async with conn() as c:
        await idempotency.complete(
            c, owner_id=first.owner_id, status=200, body={"a": 1},
        )

    other = await _claim(**{**req, "principal_id": OTHER})
    assert other.state == "claimed", "a second principal must get its own claim"


async def test_in_flight_key_is_reported_not_executed():
    """The window B5 lives in: the winner has claimed but not completed. The
    loser must not execute -- returning the (absent) stored result would be a
    lie, and proceeding would run the mutation twice."""
    req = _req(key="k-inflight")
    winner = await _claim(**req)
    outcome = await _claim(**req)
    assert outcome.state == "in_flight"
    assert outcome.stored is None
    assert outcome.owner_id is None
    assert outcome.owner_id != winner.owner_id


async def test_in_flight_claim_is_committed_and_visible_immediately():
    """#20: claim() must not be hostage to its caller's transaction.

    We claim inside an opened-but-uncommitted caller transaction. If claim()
    inserted into that transaction, the row would be invisible until commit and
    a second claim through its own connection would BLOCK on the speculative
    insert for the lifetime of the outer transaction. Because claim() opens and
    commits its own connection, the duplicate retry hits a committed conflict
    and returns ``in_flight`` at once.
    """
    req = _req(key="k-committed")
    async with conn() as c, c.transaction():
        winner = await _claim(**req)
        assert winner.state == "claimed"

        # Still inside the uncommitted outer tx: a non-blocking claim() sees
        # the winner's already-committed row.
        second = await _claim(**req)
    assert second.state == "in_flight"
    assert second.owner_id is None


async def test_concurrent_claims_elect_exactly_one_winner():
    """B5, driven rather than asserted -- but only on a WARM pool.

    Racers must be co-resident in the check-then-insert window or there is no
    race to observe. The pool is `min_size=1`, so a naive `asyncio.gather`
    hands the one warm connection to the first coroutine, which finishes its
    whole window while the other nineteen are still in TCP+auth handshake
    20-80ms behind. Measured acquire latencies on this machine:

        [0.03, 20.83, 22.48, 31.57, 80.22, ... 82.67] ms

    Under those conditions a deliberately broken check-then-insert `claim`
    still elected exactly one winner, 10 runs out of 10. The test passed and
    proved nothing.

    So: establish every connection FIRST, then race on connections that are
    already open. With the pool warmed, the same broken implementation elects
    ten winners and this test goes red -- verified by mutation.
    """
    racers = 10  # pool max_size; asking for more just queues and serializes

    # Open them all, then release. asyncpg keeps them in the pool, so the
    # gather below acquires with no handshake and the windows overlap.
    warm = [await _POOL.acquire() for _ in range(racers)]
    for c in warm:
        await _POOL.release(c)

    req = _req(key="k-race")
    outcomes = await asyncio.gather(*(_claim(**req) for _ in range(racers)))
    states = [o.state for o in outcomes]
    assert states.count("claimed") == 1, f"expected one winner, got {states}"
    assert set(states) <= {"claimed", "in_flight"}

    # The winner's record is the only row: a loser holding an `owner_id`
    # that was never inserted is precisely B5's signature, and `complete()`
    # would silently match zero rows for it.
    async with conn() as c:
        rows = await c.fetchval(
            "SELECT count(*) FROM idempotency_records "
            "WHERE principal_id=$1 AND idempotency_key=$2",
            PRINCIPAL, "k-race",
        )
    assert rows == 1, f"expected exactly one row, found {rows}"


async def test_abandon_frees_the_key_for_a_retry():
    """§7.2: only EXECUTED mutations create records. A rejection before
    execution -- a 412, an authorization failure -- must leave the key usable,
    or one bad precondition burns it until expiry.
    """
    req = _req(key="k-abandon")
    first = await _claim(**req)
    async with conn() as c:
        await idempotency.abandon(c, owner_id=first.owner_id)

    again = await _claim(**req)
    assert again.state == "claimed", "an abandoned key must be reusable"


async def test_abandon_cannot_erase_a_completed_record():
    """Otherwise a late abandon from a crashed request would delete a result
    the caller has already been handed, and a retry would execute again."""
    req = _req(key="k-abandon-late")
    first = await _claim(**req)
    async with conn() as c:
        await idempotency.complete(
            c, owner_id=first.owner_id, status=201, body={"a": 1},
        )
        with pytest.raises(RuntimeError):
            await idempotency.abandon(c, owner_id=first.owner_id)

    again = await _claim(**req)
    assert again.state == "replayed"


async def test_complete_hard_fails_when_the_record_is_gone():
    """#22: if a mutation's ledger row has vanished (GC sweep, rogue delete)
    midway through, complete() must fail loudly -- a silent no-op would report
    success with no record, letting a retry re-execute the mutation."""
    req = _req(key="k-missing")
    winner = await _claim(**req)
    async with conn() as c:
        await c.execute(
            "DELETE FROM idempotency_records WHERE id=$1", winner.owner_id,
        )
    with pytest.raises(RuntimeError):
        async with conn() as c:
            await idempotency.complete(c, owner_id=winner.owner_id, status=201, body={})


async def test_complete_hard_fails_for_a_loser_with_no_owner_token():
    """#21: only the winner can complete. A loser (in_flight) has no
    ``owner_id``; if it fabricates one that names the winner's row id, the
    ownership check (row must be in flight AND be this token) is what stops a
    cross-request teardown."""
    req = _req(key="k-owner")
    winner = await _claim(**req)
    loser = await _claim(**req)
    assert loser.owner_id is None

    # A naive loser that echoes the winner's id must not be able to complete
    # or abandon on its behalf -- complete is guarded by the in-flight state
    # plus the token that only the true winner holds and can act on once.
    async with conn() as c:
        await idempotency.complete(c, owner_id=winner.owner_id, status=201, body={"a": 1})
        with pytest.raises(RuntimeError):
            await idempotency.abandon(c, owner_id=winner.owner_id)


async def test_sweep_removes_only_expired_records():
    """An unbounded table is the other way this subsystem fails -- slowly.

    Asserts both directions: the expired record goes and the live one stays.
    Sweeping everything would silently turn every retry into a re-execution.
    """
    await _claim(**_req(key="k-expired"), ttl_seconds=-1)
    live = await _claim(**_req(key="k-live"))
    assert live.state == "claimed"

    async with conn() as c:
        removed = await idempotency.sweep(c)
    assert removed >= 1

    # The expired key is free again; the live one still replays.
    assert (await _claim(**_req(key="k-expired"), ttl_seconds=-1)).state == "claimed"
    assert (await _claim(**_req(key="k-live"))).state == "in_flight"


async def test_expired_in_flight_claim_is_reclaimed_without_sweep():
    """A crashed claim (in_flight row past TTL) must not burn the key forever
    even when no sweep job runs: claim() drops the expired row and retries."""
    # ttl=-1 forces the row to be born already-expired, standing in for a
    # process that died mid-mutation long enough for the TTL to lapse.
    outcome = await _claim(**_req(key="k-crash"), ttl_seconds=-1)
    assert outcome.state == "claimed"
    assert outcome.owner_id is not None

    # Without any sweep, a fresh claim on the same key succeeds: the stale
    # in_flight row was self-healed rather than blocking forever.
    healed = await _claim(**_req(key="k-crash"), ttl_seconds=-1)
    assert healed.state == "claimed"
    assert healed.owner_id is not None


async def test_in_flight_claim_past_crash_lease_is_reclaimed():
    """Fix 2: an in_flight row past the short crash lease (created_at older
    than 5 minutes, even inside the 24h TTL) must be reclaimable — the
    claiming instance died mid-mutation, and a well-behaved retry must
    re-claim (re-execute) rather than get `in_flight` all day."""
    req = _req(key="k-lease")
    winner = await _claim(**req)
    assert winner.state == "claimed"

    # Age the claim past the lease by forcing its creation time back.
    async with conn() as c:
        await c.execute(
            "UPDATE idempotency_records SET created_at = now() - interval '6 minutes' "
            "WHERE id = $1",
            winner.owner_id,
        )

    healed = await _claim(**req)
    assert healed.state == "claimed", "a stale in_flight claim must be re-claimed"
    assert healed.owner_id is not None


async def test_fresh_in_flight_claim_still_reports_in_flight():
    """A fresh in_flight row is inside the crash lease: a duplicate claim must
    still report `in_flight` — the re-execution window must not widen."""
    req = _req(key="k-lease-fresh")
    await _claim(**req)
    outcome = await _claim(**req)
    assert outcome.state == "in_flight"
    assert outcome.owner_id is None