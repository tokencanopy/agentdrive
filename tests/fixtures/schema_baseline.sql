-- AgentDrive v0 — the day-0 schema.
--
-- Nine tables, derived from the 39 operations of the ratified v0 contract
-- rather than pruned from the 52-table legacy baseline. Written on a blank
-- file deliberately: the legacy schema had `path` NOT NULL and `parent_id`
-- nullable, because the v0 tree was added *beside* the path-addressed
-- router. A pruned file would very plausibly have kept that inversion, and
-- it would have looked deliberate.
--
-- This file is the COMPLETE shape of a fresh database. There is no migration
-- chain behind it: the v0 chain starts at 0001 against this baseline.
-- `agentdrive.scripts.apply_schema` applies it in one transaction.
--
-- Contract: docs/superpowers/specs/2026-07-30-agentdrive-v0-api-contract-design.md
-- Section references below are to that document. §12A names, for every
-- invariant, the layer that enforces it and the artifact that proves it;
-- every constraint here is one of those artifacts.
--
-- Ownership (§3.1). Hub owns principals, workspaces, memberships, product
-- entitlement, OAuth credentials, and token issuance. AgentDrive owns
-- drives, namespaces, versions and bytes, local grants and shares, search
-- and usage, and the change feed. Hub ids appear here only as opaque
-- references (`tcagt_*`, `tcusr_*`, a workspace id) — never as a local copy
-- of a Hub row, because two planes holding the same fact is how they come
-- to disagree.


-- ---------------------------------------------------------------------------
-- drives — the storage and authorization boundary (§4, §6.1)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS drives (
  id              TEXT PRIMARY KEY
                    CHECK (id ~ '^drv_[a-f0-9]{16}$'),

  -- The Hub workspace this drive belongs to. An opaque reference: AgentDrive
  -- never reads workspace membership from a local table, it intersects the
  -- token's scope with a local grant (§3.1, §7.1).
  workspace_id    TEXT NOT NULL,

  name            TEXT NOT NULL,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- Every user-visible mutation produces a new revision, and `ETag` is the
  -- quoted current revision (§4.1). Not a transaction id, timestamp, or GCS
  -- generation — those leak server state into a client-visible identifier.
  revision        TEXT NOT NULL
                    CHECK (revision ~ '^rev_[a-f0-9]{16}$'),

  -- A drive exposes one root-folder id (§4.2). The FK is added after
  -- `folders` exists and is DEFERRABLE, so a drive and its root folder are
  -- creatable in one transaction.
  root_folder_id  TEXT,

  -- §6.1 `GET /drives/{id}/usage`. Non-negative CHECKs per §12A: a negative
  -- counter is unreachable by correct code, which is exactly why it wants a
  -- constraint — it is the signature of a double-decrement, and without this
  -- it surfaces as a wrong number rather than an error.
  storage_bytes   BIGINT NOT NULL DEFAULT 0 CHECK (storage_bytes >= 0),
  retrieval_bytes BIGINT NOT NULL DEFAULT 0 CHECK (retrieval_bytes >= 0),

  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS drives_by_workspace ON drives (workspace_id) WHERE deleted_at IS NULL;


-- ---------------------------------------------------------------------------
-- folders — mutable tree nodes (§4.2, §6.2)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS folders (
  id                TEXT PRIMARY KEY
                      CHECK (id ~ '^fld_[a-f0-9]{16}$'),

  drive_id          TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,

  -- NULL only for the drive's structural root, pinned to exactly one row per
  -- drive by `folders_one_root` below. Every other folder has one parent
  -- (§4.2); paths are derived from this chain and never stored.
  parent_id         TEXT,

  -- A single path segment, never a mutable full path (§4.2). NULL exactly
  -- when this is the root, which has no segment to name.
  name              TEXT,

  -- §6.8: `sealed` stops grant inheritance at this node.
  grant_inheritance TEXT NOT NULL DEFAULT 'inherit'
                      CHECK (grant_inheritance IN ('inherit', 'sealed')),

  metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
  revision          TEXT NOT NULL
                      CHECK (revision ~ '^rev_[a-f0-9]{16}$'),

  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ,

  CONSTRAINT folders_root_has_no_name
    CHECK ((parent_id IS NULL) = (name IS NULL)),

  -- Referenceable as a composite so children can be pinned to one drive.
  CONSTRAINT folders_drive_id_key UNIQUE (drive_id, id)
);

-- §4: a parent reference cannot cross a drive boundary. A plain FK on
-- `parent_id` alone would let a folder in drive A parent a node in drive B —
-- a state no operation can undo, because every repair path is itself
-- drive-scoped. Carrying `drive_id` into the FK makes it unrepresentable.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'folders_parent_same_drive') THEN
    ALTER TABLE folders
      ADD CONSTRAINT folders_parent_same_drive
      FOREIGN KEY (drive_id, parent_id) REFERENCES folders (drive_id, id)
      ON DELETE RESTRICT;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'drives_root_folder_fk') THEN
    ALTER TABLE drives
      ADD CONSTRAINT drives_root_folder_fk
      FOREIGN KEY (root_folder_id) REFERENCES folders (id)
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

