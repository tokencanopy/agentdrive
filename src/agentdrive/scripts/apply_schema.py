"""Apply pending migrations + schema.sql to the configured database.

Used by the `agentdrive-migrate` Cloud Run Job in the deploy workflow
(runs before each revision deploy, halting the deploy on failure), by
`tests/conftest._ensure_test_db`, and by hand against local dev — one
code path for every database, so "has change NNNN run here?" has the
same answer mechanism everywhere.

Two layers, applied in order by `apply_all()`:

  1. `migrations/NNNN_<description>.sql` — versioned, run-exactly-once
     change scripts, tracked in the `schema_migrations(version)` ledger
     inside the target database. All pending files publish in one transaction;
     a failure halts the run (and therefore the deploy) with no intermediate
     candidate schema or ledger rows visible to serving traffic.
  2. `schema.sql` — the complete declarative shape of a FRESH database.
     Fully idempotent (`CREATE … IF NOT EXISTS`), applied after the
     migrations as a single transaction.

Fresh databases (no core tables yet) skip layer 1 entirely: the
baseline already embodies every migration, because the convention
(see migrations/README.md) is that a migration PR also folds its end
state into schema.sql. The runner records all known versions as
applied so a later run doesn't try to replay history onto a database
that never had the old shape.

The DSN comes from $DATABASE_URL directly — NOT via agentdrive.config
.settings. The migration job mounts only the database secret; the full
Settings model requires SESSION_SECRET, GCS_BUCKET, etc., which the
migration job legitimately has no business knowing. Decoupling lets
this script run with the minimum env surface.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import os
import re
import sys
from pathlib import Path

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("apply_schema")


_REPO_MARKER = "pyproject.toml"
_MIGRATION_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Session-level advisory lock id serializing concurrent appliers
# against one database (e.g. two pytest sessions bootstrapping the
# same test DB, or a re-triggered deploy racing a stuck one). Held for
# the duration of the apply; released with the connection.
_APPLY_LOCK_ID = 0x00AD5EED  # arbitrary stable constant


def _resolve_repo_root() -> Path:
    """Container: /app (the Dockerfile COPYs schema.sql + migrations/
    there). Dev checkout: walk up from this file to the pyproject.toml
    marker. Walking instead of a hardcoded depth (`parents[N]`) means
    refactoring this file's location doesn't silently break the dev
    path."""
    container = Path("/app")
    if (container / "schema.sql").is_file():
        return container

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / _REPO_MARKER).is_file():
            return parent

    raise FileNotFoundError(
        f"repo root not found: no /app/schema.sql and no {_REPO_MARKER} "
        f"in any parent of {here}"
    )


def _resolve_schema_path() -> Path:
    schema = _resolve_repo_root() / "schema.sql"
    if not schema.is_file():
        raise FileNotFoundError(f"schema.sql missing at {schema}")
    return schema


def _resolve_migrations_dir() -> Path | None:
    """The migrations directory, or None if absent (legal: a checkout
    where every migration has been folded into the baseline and the
    files pruned)."""
    d = _resolve_repo_root() / "migrations"
    return d if d.is_dir() else None


def list_migrations(migrations_dir: Path | None) -> list[tuple[str, Path]]:
    """Sorted (version, path) pairs. Strict about shape: a file in
    migrations/ that doesn't match NNNN_snake_case.sql is a mistake we
    refuse to guess about, and duplicate version numbers are a merge
    artifact that must be resolved by renaming, not by picking one."""
    if migrations_dir is None:
        return []
    out: list[tuple[str, Path]] = []
    seen: dict[str, Path] = {}
    for p in sorted(migrations_dir.glob("*.sql")):
        m = _MIGRATION_NAME_RE.match(p.name)
        if not m:
            raise ValueError(
                f"migration filename {p.name!r} doesn't match "
                "NNNN_description.sql (see migrations/README.md)"
            )
        version = m.group(1)
        if version in seen:
            raise ValueError(
                f"duplicate migration version {version}: "
                f"{seen[version].name} and {p.name}"
            )
        seen[version] = p
        out.append((version, p))
    return out


@dataclasses.dataclass(frozen=True)
class ApplyReport:
    """What one `apply_all` run did — the contract callers assert on."""

    fresh: bool
    applied: tuple[str, ...]            # migration versions executed
    marked_baseline: tuple[str, ...]    # versions recorded-not-run (fresh path)
    baseline_applied: bool


async def _ensure_ledger(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version    TEXT PRIMARY KEY,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          -- sha256 of the file as applied (or as marked-embodied on
          -- the fresh path). Editing an already-applied migration is
          -- a hard error, not a silent no-op.
          checksum   TEXT
        )
        """
    )
    # Ledgers created before the checksum column existed.
    await conn.execute(
        "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT"
    )


