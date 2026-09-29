from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agentdrive.core.usage.meter import UsageMeter, UsageReservation
from agentdrive.core.usage.models import EffectiveLimit, Metric, Period, ScopeType
from agentdrive.db import conn
from agentdrive.jobs import usage_snapshot

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def clean_state(app_with_lifespan):
    async with conn() as connection:
        await connection.execute(
            "TRUNCATE usage_operations, usage_windows, upload_sessions, "
            "storage_reservations, workspace_storage, drives RESTART IDENTITY CASCADE"
        )
    yield


async def reserve_expired(
    *, action: str, amount: int = 100, suffix: str = ""
) -> None:
    async with conn() as connection:
        decision = await UsageMeter().reserve(
            connection,
            UsageReservation(
                operation_key=f"snapshot-{action}{suffix}",
                metric=Metric.PUBLIC_BYTES,
                amount=amount,
                limits=(
                    EffectiveLimit(
                        Metric.PUBLIC_BYTES,
                        ScopeType.SHARE,
                        "shr_0000000000000301",
                        Period.DAY,
                        1_000,
                    ),
                ),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
                expiry_action=action,
            ),
        )
    assert decision.allowed


async def test_expired_public_reservation_commits_full_amount(app_with_lifespan):
    await reserve_expired(action="commit_reserved")

    result = await usage_snapshot.run(now=datetime.now(UTC), dry_run=False)

    assert result.operations_committed == 1
    async with conn() as connection:
        operation = await connection.fetchrow(
            "SELECT state FROM usage_operations WHERE operation_key=$1",
            "snapshot-commit_reserved",
        )
        window = await connection.fetchrow(
            "SELECT used, reserved FROM usage_windows WHERE scope_id=$1",
            "shr_0000000000000301",
        )
    assert operation["state"] == "committed"
    assert dict(window) == {"used": 100, "reserved": 0}


async def test_expired_public_reservation_updates_visible_retrieval(
    app_with_lifespan,
):
    drive_id = "drv_0000000000000301"
    workspace_id = "tcws_0000000000000301"
    async with conn() as connection, connection.transaction():
        await connection.execute("SET CONSTRAINTS ALL DEFERRED")
        await connection.execute(
            "INSERT INTO drives (id, workspace_id, name, revision, root_folder_id) "
            "VALUES ($1,$2,'Fallback','rev_0000000000000301',"
            "'fld_0000000000000301')",
            drive_id,
            workspace_id,
        )
        await connection.execute(
            "INSERT INTO folders (id, drive_id, parent_id, name, revision) "
            "VALUES ('fld_0000000000000301',$1,NULL,NULL,"
            "'rev_0000000000000302')",
            drive_id,
        )
        decision = await UsageMeter().reserve(
            connection,
            UsageReservation(
                operation_key="snapshot-visible-retrieval",
                metric=Metric.PUBLIC_BYTES,
                amount=100,
                limits=(
                    EffectiveLimit(
                        Metric.PUBLIC_BYTES,
                        ScopeType.WORKSPACE,
                        workspace_id,
                        Period.DAY,
                        1_000,
                    ),
                    EffectiveLimit(
                        Metric.PUBLIC_BYTES,
                        ScopeType.DRIVE,
                        drive_id,
                        Period.DAY,
                        1_000,
                    ),
                ),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
                expiry_action="commit_reserved",
            ),
        )
    assert decision.allowed

    result = await usage_snapshot.run(now=datetime.now(UTC), dry_run=False)

    assert result.operations_committed == 1
    async with conn() as connection:
        retrieval_bytes = await connection.fetchval(
            "SELECT retrieval_bytes FROM drives WHERE id=$1", drive_id
        )
    assert retrieval_bytes == 100


async def test_expired_releasable_reservation_returns_capacity(app_with_lifespan):
    await reserve_expired(action="release")

    result = await usage_snapshot.run(now=datetime.now(UTC), dry_run=False)

    assert result.operations_released == 1
    async with conn() as connection:
        window = await connection.fetchrow(
            "SELECT used, reserved FROM usage_windows WHERE scope_id=$1",
            "shr_0000000000000301",
        )
    assert dict(window) == {"used": 0, "reserved": 0}


async def test_expired_operation_batches_advance_until_the_backlog_is_empty(
    app_with_lifespan, monkeypatch
):
    monkeypatch.setattr(usage_snapshot, "MAX_BATCH", 1)
    await reserve_expired(action="commit_reserved", amount=40, suffix="-one")
    await reserve_expired(action="commit_reserved", amount=60, suffix="-two")

    result = await usage_snapshot.run(now=datetime.now(UTC), dry_run=False)

    assert result.operations_committed == 2
    async with conn() as connection:
        states = await connection.fetchval(
            "SELECT array_agg(state ORDER BY operation_key) FROM usage_operations"
        )
    assert states == ["committed", "committed"]


