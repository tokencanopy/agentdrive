"""The migration runner (`apply_schema.apply_all`) against real
Postgres — scratch databases, synthetic mini-schemas.

Contract under test:

  * fresh DB → baseline only; every migration recorded as embodied,
    never executed
  * existing DB → pending migrations run in version order in one
    candidate-schema transaction, recorded in `schema_migrations`; re-runs are no-ops
  * a failing migration rolls back the entire pending batch, records nothing, and
    raises (halting the deploy)
  * malformed / duplicate-version filenames are rejected up front

The real schema.sql + real migrations are exercised by conftest's
bootstrap (every integration test) and the parity guard in
tests/test_schema_baseline_parity.py — here we isolate the runner's
mechanics with tiny fixtures so failures point at the runner, not the
schema.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import _ADMIN_DB_URL, _HOST_CREDS, _db_available

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _db_available(), reason="Postgres not reachable",
    ),
]

# Mini-baseline: stands in for schema.sql.
BASELINE = """
CREATE TABLE IF NOT EXISTS drives (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS widgets (
  id TEXT PRIMARY KEY,
  color TEXT NOT NULL DEFAULT 'blue'
);
"""

# Same shape as BASELINE minus the `color` column — the "old" baseline
# an existing database was built from.
OLD_BASELINE = """
CREATE TABLE IF NOT EXISTS drives (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS widgets (id TEXT PRIMARY KEY);
"""

MIGRATION_0001 = "ALTER TABLE widgets ADD COLUMN IF NOT EXISTS color TEXT NOT NULL DEFAULT 'blue';"


@pytest.fixture
async def scratch_db():
    """A throwaway database, dropped after the test. CREATE inside the
    try so the admin connection can't leak on failure (review nit);
    WITH (FORCE) replaces terminate-then-drop's reconnect race."""
    import asyncpg

    name = f"agentdrive_migtest_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(_ADMIN_DB_URL)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        yield f"{_HOST_CREDS}/{name}"
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def _write_tree(tmp_path, baseline: str, migrations: dict[str, str]):
    schema = tmp_path / "schema.sql"
    schema.write_text(baseline)
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir(exist_ok=True)
    for name, sql in migrations.items():
        (mig_dir / name).write_text(sql)
    return schema, mig_dir


async def _versions(dsn: str) -> list[str]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        return [
            r["version"] for r in await conn.fetch(
                "SELECT version FROM schema_migrations ORDER BY version",
            )
        ]
    finally:
        await conn.close()


async def _column_exists(dsn: str, table: str, column: str) -> bool:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(
            "SELECT count(*) = 1 FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2", table, column,
        )
    finally:
        await conn.close()


async def test_fresh_db_gets_baseline_and_marks_history(scratch_db, tmp_path):
    from agentdrive.scripts.apply_schema import apply_all

    schema, mig_dir = _write_tree(
        tmp_path, BASELINE, {"0001_add_color.sql": MIGRATION_0001},
    )
    report = await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)
    assert report.fresh is True
    assert report.applied == ()                  # never executed
    assert report.marked_baseline == ("0001",)   # recorded as embodied
    assert await _column_exists(scratch_db, "widgets", "color")
    assert await _versions(scratch_db) == ["0001"]

    # Idempotent: second run sees a non-fresh, fully-applied DB.
    report2 = await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)
    assert report2.fresh is False
    assert report2.applied == ()
    assert report2.marked_baseline == ()


async def test_existing_db_runs_pending_migrations_in_order(scratch_db, tmp_path):
    import asyncpg

    from agentdrive.scripts.apply_schema import apply_all

    # Build the "deployed" DB from the OLD baseline (no color column).
    conn = await asyncpg.connect(scratch_db)
    await conn.execute(OLD_BASELINE)
    await conn.close()

    schema, mig_dir = _write_tree(
        tmp_path, BASELINE, {
            "0001_add_color.sql": MIGRATION_0001,
            "0002_seed_widget.sql":
                "INSERT INTO widgets (id, color) VALUES ('w1', 'red') "
                "ON CONFLICT DO NOTHING;",
        },
    )
    report = await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)
    assert report.fresh is False
    assert report.applied == ("0001", "0002")    # order matters: 0002 needs 0001
    assert await _column_exists(scratch_db, "widgets", "color")
    assert await _versions(scratch_db) == ["0001", "0002"]

    # Re-run: ledger says done; nothing replays (the INSERT would
    # otherwise conflict-skip, but the point is it isn't attempted).
    report2 = await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)
    assert report2.applied == ()


async def test_failing_migration_rolls_back_and_records_nothing(scratch_db, tmp_path):
    import asyncpg

    from agentdrive.scripts.apply_schema import apply_all

    conn = await asyncpg.connect(scratch_db)
    await conn.execute(OLD_BASELINE)
    await conn.close()

    schema, mig_dir = _write_tree(
        tmp_path, BASELINE, {
            # First statement succeeds, second fails → the whole
            # migration must roll back, including the seed row.
            "0001_bad.sql":
                "INSERT INTO widgets (id) VALUES ('partial');\n"
                "ALTER TABLE nonexistent ADD COLUMN x TEXT;",
        },
    )
    with pytest.raises(asyncpg.PostgresError):
        await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)

    assert await _versions(scratch_db) == []     # nothing recorded
    conn = await asyncpg.connect(scratch_db)
    try:
        n = await conn.fetchval("SELECT count(*) FROM widgets WHERE id = 'partial'")
    finally:
        await conn.close()
    assert n == 0                                # partial work rolled back


