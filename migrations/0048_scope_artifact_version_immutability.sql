-- Scope the artifact_versions immutability trigger to CONTENT IDENTITY.
--
-- The original trigger (schema.sql §6.4) rejected ANY update to a version row.
-- That is safe today — nothing updates version rows — but over-broad: the
-- planned 20/200 version cap prunes old versions, and `parent_version_id`
-- carries `ON DELETE SET NULL`, so deleting a parent fires an UPDATE on the
-- surviving child (setting its parent pointer to NULL). A blanket reject would
-- break that prune.
--
-- This replaces the function body (CREATE OR REPLACE — idempotent, function
-- name and trigger wiring unchanged) so it RAISEs only when a content-identity
-- column changes, or when `parent_version_id` is re-pointed to a DIFFERENT
-- non-NULL version. Clearing `parent_version_id` to NULL is the one permitted
-- transition. Frozen: id, artifact_id, checksum, content_type, size_bytes,
-- storage_object, actor_type, actor_id, ordinal, created_at. DELETE is
-- unaffected (BEFORE UPDATE only).

CREATE OR REPLACE FUNCTION reject_artifact_version_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  -- Frozen content-identity columns: any change is rejected (NULL-safe compare).
  IF NEW.id             IS DISTINCT FROM OLD.id
     OR NEW.artifact_id    IS DISTINCT FROM OLD.artifact_id
     OR NEW.checksum       IS DISTINCT FROM OLD.checksum
     OR NEW.content_type   IS DISTINCT FROM OLD.content_type
     OR NEW.size_bytes     IS DISTINCT FROM OLD.size_bytes
     OR NEW.storage_object IS DISTINCT FROM OLD.storage_object
     OR NEW.actor_type     IS DISTINCT FROM OLD.actor_type
     OR NEW.actor_id       IS DISTINCT FROM OLD.actor_id
     OR NEW.ordinal        IS DISTINCT FROM OLD.ordinal
     OR NEW.created_at     IS DISTINCT FROM OLD.created_at
  THEN
    RAISE EXCEPTION
      'artifact_versions is append-only: content identity of version % cannot be updated',
      OLD.id
      USING ERRCODE = 'restrict_violation';
  END IF;

  -- parent_version_id: may only be CLEARED to NULL (the ON DELETE SET NULL tail
  -- reporting a pruned parent). Re-pointing to a different non-NULL version is
  -- rejected.
  IF NEW.parent_version_id IS DISTINCT FROM OLD.parent_version_id
     AND NEW.parent_version_id IS NOT NULL
  THEN
    RAISE EXCEPTION
      'artifact_versions.parent_version_id may only be cleared to NULL (pruned parent), not repointed: version %',
      OLD.id
      USING ERRCODE = 'restrict_violation';
  END IF;

  RETURN NEW;
END;
$$;

-- Trigger name and wiring (BEFORE UPDATE, FOR EACH ROW) are unchanged, so the
-- CREATE OR REPLACE above is a clean in-place swap; no DROP/CREATE TRIGGER.
