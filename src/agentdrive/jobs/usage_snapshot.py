from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from agentdrive.config import settings
from agentdrive.core import v0_content_commit, v0_uploads
from agentdrive.core.usage.meter import UsageMeter
from agentdrive.core.usage.models import Metric
from agentdrive.observability import setup_logging

log = logging.getLogger(__name__)

USAGE_SNAPSHOT_LOCK = 0x75736167655F7630
MAX_BATCH = 500


@dataclass
class SnapshotResult:
    dry_run: bool = False
    skipped: bool = False
    operations_released: int = 0
    operations_committed: int = 0
    operation_rows_pruned: int = 0
    window_rows_pruned: int = 0
    storage_drifts: int = 0
    oversized_scratch_deleted: int = 0
    operation_rows: int = 0
    window_rows: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _transfer_storage():
    if not settings.direct_transfer_enabled:
        return None
    from agentdrive.storage_transfers import build_transfer_storage

    return build_transfer_storage()


async def _finalize_expired(
    connection: asyncpg.Connection,
    result: SnapshotResult,
    *,
    now: datetime,
) -> None:
    meter = UsageMeter()
    while True:
        rows = await connection.fetch(
            "SELECT operation_key, metric, amount, dimensions_json, expiry_action "
            "FROM usage_operations WHERE state='reserved' AND expires_at <= $1 "
            "ORDER BY expires_at, id LIMIT $2",
            now,
            MAX_BATCH,
        )
        if not rows:
            return
        for row in rows:
            log.info(
                "at=usage_reservation_expired expiry_action=%s",
                row["expiry_action"],
            )
            if row["expiry_action"] == "commit_reserved":
                result.operations_committed += 1
                metric = Metric(row["metric"])
                async with connection.transaction():
                    await meter.commit(
                        connection,
                        operation_key=row["operation_key"],
                        metric=metric,
                        actual_amount=row["amount"],
                    )
                    if metric == Metric.PUBLIC_BYTES:
                        dimensions = row["dimensions_json"]
                        if isinstance(dimensions, str):
                            dimensions = json.loads(dimensions)
                        drive_ids = {
                            dimension["scope_id"]
                            for dimension in dimensions
                            if dimension["scope_type"] == "drive"
                        }
                        workspace_ids = {
                            dimension["scope_id"]
                            for dimension in dimensions
                            if dimension["scope_type"] == "workspace"
                        }
                        if len(drive_ids) == 1 and len(workspace_ids) == 1:
                            updated = await connection.execute(
                                "UPDATE drives "
                                "SET retrieval_bytes=retrieval_bytes+$3, "
                                "updated_at=now() WHERE id=$1 AND workspace_id=$2",
                                drive_ids.pop(),
                                workspace_ids.pop(),
                                row["amount"],
                            )
                            if updated != "UPDATE 1":
                                log.warning(
                                    "at=usage_expired_retrieval_target_missing"
                                )
            else:
                result.operations_released += 1
                await meter.release(
                    connection,
                    operation_key=row["operation_key"],
                    metric=Metric(row["metric"]),
                )


async def _prune(
    connection: asyncpg.Connection,
    result: SnapshotResult,
    *,
    now: datetime,
) -> None:
    while True:
        operations = await connection.fetch(
            "DELETE FROM usage_operations WHERE id IN ("
            "SELECT id FROM usage_operations WHERE state <> 'reserved' "
            "AND finalized_at < $1 ORDER BY finalized_at, id LIMIT $2) RETURNING id",
            now - timedelta(hours=72),
            MAX_BATCH,
        )
        result.operation_rows_pruned += len(operations)
        if len(operations) < MAX_BATCH:
            break
    while True:
        windows = await connection.fetch(
            "DELETE FROM usage_windows WHERE ctid IN ("
            "SELECT ctid FROM usage_windows "
            "WHERE window_start < $1::timestamptz - interval '15 months' "
            "ORDER BY window_start LIMIT $2) RETURNING window_start",
            now,
            MAX_BATCH,
        )
        result.window_rows_pruned += len(windows)
        if len(windows) < MAX_BATCH:
            break


