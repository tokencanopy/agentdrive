-- Recursive-deletion cohorts: every row a recursive folder delete removes is
-- stamped with ONE shared cohort id, so a restore can resurrect exactly the
-- deletion cohort — the rows that were LIVE at the moment of that recursive
-- delete — and never sweep up rows deleted earlier for unrelated reasons.
--
-- The old restore walked "every row under the subtree with deleted_at IS NOT
-- NULL", which resurrected individually-deleted items and, in the wedge
-- case, bulk-un-deleted BOTH rows of a (parent_id, name) slot (one deleted
-- individually, a same-named replacement deleted with the folder), violating
-- the partial unique index into a permanent, unretryable 500.
--
-- NULL `deleted_cohort_id` on an already-deleted row means the deletion
-- predates cohort tracking. No backfill is attempted: prod is wiped at
-- cutover and staging holds synthetic data, so a pre-migration soft-deleted
-- row simply stays deleted (a restore of a NULL-cohort folder is refused).

ALTER TABLE folders  ADD COLUMN IF NOT EXISTS deleted_cohort_id TEXT;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS deleted_cohort_id TEXT;
