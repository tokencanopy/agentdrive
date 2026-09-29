"""Fresh-vs-migrated parity: the fold-in-same-PR convention, enforced.

Builds two scratch databases:

  A (fresh):    new baseline via apply_all          — what new DBs get
  B (migrated): merge-base baseline, then apply_all — what deployed
                DBs become after this PR's migrations run

and diffs their structural dumps. Divergence means a PR changed the
baseline without a matching migration (deployed DBs strand on the old
shape — the folder_purged / workos-constraint bug class) or shipped a
migration without folding it into the baseline (fresh DBs strand).

The "old" baseline comes from `git merge-base HEAD origin/main`'s
schema.sql, so the test is meaningful on feature branches and a
near-no-op self-check on main itself. Skips cleanly when git or the
ref isn't available (e.g. shallow CI clones without fetch).
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.conftest import _ADMIN_DB_URL, _HOST_CREDS, _db_available

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _db_available(), reason="Postgres not reachable",
    ),
]

REPO_ROOT = Path(__file__).resolve().parent.parent

# The structural dump: tables, columns (name/type/nullable/default),
# and constraint definitions. Indexes are included via pg_indexes.
# Ordered so the diff is stable.
_STRUCTURE_SQL = """
SELECT 'column' AS kind,
       table_name || '.' || column_name AS name,
       data_type || ':' || is_nullable || ':' ||
       coalesce(column_default, '-') AS detail
  FROM information_schema.columns
 WHERE table_schema = 'public'
   AND table_name <> 'schema_migrations'
UNION ALL
SELECT 'constraint',
       conrelid::regclass::text || '.' || conname,
       pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE connamespace = 'public'::regnamespace
   AND conrelid::regclass::text <> 'schema_migrations'
UNION ALL
SELECT 'index', schemaname || '.' || indexname, indexdef
  FROM pg_indexes
 WHERE schemaname = 'public'
   AND tablename <> 'schema_migrations'
UNION ALL
-- Triggers + their functions: a fold-in that adds a trigger (e.g. the
-- events append-only guard, DEBT-4) must land identically in schema.sql AND
-- the migration, or fresh and migrated DBs diverge on protection. The
-- earlier arms don't see triggers/functions, so without this a trigger in
-- one but not the other would pass silently.
SELECT 'trigger', tgrelid::regclass::text || '.' || tgname,
       pg_get_triggerdef(oid)
  FROM pg_trigger
 WHERE NOT tgisinternal
   AND tgrelid::regclass::text <> 'schema_migrations'
UNION ALL
-- Only our own plain functions: prokind 'f' excludes aggregates/window/
-- procedures (pg_get_functiondef errors on an aggregate like a pg_trgm
-- `avg`), and the pg_depend 'e' exclusion drops extension-owned functions
-- (identical on both DBs, just noise). Leaves update_drive_storage_bytes +
-- reject_events_mutation — the ones a fold-in must keep in lockstep.
SELECT 'function', p.proname,
       pg_get_functiondef(p.oid)
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public'
   AND p.prokind = 'f'
   AND NOT EXISTS (
     SELECT 1 FROM pg_depend d
      WHERE d.objid = p.oid AND d.deptype = 'e'
   )
