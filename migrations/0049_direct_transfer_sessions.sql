-- 0049 — B3 direct transfer sessions (expand-only).
--
-- Governing contract: TokenCanopy
-- docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md §6.
--
-- Adds (never removes):
--   * artifact_versions.storage_bucket / storage_generation — nullable object
--     coordinates. New inline writes persist them; the guarded
--     reconcile_generations job fills legacy CAS rows NULL → observed value.
--   * upload_sessions — the durable, credential-free direct-upload session
--     record: publication + cleanup state machines, transition leases,
--     declared content, server-selected scratch/final keys, observation and
--     result columns. The dormant legacy v0_uploads table is NOT promoted and
--     is left untouched.
--   * storage_reservations — the promised-logical-byte ledger with an
--     exactly-once conditional release.
--   * workspace_storage — the additive per-workspace committed/reserved
--     logical-byte accounting row.
--   * a backfill making drives.storage_bytes the authoritative per-drive
--     committed logical counter (= sum(artifact_versions.size_bytes)).
--
-- Rollback is flag-first and forward-only: transfer stays disabled by
-- default and this migration is never reverted; retained session rows and GC
-- finish safely under a disabled flag.

-- ---------------------------------------------------------------------------
-- artifact_versions: nullable object coordinates
-- ---------------------------------------------------------------------------

ALTER TABLE artifact_versions ADD COLUMN IF NOT EXISTS storage_bucket TEXT;
ALTER TABLE artifact_versions ADD COLUMN IF NOT EXISTS storage_generation BIGINT;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'artifact_versions_generation_positive'
  ) THEN
    ALTER TABLE artifact_versions
      ADD CONSTRAINT artifact_versions_generation_positive
      CHECK (storage_generation IS NULL OR storage_generation > 0);
  END IF;
END $$;

-- Coordinates are all-or-none: a generation without its bucket (or the
-- reverse, or an empty bucket) is unrepresentable, so readiness and the
-- reconcile job can treat "resolved" as one fact (packet-1 correction,
-- blocker 6).
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'artifact_versions_coordinates_all_or_none'
  ) THEN
    ALTER TABLE artifact_versions
      ADD CONSTRAINT artifact_versions_coordinates_all_or_none
      CHECK (
        ((storage_bucket IS NULL) = (storage_generation IS NULL))
        AND (storage_bucket IS NULL OR storage_bucket <> '')
      );
  END IF;
END $$;

-- The immutability trigger gains the one permitted reconciliation
-- transition: storage_bucket / storage_generation may go NULL → observed
-- value exactly once; any change of a non-NULL value (including clearing)
-- is history rewriting and is rejected. Identical to the schema.sql
-- baseline definition (fold-in-same-PR convention).
CREATE OR REPLACE FUNCTION reject_artifact_version_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
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

  IF NEW.parent_version_id IS DISTINCT FROM OLD.parent_version_id
     AND NEW.parent_version_id IS NOT NULL
  THEN
    RAISE EXCEPTION
      'artifact_versions.parent_version_id may only be cleared to NULL (pruned parent), not repointed: version %',
      OLD.id
      USING ERRCODE = 'restrict_violation';
  END IF;

  -- B3 object coordinates: NULL → observed value is the one permitted
  -- reconciliation transition; a resolved coordinate never changes and
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

