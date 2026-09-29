-- Where a version came from, kept on the version itself.
--
-- A completed sheet session published a version and then vanished from the
-- record: nothing on `artifact_versions` said which session produced it, and
-- `sheet_sessions_complete`'s `message` — an accepted, documented request
-- field — was hashed for idempotency and then discarded. So "what were these
-- six cell changes, and who asked for them" was unanswerable the moment the
-- session ended.
--
-- DENORMALISED ON PURPOSE, and no foreign key. `sheet_sessions` rows are
-- swept a day after they terminate (GC's `_sweep_sheet_sessions`), which is
-- exactly when this history starts being worth having. An FK would either
-- block that sweep or, with ON DELETE SET NULL, erase the link precisely when
-- it matters. So the id is stored as an opaque value that may no longer
-- resolve, and the human-readable half is copied here to outlive it.
--
-- Consequence worth stating: the MESSAGE and the session id are permanent,
-- the per-cell edit LOG is not — it lives only as long as the session row.
-- Anything wanting durable cell-level history has to write it here too.
--
-- Deliberately generic (`origin_*`, not `sheet_session_*`): an upload session
-- and a restore are the same shape of fact, and neither should need a column
-- of its own.

ALTER TABLE artifact_versions
  ADD COLUMN IF NOT EXISTS origin_session_id TEXT,
  ADD COLUMN IF NOT EXISTS origin_message    TEXT;

-- Both are part of the version's identity once written, so the append-only
-- trigger has to know about them. Without this they would be the only
-- columns on the table an UPDATE could quietly rewrite — the guard
-- enumerates columns rather than comparing whole rows.
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
     OR NEW.origin_session_id IS DISTINCT FROM OLD.origin_session_id
     OR NEW.origin_message    IS DISTINCT FROM OLD.origin_message
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

  -- B3 object coordinates (migration 0049): NULL → observed value is the one
  -- permitted reconciliation transition — the guarded reconcile_generations
  -- job filling a legacy CAS row. A resolved coordinate never changes and
  -- never un-resolves.
  IF NEW.storage_bucket IS DISTINCT FROM OLD.storage_bucket
     AND OLD.storage_bucket IS NOT NULL
  THEN
    RAISE EXCEPTION
      'artifact_versions.storage_bucket is resolved once and immutable: version %',
      OLD.id
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF NEW.storage_generation IS DISTINCT FROM OLD.storage_generation
     AND OLD.storage_generation IS NOT NULL
  THEN
    RAISE EXCEPTION
      'artifact_versions.storage_generation is resolved once and immutable: version %',
      OLD.id
      USING ERRCODE = 'restrict_violation';
  END IF;

  RETURN NEW;
END;
$$;