-- §6.2: the structural root is the only parent-less folder in a drive.
CREATE UNIQUE INDEX IF NOT EXISTS folders_one_root ON folders (drive_id) WHERE parent_id IS NULL;

-- §6.2: sibling artifact and folder names share ONE collision domain. This
-- is half of that guarantee — Postgres has no cross-table unique index, so
-- each table is pinned here and the folder-vs-artifact exclusion is held by
-- the Layer 5 mutation transaction plus an invariant test (§12A).
CREATE UNIQUE INDEX IF NOT EXISTS folders_namespace ON folders (parent_id, name)
  WHERE deleted_at IS NULL;


-- ---------------------------------------------------------------------------
-- artifacts — mutable identities with a head version (§4, §6.3)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS artifacts (
  id              TEXT PRIMARY KEY
                    CHECK (id ~ '^art_[a-f0-9]{16}$'),

  drive_id        TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,

  -- Mandatory, unlike folders: an artifact is never the root of anything.
  -- The legacy schema had this nullable and `path` NOT NULL, which is the
  -- inversion this file exists to correct.
  parent_id       TEXT NOT NULL,
  name            TEXT NOT NULL,

  -- Denormalized from the head version for listing and filtering (§6.11).
  -- Authoritative content facts live on the version row.
  content_type    TEXT,
  content_preview TEXT,
  labels          TEXT[] NOT NULL DEFAULT '{}',

  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- Set after the first version exists; the composite FK below pins it to a
  -- version of THIS artifact.
  head_version_id TEXT,

  revision        TEXT NOT NULL
                    CHECK (revision ~ '^rev_[a-f0-9]{16}$'),

  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at      TIMESTAMPTZ,

  -- §6.6 and blocker B3. A trigger- or handler-maintained tsvector goes
  -- stale the first time a writer forgets, and the legacy surface proved
  -- that happens. GENERATED cannot: Postgres recomputes it on every write,
  -- including from a code path nobody has written yet. The cost is that only
  -- same-row columns are visible — which is why ancestor path is not indexed
  -- here, and why search is name/preview/metadata/label scoped by design
  -- rather than by omission.
  --
  -- Every arm must be IMMUTABLE or Postgres rejects the column outright.
  -- `array_to_string` is only STABLE (it calls the element type's output
  -- function), so labels go through `array_to_tsvector` instead — which is
  -- also the semantically correct tool: a label is an exact token to match,
  -- not prose to stem. `apple-pie` stays one term rather than becoming
  -- `appl` + `pie`.
  search_tsv      tsvector GENERATED ALWAYS AS (
                       setweight(to_tsvector('english'::regconfig,
                         regexp_replace(name, '[._-]+', ' ', 'g')), 'A')
                    || setweight(to_tsvector('english'::regconfig,
                         coalesce(content_preview, '')), 'B')
                    || setweight(jsonb_to_tsvector('english'::regconfig,
                         coalesce(metadata, '{}'::jsonb), '["string"]'::jsonb), 'C')
                    || setweight(array_to_tsvector(labels), 'D')
                  ) STORED,

  CONSTRAINT artifacts_drive_id_key UNIQUE (drive_id, id)
);

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'artifacts_parent_same_drive') THEN
    ALTER TABLE artifacts
      ADD CONSTRAINT artifacts_parent_same_drive
      FOREIGN KEY (drive_id, parent_id) REFERENCES folders (drive_id, id)
      ON DELETE RESTRICT;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS artifacts_namespace ON artifacts (parent_id, name)
  WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS artifacts_search ON artifacts USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS artifacts_by_parent ON artifacts (parent_id, id)
  WHERE deleted_at IS NULL;


