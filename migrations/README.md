# Schema migrations

Versioned, run-exactly-once change scripts for databases that already
exist. Fresh databases never run these — they get `schema.sql` (the
complete declarative baseline) and the runner records every migration
here as already-embodied.

## The v0 chain continues at 0044+

The 43 migrations that once stood here (`0001`–`0043`) belonged to the
legacy 87-operation surface. Layer 1 of the v0 contract reset replaced
`schema.sql` with a day-0 baseline, folded their end state into it, and
removed the files from the tree (they remain in git history). A fresh v0
database gets `schema.sql` and the runner records the migrations currently
in the tree as already-embodied; it never replays `0001`–`0043`.

The migration **numbering was not restarted** — the v0 chain continues
from where the legacy chain left off. The files in the tree today are
`0044_deleted_cohort_id.sql`, `0045_grant_share_resource_lookup.sql`, and
`0046_viewer_sessions.sql`; the next migration is `0047`, not `0001`.

Note that pointing v0 code at an **un-wiped legacy database** is still
unsupported: its `schema_migrations` ledger holds `0001`–`0043` against a
schema that no longer exists, and it cannot serve v0 code at all. That is
why `docker compose down -v` is not optional when moving a dev database
across the reset.

Executed by `agentdrive.scripts.apply_schema.apply_all()` — the same
code path for prod (the `agentdrive-migrate` Cloud Run Job, before the
traffic swap), the test bootstrap (`tests/conftest`), and local dev
(`uv run python -m agentdrive.scripts.apply_schema`). The ledger is
the `schema_migrations(version)` table inside each database.

## Writing a migration

1. Name it `NNNN_short_description.sql` — zero-padded, next number,
   snake_case. Duplicate numbers are rejected at runtime (resolve
   merge races by renumbering, never by deleting the ledger row).
2. **Fold the end state into `schema.sql` in the same PR.** The
   baseline must always describe a fresh database completely; the
   migration carries the same change to databases that predate it.
   The parity test (`tests/test_schema_baseline_parity.py`) fails the
   PR if the two diverge.
3. All pending migrations in one invocation publish in one transaction. This
   prevents serving traffic from observing an intermediate candidate schema;
   a failure rolls back every pending file and its ledger row. Don't use
   statements that can't run in a transaction (`CREATE INDEX CONCURRENTLY`,
   `VACUUM`); if one becomes necessary, the runner needs a non-transactional
   phase first — stop and design that.
4. Migrations run while the **old revision still serves traffic**
   (the job runs before the Cloud Run traffic swap). Every change must
   be compatible with currently-deployed code: adding nullable columns,
   tables, indexes is fine; renames/drops need the expand–contract
   two-step across two releases.
5. Never edit a migration that may have been applied anywhere. Write a
   new one. Enforced: the ledger records a sha256 per applied version
   and the runner fails loudly on mismatch.
6. Idempotent SQL (`IF [NOT] EXISTS`) is encouraged but not required —
   the ledger guarantees exactly-once. (0001/0002 are idempotent only
   because they bootstrap databases that received the same change via
   baseline re-apply before the runner existed.)
7. Migrations run BEFORE the baseline, so a migration may only
   reference objects that exist in the **oldest baseline still
   deployed anywhere** — not objects a recent baseline-only change
   added. (A stale DB would run your migration before the baseline
   that creates the object.) When in doubt, guard defensively
   (`IF EXISTS`) or create the object in the migration itself; the
   parity test cannot catch this case.

## Pruning

Once a migration has been applied to every database that matters
(prod + active dev/test DBs), it may be deleted; fresh databases never
needed it. There's no rush — files here are cheap, history is useful.