async def test_dry_run_reports_without_finalizing(app_with_lifespan):
    await reserve_expired(action="commit_reserved")

    result = await usage_snapshot.run(now=datetime.now(UTC), dry_run=True)

    assert result.operations_committed == 1
    async with conn() as connection:
        state = await connection.fetchval(
            "SELECT state FROM usage_operations WHERE operation_key=$1",
            "snapshot-commit_reserved",
        )
    assert state == "reserved"


@dataclass(frozen=True)
class Observation:
    generation: int
    size: int


class FakeTransferStorage:
    def __init__(self):
        self.deleted: list[tuple[str, int]] = []

    async def stat_object(self, _object_name: str) -> Observation:
        return Observation(generation=7, size=2)

    async def delete_generation(self, object_name: str, generation: int) -> None:
        self.deleted.append((object_name, generation))


async def seed_upload() -> None:
    async with conn() as connection, connection.transaction():
        await connection.execute("SET CONSTRAINTS ALL DEFERRED")
        await connection.execute(
            "INSERT INTO drives (id, workspace_id, name, revision, root_folder_id) "
            "VALUES ('drv_0000000000000301','tcws_0000000000000301','Scratch',"
            "'rev_0000000000000301','fld_0000000000000301')"
        )
        await connection.execute(
            "INSERT INTO folders (id, drive_id, parent_id, name, revision) VALUES "
            "('fld_0000000000000301','drv_0000000000000301',NULL,NULL,"
            "'rev_0000000000000302')"
        )
        await connection.execute(
            "INSERT INTO upload_sessions ("
            "id,workspace_id,drive_id,principal_type,principal_id,target_kind,"
            "parent_folder_id,artifact_name,declared_size_bytes,declared_media_type,"
            "declared_crc32c,adoption_marker,scratch_object,final_object,expires_at,state) "
            "VALUES ('upld_0000000000000301','tcws_0000000000000301',"
            "'drv_0000000000000301','agent','tcagt_0000000000000301','artifact',"
            "'fld_0000000000000301','large.bin',1,'application/octet-stream',"
            "'AAAAAA==','marker','scratch/synthetic','immutable/synthetic',"
            "now()+interval '1 hour','active')"
        )


async def test_oversized_finalized_scratch_is_rejected_and_deleted(
    app_with_lifespan,
):
    await seed_upload()
    transfer = FakeTransferStorage()

    result = await usage_snapshot.run(
        now=datetime.now(UTC), dry_run=False, transfer_storage=transfer
    )

    assert result.oversized_scratch_deleted == 1
    assert transfer.deleted == [("scratch/synthetic", 7)]
    async with conn() as connection:
        row = await connection.fetchrow(
            "SELECT state, failure_code FROM upload_sessions WHERE id=$1",
            "upld_0000000000000301",
        )
    assert dict(row) == {
        "state": "rejected",
        "failure_code": "OBSERVED_SIZE_MISMATCH",
    }


async def test_storage_parity_checks_every_workspace_and_both_counters(
    app_with_lifespan, monkeypatch
):
    monkeypatch.setattr(usage_snapshot, "MAX_BATCH", 1)
    async with conn() as connection, connection.transaction():
        await connection.execute("SET CONSTRAINTS ALL DEFERRED")
        for suffix in ("0301", "0302"):
            await connection.execute(
                "INSERT INTO drives (id, workspace_id, name, revision, root_folder_id) "
                "VALUES ($1,$2,$3,$4,$5)",
                f"drv_000000000000{suffix}",
                f"tcws_000000000000{suffix}",
                f"Workspace {suffix}",
                f"rev_000000000000{suffix}",
                f"fld_000000000000{suffix}",
            )
            await connection.execute(
                "INSERT INTO folders (id, drive_id, parent_id, name, revision) "
                "VALUES ($1,$2,NULL,NULL,$3)",
                f"fld_000000000000{suffix}",
                f"drv_000000000000{suffix}",
                f"rev_000000000001{suffix}",
            )
            await connection.execute(
                "INSERT INTO workspace_storage "
                "(workspace_id, committed_bytes, reserved_bytes) VALUES ($1,0,0)",
                f"tcws_000000000000{suffix}",
            )
        await connection.execute(
            "UPDATE workspace_storage SET committed_bytes=1, reserved_bytes=1 "
            "WHERE workspace_id='tcws_0000000000000302'"
        )

    result = await usage_snapshot.run(now=datetime.now(UTC), dry_run=False)

    assert result.storage_drifts == 1
