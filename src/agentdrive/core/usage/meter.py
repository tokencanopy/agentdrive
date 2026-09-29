from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Literal

import asyncpg

from agentdrive.db import DBConn

from .models import EffectiveLimit, Metric, Period

ExpiryAction = Literal["release", "commit_reserved"]
log = logging.getLogger(__name__)


class OperationReplayConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class UsageCharge:
    operation_key: str
    metric: Metric
    amount: int
    limits: tuple[EffectiveLimit, ...]
    enforce: bool = field(default=True, kw_only=True)


@dataclass(frozen=True)
class UsageReservation(UsageCharge):
    expires_at: datetime
    expiry_action: ExpiryAction = "release"


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    metric: Metric
    would_refuse: bool = False
    scope_type: str | None = None
    scope_id: str | None = None
    period: str | None = None
    used: int = 0
    reserved: int = 0
    limit: int = 0
    requested: int = 0
    reset_at: datetime | None = None


class _Refused(Exception):
    def __init__(self, decision: LimitDecision):
        self.decision = decision


def window_bounds(period: Period, now: datetime) -> tuple[datetime, datetime]:
    current = now.astimezone(UTC)
    if period is Period.TEN_SECONDS:
        start = current.replace(microsecond=0, second=(current.second // 10) * 10)
        return start, start + timedelta(seconds=10)
    if period is Period.MINUTE:
        start = current.replace(second=0, microsecond=0)
        return start, start + timedelta(minutes=1)
    if period is Period.HOUR:
        start = current.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    if period is Period.DAY:
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, next_month


def _dimensions(limits: tuple[EffectiveLimit, ...]) -> list[dict[str, object]]:
    return [
        asdict(limit)
        for limit in sorted(
            limits,
            key=lambda item: (
                item.metric.value,
                item.scope_type.value,
                item.scope_id,
                item.period.value if item.period else "",
            ),
        )
    ]


def _json_dimensions(limits: tuple[EffectiveLimit, ...]) -> str:
    return json.dumps(_dimensions(limits), default=str, sort_keys=True, separators=(",", ":"))


def _canonical_stored_dimensions(value: str) -> str:
    dimensions = json.loads(value)
    for dimension in dimensions:
        dimension.pop("window_start", None)
    return json.dumps(dimensions, sort_keys=True, separators=(",", ":"))


def _retryable(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            asyncpg.DeadlockDetectedError,
            asyncpg.SerializationError,
            asyncpg.LockNotAvailableError,
            asyncpg.QueryCanceledError,
        ),
    )


class UsageMeter:
    async def charge(self, connection: DBConn, request: UsageCharge) -> LimitDecision:
        return await self._admit(connection, request, reserve=False)

    async def reserve(
        self, connection: DBConn, request: UsageReservation
    ) -> LimitDecision:
        return await self._admit(connection, request, reserve=True)

    async def _admit(
        self, connection: DBConn, request: UsageCharge, *, reserve: bool
    ) -> LimitDecision:
        if request.amount < 0 or not request.operation_key or not request.limits:
            raise ValueError("usage admission requires a key, non-negative amount, and limits")
        if any(
            limit.metric is not request.metric or limit.period is None
            for limit in request.limits
        ):
            raise ValueError("every usage limit must match the operation metric and have a period")
        for attempt in range(3):
            try:
                decision = await self._admit_once(connection, request, reserve=reserve)
                log.info(
                    "at=usage_limit_decision metric=%s result=%s",
                    request.metric.value,
                    (
                        "would_refuse"
                        if decision.would_refuse
                        else "allowed" if decision.allowed else "refused"
                    ),
                )
                return decision
            except Exception as exc:
                if attempt == 2 or not _retryable(exc):
                    raise
                await asyncio.sleep(random.uniform(0.005, 0.025) * (attempt + 1))
        raise AssertionError("unreachable")

    async def _admit_once(
        self, connection: DBConn, request: UsageCharge, *, reserve: bool
    ) -> LimitDecision:
        dimensions_json = _json_dimensions(request.limits)
        started = perf_counter()
        lock_wait_seconds = 0.0
        try:
            async with connection.transaction():
                await connection.execute("SET LOCAL lock_timeout = '2s'")
                await connection.execute("SET LOCAL statement_timeout = '5s'")
                now = await connection.fetchval("SELECT transaction_timestamp()")
                lock_started = perf_counter()
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"usage:{request.operation_key}:{request.metric.value}",
                )
                lock_wait_seconds += perf_counter() - lock_started
                lock_started = perf_counter()
                existing = await connection.fetchrow(
                    """
                    SELECT amount, dimensions_json::text AS dimensions_json, state
                      FROM usage_operations
                     WHERE operation_key = $1 AND metric = $2
                     FOR UPDATE
                    """,
                    request.operation_key,
                    request.metric.value,
                )
                lock_wait_seconds += perf_counter() - lock_started
                if existing is not None:
                    stored = _canonical_stored_dimensions(existing["dimensions_json"])
                    if existing["amount"] != request.amount or stored != dimensions_json:
                        raise OperationReplayConflict(
                            "usage operation key was replayed differently"
                        )
                    expected = "reserved" if reserve else "committed"
                    if existing["state"] != expected:
                        raise OperationReplayConflict(
                            "usage operation was already finalized differently"
                        )
                    return LimitDecision(allowed=True, metric=request.metric)

                rows: list[tuple[EffectiveLimit, datetime, datetime, asyncpg.Record]] = []
                for limit in sorted(
                    request.limits,
                    key=lambda item: (
                        item.metric.value,
                        item.scope_type.value,
                        item.scope_id,
                        item.period.value if item.period else "",
                    ),
                ):
                    start, end = window_bounds(limit.period, now)
                    await connection.execute(
                        """
                        INSERT INTO usage_windows
                          (metric, scope_type, scope_id, period, window_start)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT DO NOTHING
                        """,
                        request.metric.value,
                        limit.scope_type.value,
                        limit.scope_id,
                        limit.period.value,
                        start,
                    )
                    lock_started = perf_counter()
                    row = await connection.fetchrow(
                        """
                        SELECT used, reserved FROM usage_windows
                         WHERE metric=$1 AND scope_type=$2 AND scope_id=$3
                           AND period=$4 AND window_start=$5
                         FOR UPDATE
                        """,
                        request.metric.value,
                        limit.scope_type.value,
                        limit.scope_id,
                        limit.period.value,
                        start,
                    )
                    lock_wait_seconds += perf_counter() - lock_started
                    assert row is not None
                    rows.append((limit, start, end, row))

                would_refuse: LimitDecision | None = None
                for limit, _start, end, row in rows:
                    if row["used"] + row["reserved"] + request.amount > limit.limit:
                        decision = LimitDecision(
                            allowed=False,
                            metric=request.metric,
                            scope_type=limit.scope_type.value,
                            scope_id=limit.scope_id,
                            period=limit.period.value,
                            used=row["used"],
                            reserved=row["reserved"],
                            limit=limit.limit,
                            requested=request.amount,
                            reset_at=end,
                        )
                        if request.enforce:
                            raise _Refused(decision)
                        would_refuse = would_refuse or decision

                column = "reserved" if reserve else "used"
                for limit, start, _end, _row in rows:
                    await connection.execute(
                        f"""
                        UPDATE usage_windows SET {column} = {column} + $6, updated_at=$7
                         WHERE metric=$1 AND scope_type=$2 AND scope_id=$3
                           AND period=$4 AND window_start=$5
                        """,
                        request.metric.value,
                        limit.scope_type.value,
                        limit.scope_id,
                        limit.period.value,
                        start,
                        request.amount,
                        now,
                    )
                reservation = request if isinstance(request, UsageReservation) else None
                stored_dimensions = [
                    {
                        **asdict(limit),
                        "window_start": start.isoformat(),
                    }
                    for limit, start, _end, _row in rows
                ]
                await connection.execute(
                    """
                    INSERT INTO usage_operations
                      (operation_key, metric, amount, dimensions_json, state,
                       expiry_action, expires_at, finalized_at, created_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
                    """,
                    request.operation_key,
                    request.metric.value,
                    request.amount,
                    json.dumps(stored_dimensions, default=str, sort_keys=True),
                    "reserved" if reserve else "committed",
                    reservation.expiry_action if reservation else "release",
                    reservation.expires_at if reservation else None,
                    None if reserve else now,
                    now,
                )
                if would_refuse is not None:
                    return LimitDecision(
                        **{
                            **asdict(would_refuse),
                            "allowed": True,
                            "would_refuse": True,
                        }
                    )
                return LimitDecision(allowed=True, metric=request.metric)
        except _Refused as refused:
            return refused.decision
        finally:
            log.info(
                "at=usage_admission transaction_ms=%.3f lock_wait_ms=%.3f",
                (perf_counter() - started) * 1000,
                lock_wait_seconds * 1000,
            )

    async def commit(
        self,
        connection: DBConn,
        *,
        operation_key: str,
        metric: Metric,
        actual_amount: int,
    ) -> None:
        if actual_amount < 0:
            raise ValueError("actual usage cannot be negative")
        await self._finalize(
            connection,
            operation_key=operation_key,
            metric=metric,
            actual_amount=actual_amount,
            state="committed",
        )

    async def release(
        self, connection: DBConn, *, operation_key: str, metric: Metric
    ) -> None:
        await self._finalize(
            connection,
            operation_key=operation_key,
            metric=metric,
            actual_amount=0,
            state="released",
        )

    async def _finalize(
        self,
        connection: DBConn,
        *,
        operation_key: str,
        metric: Metric,
        actual_amount: int,
        state: Literal["committed", "released"],
    ) -> None:
        async with connection.transaction():
            now = await connection.fetchval("SELECT transaction_timestamp()")
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"usage:{operation_key}:{metric.value}",
            )
            operation = await connection.fetchrow(
                """
                SELECT amount, dimensions_json, state FROM usage_operations
                 WHERE operation_key=$1 AND metric=$2 FOR UPDATE
                """,
                operation_key,
                metric.value,
            )
            if operation is None:
                raise KeyError("usage reservation does not exist")
            if operation["state"] == state:
                return
            if operation["state"] != "reserved":
                raise OperationReplayConflict("usage reservation is already finalized")
            amount = operation["amount"]
            if actual_amount > amount:
                raise ValueError("actual usage exceeds the reservation")
            dimensions = operation["dimensions_json"]
            if isinstance(dimensions, str):
                dimensions = json.loads(dimensions)
            for dimension in dimensions:
                start = datetime.fromisoformat(dimension["window_start"])
                updated = await connection.execute(
                    """
                    UPDATE usage_windows
                       SET reserved=reserved-$6, used=used+$7, updated_at=$8
                     WHERE metric=$1 AND scope_type=$2 AND scope_id=$3
                       AND period=$4 AND window_start=$5 AND reserved >= $6
                    """,
                    metric.value,
                    dimension["scope_type"],
                    dimension["scope_id"],
                    dimension["period"],
                    start,
                    amount,
                    actual_amount,
                    now,
                )
                if updated != "UPDATE 1":
                    raise RuntimeError("usage reservation counters are inconsistent")
            await connection.execute(
                """
                UPDATE usage_operations SET state=$3, finalized_at=$4
                 WHERE operation_key=$1 AND metric=$2
                """,
                operation_key,
                metric.value,
                state,
                now,
            )

    async def snapshot(
        self,
        connection: DBConn,
        *,
        limits: tuple[EffectiveLimit, ...],
        now: datetime | None = None,
    ) -> dict[tuple[Metric, str, str, Period], LimitDecision]:
        if now is None:
            now = await connection.fetchval("SELECT transaction_timestamp()")
        result = {}
        for limit in limits:
            assert limit.period is not None
            start, end = window_bounds(limit.period, now)
            row = await connection.fetchrow(
                """
                SELECT used, reserved FROM usage_windows
                 WHERE metric=$1 AND scope_type=$2 AND scope_id=$3
                   AND period=$4 AND window_start=$5
                """,
                limit.metric.value,
                limit.scope_type.value,
                limit.scope_id,
                limit.period.value,
                start,
            )
            result[(limit.metric, limit.scope_type.value, limit.scope_id, limit.period)] = (
                LimitDecision(
                    allowed=True,
                    metric=limit.metric,
                    scope_type=limit.scope_type.value,
                    scope_id=limit.scope_id,
                    period=limit.period.value,
                    used=row["used"] if row else 0,
                    reserved=row["reserved"] if row else 0,
                    limit=limit.limit,
                    reset_at=end,
                )
            )
        return result
