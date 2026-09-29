-- 0047 — `parent_version_id` must reference a version of the SAME artifact.
--
-- `artifact_versions.parent_version_id` shipped as a bare self-reference
-- (`REFERENCES artifact_versions(id)`), which only guarantees "the parent is
-- SOME version" — not that it belongs to this row's artifact. A bug or a
-- direct write could then point artifact A's version at artifact B's version,
-- a cross-artifact history edge that every version-walk would silently
-- follow. This mirrors the fix already in place for `head_version_id`
-- (`artifacts_head_version_is_own`): pin the reference to the artifact with a
-- composite (artifact_id, <version>) FK so the cross-artifact edge is
-- structurally unrepresentable.
--
-- Defense-in-depth: no current API can set `parent_version_id` cross-artifact;
-- this makes it impossible at the storage layer regardless.
--
-- Idempotent: the DROP is `IF EXISTS` (the auto-named plain FK Postgres
-- generated for the inline reference), and the ADD is guarded on
-- `pg_constraint`, so a re-run — or a fresh-baseline DB that never had the
-- plain FK — is a no-op.

ALTER TABLE artifact_versions
  DROP CONSTRAINT IF EXISTS artifact_versions_parent_version_id_fkey;

-- ON DELETE SET NULL uses the PG15+ column-list form so only
-- `parent_version_id` is nulled: a bare `SET NULL` would also try to null
-- `artifact_id`, which is NOT NULL and part of the (artifact_id, id) key the
-- FK references, and the delete would fail.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'artifact_versions_parent_is_own') THEN
    ALTER TABLE artifact_versions
      ADD CONSTRAINT artifact_versions_parent_is_own
      FOREIGN KEY (artifact_id, parent_version_id)
      REFERENCES artifact_versions (artifact_id, id)
      ON DELETE SET NULL (parent_version_id);
  END IF;
END $$;
