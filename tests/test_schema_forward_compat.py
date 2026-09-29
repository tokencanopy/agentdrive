"""Regression test for a failure mode that once broke a deploy: a column
was added inside a
`CREATE TABLE IF NOT EXISTS` body without a matching
`ALTER TABLE … ADD COLUMN IF NOT EXISTS`. On a pre-existing table,
`CREATE TABLE IF NOT EXISTS` is a no-op — the column never lands,
and the next `CREATE INDEX` referencing it raises
`UndefinedColumnError`.

This test applies a **frozen** baseline `schema.sql` (the snapshot
taken at the time this test was added — see
`tests/fixtures/schema_baseline.sql`) and then applies the current
`schema.sql` on top. Any future PR that adds a column to a
pre-existing table without the paired `ALTER` will fail here in
exactly the way it would fail against prod.

When you intentionally squash schema history (a rare event — see PR
#99 for the last one), refresh the baseline:

    cp schema.sql tests/fixtures/schema_baseline.sql

That re-pins the test to the new "starting point" without losing
the forward-compat guarantee from that point on.

The test skips cleanly when local Postgres isn't reachable; it runs
in CI because the `test` job in `.github/workflows/deploy.yml`
already spins up a Postgres service container.
"""

from __future__ import annotations

import os
import pathlib
import socket

import pytest
import pytest_asyncio

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CURRENT_SCHEMA = _REPO_ROOT / "schema.sql"
_BASELINE_SCHEMA = _REPO_ROOT / "tests" / "fixtures" / "schema_baseline.sql"

# Same host/port convention as conftest — local docker-compose Postgres.
# Replay against an ephemeral DB so we don't perturb the suite's main
# test DB. PID-keyed for the same reason conftest's DB name is:
# a fixed name lets two concurrent pytest runs FORCE-drop the replay
# DB out from under each other. Orphans from killed runs are reaped
# by conftest._reap_orphan_test_dbs() on the next session.
_DEV_DSN_BASE = "postgresql://agentdrive:dev@localhost:5432"
_REPLAY_DB = f"agentdrive_schema_replay_{os.getpid()}"


def _pg_reachable() -> bool:
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture
async def replay_db():
    """Drop/recreate an ephemeral DB for each test run and yield its DSN.

    Using a fresh DB (rather than TRUNCATE) is essential here: the
    failure mode we want to catch is `CREATE TABLE IF NOT EXISTS`
    being a no-op against an existing table. We need the tables to
    NOT exist at the start of the baseline apply.
    """
    if not _pg_reachable():
        pytest.skip("Postgres on localhost:5432 not reachable")

    import asyncpg

    admin = await asyncpg.connect(f"{_DEV_DSN_BASE}/postgres")
    try:
        # Drop any leftover replay DB from a prior aborted run, then
        # create a clean one. Both as separate statements because
        # asyncpg's simple-query protocol disallows DROP/CREATE
        # DATABASE inside an implicit transaction.
        await admin.execute(
            f"DROP DATABASE IF EXISTS {_REPLAY_DB} WITH (FORCE)"
        )
        await admin.execute(f"CREATE DATABASE {_REPLAY_DB}")
    finally:
        await admin.close()

    dsn = f"{_DEV_DSN_BASE}/{_REPLAY_DB}"
    try:
        yield dsn
    finally:
        admin = await asyncpg.connect(f"{_DEV_DSN_BASE}/postgres")
        try:
            await admin.execute(
                f"DROP DATABASE IF EXISTS {_REPLAY_DB} WITH (FORCE)"
            )
        finally:
            await admin.close()


async def _apply(dsn: str, sql: str) -> None:
    """Mirror apply_schema._apply: one transaction, fail-fast."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute(sql)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_current_schema_applies_on_top_of_frozen_baseline(replay_db):
    """The forward-compat guarantee: applying the current schema on top
    of the frozen baseline must succeed without errors.

    Reproduces the 2026-06-08 incident shape if a future PR re-introduces
    it: a column added to a `CREATE TABLE IF NOT EXISTS` body without a
    matching migration (migrations/NNNN, post-#148) will raise
    `UndefinedColumnError` on the subsequent index creation.
    """
    baseline_sql = _BASELINE_SCHEMA.read_text(encoding="utf-8")

    await _apply(replay_db, baseline_sql)
    # Post-#148, "apply the current schema" means what prod's migrate
    # job actually runs: apply_all() = pending migrations first, then
    # the baseline. A column added inline without a paired migration
    # still fails here (UndefinedColumnError on the index that
    # references it) — same guarantee, same code path as the deploy.
    from agentdrive.scripts.apply_schema import apply_all

    await apply_all(replay_db)


@pytest.mark.asyncio
async def test_current_schema_is_idempotent_on_fresh_db(replay_db):
    """Second apply against the same DB is a no-op. Catches accidental
    use of non-idempotent DDL (a `CREATE TABLE` without `IF NOT EXISTS`,
    an `ALTER TABLE … ADD COLUMN` without `IF NOT EXISTS`, etc.).
    """
    current_sql = _CURRENT_SCHEMA.read_text(encoding="utf-8")
    await _apply(replay_db, current_sql)
    await _apply(replay_db, current_sql)  # must not raise