async def test_failing_later_migration_rolls_back_the_whole_pending_batch(
    scratch_db, tmp_path
):
    """No intermediate candidate schema may become visible to old traffic.

    This is the exact class of failure in 0061/0062: if 0061 commits before
    0062 relaxes its constraint, the previous serving revision can fail writes
    in between. The pending set is one publication boundary.
    """
    import asyncpg

    from agentdrive.scripts.apply_schema import apply_all

    conn = await asyncpg.connect(scratch_db)
    await conn.execute(OLD_BASELINE)
    await conn.close()

    schema, mig_dir = _write_tree(
        tmp_path,
        BASELINE,
        {
            "0001_add_color.sql": MIGRATION_0001,
            "0002_bad.sql": "ALTER TABLE nonexistent ADD COLUMN x TEXT;",
        },
    )
    with pytest.raises(asyncpg.PostgresError):
        await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)

    assert await _versions(scratch_db) == []
    assert not await _column_exists(scratch_db, "widgets", "color")


async def test_0061_and_0062_keep_the_previous_edit_writer_compatible(
    scratch_db, tmp_path
):
    """The previous revision omits actor_subject_type from its INSERT."""
    import asyncpg

    from agentdrive.scripts.apply_schema import apply_all

    old_schema = """
    CREATE TABLE drives (id TEXT PRIMARY KEY);
    CREATE TABLE sheet_sessions (
      id TEXT PRIMARY KEY,
      actor_subject_type TEXT NOT NULL
    );
    CREATE TABLE sheet_session_edits (
      session_id TEXT NOT NULL REFERENCES sheet_sessions(id),
      seq INTEGER NOT NULL,
      actor_subject TEXT NOT NULL,
      PRIMARY KEY (session_id, seq)
    );
    """
    current_schema = """
    CREATE TABLE IF NOT EXISTS drives (id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS sheet_sessions (
      id TEXT PRIMARY KEY,
      actor_subject_type TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sheet_session_edits (
      session_id TEXT NOT NULL REFERENCES sheet_sessions(id),
      seq INTEGER NOT NULL,
      actor_subject_type TEXT,
      actor_subject TEXT NOT NULL,
      PRIMARY KEY (session_id, seq)
    );
    """
    repo = __import__("pathlib").Path(__file__).resolve().parent.parent
    migrations = {
        "0061_sheet_session_edit_actor_type.sql": (
            repo / "migrations/0061_sheet_session_edit_actor_type.sql"
        ).read_text(),
        "0062_sheet_session_edit_actor_type_compat.sql": (
            repo / "migrations/0062_sheet_session_edit_actor_type_compat.sql"
        ).read_text(),
    }

    conn = await asyncpg.connect(scratch_db)
    await conn.execute(old_schema)
    await conn.execute(
        "INSERT INTO sheet_sessions (id, actor_subject_type) VALUES ('s1', 'agent')"
    )
    await conn.execute(
        "INSERT INTO sheet_session_edits (session_id, seq, actor_subject) "
        "VALUES ('s1', 1, 'tcusr_0000000000000010')"
    )
    await conn.close()

    schema, mig_dir = _write_tree(tmp_path, current_schema, migrations)
    report = await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)
    assert report.applied == ("0061", "0062")

    conn = await asyncpg.connect(scratch_db)
    try:
        # This is the pre-0061 write shape. It must remain valid until every
        # rollback target includes the new column in its INSERT.
        await conn.execute(
            "INSERT INTO sheet_session_edits (session_id, seq, actor_subject) "
            "VALUES ('s1', 2, 'tcagt_0000000000000001')"
        )
        assert await conn.fetchval(
            "SELECT actor_subject_type FROM sheet_session_edits "
            "WHERE session_id = 's1' AND seq = 1"
        ) == "user"
        assert await conn.fetchval(
            "SELECT actor_subject_type IS NULL FROM sheet_session_edits "
            "WHERE session_id = 's1' AND seq = 2"
        )
    finally:
        await conn.close()