async def _check_storage_parity(
    connection: asyncpg.Connection, result: SnapshotResult
) -> None:
    last_drive_id = ""
    while True:
        drive_ids = await connection.fetch(
            "SELECT id FROM drives WHERE id > $1 ORDER BY id LIMIT $2",
            last_drive_id,
            MAX_BATCH,
        )
        if not drive_ids:
            break
        for row in drive_ids:
            counter, live_sum = await v0_content_commit.storage_bytes_parity(
                connection, row["id"]
            )
            if counter != live_sum:
                result.storage_drifts += 1
                log.warning("at=usage_storage_parity_drift")
        last_drive_id = drive_ids[-1]["id"]

    last_workspace_id = ""
    while True:
        workspace_ids = await connection.fetch(
            "SELECT workspace_id FROM ("
            "SELECT workspace_id FROM drives UNION "
            "SELECT workspace_id FROM workspace_storage UNION "
            "SELECT workspace_id FROM storage_reservations "
            "WHERE upload_id IS NOT NULL AND released_at IS NULL"
            ") AS workspaces WHERE workspace_id > $1 "
            "ORDER BY workspace_id LIMIT $2",
            last_workspace_id,
            MAX_BATCH,
        )
        if not workspace_ids:
            break
        for row in workspace_ids:
            workspace_id = row["workspace_id"]
            counters = await connection.fetchrow(
                "SELECT committed_bytes, reserved_bytes FROM workspace_storage "
                "WHERE workspace_id=$1",
                workspace_id,
            )
            live_committed = await connection.fetchval(
                "SELECT COALESCE(sum(storage_bytes), 0) FROM drives "
                "WHERE workspace_id=$1",
                workspace_id,
            )
            live_reserved = await connection.fetchval(
                "SELECT COALESCE(sum(size_bytes), 0) FROM storage_reservations "
                "WHERE workspace_id=$1 AND upload_id IS NOT NULL "
                "AND released_at IS NULL",
                workspace_id,
            )
            committed = int(counters["committed_bytes"]) if counters else 0
            reserved = int(counters["reserved_bytes"]) if counters else 0
            if committed != int(live_committed) or reserved != int(live_reserved):
                result.storage_drifts += 1
                log.warning("at=usage_storage_parity_drift")
        last_workspace_id = workspace_ids[-1]["workspace_id"]


async def _delete_oversized_scratch(
    connection: asyncpg.Connection,
    result: SnapshotResult,
    *,
    transfer: Any,
) -> None:
    if transfer is None:
        return
    last_upload_id = ""
    while True:
        rows = await connection.fetch(
            "SELECT id, declared_size_bytes, scratch_object FROM upload_sessions "
            "WHERE state IN ('preparing','active') AND id > $1 "
            "ORDER BY id LIMIT $2",
            last_upload_id,
            MAX_BATCH,
        )
        if not rows:
            break
        for row in rows:
            try:
                observed = await transfer.stat_object(row["scratch_object"])
                if observed is None or observed.size is None:
                    continue
                if observed.size <= min(
                    row["declared_size_bytes"], settings.max_file_bytes
                ):
                    continue
                if result.dry_run:
                    result.oversized_scratch_deleted += 1
                    continue
                async with connection.transaction():
                    await v0_uploads.reject_session(
                        connection,
                        upload_id=row["id"],
                        failure_code="OBSERVED_SIZE_MISMATCH",
                    )
                if observed.generation is not None:
                    await transfer.delete_generation(
                        row["scratch_object"], observed.generation
                    )
                    result.oversized_scratch_deleted += 1
                    log.info("at=usage_oversized_scratch_deleted")
            except Exception as exc:  # noqa: BLE001 - isolate one provider object
                result.errors += 1
                log.error(
                    "at=usage_snapshot.scratch_failed error_class=%s",
                    type(exc).__name__,
                )
        last_upload_id = rows[-1]["id"]


async def run(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    transfer_storage: Any | None = None,
) -> SnapshotResult:
    result = SnapshotResult(dry_run=dry_run)
    connection = await asyncpg.connect(settings.database_url)
    try:
        locked = await connection.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)", USAGE_SNAPSHOT_LOCK
        )
        if not locked:
            result.skipped = True
            return result
        current = now or await connection.fetchval("SELECT clock_timestamp()")
        transaction = connection.transaction() if dry_run else None
        if transaction is not None:
            await transaction.start()
        try:
            await _finalize_expired(connection, result, now=current)
            await _prune(connection, result, now=current)
            await _check_storage_parity(connection, result)
            await _delete_oversized_scratch(
                connection,
                result,
                transfer=transfer_storage
                if transfer_storage is not None
                else _transfer_storage(),
            )
            result.operation_rows = await connection.fetchval(
                "SELECT count(*) FROM usage_operations"
            )
            result.window_rows = await connection.fetchval(
                "SELECT count(*) FROM usage_windows"
            )
        finally:
            if transaction is not None:
                await transaction.rollback()
        return result
    finally:
        with contextlib.suppress(Exception):
            await connection.execute(
                "SELECT pg_advisory_unlock($1::bigint)", USAGE_SNAPSHOT_LOCK
            )
        await connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentdrive.jobs.usage_snapshot")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-notify", action="store_true", help=argparse.SUPPRESS)
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    result = await run(dry_run=args.dry_run)
    print(json.dumps(result.as_dict(), sort_keys=True))
    failed = bool(result.errors or result.storage_drifts)
    if not failed and not result.skipped and not result.dry_run:
        log.info("at=usage_snapshot_success")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    return asyncio.run(_run_cli(build_parser().parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
