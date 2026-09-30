"""Job entrypoint for the GC sweeper.

Thin wrapper around ``GCSweeper(...).run()``, run as
``python -m agentdrive.jobs.gc`` hourly with ``--sessions-only``, daily at
03:00 UTC for the full sweep (session reconciliation + transfer cleanup +
purge + CAS mark-sweep + scratch sweep), and weekly on Sunday 04:00 UTC with
``--orphan-sweep`` appended. That cadence is ``agentdrive.jobs.schedule``:
a self-hosted install runs it with ``python -m agentdrive.jobs.scheduler``,
a hosted deployment with its platform's scheduler. The sweep semantics live
in ``agentdrive.core.gc``.

CLI (the scheduled contract — do not change argument meanings):
  --dry-run        Preview without persisting: all database work runs inside
                   one rolled-back transaction and no object-store delete is
                   issued. Counters report the would-be work.
  --orphan-sweep   Include the weekly orphaned-prefix sweep over
                   `cas/<drive_id>/` prefixes whose drives were hard-purged.
  --sessions-only  Run ONLY the four session phases and stop — the hourly
                   pass. Releases an abandoned upload's ceiling slot, byte
                   reservation, and scratch object without paying the full
                   sweep's per-drive object-store listing, which is unbounded
                   in stored objects and must not run 24x a day. Combining it
                   with --orphan-sweep is accepted and the orphan sweep does
                   not run: it is one of the listing sweeps being skipped.

Exit code is 0 on a normal completion (including a lock-held no-op),
non-zero on an uncaught exception.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from agentdrive.core.gc import GCSweeper
from agentdrive.observability import setup_logging


def build_parser() -> argparse.ArgumentParser:
    """Factory split out from main() so tests can drive argparse without the
    asyncio.run + DB connection setup."""
    p = argparse.ArgumentParser(
        prog="agentdrive.jobs.gc",
        description="GC sweeper — session reconciliation + transfer cleanup "
                    "+ purge + mark-sweep + scratch sweep "
                    "+ (optional) orphan sweep.",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--orphan-sweep", action="store_true",
        help="Include the weekly orphaned-prefix sweep.",
    )
    p.add_argument(
        "--sessions-only", action="store_true",
        help="Hourly pass: session phases only, no object-store listing.",
    )
    return p


def make_sweeper(args: argparse.Namespace) -> GCSweeper:
    """Plumb argparse into the GCSweeper constructor.

    The dedicated transfer adapter rides along whenever direct transfer is
    enabled, so session reconciliation can actually resume stale durable
    completions (review round 3) and transfer cleanup can delete
    generation-safely. While disabled, no transfer sessions exist and the
    fail-typed default adapter stands in."""
    from agentdrive.config import settings

    transfer = None
    if settings.direct_transfer_enabled:
        from agentdrive.storage_transfers import build_transfer_storage

        transfer = build_transfer_storage()
    # getattr, not attribute access: `make_sweeper` is called with
    # hand-built Namespaces in tests and by operators, and a new flag must
    # not turn those into AttributeErrors.
    return GCSweeper(
        include_orphan_sweep=args.orphan_sweep,
        sessions_only=getattr(args, "sessions_only", False),
        transfer_storage=transfer,
    )


async def _run(args: argparse.Namespace) -> int:
    sweeper = make_sweeper(args)
    result = await sweeper.run(dry_run=args.dry_run)
    # JSON to stdout for log/dashboard consumers; structured stage lines
    # already went to Cloud Logging.
    print(json.dumps(result.as_dict(), indent=2, default=str))
    # Phase errors and counter/live-sum parity mismatches FAIL the job so the
    # Cloud Run failure alert pages — exiting 0 on either would be fail-open.
    return 1 if result.failed else 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
