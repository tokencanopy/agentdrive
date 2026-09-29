"""Unit coverage for the schema-apply migration script.

The script runs as a Cloud Run Job during deploy. We care that:
  1. It locates schema.sql correctly in both prod (/app/schema.sql) and
     dev (walk up from the script's location to find pyproject.toml).
  2. main() reads that file and pushes its contents through asyncpg.

Both are covered without touching a real DB or container — we monkey-
patch the asyncpg connect call.
"""

from __future__ import annotations


def test_resolve_schema_path_finds_repo_root_in_dev_checkout():
    """In a real checkout, the walker should land on the repo-root schema.sql.

    This test protects against accidental refactors that break the path
    resolution (e.g. moving the script deeper, flattening scripts/ into
    the package). M2 from PR #23 review.
    """
    from agentdrive.scripts.apply_schema import _resolve_schema_path

    p = _resolve_schema_path()
    assert p.is_file(), f"resolver returned non-file: {p}"
    # In dev: ends with /schema.sql at the repo root, NOT /app/schema.sql
    # (we're not in the container). Both endings are acceptable values, but
    # the file must actually contain the schema.
    content = p.read_text()
    assert "CREATE TABLE IF NOT EXISTS drives" in content
    assert "CREATE TABLE IF NOT EXISTS artifacts" in content


def test_main_wires_env_dsn_into_apply_all(monkeypatch):
    """main() is a thin wrapper: DATABASE_URL from the environment (NOT
    via agentdrive.config.settings — that would force every other env
    var to be set just to run a migration) handed to apply_all(). The
    runner's actual DB behavior is covered against real Postgres in
    tests/test_schema_migrations_runner.py.

    main() is sync (it owns asyncio.run internally), so this test is
    sync too — calling main() from an async test would collide with the
    running event loop.
    """
    from agentdrive.scripts import apply_schema

    monkeypatch.setenv("DATABASE_URL", "postgresql://t:p@/d")
    captured: dict = {}

    async def fake_apply_all(dsn, **kwargs):
        captured["dsn"] = dsn
        return apply_schema.ApplyReport(
            fresh=False, applied=("0001",), marked_baseline=(),
            baseline_applied=True,
        )

    monkeypatch.setattr(apply_schema, "apply_all", fake_apply_all)
    rc = apply_schema.main()
    assert rc == 0
    assert captured["dsn"] == "postgresql://t:p@/d"


def test_main_fails_loudly_when_database_url_unset(monkeypatch, tmp_path):
    """If DATABASE_URL is missing, main() must exit non-zero, not crash
    with a confusing AttributeError later."""
    from agentdrive.scripts import apply_schema

    fake_schema = tmp_path / "schema.sql"
    fake_schema.write_text("SELECT 1;")
    monkeypatch.setattr(apply_schema, "_resolve_schema_path", lambda: fake_schema)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    rc = apply_schema.main()
    assert rc == 2


def test_safe_dsn_masks_password():
    """The DSN is logged at startup — make sure the password is masked."""
    from agentdrive.scripts.apply_schema import _safe_dsn

    assert _safe_dsn("postgresql://user:supersecret@/db?host=/cloudsql/x") == \
        "postgresql://user:***@/db?host=/cloudsql/x"
    # Idempotent on DSNs without a password (shouldn't happen in prod but
    # shouldn't crash either).
    assert _safe_dsn("postgresql://user@/db") == "postgresql://user@/db"


def test_migration_chain_carries_0049_direct_transfer_sessions():
    """The B3 packet-1 migration is present, expand-only in shape, and its
    end state is folded into schema.sql (fold-in-same-PR convention; the
    structural equivalence itself is proven by test_schema_baseline_parity).
    """
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    migration = repo / "migrations" / "0049_direct_transfer_sessions.sql"
    assert migration.is_file(), "migrations/0049_direct_transfer_sessions.sql missing"
    sql = migration.read_text()
    # Expand-only: the migration may create/extend, never destroy. (0049
    # extends the immutability trigger via CREATE OR REPLACE FUNCTION —
    # no DROP of any kind appears in it.)
    for forbidden in (
        "DROP TABLE", "DROP COLUMN", "DROP INDEX", "DROP CONSTRAINT",
        "DROP TRIGGER", "ALTER COLUMN", "TRUNCATE",
    ):
        assert forbidden not in sql.upper(), (
            f"0049 must stay expand-only; found {forbidden!r}"
        )
    for required in (
        "upload_sessions",
        "storage_reservations",
        "workspace_storage",
        "storage_generation",
        "storage_bucket",
    ):
        assert required in sql, f"0049 is missing {required!r}"