-- ---------------------------------------------------------------------------
-- artifact_versions — immutable bytes (§4.3, §6.4)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS artifact_versions (
  id                TEXT PRIMARY KEY
                      CHECK (id ~ '^ver_[a-f0-9]{16}$'),

  artifact_id       TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,

  -- Stored so linear history can become a DAG later without redefining
  -- artifact identity (§4.3). v0 exposes it as linear.
  parent_version_id TEXT REFERENCES artifact_versions(id) ON DELETE SET NULL,

  checksum          TEXT NOT NULL,
  content_type      TEXT NOT NULL,
  size_bytes        BIGINT NOT NULL CHECK (size_bytes >= 0),

  -- Object-store key. The bytes themselves never live in Postgres; `GET
  -- /content` is normally a 307 to the object store (§6.3).
  storage_object    TEXT NOT NULL,

  -- Server-observed attribution (§4). The authenticated principal, never a
  -- client-supplied claim, and never a generic worker standing in for the
  -- initiator (§6.7).
  actor_type        TEXT NOT NULL CHECK (actor_type IN ('agent', 'user', 'system')),
  actor_id          TEXT,

  -- Informational only. The version id is the stable handle; numeric version
  -- route parameters are removed (§6.4).
  ordinal           INTEGER NOT NULL CHECK (ordinal >= 1),

  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Lets `artifacts.head_version_id` be pinned to a version of its own row.
  CONSTRAINT artifact_versions_artifact_id_key UNIQUE (artifact_id, id),
  CONSTRAINT artifact_versions_ordinal_key UNIQUE (artifact_id, ordinal)
);

-- §6.4 via §12A: `head_version_id` belongs to the same artifact. A bare FK on
-- the id alone would let an artifact point its head at another artifact's
-- version, and every read of it would be silently wrong. DEFERRABLE because
-- artifact and first version are created in one transaction, each
-- referencing the other.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'artifacts_head_version_is_own') THEN
    ALTER TABLE artifacts
      ADD CONSTRAINT artifacts_head_version_is_own
      FOREIGN KEY (id, head_version_id)
      REFERENCES artifact_versions (artifact_id, id)
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS artifact_versions_by_artifact
  ON artifact_versions (artifact_id, ordinal DESC);

-- §6.4 via §12A: versions are immutable. A CHECK cannot express "no UPDATE
-- ever" — it only sees the row being written — so this is a trigger, which
-- also binds the table owner. Without it, immutability is a property of the
-- handlers that happen to exist today rather than of the data.
CREATE OR REPLACE FUNCTION reject_artifact_version_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION
    'artifact_versions is append-only: version % cannot be updated', OLD.id
    USING ERRCODE = 'restrict_violation';
END;
$$;

DROP TRIGGER IF EXISTS artifact_versions_immutable ON artifact_versions;
CREATE TRIGGER artifact_versions_immutable
  BEFORE UPDATE ON artifact_versions
  FOR EACH ROW
  EXECUTE FUNCTION reject_artifact_version_update();


-- ---------------------------------------------------------------------------
-- grants — local capability, intersected with token scope (§6.8, §7.1)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS grants (
  id             TEXT PRIMARY KEY
                   CHECK (id ~ '^grn_[a-f0-9]{16}$'),

  drive_id       TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,

  resource_type  TEXT NOT NULL
                   CHECK (resource_type IN ('drive', 'folder', 'artifact')),
  resource_id    TEXT NOT NULL,

  -- Explicit, stable references only (§6.8). No email address, display name,
  -- or mutable path is a principal — each of those is a value that can be
  -- reassigned to a different human, which would silently transfer access.
  principal_type TEXT NOT NULL
                   CHECK (principal_type IN ('agent', 'user', 'workspace', 'public')),
  principal_id   TEXT,

  role           TEXT NOT NULL
                   CHECK (role IN ('manager', 'editor', 'viewer')),

  expires_at     TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at     TIMESTAMPTZ,

  -- §6.8: `public` is the publication mechanism and carries no id. Every
  -- other principal must name one.
  CONSTRAINT grants_public_has_no_id
    CHECK ((principal_type = 'public') = (principal_id IS NULL)),

  -- §6.8 via §12A: public, only with viewer. A public grant above viewer is
  -- an anonymous write, so the schema refuses to represent one rather than
  -- trusting every future handler to check.
  CONSTRAINT grants_public_is_viewer_only
    CHECK (principal_type <> 'public' OR role = 'viewer'),

  -- Hub id shapes (§6.8). A workspace id is opaque and unprefixed.
  CONSTRAINT grants_principal_id_shape CHECK (
       (principal_type = 'agent'     AND principal_id ~ '^tcagt_')
    OR (principal_type = 'user'      AND principal_id ~ '^tcusr_')
    OR (principal_type = 'workspace' AND principal_id IS NOT NULL)
    OR (principal_type = 'public'    AND principal_id IS NULL)
  )
);