-- ---------------------------------------------------------------------------
-- upload_sessions — durable direct-upload sessions (no credentials)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS upload_sessions (
  id              TEXT PRIMARY KEY CHECK (id ~ '^upld_[a-f0-9]{16}$'),
  workspace_id    TEXT NOT NULL,
  drive_id        TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  principal_type  TEXT NOT NULL CHECK (principal_type IN ('agent', 'user')),
  principal_id    TEXT NOT NULL,

  -- Strict target discriminator: invalid combinations are unrepresentable.
  target_kind                TEXT NOT NULL CHECK (target_kind IN ('artifact', 'version')),
  parent_folder_id           TEXT,
  artifact_name              TEXT,
  artifact_id                TEXT,
  expected_artifact_revision TEXT
    CHECK (expected_artifact_revision IS NULL
           OR expected_artifact_revision ~ '^rev_[a-f0-9]{16}$'),
  CONSTRAINT upload_sessions_target_shape CHECK (
    (target_kind = 'artifact'
       AND parent_folder_id IS NOT NULL AND artifact_name IS NOT NULL
       AND artifact_id IS NULL AND expected_artifact_revision IS NULL)
    OR
    (target_kind = 'version'
       AND artifact_id IS NOT NULL AND expected_artifact_revision IS NOT NULL
       AND parent_folder_id IS NULL AND artifact_name IS NULL)
  ),

  -- Declared content + server-selected object coordinates. The CRC32C is
  -- canonical padded RFC 4648 base64 of exactly four bytes; the server
  -- byte-decodes and re-encodes before storage, the CHECK is the backstop.
  declared_size_bytes BIGINT NOT NULL CHECK (declared_size_bytes >= 0),
  declared_media_type TEXT NOT NULL CHECK (declared_media_type <> ''),
  declared_crc32c     TEXT NOT NULL CHECK (declared_crc32c ~ '^[A-Za-z0-9+/]{6}==$'),
  adoption_marker     TEXT NOT NULL CHECK (adoption_marker <> ''),
  scratch_object      TEXT NOT NULL CHECK (scratch_object <> ''),
  final_object        TEXT NOT NULL CHECK (final_object <> ''),
  expires_at          TIMESTAMPTZ NOT NULL,

  -- Publication and cleanup are DISTINCT state machines (§6): cleanup never
  -- changes a terminal publication outcome. Both enumerations fail closed —
  -- the decoder and these CHECKs reject any other value.
  state TEXT NOT NULL DEFAULT 'preparing'
    CHECK (state IN ('preparing', 'active', 'completing', 'cancelling',
                     'completed', 'cancelled', 'expired', 'rejected')),
  cleanup_state TEXT NOT NULL DEFAULT 'none'
    CHECK (cleanup_state IN ('none', 'pending', 'quarantined', 'deleting',
                             'cleaned', 'blocked')),
  session_revision BIGINT NOT NULL DEFAULT 1 CHECK (session_revision >= 1),

  -- One transition owner at a time: the fence is a durable action + bounded
  -- lease; a lease-aware reconciler resumes the SAME action.
  transition_action           TEXT CHECK (transition_action IN ('initiate', 'complete', 'cancel')),
  transition_lease_id         TEXT,
  transition_lease_expires_at TIMESTAMPTZ,
  CONSTRAINT upload_sessions_lease_shape CHECK (
    ((transition_action IS NULL) = (transition_lease_id IS NULL))
    AND ((transition_action IS NULL) = (transition_lease_expires_at IS NULL))
  ),

  -- Begin-saga crash discipline: provider_attempted_at commits BEFORE the
  -- one outbound initiation; once set, this session never initiates again.
  provider_attempted_at TIMESTAMPTZ,
  target_disclosed      BOOLEAN NOT NULL DEFAULT false,

  -- Object observations (non-secret coordinates only; never a URI).
  observed_scratch_generation BIGINT
    CHECK (observed_scratch_generation IS NULL OR observed_scratch_generation > 0),
  observed_scratch_size BIGINT
    CHECK (observed_scratch_size IS NULL OR observed_scratch_size >= 0),
  observed_scratch_crc32c TEXT
    CHECK (observed_scratch_crc32c IS NULL
           OR observed_scratch_crc32c ~ '^[A-Za-z0-9+/]{6}==$'),
  adopted_generation BIGINT
    CHECK (adopted_generation IS NULL OR adopted_generation > 0),
  -- The durable adoption PROOF is the full observed identity (§6): type,
  -- size, CRC32C, and generation together — a bare generation is not proof
  -- and is unrepresentable (packet-1 correction, blocker 7).
  adopted_size BIGINT
    CHECK (adopted_size IS NULL OR adopted_size >= 0),
  adopted_crc32c TEXT
    CHECK (adopted_crc32c IS NULL OR adopted_crc32c ~ '^[A-Za-z0-9+/]{6}==$'),
  adopted_content_type TEXT
    CHECK (adopted_content_type IS NULL OR adopted_content_type <> ''),
  CONSTRAINT upload_sessions_adopted_proof_shape CHECK (
    ((adopted_generation IS NULL) = (adopted_size IS NULL))
    AND ((adopted_generation IS NULL) = (adopted_crc32c IS NULL))
    AND ((adopted_generation IS NULL) = (adopted_content_type IS NULL))
  ),
  -- Server-only rewrite recovery data. Not a browser bearer; never exposed
  -- on any wire representation or log.
  rewrite_continuation TEXT,

  -- Safe terminal failure classification + the one durable public result.
  failure_code       TEXT,
  result_artifact_id TEXT CHECK (result_artifact_id IS NULL OR result_artifact_id ~ '^art_[a-f0-9]{16}$'),
  result_version_id  TEXT CHECK (result_version_id IS NULL OR result_version_id ~ '^ver_[a-f0-9]{16}$'),
  result_revision    TEXT CHECK (result_revision IS NULL OR result_revision ~ '^rev_[a-f0-9]{16}$'),
  CONSTRAINT upload_sessions_completed_has_result CHECK (
    (state = 'completed') = (result_artifact_id IS NOT NULL
                             AND result_version_id IS NOT NULL
                             AND result_revision IS NOT NULL
                             AND adopted_generation IS NOT NULL)
  ),
  CONSTRAINT upload_sessions_failure_only_when_failed CHECK (
    failure_code IS NULL OR state IN ('rejected', 'expired')
  ),

  cleanup_next_attempt_at TIMESTAMPTZ,
  cleanup_attempts        INTEGER NOT NULL DEFAULT 0 CHECK (cleanup_attempts >= 0),
  cleanup_failure_class   TEXT,
  terminal_at             TIMESTAMPTZ,
  retention_until         TIMESTAMPTZ,
  CONSTRAINT upload_sessions_terminal_shape CHECK (
    (state IN ('completed', 'cancelled', 'expired', 'rejected'))
    = (terminal_at IS NOT NULL)
  ),

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS upload_sessions_live
  ON upload_sessions (drive_id)
  WHERE state IN ('preparing', 'active', 'completing', 'cancelling');
CREATE INDEX IF NOT EXISTS upload_sessions_deadline
  ON upload_sessions (expires_at)
  WHERE state IN ('preparing', 'active', 'completing', 'cancelling');
CREATE INDEX IF NOT EXISTS upload_sessions_cleanup_due
  ON upload_sessions (cleanup_next_attempt_at)
  WHERE cleanup_state IN ('pending', 'quarantined', 'deleting', 'blocked');

-- ---------------------------------------------------------------------------
-- storage_reservations — promised logical bytes, exactly-once release
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS storage_reservations (
  id           TEXT PRIMARY KEY CHECK (id ~ '^rsv_[a-f0-9]{16}$'),
  workspace_id TEXT NOT NULL,
  drive_id     TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  principal_id TEXT NOT NULL,
  -- NULL for inline/copy/restore producers whose reservation lives only for
  -- the duration of their own transaction; set for direct-upload sessions.
  upload_id    TEXT REFERENCES upload_sessions(id) ON DELETE SET NULL,
  size_bytes   BIGINT NOT NULL CHECK (size_bytes >= 0),
  acquired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- The exactly-once release: every terminal path runs the same conditional
  -- `released_at IS NULL` update; only one succeeds.
  released_at  TIMESTAMPTZ,
  release_kind TEXT CHECK (release_kind IN ('converted', 'released')),
  CONSTRAINT storage_reservations_release_shape
    CHECK ((released_at IS NULL) = (release_kind IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS storage_reservations_one_live_per_upload
  ON storage_reservations (upload_id)
  WHERE released_at IS NULL AND upload_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS storage_reservations_live
  ON storage_reservations (workspace_id) WHERE released_at IS NULL;

-- ---------------------------------------------------------------------------
-- workspace_storage — additive workspace committed/reserved accounting row
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workspace_storage (
  workspace_id    TEXT PRIMARY KEY,
  committed_bytes BIGINT NOT NULL DEFAULT 0 CHECK (committed_bytes >= 0),
  reserved_bytes  BIGINT NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Backfill: drives.storage_bytes becomes the authoritative per-drive
-- committed logical counter, and workspace_storage is seeded from it.
-- Migration-only — fresh databases start at zero with zero rows, so the
-- baseline carries no data statements.
-- ---------------------------------------------------------------------------

UPDATE drives d SET storage_bytes = COALESCE(
  (SELECT sum(v.size_bytes)
     FROM artifact_versions v
     JOIN artifacts a ON a.id = v.artifact_id
    WHERE a.drive_id = d.id),
  0);

INSERT INTO workspace_storage (workspace_id, committed_bytes)
SELECT workspace_id, COALESCE(sum(storage_bytes), 0)
  FROM drives
 GROUP BY workspace_id
ON CONFLICT (workspace_id)
DO UPDATE SET committed_bytes = EXCLUDED.committed_bytes, updated_at = now();
