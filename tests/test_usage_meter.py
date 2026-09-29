from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agentdrive import db
from agentdrive.core.usage.meter import (
    OperationReplayConflict,
    UsageCharge,
    UsageMeter,
    UsageReservation,
    window_bounds,
)
from agentdrive.core.usage.models import (
    EffectiveLimit,
    Metric,
    Period,
    ScopeType,
)


def limit(scope_id: str, maximum: int, scope=ScopeType.WORKSPACE):
    return EffectiveLimit(
        metric=Metric.DOWNLOAD_BYTES,
        scope_type=scope,
        scope_id=scope_id,
        period=Period.DAY,
        limit=maximum,
    )


async def clean():
    async with db.conn() as connection:
        await connection.execute("TRUNCATE usage_operations, usage_windows RESTART IDENTITY")


@pytest.mark.asyncio
async def test_two_connections_cannot_cross_shared_limit(app_with_lifespan):
    await clean()
    meter = UsageMeter()

    async def charge(key: str):
        async with db.conn() as connection:
            return await meter.charge(
                connection,
                UsageCharge(key, Metric.DOWNLOAD_BYTES, 60, (limit("tcws_shared", 100),)),
            )

    first, second = await asyncio.gather(charge("req_01"), charge("req_02"))
    assert sorted([first.allowed, second.allowed]) == [False, True]
    async with db.conn() as connection:
        assert await connection.fetchval("SELECT used FROM usage_windows") == 60


@pytest.mark.asyncio
async def test_multi_scope_refusal_charges_no_sibling(app_with_lifespan):
    await clean()
    meter = UsageMeter()
    async with db.conn() as connection:
        decision = await meter.charge(
            connection,
            UsageCharge(
                "req_03",
                Metric.DOWNLOAD_BYTES,
                20,
                (
                    limit("tcws_synthetic", 100),
                    limit("shr_synthetic", 10, ScopeType.SHARE),
                ),
            ),
        )
        assert decision.allowed is False
        assert await connection.fetchval("SELECT count(*) FROM usage_windows") == 0
        assert await connection.fetchval("SELECT count(*) FROM usage_operations") == 0


@pytest.mark.asyncio
async def test_identical_replay_is_free_and_conflicting_replay_fails(app_with_lifespan):
    await clean()
    meter = UsageMeter()
    request = UsageCharge(
        "req_replay", Metric.DOWNLOAD_BYTES, 30, (limit("tcws_replay", 100),)
    )
    async with db.conn() as connection:
        assert (await meter.charge(connection, request)).allowed
        assert (await meter.charge(connection, request)).allowed
        with pytest.raises(OperationReplayConflict):
            await meter.charge(
                connection,
                UsageCharge(
                    "req_replay",
                    Metric.DOWNLOAD_BYTES,
                    31,
                    (limit("tcws_replay", 100),),
                ),
            )
        assert await connection.fetchval("SELECT used FROM usage_windows") == 30


@pytest.mark.asyncio
async def test_partial_reservation_commit_releases_unused_bytes(app_with_lifespan):
    await clean()
    meter = UsageMeter()
    reservation = UsageReservation(
        "req_reserve",
        Metric.DOWNLOAD_BYTES,
        80,
        (limit("tcws_reserve", 100),),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        expiry_action="commit_reserved",
    )
    async with db.conn() as connection:
        assert (await meter.reserve(connection, reservation)).allowed
        await meter.commit(
            connection,
            operation_key=reservation.operation_key,
            metric=reservation.metric,
            actual_amount=25,
        )
        row = await connection.fetchrow("SELECT used, reserved FROM usage_windows")
        assert dict(row) == {"used": 25, "reserved": 0}


@pytest.mark.asyncio
async def test_shadow_admission_records_usage_and_reports_would_refuse(
    app_with_lifespan,
):
    await clean()
    shadow_limit = EffectiveLimit(
        Metric.UPLOAD_BYTES,
        ScopeType.WORKSPACE,
        "tcws_0000000000000203",
        Period.HOUR,
        10,
    )
    async with db.conn() as connection:
        decision = await UsageMeter().charge(
            connection,
            UsageCharge(
                "shadow-upload",
                Metric.UPLOAD_BYTES,
                11,
                (shadow_limit,),
                enforce=False,
            ),
        )
        used = await connection.fetchval(
            "SELECT used FROM usage_windows WHERE metric='upload_bytes' "
            "AND scope_id=$1",
            shadow_limit.scope_id,
        )

    assert decision.allowed is True
    assert decision.would_refuse is True
    assert decision.limit == 10
    assert used == 11


@pytest.mark.asyncio
async def test_concurrent_share_refusal_never_partially_charges_workspace(
    app_with_lifespan,
):
    await clean()
    meter = UsageMeter()
    limits = (
        limit("tcws_distributed", 100),
        limit("shr_distributed", 10, ScopeType.SHARE),
    )

    async def charge(operation_key: str):
        async with db.conn() as connection:
            return await meter.charge(
                connection,
                UsageCharge(
                    operation_key,
                    Metric.DOWNLOAD_BYTES,
                    6,
                    limits,
                ),
            )

    decisions = await asyncio.gather(charge("share-a"), charge("share-b"))
    assert sorted(decision.allowed for decision in decisions) == [False, True]
    async with db.conn() as connection:
        rows = await connection.fetch(
            "SELECT scope_type, used FROM usage_windows ORDER BY scope_type"
        )
    assert {(row["scope_type"], row["used"]) for row in rows} == {
        ("share", 6),
        ("workspace", 6),
    }


def test_month_window_uses_utc_calendar_boundary():
    start, end = window_bounds(
        Period.MONTH, datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    )
    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)