-- One live grant per (resource, principal); role changes are PATCH, not a
-- second row (§6.8).
CREATE UNIQUE INDEX IF NOT EXISTS grants_one_live_per_principal
  ON grants (resource_type, resource_id, principal_type, coalesce(principal_id, ''))
  WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS grants_by_resource ON grants (resource_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS grants_by_drive ON grants (drive_id) WHERE revoked_at IS NULL;


-- ---------------------------------------------------------------------------
-- shares — expiring read-only bearer links (§6.9)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS shares (
  id            TEXT PRIMARY KEY
                  CHECK (id ~ '^shr_[a-f0-9]{16}$'),

  drive_id      TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,

  -- §6.9 distinguishes an immutable version snapshot from a live artifact or
  -- folder, because sharing a live artifact exposes edits made after the fact.
  resource_type TEXT NOT NULL
                  CHECK (resource_type IN ('artifact', 'artifact_version', 'folder')),
  resource_id   TEXT NOT NULL,

  -- The secret is returned once at creation or rotation and stored only as a
  -- hash (§6.9). There is deliberately no column that could hold the secret
  -- itself — a nullable plaintext column is an invitation to populate it.
  secret_hash   TEXT NOT NULL,

  expires_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  rotated_at    TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS shares_secret_hash ON shares (secret_hash);
CREATE INDEX IF NOT EXISTS shares_by_drive ON shares (drive_id) WHERE revoked_at IS NULL;


-- ---------------------------------------------------------------------------
-- idempotency_records — replay of executed mutations (§7.2)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS idempotency_records (
  id              TEXT PRIMARY KEY
                    CHECK (id ~ '^idem_[a-f0-9]{16}$'),

  -- Scoped to the principal: one agent's key must never replay another's
  -- result. The composite unique below is the real key.
  principal_id    TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,

  -- A repeated key with the same principal, method, path and request hash
  -- returns the original result; reusing it for a different request is
  -- 409 IDEMPOTENCY_CONFLICT (§7.2). Storing all three is what lets the
  -- handler tell those two cases apart.
  method          TEXT NOT NULL,
  path            TEXT NOT NULL,
  request_hash    TEXT NOT NULL,

  response_status  INTEGER NOT NULL CHECK (response_status BETWEEN 100 AND 599),
  response_headers JSONB NOT NULL DEFAULT '{}'::jsonb,
  response_body    JSONB,

  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,

  CONSTRAINT idempotency_records_principal_key UNIQUE (principal_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idempotency_records_expiry ON idempotency_records (expires_at);


-- ---------------------------------------------------------------------------
-- drive_changes — the per-drive change feed (§6.7, D14)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS drive_changes (
  id                TEXT PRIMARY KEY
                      CHECK (id ~ '^chg_[a-f0-9]{16}$'),

  drive_id          TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,

  -- Dense per-drive ordering. §6.7 guarantees total order within one drive,
  -- explicitly not globally — a global sequence would serialize every
  -- drive's writes against every other's.
  sequence          BIGINT NOT NULL CHECK (sequence >= 1),

  -- Recursive operations produce multiple changes sharing one set id (§6.7).
  change_set_id     TEXT NOT NULL,

  type              TEXT NOT NULL,

  -- §6.7: an authenticated change actor is a Hub agent or user. Workspace and
  -- `public` grant principals never act. `system` is reserved for
  -- server-initiated maintenance, which uses an explicit actor and a new
  -- event type rather than impersonating a human or agent.
  actor_type        TEXT NOT NULL CHECK (actor_type IN ('agent', 'user', 'system')),
  actor_id          TEXT,

  resource_type     TEXT NOT NULL
                      CHECK (resource_type IN ('drive', 'folder', 'artifact')),
  resource_id       TEXT NOT NULL,

  previous_revision TEXT,
  revision          TEXT,

  data              JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT drive_changes_sequence_key UNIQUE (drive_id, sequence)
);

CREATE INDEX IF NOT EXISTS drive_changes_by_set ON drive_changes (change_set_id);


-- §6.7 + D14: the dense sequence head and the retention floor. There is
-- deliberately NO per-client cursor table: the reader carries its position in
-- a sealed token, so re-presenting a cursor re-delivers the same page. A
-- server-side position that advances at read time yields at-MOST-once
-- delivery, and the dropped page becomes unreachable — which is blocker B2,
-- made unrepresentable here rather than fixed in a handler.
CREATE TABLE IF NOT EXISTS drive_change_heads (
  drive_id               TEXT PRIMARY KEY REFERENCES drives(id) ON DELETE CASCADE,
  last_sequence          BIGINT NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
  retained_from_sequence BIGINT NOT NULL DEFAULT 1 CHECK (retained_from_sequence >= 1),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- The floor may sit one past the head (an empty, fully-trimmed feed) but
  -- never beyond it.
  CONSTRAINT drive_change_heads_floor_within_head
    CHECK (retained_from_sequence <= last_sequence + 1)
);
