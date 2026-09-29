"""Guarded generation backfill for legacy CAS version rows (B3 §7).

``python -m agentdrive.jobs.reconcile_generations [--dry-run]`` scans every
version row whose object coordinates are incomplete
(``storage_generation IS NULL``), verifies the landed object's identity, and
fills ``storage_bucket``/``storage_generation`` only while the locked row
still holds the same object/checksum/size:

  * the row must be a CAS row (``cas/…`` key, ``sha256:`` checksum) whose
    key digest equals the stored algorithm-qualified checksum — the CAS
    key's SHA-256 identity check;
  * the object must exist in the configured artifact bucket with the exact
    recorded size and a real generation.

Missing, changed, or inconsistent objects remain unresolved and are
reported as safe counts/ids for operator repair. The job never deletes or
rewrites content, is idempotent, and the ``artifact_versions_immutable``
trigger permits only this guarded NULL → observed-value transition.
Transfer readiness (`core.v0_uploads.transfer_readiness`) stays false while
any row is unresolved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field

import asyncpg

from agentdrive import storage
from agentdrive.observability import setup_logging

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    """Safe, non-secret outcome counts (JSON-printed by the CLI)."""

    dry_run: bool = False
    scanned: int = 0
    resolved: int = 0
    unresolved: int = 0
    unresolved_version_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _cas_digest(storage_object: str) -> str | None:
    """The SHA-256 hex a CAS key claims (`cas/{drive}/{digest}`), or None
    for a non-CAS key."""
    if not storage_object.startswith(storage.CAS_PREFIX):
        return None
    digest = storage_object.rsplit("/", 1)[-1]
    return digest if len(digest) == 64 else None


async def reconcile_all(*, dry_run: bool, batch: int = 500) -> ReconcileReport:
    """One idempotent pass over every unresolved row."""
    from agentdrive.config import settings

    report = ReconcileReport(dry_run=dry_run)
    conn = await asyncpg.connect(settings.database_url)
    try:
        after = ""
        while True:
            rows = await conn.fetch(
                "SELECT id, storage_object, checksum, size_bytes "
                "FROM artifact_versions "
                "WHERE (storage_generation IS NULL OR storage_bucket IS NULL "
                "       OR storage_bucket = '') AND id > $1 "
                "ORDER BY id LIMIT $2",
                after, batch,
            )
            if not rows:
                break
            for row in rows:
                report.scanned += 1
                if await _reconcile_one(conn, row, storage.store_id(), dry_run):
                    report.resolved += 1
                else:
                    report.unresolved += 1
                    report.unresolved_version_ids.append(row["id"])
            after = rows[-1]["id"]
        return report
    finally:
        await conn.close()


async def _reconcile_one(
    conn: asyncpg.Connection, row: asyncpg.Record, bucket: str, dry_run: bool
) -> bool:
    version_id = row["id"]
    digest = _cas_digest(row["storage_object"])
    if digest is None or row["checksum"] != f"sha256:{digest}":
        # The CAS key's SHA-256 identity does not equal the stored
        # algorithm-qualified checksum: operator repair, never a guess.
        log.warning(
            "at=reconcile.identity_mismatch version_id=%s", version_id
        )
        return False
    stat = await storage.stat(row["storage_object"])
    if stat is None:
        log.warning("at=reconcile.object_missing version_id=%s", version_id)
        return False
    if stat.size != row["size_bytes"] or stat.generation is None:
        log.warning(
            "at=reconcile.size_or_generation_mismatch version_id=%s", version_id
        )
        return False
    if dry_run:
        return True
    # The conditional UPDATE is the guard: it matches only while the row
    # still holds the same object/checksum/size (no row lock is needed —
    # the immutable trigger additionally guards NULL → value).
    updated = await conn.execute(
        "UPDATE artifact_versions SET storage_bucket = $2, "
        "storage_generation = $3 "
        "WHERE id = $1 AND storage_object = $4 AND checksum = $5 "
        "  AND size_bytes = $6 AND storage_generation IS NULL",
        version_id, bucket, stat.generation, row["storage_object"],
        row["checksum"], row["size_bytes"],
    )
    return updated == "UPDATE 1"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentdrive.jobs.reconcile_generations",
        description="Backfill artifact_versions.storage_bucket/"
                    "storage_generation for verified legacy CAS rows.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Report resolvable/unresolved rows without writing.",
    )
    return p


async def _run(args: argparse.Namespace) -> int:
    report = await reconcile_all(dry_run=args.dry_run)
    print(json.dumps(report.as_dict(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