async def _is_fresh(conn: asyncpg.Connection) -> bool:
    """Fresh = zero user tables in `public` (the ledger itself doesn't
    count). Only a truly empty database may take the baseline-only
    path; anything partial — a pre-tx-era half-apply, a manual
    restore, a hand-built dev DB — must take the migration path, where
    idempotent migrations self-heal and real conflicts fail loudly.
    (Probing a single sentinel table misclassified exactly those
    partial states as fresh and silently marked history as embodied —
    review finding on PR #148.)"""
    return await conn.fetchval(
        """
        SELECT count(*) = 0
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND c.relname <> 'schema_migrations'
        """
    )


async def apply_all(
    dsn: str,
    *,
    schema_path: Path | None = None,
    migrations_dir: Path | None = None,
) -> ApplyReport:
    """Bring one database fully up to date: pending migrations in
    order, then the declarative baseline. The single entrypoint shared
    by the Cloud Run Job, tests/conftest, and dev usage.

    Path overrides exist for tests; production callers pass only the
    DSN and get repo-root resolution.
    """
    schema_path = schema_path or _resolve_schema_path()
    if migrations_dir is None:
        migrations_dir = _resolve_migrations_dir()
    migrations = list_migrations(migrations_dir)
    schema_sql = schema_path.read_text(encoding="utf-8")

    conn = await asyncpg.connect(dsn)
    try:
        # Serialize concurrent appliers (second pytest session, retried
        # deploy job). Session-level: released on disconnect even if we
        # die mid-apply.
        await conn.execute("SELECT pg_advisory_lock($1)", _APPLY_LOCK_ID)

        await _ensure_ledger(conn)
        done: dict[str, str | None] = {
            r["version"]: r["checksum"]
            for r in await conn.fetch(
                "SELECT version, checksum FROM schema_migrations",
            )
        }

        # Editing an already-applied migration must fail loudly — it
        # would re-deploy as a silent no-op everywhere it already ran
        # while fresh databases got the edited behavior. NULL stored
        # checksums (rows recorded before checksumming) are exempt.
        for version, path in migrations:
            stored = done.get(version)
            if stored is not None:
                current = _sha256(path.read_text(encoding="utf-8"))
                if current != stored:
                    raise RuntimeError(
                        f"migration {version} ({path.name}) was edited "
                        f"after being applied (checksum {current[:12]}… != "
                        f"recorded {stored[:12]}…). Write a NEW migration "
                        "instead — see migrations/README.md rule 5."
                    )

        fresh = await _is_fresh(conn)
        applied: list[str] = []
        marked: list[str] = []

        if fresh:
            # Baseline embodies all migrations (fold-in-same-PR
            # convention) — record them as applied without running:
            # they target shapes this database never had.
            #
            # Single transaction (baseline + history marks commit
            # atomically), which requires every statement in
            # schema.sql to be transaction-safe — no `CREATE INDEX
            # CONCURRENTLY`, `VACUUM`, `CLUSTER`, `ALTER SYSTEM`. If
            # one ever becomes necessary it needs a separate
            # non-transactional pass, designed deliberately.
            async with conn.transaction():
                await conn.execute(schema_sql)
                for version, path in migrations:
                    if version not in done:
                        await conn.execute(
                            "INSERT INTO schema_migrations (version, checksum) "
                            "VALUES ($1, $2)",
                            version,
                            _sha256(path.read_text(encoding="utf-8")),
                        )
                        marked.append(version)
        else:
            pending = [(version, path) for version, path in migrations if version not in done]
            if pending:
                # One publication boundary for the candidate schema. An old
                # revision serves throughout this job; committing file N
                # before its compatibility follow-up N+1 creates a real mixed-
                # version failure window even if it lasts milliseconds.
                async with conn.transaction():
                    for version, path in pending:
                        sql = path.read_text(encoding="utf-8")
                        log.info("applying migration %s (%s)", version, path.name)
                        await conn.execute(sql)
                        await conn.execute(
                            "INSERT INTO schema_migrations (version, checksum) "
                            "VALUES ($1, $2)",
                            version, _sha256(sql),
                        )
                        applied.append(version)

            # Baseline last, as before — single transaction, fully
            # idempotent, additive-only by convention.
            async with conn.transaction():
                await conn.execute(schema_sql)

        return ApplyReport(
            fresh=fresh,
            applied=tuple(applied),
            marked_baseline=tuple(marked),
            baseline_applied=True,
        )
    finally:
        await conn.close()


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL is unset")
        return 2

    log.info("applying migrations + schema to %s", _safe_dsn(dsn))
    report = asyncio.run(apply_all(dsn))
    log.info(
        "done: fresh=%s migrations_applied=%s marked_as_baseline=%s",
        report.fresh,
        ",".join(report.applied) or "-",
        ",".join(report.marked_baseline) or "-",
    )
    return 0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_dsn(dsn: str) -> str:
    """Strip the password segment for logging."""
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", dsn)


if __name__ == "__main__":
    sys.exit(main())