async def test_0062_repairs_an_already_applied_0061(scratch_db, tmp_path):
    """The corrective migration works when 0061 committed in an earlier run."""
    import asyncpg

    from agentdrive.scripts.apply_schema import apply_all

    old_schema = """
    CREATE TABLE drives (id TEXT PRIMARY KEY);
    CREATE TABLE sheet_sessions (
      id TEXT PRIMARY KEY,
      actor_subject_type TEXT NOT NULL
    );
    CREATE TABLE sheet_session_edits (
      session_id TEXT NOT NULL REFERENCES sheet_sessions(id),
      seq INTEGER NOT NULL,
      actor_subject TEXT NOT NULL,
      PRIMARY KEY (session_id, seq)
    );
    """
    current_schema = """
    CREATE TABLE IF NOT EXISTS drives (id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS sheet_sessions (
      id TEXT PRIMARY KEY,
      actor_subject_type TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sheet_session_edits (
      session_id TEXT NOT NULL REFERENCES sheet_sessions(id),
      seq INTEGER NOT NULL,
      actor_subject_type TEXT,
      actor_subject TEXT NOT NULL,
      PRIMARY KEY (session_id, seq)
    );
    """
    repo = __import__("pathlib").Path(__file__).resolve().parent.parent
    migration_0061 = (
        repo / "migrations/0061_sheet_session_edit_actor_type.sql"
    ).read_text()
    migration_0062 = (
        repo / "migrations/0062_sheet_session_edit_actor_type_compat.sql"
    ).read_text()

    conn = await asyncpg.connect(scratch_db)
    await conn.execute(old_schema)
    await conn.execute(
        "INSERT INTO sheet_sessions (id, actor_subject_type) VALUES ('s1', 'agent')"
    )
    await conn.execute(
        "INSERT INTO sheet_session_edits (session_id, seq, actor_subject) "
        "VALUES ('s1', 1, 'tcusr_0000000000000010')"
    )
    await conn.close()

    first_root = tmp_path / "first"
    first_root.mkdir()
    schema, first_dir = _write_tree(
        first_root,
        current_schema,
        {"0061_sheet_session_edit_actor_type.sql": migration_0061},
    )
    first = await apply_all(scratch_db, schema_path=schema, migrations_dir=first_dir)
    assert first.applied == ("0061",)

    second_root = tmp_path / "second"
    second_root.mkdir()
    second_schema, second_dir = _write_tree(
        second_root,
        current_schema,
        {
            "0061_sheet_session_edit_actor_type.sql": migration_0061,
            "0062_sheet_session_edit_actor_type_compat.sql": migration_0062,
        },
    )
    second = await apply_all(
        scratch_db, schema_path=second_schema, migrations_dir=second_dir
    )
    assert second.applied == ("0062",)

    conn = await asyncpg.connect(scratch_db)
    try:
        assert await conn.fetchval(
            "SELECT actor_subject_type FROM sheet_session_edits "
            "WHERE session_id = 's1' AND seq = 1"
        ) == "user"
        await conn.execute(
            "INSERT INTO sheet_session_edits (session_id, seq, actor_subject) "
            "VALUES ('s1', 2, 'tcsvc_0000000000000011')"
        )
        assert await conn.fetchval(
            "SELECT actor_subject_type IS NULL FROM sheet_session_edits "
            "WHERE session_id = 's1' AND seq = 2"
        )
    finally:
        await conn.close()


async def test_malformed_and_duplicate_names_rejected(tmp_path):
    from agentdrive.scripts.apply_schema import list_migrations

    _, mig_dir = _write_tree(tmp_path, BASELINE, {"0001_ok.sql": "SELECT 1;"})
    (mig_dir / "freeform.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="doesn't match"):
        list_migrations(mig_dir)
    (mig_dir / "freeform.sql").unlink()

    (mig_dir / "0001_other_name.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="duplicate migration version"):
        list_migrations(mig_dir)


async def test_half_built_db_takes_migration_path_and_self_heals(scratch_db, tmp_path):
    """Review MEDIUM on PR #148: a partial database (some tables, but
    not the runner's old sentinel `drives`) must NOT be classified
    fresh — that marked history as embodied without running it,
    permanently stranding the DB. With table-count detection it takes
    the migration path and idempotent migrations self-heal."""
    import asyncpg

    from agentdrive.scripts.apply_schema import apply_all

    conn = await asyncpg.connect(scratch_db)
    # Old-shape widgets only; crucially NO drives table.
    await conn.execute("CREATE TABLE widgets (id TEXT PRIMARY KEY);")
    await conn.close()

    schema, mig_dir = _write_tree(
        tmp_path, BASELINE, {"0001_add_color.sql": MIGRATION_0001},
    )
    report = await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)
    assert report.fresh is False                 # the fix under test
    assert report.applied == ("0001",)           # executed, not marked
    assert await _column_exists(scratch_db, "widgets", "color")
    assert await _column_exists(scratch_db, "drives", "id")  # baseline filled in


async def test_edited_applied_migration_fails_loudly(scratch_db, tmp_path):
    """README rule 5, enforced: editing an already-applied migration
    raises instead of silently no-opping on deployed DBs while fresh
    DBs get the new behavior."""
    from agentdrive.scripts.apply_schema import apply_all

    schema, mig_dir = _write_tree(
        tmp_path, BASELINE, {"0001_add_color.sql": MIGRATION_0001},
    )
    await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)

    (mig_dir / "0001_add_color.sql").write_text(
        MIGRATION_0001 + "\n-- sneaky edit\n",
    )
    with pytest.raises(RuntimeError, match="was edited"):
        await apply_all(scratch_db, schema_path=schema, migrations_dir=mig_dir)
