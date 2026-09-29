from __future__ import annotations

import argparse
import asyncio
import json
import sys

import asyncpg

from agentdrive.config import settings
from agentdrive.jobs import usage_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentdrive.scripts.manage_usage")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--dry-run", action="store_true")
    show = commands.add_parser("show")
    show.add_argument("--workspace", required=True)
    return parser


async def _show(workspace_id: str) -> dict:
    connection = await asyncpg.connect(settings.database_url)
    try:
        storage = await connection.fetchrow(
            "SELECT COALESCE(sum(storage_bytes),0) AS used, "
            "COALESCE(sum(storage_reserved_bytes),0) AS reserved "
            "FROM drives WHERE workspace_id=$1 AND deleted_at IS NULL",
            workspace_id,
        )
        windows = await connection.fetch(
            "SELECT metric, period, window_start, used, reserved "
            "FROM usage_windows WHERE scope_type='workspace' AND scope_id=$1 "
            "ORDER BY metric, period, window_start DESC",
            workspace_id,
        )
        return {
            "workspace_id": workspace_id,
            "storage": dict(storage),
            "windows": [dict(row) for row in windows],
        }
    finally:
        await connection.close()


async def _run(args: argparse.Namespace) -> int:
    if args.command == "snapshot":
        result = await usage_snapshot.run(dry_run=args.dry_run)
        payload = result.as_dict()
    else:
        payload = await _show(args.workspace)
    print(json.dumps(payload, default=str, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(build_parser().parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
