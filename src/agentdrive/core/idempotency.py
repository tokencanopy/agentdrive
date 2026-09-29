"""Idempotency-key claim, replay, and expiry (§7.2).

Every v0 mutation carries an `Idempotency-Key`. The lifecycle is:

    claim  ->  execute the mutation  ->  complete
           \\-> rejected before executing -> abandon

`claim` is the interesting one, because it is where blocker B5 lives. The
obvious implementation reads the table, sees no record, and inserts one. Two
concurrent requests both read "absent" before either writes, both believe they
own the key, and the mutation runs twice -- a takeover. No amount of care in
the handler fixes that, because the read and the write are separate statements.

So the claim *is* the write: a single `INSERT ... ON CONFLICT DO NOTHING`
against `UNIQUE (principal_id, idempotency_key)`. Exactly one caller can
insert. Everyone else gets zero rows back and then reads what the winner
wrote, which tells them whether to replay a finished result, report a conflict,
or wait.

The four outcomes `claim` returns map to §7.2's four sentences:

    claimed    execute the mutation, then `complete`
    replayed   return `stored` verbatim; do NOT re-run preconditions
    conflict   409 IDEMPOTENCY_CONFLICT -- same key, different request
    in_flight  the winner is still executing; retry

Ownership and the block (findings #20/#21/#22):

* claim() opens and commits its OWN connection. The winner's row is therefore
  visible to competitors the instant it is inserted, long before the mutation
  runs. A duplicate retry hits a *committed* conflict and returns `in_flight`
  immediately -- it never blocks on the winner's open transaction for the
  whole duration of the mutation. (Running the claim inside the caller's
  mutation transaction would be the bug: PostgreSQL's speculative insert on an
  uncommitted conflicting tuple waits until that transaction commits, tying up
  a pool connection and making `in_flight` unreachable.)
* only the winner is handed an `owner_id`. A loser -- `in_flight`, `conflict`,
  or `replayed` -- gets `None`, so it structurally cannot complete or abandon
  the winner's claim (#21).
* complete()/abandon() act on that owner token and demand the row be present
  and in flight; they raise rather than silently no-op (#22). complete() must
  run on the mutation's transaction connection so the ledger update and the
  effect commit atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from ..db import conn
from .ids import new_id

# §7.2 gives no retention figure. 24h is long enough to cover any retry a
# client would reasonably make and short enough that the table stays small;
# `sweep` is what actually bounds it.
DEFAULT_TTL_SECONDS = 24 * 60 * 60

# Crash lease for IN-FLIGHT claims, distinct from the completed-replay TTL.
# `expires_at` (24h) is about how long a *completed* result is replayable; a
# claimed-but-never-completed row must not wedge its key for that whole window
# when the claiming instance dies. The claim row records `created_at`, so the
# lease is derived rather than migrated: an in_flight row older than this is
# treated as expired at claim time and re-claimed (re-executed) by a retry.
# 5 minutes comfortably exceeds any legitimate v0 mutation (inline content is
# capped at 15 MiB and mutations are single transactions), so a re-claim cannot
# race a still-running winner in practice.
IN_FLIGHT_LEASE_SECONDS = 5 * 60


class ClaimVanishedError(RuntimeError):
    """The claim row this owner token names is gone or no longer in flight
    — typically reaped by the crash lease under a long-running mutation.
    Callers with an already-committed effect may treat this as benign
    bookkeeping loss (the terminal state answers later same-key retries);
    callers without one must fail loudly."""


@dataclass(frozen=True)
class StoredResponse:
    """The response captured at first execution, replayed verbatim."""

    status: int
    headers: dict[str, Any]
    body: Any


@dataclass(frozen=True)
class ClaimOutcome:
    state: str  # claimed | replayed | conflict | in_flight
    owner_id: str | None = None  # set ONLY for the winning claimer (#21)
    stored: StoredResponse | None = None


async def claim(
    *,
    principal_id: str,
    key: str,
    method: str,
    path: str,
    request_hash: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> ClaimOutcome:
    """Attempt to take ownership of `key` for this principal.

    Runs on its own just-opened connection so the claim commits at once,
    independent of any caller transaction (see module docstring, #20).
    """
    async with conn() as c:
        record_id = new_id("idem")
        row = await c.fetchrow(
            """
            INSERT INTO idempotency_records
                (id, principal_id, idempotency_key, method, path, request_hash,
                 state, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, 'in_flight',
                    now() + make_interval(secs => $7))
            ON CONFLICT (principal_id, idempotency_key) DO NOTHING
            RETURNING id
            """,
            record_id, principal_id, key, method, path, request_hash, float(ttl_seconds),
        )
        if row is not None:
            return ClaimOutcome(state="claimed", owner_id=row["id"])

        # Lost the race, or this is a genuine retry. Either way the winner's
        # row is authoritative -- and because claim() commits on its own
        # connection, it is ALREADY COMMITTED and visible here, so this read
        # does not and cannot block on the winner's mutation.
        existing = await c.fetchrow(
            """
            SELECT id, method, path, request_hash, state,
                   response_status, response_headers, response_body, expires_at,
                   created_at
              FROM idempotency_records
             WHERE principal_id = $1 AND idempotency_key = $2
            """,
            principal_id, key,
        )
        if existing is None:
            # The row expired and was swept between our INSERT and this SELECT.
            # Retrying the claim is correct: the key is genuinely free again.
            return await claim(
                principal_id=principal_id, key=key, method=method, path=path,
                request_hash=request_hash, ttl_seconds=ttl_seconds,
            )

        # A row is stale when it is past its 24h TTL (a `completed` row no
        # longer needs replaying; an `in_flight` row's holder died long ago) OR
        # when it is an `in_flight` row past the SHORT crash lease
        # (`IN_FLIGHT_LEASE_SECONDS`, derived from `created_at`) — a claimed
        # row whose instance died mid-mutation must not burn the key for the
        # full 24h, or every well-behaved retry gets `in_flight` all day.
        # Either way the key is free: drop it and retry the claim rather than
        # burning the key forever (§7.2 self-healing, so a crashed claim never
        # blocks a retry even without a sweep job).
        now = datetime.now(UTC)
        stale = existing["expires_at"] < now
        if not stale and existing["state"] == "in_flight":
            stale = existing["created_at"] < now - timedelta(
                seconds=IN_FLIGHT_LEASE_SECONDS
            )
        if stale:
            await c.execute(
                "DELETE FROM idempotency_records WHERE id = $1", existing["id"]
            )
            return await claim(
                principal_id=principal_id, key=key, method=method, path=path,
                request_hash=request_hash, ttl_seconds=ttl_seconds,
            )

        # §7.2: the match is on principal, method, path AND request hash. Matching
        # on the key alone would replay one request's result for another's input.
        same_request = (
            existing["method"] == method
            and existing["path"] == path
            and existing["request_hash"] == request_hash
        )
        if not same_request:
            return ClaimOutcome(state="conflict")

        if existing["state"] != "completed":
            return ClaimOutcome(state="in_flight")

        import json

        headers = existing["response_headers"]
        body = existing["response_body"]
        return ClaimOutcome(
            state="replayed",
            stored=StoredResponse(
                status=existing["response_status"],
                headers=json.loads(headers) if isinstance(headers, str) else (headers or {}),
                body=json.loads(body) if isinstance(body, str) else body,
            ),
        )


async def complete(
    c: asyncpg.Connection,
    *,
    owner_id: str,
    status: int,
    body: Any,
    headers: dict[str, Any] | None = None,
) -> None:
    """Record the result of an executed mutation.

    Must be called on the SAME connection/transaction as the mutation, so the
    ledger update and the effect commit together: a record without its mutation
    would replay a success that never happened, and a mutation without its
    record would let a retry run it twice.

    Guarded on `owner_id` AND `state = 'in_flight'`. It raises if the owner
    token names a row that is gone or not in flight, so a mutation whose ledger
    row vanished midway fails loudly instead of silently re-executing on retry
    (#22).
    """
    import json

    result = await c.execute(
        """
        UPDATE idempotency_records
           SET state = 'completed',
               response_status = $2,
               response_headers = $3::jsonb,
               response_body = $4::jsonb
         WHERE id = $1 AND state = 'in_flight'
        """,
        owner_id, status, json.dumps(headers or {}), json.dumps(body),
    )
    if result != "UPDATE 1":
        raise ClaimVanishedError("idempotency owner is not in flight")


async def abandon(c: asyncpg.Connection, *, owner_id: str) -> None:
    """Release a claim whose mutation never executed.

    §7.2: only executed mutations create records. A `428`, a `412`, an
    authorization failure, or a target already in a state that precludes the
    operation must all leave the key usable -- otherwise one bad precondition
    burns it until expiry and the caller cannot retry at all.

    Restricted to the owner token and `in_flight` so a late abandon from a
    crashed request cannot erase a completed result and let the mutation run a
    second time. Raises if the row is gone or already completed (#21/#22).
    """
    result = await c.execute(
        "DELETE FROM idempotency_records WHERE id = $1 AND state = 'in_flight'",
        owner_id,
    )
    if result != "DELETE 1":
        raise ClaimVanishedError("idempotency owner is not in flight")


async def sweep(c: asyncpg.Connection) -> int:
    """Drop expired records. Returns how many went.

    Both states are swept. An `in_flight` row past its TTL means the process
    holding it died mid-mutation; leaving it would block that key forever.
    """
    result = await c.execute(
        "DELETE FROM idempotency_records WHERE expires_at < now()"
    )
    return int(result.split()[-1]) if result.startswith("DELETE") else 0