-- Seed-row data: the one non-structural surface that can genuinely
-- diverge. The v0 day-0 baseline carries NO seed rows (the contract-reset
-- schema is pure DDL), so there is nothing to diff here. The pre-reset
-- `tiers` arm was deleted with the reset: that table no longer exists in
-- any baseline, and leaving the reference would crash every parity run the
-- moment a migration exists to re-arm it.
ORDER BY 1, 2, 3
"""


def _tree_prefix() -> str:
    """This app's path from the git root: `` standalone, `apps/drive/` here.

    `git show <rev>:<path>` and `git ls-tree <rev> <path>` both address from the
    ROOT of the repository, not the working directory, so every revision-scoped
    lookup below needs this prepended. Asking git rather than hardcoding it
    keeps the guard working in both layouts — the app was ported into a
    monorepo once and could be extracted again.
    """
    try:
        return subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _merge_base() -> str | None:
    try:
        return subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _baseline_exists(base: str) -> bool:
    """Whether the merge-base commit carries this app's schema at all.

    Distinct from `_old_baseline() is None`, and the distinction is the whole
    point: a merge-base that resolves but holds no `schema.sql` is not a broken
    checkout, it is a commit from before this app existed in the repository.
    That is exactly the merge-base of the import pull request, whose base is
    this monorepo's `main` from before `apps/drive` was here.
    """
    return subprocess.run(
        ["git", "cat-file", "-e", f"{base}:{_tree_prefix()}schema.sql"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).returncode == 0


def _old_baseline(base: str) -> str | None:
    """schema.sql as of the merge-base with origin/main, or None when
    unavailable."""
    try:
        return subprocess.run(
            ["git", "show", f"{base}:{_tree_prefix()}schema.sql"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _old_migration_versions(base: str) -> list[str]:
    """The migration versions (`NNNN`) present at the merge-base. A real
    deployed DB at that commit has applied every one of them — their
    effects are folded into the merge-base `schema.sql`. We seed B's ledger
    with these so `apply_all` runs only THIS PR's *new* migrations on top,
    exactly as a deployed DB would. Without it the parity check would
    re-run all historical migrations against the folded baseline, which
    breaks the moment a migration references a column a *later* migration
    dropped (e.g. 0008/0010's `visibility` reads vs 0011's drop)."""
    # `--full-tree` is load-bearing, and its absence is why this guard silently
    # replayed every migration after the monorepo port. `git show <rev>:<path>`
    # resolves from the repository ROOT, but `git ls-tree` resolves its pathspec
    # from the CURRENT DIRECTORY unless told otherwise — so the same
    # `apps/drive/` prefix that fixed `show` made `ls-tree` look for
    # `apps/drive/apps/drive/migrations/` and match nothing.
    #
    # It returned zero files and exit 0. No error, no stderr: the ledger was
    # seeded with nothing, so B replayed the entire migration history against a
    # baseline that already folded it in, and the first migration to CREATE a
    # table that schema.sql also creates failed with DuplicateTableError. The
    # guard degraded into a different, wrong test rather than reporting a
    # problem — which is the failure mode worth naming here.
    out = subprocess.run(
        [
            "git", "ls-tree", "-r", "--full-tree", "--name-only",
            base, f"{_tree_prefix()}migrations/",
        ],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    versions: list[str] = []
    for line in out.splitlines():
        name = line.rsplit("/", 1)[-1]
        m = re.match(r"^(\d{4})_.*\.sql$", name)
        if m:
            versions.append(m.group(1))

    # `--full-tree` fixed the lookup. This stops the NEXT one failing the same
    # way, which is the part that actually bit: a broken pathspec is
    # indistinguishable from "no migrations yet" — both are an empty list and
    # exit 0 — and the guard cannot tell that it has stopped testing anything.
    #
    # The two ARE distinguishable one level up: ask git whether the directory
    # exists in that tree. If it does and the listing is empty, the lookup is
    # broken, not the history. Raise instead of seeding an empty ledger and
    # replaying migrations that are already folded into the baseline.
    if not versions:
        directory = subprocess.run(
            ["git", "cat-file", "-e", f"{base}:{_tree_prefix()}migrations"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if directory.returncode == 0:
            raise AssertionError(
                f"{_tree_prefix()}migrations/ exists at {base[:12]} but the "
                "listing is empty — the pathspec is wrong, not the history. "
                "Seeding an empty ledger here would replay every migration "
                "against a baseline that already contains them and silently "
                "test the wrong thing."
            )
    return sorted(versions)


async def _structure(dsn: str) -> list[tuple[str, str, str]]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_STRUCTURE_SQL)
        return [(r["kind"], r["name"], r["detail"]) for r in rows]
    finally:
        await conn.close()


async def test_fresh_baseline_equals_old_baseline_plus_migrations(tmp_path):
    import asyncpg

    from agentdrive.scripts.apply_schema import _ensure_ledger, apply_all

    # Across the v0 contract reset this comparison is vacuous by
    # construction. The guard asks "does old baseline + migrations equal the
    # new baseline?" -- but the reset replaced the baseline wholesale AND
    # emptied the chain, so the migrated side is just the OLD baseline and
    # the two can never match. Skipping is honest; loosening the diff to
    # make it pass would disarm the guard permanently.
    #
    # It re-arms on its own with the first v0 migration, because from then
    # on both sides share a lineage again. Keyed on "no migrations exist"
    # rather than a pinned commit so nobody has to remember to switch it
    # back on.
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    if not sorted(migrations_dir.glob("*.sql")):
        pytest.skip(
            "migrations/ is empty (v0 contract reset) -- baseline+migrations "
            "cannot reproduce a deliberately replaced baseline; the guard "
            "re-arms with the first v0 migration"
        )

    base = _merge_base()
    # A merge-base that resolves but predates this app in the repository is a
    # different thing from a checkout that cannot resolve one, and only the
    # second is a regression. The import pull request is the first case: its
    # base is the monorepo's `main` from before `apps/drive` existed, so
    # there is no prior baseline to migrate FROM and nothing this guard can
    # compare. Checked before the CI failure below so that case skips instead
    # of reporting a fetch-depth problem that is not there.
    if base and not _baseline_exists(base):
        pytest.skip(
            f"no schema.sql at merge-base {base[:12]} — expected only where this "
            "app is new to the repository (the AgentDrive import); on an "
            "ordinary pull request it means the tree prefix is wrong"
        )

    old_schema = _old_baseline(base) if base else None
    if old_schema is None:
        # Locally a missing ref is benign (detached worktree, no
        # remote). In CI a silent skip would hide the guard forever —
        # the test workflow sets fetch-depth: 0 precisely so this
        # resolves; failing loudly catches a checkout regression.
        if os.environ.get("CI"):
            pytest.fail(
                "parity guard could not resolve merge-base(HEAD, "
                "origin/main) in CI — check the workflow's fetch-depth"
            )
        pytest.skip("git merge-base/show unavailable")

    admin = await asyncpg.connect(_ADMIN_DB_URL)
    suffix = uuid.uuid4().hex[:8]
    db_fresh = f"agentdrive_parity_a_{suffix}"
    db_migrated = f"agentdrive_parity_b_{suffix}"
    dsn_fresh = f"{_HOST_CREDS}/{db_fresh}"
    dsn_migrated = f"{_HOST_CREDS}/{db_migrated}"
    try:
        # Inside the try so a failure on the SECOND create still drops
        # the first (review cleanup nit).
        await admin.execute(f'CREATE DATABASE "{db_fresh}"')
        await admin.execute(f'CREATE DATABASE "{db_migrated}"')
        # A: brand-new database, current baseline.
        await apply_all(dsn_fresh)

        # B: "deployed" database — old baseline first (it carried its own
        # defensive blocks at the time), THEN seed the ledger to reflect a
        # real deployed DB at merge-base (every migration that existed then
        # was applied; NULL checksum = exempt from the edit-guard). The full
        # runner then applies only THIS PR's *new* migrations on top.
        conn = await asyncpg.connect(dsn_migrated)
        try:
            async with conn.transaction():
                await conn.execute(old_schema)
                await _ensure_ledger(conn)
                await conn.executemany(
                    "INSERT INTO schema_migrations (version, checksum) "
                    "VALUES ($1, NULL) ON CONFLICT (version) DO NOTHING",
                    [(v,) for v in _old_migration_versions(base)],
                )
        finally:
            await conn.close()
        report = await apply_all(dsn_migrated)
        assert report.fresh is False    # B must take the migration path

        struct_fresh = await _structure(dsn_fresh)
        struct_migrated = await _structure(dsn_migrated)

        only_fresh = sorted(set(struct_fresh) - set(struct_migrated))
        only_migrated = sorted(set(struct_migrated) - set(struct_fresh))
        assert not only_fresh and not only_migrated, (
            "Baseline and migrations diverged.\n"
            "Present only in FRESH (baseline changed without a "
            "migration?):\n  " + "\n  ".join(map(str, only_fresh))
            + "\nPresent only in MIGRATED (migration not folded into "
            "baseline?):\n  " + "\n  ".join(map(str, only_migrated))
        )
    finally:
        for name in (db_fresh, db_migrated):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
