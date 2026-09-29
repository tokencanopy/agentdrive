-- 0045 — indexes for the (drive_id, resource_id) grant/share lookup.
--
-- `grants_list` and `shares_list` gain an exact-match `resource_type` +
-- `resource_id` filter ("what access / what links exist on THIS resource"),
-- restoring the only query shape the pre-reset surface had. Both listings
-- previously had only a drive-wide index, so the filtered predicate degraded
-- to "scan every live row in the drive and discard almost all of them" —
-- fine on a toy drive, quadratic on a real one, and it is the share dialog's
-- hot path.
--
-- Both are PARTIAL on `revoked_at IS NULL`, matching the existing
-- `grants_by_drive` / `shares_by_drive` pattern: `lifecycle=active` is the
-- default and overwhelmingly the common case, and keeping revoked tombstones
-- out of the index keeps it small as grants churn. `lifecycle=revoked|all`
-- still falls back to the drive-wide index — deliberate, those are audit
-- queries, not hot paths.
--
-- NOT `CONCURRENTLY`: `apply_schema` runs each migration inside a
-- transaction, and `CREATE INDEX CONCURRENTLY` cannot run in one. These are
-- plain `CREATE INDEX` and take a SHARE lock that blocks writes to `grants` /
-- `shares` while the build runs and the previous revision is still serving.
-- Acceptable at this size — the v0 reset emptied prod and both tables are
-- low-cardinality by nature, so the build is sub-second. Re-check row counts
-- before the migrate job if that stops being true; a genuinely large table
-- needs a concurrent build, and therefore a runner that can execute a
-- migration outside a transaction.
--
-- The leading `drive_id` column (rather than `resource_id` alone, which
-- `grants_by_resource` already provides) keeps the index aligned with the
-- tenancy boundary every one of these queries starts from, so the scan is
-- bounded by the drive before resource selectivity is applied.

CREATE INDEX IF NOT EXISTS grants_by_drive_resource
  ON grants (drive_id, resource_id) WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS shares_by_drive_resource
  ON shares (drive_id, resource_id) WHERE revoked_at IS NULL;
