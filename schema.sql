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

  -- Immutable server-observed attribution (§4.1): the authenticated principal
  -- that created the drive, never a client-supplied claim. Stored once at
  -- creation and never mutated — the durable "who created this drive"
  -- record. The creator is ALSO the first drive-level `manager` grant
  -- (grants row), but this column answers the question without a join and
  -- survives any later grant changes (revocation/rotation) unchanged.
  created_by_principal_id TEXT,

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

  metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
  revision          TEXT NOT NULL
                      CHECK (revision ~ '^rev_[a-f0-9]{16}$'),

  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ,

  -- Recursive-deletion cohort (§6.2 restore semantics): every row a
  -- recursive folder delete removes is stamped with ONE shared cohort id, so
  -- a restore resurrects exactly that deletion cohort and never sweeps up
  -- rows deleted earlier for unrelated reasons. NULL on a deleted row = the
  -- deletion predates cohort tracking and is not restorable.
  deleted_cohort_id  TEXT,

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

-- Composite, for the same reason `folders_parent_same_drive` is: §12A puts
-- "parent AND ROOT references stay inside one drive" at the DB-constraint
-- layer, and a bare FK on `root_folder_id` alone let a drive root itself at a
-- folder belonging to another drive. No drive-scoped operation can repair
-- that, because every repair path is itself drive-scoped.
--
-- MATCH SIMPLE short-circuits when `root_folder_id` IS NULL, so the
-- create-drive-then-create-root flow still works, and DEFERRABLE still lets
-- both rows land in one transaction.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'drives_root_folder_fk') THEN
    ALTER TABLE drives
      ADD CONSTRAINT drives_root_folder_fk
      FOREIGN KEY (id, root_folder_id) REFERENCES folders (drive_id, id)
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

-- Replay-path backfill: a non-fresh database created `drives` before the
-- `created_by_principal_id` column existed. The table definition above does
-- not add it to an existing table, so this idempotent ADD closes the gap.
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'drives' AND column_name = 'created_by_principal_id'
  ) THEN
    ALTER TABLE drives ADD COLUMN created_by_principal_id TEXT;
  END IF;
END $$;

-- §6.2: the structural root is the only parent-less folder in a drive.
CREATE UNIQUE INDEX IF NOT EXISTS folders_one_root ON folders (drive_id) WHERE parent_id IS NULL;

-- §6.2: sibling artifact and folder names share ONE collision domain.
--
-- Postgres has no cross-table unique index, so this is enforced in two
-- pieces, and it is worth being exact about which piece holds what because
-- §12A assigns the whole invariant to the DB-constraint layer:
--
--   * WITHIN a table, the partial unique index below is a real DB constraint
--     and cannot be bypassed.
--   * ACROSS the two tables, `reject_cross_kind_name_collision` (defined
--     after `artifacts`) is a trigger — §12A's layer 2. It rejects every
--     sequential violation from any writer, including writers nobody has
--     written yet, which is strictly more than the comment here used to
--     claim while nothing enforced it at all.
--
-- What the trigger does NOT close is the concurrent case: under READ
-- COMMITTED two transactions inserting the same name, one per table, cannot
-- see each other's uncommitted row, so both pass and both commit. Closing
-- that needs the Layer 5 mutation transaction to serialize on the parent
-- (an advisory lock keyed on parent_id), which is where the contract's
-- §12A "app transaction" fallback genuinely applies.
--
-- Recorded rather than glossed: this is a partial demotion from §12A's
-- stated layer, and §12A requires such a move to be deliberate.
CREATE UNIQUE INDEX IF NOT EXISTS folders_namespace ON folders (parent_id, name)
  WHERE deleted_at IS NULL;


-- ---------------------------------------------------------------------------
-- Search helpers
-- ---------------------------------------------------------------------------

-- Joins a label array so `to_tsvector` can stem it.
--
-- This exists only to satisfy a volatility constraint. A generated column's
-- expression must be IMMUTABLE, and `array_to_string` is merely STABLE — it
-- calls the element type's output function, which for some types (dates,
-- floats) varies with session settings like DateStyle. Postgres marks it
-- conservatively for ALL types rather than per-type.
--
-- Declaring IMMUTABLE over a STABLE call is normally how you corrupt an
-- index. It is sound HERE, and only here, because the argument is pinned to
-- `text[]`: text's output function is the identity and reads no session
-- state, so the result genuinely depends on nothing but the input.
--
-- Two rules follow, and both matter:
--   * Do not widen the signature to `anyarray`. That reintroduces exactly
--     the type-dependent output the pin rules out.
--   * Do not CREATE OR REPLACE this with different behavior. Stored
--     generated values are NOT recomputed on redefinition, so old rows
--     would keep the old tokenization while new rows got the new one — a
--     silently half-migrated index. Changing it means dropping and
--     re-adding `artifacts.search_tsv`, which rewrites the table.
CREATE OR REPLACE FUNCTION labels_text(labels text[])
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
STRICT
AS $$ SELECT array_to_string(labels, ' ') $$;


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

  -- Recursive-deletion cohort: see the note on `folders.deleted_cohort_id`.
  -- A recursive folder delete stamps its artifacts with the SAME cohort id as
  -- the folders it removes; an individual artifact soft-delete stamps its own
  -- cohort of one. Restore clears both `deleted_at` and the cohort.
  deleted_cohort_id  TEXT,

  -- §6.6 and blocker B3. A trigger- or handler-maintained tsvector goes
  -- stale the first time a writer forgets, and the legacy surface proved
  -- that happens. GENERATED cannot: Postgres recomputes it on every write,
  -- including from a code path nobody has written yet. The cost is that only
  -- same-row columns are visible — which is why ancestor path is not indexed
  -- here, and why search is name/preview/metadata/label scoped by design
  -- rather than by omission.
  --
  -- Every arm must be IMMUTABLE or Postgres rejects the column outright,
  -- which is why the label arm goes through `labels_text` — see the note on
  -- that function. Labels are stemmed like everything else, so a search for
  -- `quarter` finds an artifact labelled `quarterly`; exact tokens would
  -- make a label findable only by typing it in full.
  --
  -- B3 (finding 2 in schema-integrity): the values feeding these arms are
  -- unbounded (name/preview/labels carry no length CHECK; metadata is
  -- unbounded JSONB, and the v0 inline-body ceiling is 20 MiB), but a
  -- tsvector is capped at 1 MiB. Left unbounded, a legal-sized row makes
  -- every INSERT/UPDATE fail at the storage layer with
  -- "string is too long for tsvector". Each arm therefore truncates its
  -- input with `left(...)`. The caps are generous — a search index that
  -- covers the first N bytes of an oversized document is far more useful
  -- than a write that hard-fails — while their total (≈180 KB exercise
  -- worst case, well under the 1 MiB limit even after lexeme/position
  -- expansion) keeps the generated output from ever tripping the guard.
  -- Indexing metadata as text (not jsonb_to_tsvector) is deliberate:
  -- `left(metadata::text, N)::jsonb` could truncate mid-token into invalid
  -- JSON and re-introduce the very write-blocker being removed.
  search_tsv      tsvector GENERATED ALWAYS AS (
                       setweight(to_tsvector('english'::regconfig,
                         left(regexp_replace(name, '[._-]+', ' ', 'g'), 4096)), 'A')
                    || setweight(to_tsvector('english'::regconfig,
                         left(coalesce(content_preview, ''), 65536)), 'B')
                    || setweight(to_tsvector('english'::regconfig,
                         left(coalesce(metadata::text, '{}'), 98304)), 'C')
                    || setweight(to_tsvector('english'::regconfig,
                         left(labels_text(labels), 16384)), 'D')
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


-- §6.2 across the two tables. See the note on `folders_namespace` for what
-- this does and does not guarantee.
--
-- `pg_trigger_depth() > 1` is not used: this trigger writes nothing, so it
-- cannot recurse. The lookup is a single indexed probe on the other table's
-- namespace index, on a path that is already doing a unique-index insert.
CREATE OR REPLACE FUNCTION reject_cross_kind_name_collision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  clash TEXT;
BEGIN
  IF NEW.deleted_at IS NOT NULL OR NEW.name IS NULL THEN
    RETURN NEW;
  END IF;

  IF TG_TABLE_NAME = 'folders' THEN
    SELECT id INTO clash FROM artifacts
     WHERE parent_id = NEW.parent_id AND name = NEW.name AND deleted_at IS NULL
     LIMIT 1;
  ELSE
    SELECT id INTO clash FROM folders
     WHERE parent_id = NEW.parent_id AND name = NEW.name AND deleted_at IS NULL
     LIMIT 1;
  END IF;

  IF clash IS NOT NULL THEN
    RAISE EXCEPTION
      'name % already exists under % (as %)', NEW.name, NEW.parent_id, clash
      USING ERRCODE = 'unique_violation';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS folders_namespace_cross_kind ON folders;
CREATE TRIGGER folders_namespace_cross_kind
  BEFORE INSERT OR UPDATE OF parent_id, name, deleted_at ON folders
  FOR EACH ROW EXECUTE FUNCTION reject_cross_kind_name_collision();

DROP TRIGGER IF EXISTS artifacts_namespace_cross_kind ON artifacts;
CREATE TRIGGER artifacts_namespace_cross_kind
  BEFORE INSERT OR UPDATE OF parent_id, name, deleted_at ON artifacts
  FOR EACH ROW EXECUTE FUNCTION reject_cross_kind_name_collision();


-- ---------------------------------------------------------------------------
-- artifact_versions — immutable bytes (§4.3, §6.4)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS artifact_versions (
  id                TEXT PRIMARY KEY
                      CHECK (id ~ '^ver_[a-f0-9]{16}$'),

  artifact_id       TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,

  -- Stored so linear history can become a DAG later without redefining
  -- artifact identity (§4.3). v0 exposes it as linear. The FK is a composite
  -- (artifact_id, parent_version_id) constraint added below, not a bare
  -- self-reference, so a parent must be a version of the *same* artifact.
  parent_version_id TEXT,

  checksum          TEXT NOT NULL,
  content_type      TEXT NOT NULL,
  size_bytes        BIGINT NOT NULL CHECK (size_bytes >= 0),

  -- Object-store key. The bytes themselves never live in Postgres; `GET
  -- /content` is normally a 307 to the object store (§6.3).
  storage_object    TEXT NOT NULL,

  -- B3 direct-transfer coordinates (migration 0049): the object's bucket and
  -- exact GCS generation. Nullable during reconciliation — the guarded
  -- reconcile_generations job resolves legacy CAS rows NULL → observed value;
  -- new writes persist both at commit. Never fabricated. The immutability
  -- trigger below permits ONLY that one NULL → value transition.
  storage_bucket     TEXT,
  storage_generation BIGINT
    CONSTRAINT artifact_versions_generation_positive
      CHECK (storage_generation IS NULL OR storage_generation > 0),
  -- Coordinates are all-or-none (nonempty bucket): "resolved" is one fact.
  -- Where this version came from (migration 0053). Denormalised and
  -- FK-free on purpose: `sheet_sessions` rows are swept a day after they
  -- terminate, which is exactly when the provenance becomes worth keeping,
  -- so the id is opaque and may no longer resolve while the message
  -- outlives it. Both are frozen by the append-only trigger below.
  origin_session_id TEXT,
  origin_message    TEXT,

  CONSTRAINT artifact_versions_coordinates_all_or_none
    CHECK (
      ((storage_bucket IS NULL) = (storage_generation IS NULL))
      AND (storage_bucket IS NULL OR storage_bucket <> '')
    ),

  -- Server-observed attribution (§4). The authenticated principal, never a
  -- client-supplied claim, and never a generic worker standing in for the
  -- initiator (§6.7).
  actor_type        TEXT NOT NULL
                      CHECK (actor_type IN ('agent', 'user', 'service', 'system')),
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

-- §4.3 via §12A: `parent_version_id` belongs to the same artifact. Mirrors
-- `artifacts_head_version_is_own` above — a bare FK on the id alone would let
-- one artifact's version claim another artifact's version as its parent, a
-- cross-artifact history edge that every version-walk would silently follow.
-- ON DELETE SET NULL uses the PG15+ column-list form so only
-- `parent_version_id` is nulled: a bare `SET NULL` would also try to null
-- `artifact_id`, which is NOT NULL and part of the (artifact_id, id) key.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'artifact_versions_parent_is_own') THEN
    ALTER TABLE artifact_versions
      ADD CONSTRAINT artifact_versions_parent_is_own
      FOREIGN KEY (artifact_id, parent_version_id)
      REFERENCES artifact_versions (artifact_id, id)
      ON DELETE SET NULL (parent_version_id);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS artifact_versions_by_artifact
  ON artifact_versions (artifact_id, ordinal DESC);

-- B3 transfer readiness (migration 0050): the per-request readiness gate
-- counts unresolved-coordinate rows; this partial index is empty exactly
-- when the deployment is ready, so the uncached predicate stays
-- O(unresolved). Predicate text matches transfer_readiness verbatim.
CREATE INDEX IF NOT EXISTS artifact_versions_unresolved_coordinates
  ON artifact_versions (id)
  WHERE storage_generation IS NULL OR storage_bucket IS NULL
     OR storage_bucket = '';

-- §6.4 via §12A: a version's CONTENT IDENTITY is immutable. A CHECK cannot
-- express "no UPDATE ever" — it only sees the row being written — so this is a
-- trigger, which also binds the table owner. Without it, immutability is a
-- property of the handlers that happen to exist today rather than of the data.
--
-- Scope (deliberately narrow, default-deny in spirit):
--   * This is BEFORE UPDATE only. DELETE is unaffected — retention pruning
--     (the planned 20/200 version cap) still removes whole version rows.
--   * FROZEN content-identity columns — the version `id`, `artifact_id`,
--     `checksum`, `content_type`, `size_bytes`, `storage_object`, `actor_type`,
--     `actor_id`, `ordinal`, and `created_at` — can never change. A reader, the
--     ETag/checksum comparison, and the GC mark-sweep's live-blob set all
--     derive from these; mutating one would silently corrupt them, so any
--     change still RAISEs.
--   * The ONE permitted transition is `parent_version_id` being set to NULL.
--     That column carries `ON DELETE SET NULL`: when pruning deletes an old
--     version, the referential action fires an UPDATE on the surviving child to
--     null its now-dangling parent pointer, and a blanket reject would break
--     that prune. Re-pointing a parent to a DIFFERENT non-NULL version is
--     history rewriting and is still rejected.
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
                   CHECK (principal_type IN ('agent', 'user', 'service', 'workspace', 'public')),
  principal_id   TEXT,

  role           TEXT NOT NULL
                   CHECK (role IN ('manager', 'editor', 'viewer')),

  -- §4.1: every user-visible mutation of mutable state produces a new
  -- revision, and `ETag` is the quoted current revision. Grants are mutable
  -- (role/expiry changes, revocation), so they carry one — without it,
  -- `If-Match`/`412` would be structurally impossible (the ETag would be the
  -- immutable id).
  revision       TEXT NOT NULL
                   CHECK (revision ~ '^rev_[a-f0-9]{16}$'),

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
    OR (principal_type = 'service'   AND principal_id ~ '^tcsvc_')
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
-- `grants_list?resource_type=&resource_id=` — one resource's access list,
-- bounded by the drive first (0045). Partial like its siblings: the default
-- `state=active` never wants revoked tombstones in the index.
CREATE INDEX IF NOT EXISTS grants_by_drive_resource
  ON grants (drive_id, resource_id) WHERE revoked_at IS NULL;

-- Principal matching for authorization (§8): does a grant's principal
-- describe the acting principal? `agent`/`user`/`service` match the subject
-- exactly; `workspace` covers the acting workspace (human members and agent
-- memberships, but NOT a Service Account, which has no membership);
-- `public` (principal_id NULL) covers anyone. One function so every
-- grant-resolution query (folder ancestry, artifact, drive) uses the same
-- rule and they cannot drift apart.
CREATE OR REPLACE FUNCTION _principal_matches(
  actor_type TEXT,
  actor_subject TEXT,
  actor_workspace TEXT,
  principal_type TEXT,
  principal_id TEXT
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT
    (principal_type = actor_type AND principal_id = actor_subject)
    -- `workspace` means everyone IN the workspace, which for humans and
    -- agents is their workspace membership. A Service Account has none: it
    -- belongs to a workspace without being a member of one, so its access is
    -- exactly its explicit `service` grants plus drives it created (service
    -- account design §7.1). Excluded here rather than at each call site --
    -- that is what one shared rule is FOR.
    OR (principal_type = 'workspace' AND principal_id = actor_workspace
        AND actor_type <> 'service')
    OR (principal_type = 'public' AND principal_id IS NULL)
$$;


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

  -- Server-observed attribution (§4): who minted the share link. Like the
  -- drive's `created_by_principal_id`, this is the durable record of who
  -- created a credential that grants anonymous access — auditability for a
  -- capability with no identity behind it. Never a client-supplied claim.
  created_by_principal_type TEXT,
  created_by_principal_id   TEXT,

  -- §4.1: shares are mutable (rotate changes the secret, revoke changes
  -- state), so they carry a revision for If-Match/ETag.
  revision       TEXT NOT NULL
                   CHECK (revision ~ '^rev_[a-f0-9]{16}$'),

  expires_at    TIMESTAMPTZ NOT NULL,
  daily_byte_limit BIGINT NOT NULL CHECK (daily_byte_limit > 0),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  rotated_at    TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS shares_secret_hash ON shares (secret_hash);

-- Replay-path backfill: a non-fresh database created `shares` before the
-- attribution columns existed. The table definition above does not add them
-- to an existing table, so this idempotent ADD closes the gap.
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'shares' AND column_name = 'created_by_principal_type'
  ) THEN
    ALTER TABLE shares
      ADD COLUMN created_by_principal_type TEXT,
      ADD COLUMN created_by_principal_id TEXT;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS shares_by_drive ON shares (drive_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS shares_active_by_drive
  ON shares (drive_id, resource_type, resource_id) WHERE revoked_at IS NULL;
-- `shares_list?resource_type=&resource_id=` — the links on one resource, the
-- share dialog's hot path (0045).
CREATE INDEX IF NOT EXISTS shares_by_drive_resource
  ON shares (drive_id, resource_id) WHERE revoked_at IS NULL;


-- ---------------------------------------------------------------------------
-- Grants and shares are drive-scoped, and so are the resources they name
-- (§6.8, §6.9, §12A). `resource_id` is polymorphic — `resource_type` picks
-- the table — so no single composite FK can express "this resource belongs
-- to the same drive as this row". A trigger is the layer-2 mechanism for
-- exactly that, the same way `reject_cross_kind_name_collision` enforces
-- the §6.2 cross-table namespace. A bare FK on `resource_id` alone would
-- also let a grant in drive A name a resource in drive B — a state no
-- drive-scoped operation can repair.
--
-- Two rules:
--   * A `drive` grant names the drive itself: `resource_id = drive_id`
--     (and the `drive_id` FK already guarantees that drive exists).
--   * Every other resource must exist and carry this row's `drive_id`.
--     `artifact_versions` has no `drive_id` of its own; its drive is its
--     artifact's, reached through the join.
CREATE OR REPLACE FUNCTION reject_out_of_drive_resource()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  ref_drive TEXT;
BEGIN
  IF NEW.resource_type = 'drive' THEN
    IF NEW.resource_id IS DISTINCT FROM NEW.drive_id THEN
      RAISE EXCEPTION
        'a drive grant must name that drive, not %', NEW.resource_id
        USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.resource_type = 'folder' THEN
    SELECT drive_id INTO ref_drive FROM folders WHERE id = NEW.resource_id;
  ELSIF NEW.resource_type = 'artifact' THEN
    SELECT drive_id INTO ref_drive FROM artifacts WHERE id = NEW.resource_id;
  ELSE
    SELECT a.drive_id INTO ref_drive
      FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id
     WHERE v.id = NEW.resource_id;
  END IF;

  IF ref_drive IS NULL THEN
    RAISE EXCEPTION
      'resource % of type % does not exist', NEW.resource_id, NEW.resource_type
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF ref_drive IS DISTINCT FROM NEW.drive_id THEN
    RAISE EXCEPTION
      'resource % belongs to drive %, not %',
      NEW.resource_id, ref_drive, NEW.drive_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS grants_resource_in_own_drive ON grants;
CREATE TRIGGER grants_resource_in_own_drive
  BEFORE INSERT OR UPDATE OF drive_id, resource_type, resource_id ON grants
  FOR EACH ROW EXECUTE FUNCTION reject_out_of_drive_resource();

DROP TRIGGER IF EXISTS shares_resource_in_own_drive ON shares;
CREATE TRIGGER shares_resource_in_own_drive
  BEFORE INSERT OR UPDATE OF drive_id, resource_type, resource_id ON shares
  FOR EACH ROW EXECUTE FUNCTION reject_out_of_drive_resource();


-- ---------------------------------------------------------------------------
-- viewer_sessions — short-lived hashed credentials for the private console
-- viewer (0046; 2026-08-09 private-viewer design). Minted on /v0, redeemed on
-- the isolated viewer host. The composite FK pins the session to one
-- immutable version and proves it belongs to the named artifact — a session
-- can never silently render a newer head. The credential is stored only as a
-- SHA-256 hash; there is deliberately no column that could hold the
-- plaintext. The minting principal is stored so resolution can re-check the
-- CURRENT viewer grant — revocation takes effect within one fetch.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS viewer_sessions (
  id              TEXT PRIMARY KEY
                    CHECK (id ~ '^vwr_[a-f0-9]{16}$'),

  drive_id        TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  artifact_id     TEXT NOT NULL,
  version_id      TEXT NOT NULL
                    CHECK (version_id ~ '^ver_[a-f0-9]{16}$'),

  workspace_id    TEXT NOT NULL,
  -- Deliberately NOT widened to `service` when the other principal columns
  -- were (Token Canopy service account design §7.1). A private viewer
  -- session is a narrow browser console capability minted by the Human BFF
  -- path; a Service Account has no browser, and admitting one here would
  -- turn a console affordance into a backend interface.
  principal_type  TEXT NOT NULL CHECK (principal_type IN ('agent', 'user')),
  principal_id    TEXT NOT NULL,
  -- Snapshot of the minting token's `workspace_role` (0055): lets the
  -- token-less resolution re-check honor the workspace-admin overlay for a
  -- session an owner/admin minted without a grant row. NULL for agents and
  -- pre-0055 rows — no overlay at re-check.
  principal_workspace_role TEXT,

  credential_hash TEXT NOT NULL,

  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,

  FOREIGN KEY (artifact_id, version_id)
    REFERENCES artifact_versions (artifact_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS viewer_sessions_credential_hash
  ON viewer_sessions (credential_hash);

CREATE INDEX IF NOT EXISTS viewer_sessions_expiry ON viewer_sessions (expires_at);


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

  -- A record exists from the moment the key is CLAIMED, before any response
  -- exists to store. That ordering is the whole mechanism: two concurrent
  -- requests race to INSERT, exactly one wins the unique index, and the loser
  -- is told the key is in flight rather than executing the mutation a second
  -- time. A schema that only admitted finished records would force a
  -- check-then-insert, which is the race itself.
  state            TEXT NOT NULL DEFAULT 'in_flight'
                     CHECK (state IN ('in_flight', 'completed')),

  response_status  INTEGER CHECK (response_status BETWEEN 100 AND 599),
  response_headers JSONB NOT NULL DEFAULT '{}'::jsonb,
  response_body    JSONB,

  -- A completed record must carry its response and an in-flight one must not:
  -- otherwise a replay could return `null` as if it were the original result.
  CONSTRAINT idempotency_records_response_matches_state
    CHECK ((state = 'completed') = (response_status IS NOT NULL)),

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
  actor_type        TEXT NOT NULL
                      CHECK (actor_type IN ('agent', 'user', 'service', 'system')),
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


-- ---------------------------------------------------------------------------
-- upload_sessions — B3 direct-transfer sessions (migration 0049)
--
-- Governing contract: TokenCanopy
-- docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md §6.
-- Durable and credential-free: a resumable URI, signed URL, or provider
-- response is NEVER persisted here. Publication and cleanup are DISTINCT
-- state machines — cleanup never changes a terminal publication outcome.
-- The dormant legacy v0_uploads table below is deliberately NOT promoted.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS upload_sessions (
  id              TEXT PRIMARY KEY CHECK (id ~ '^upld_[a-f0-9]{16}$'),
  workspace_id    TEXT NOT NULL,
  drive_id        TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  principal_type  TEXT NOT NULL
                    CHECK (principal_type IN ('agent', 'user', 'service')),
  principal_id    TEXT NOT NULL,
  -- Snapshot of the minting token's `workspace_role` (0055): the
  -- reconciler's token-less publication re-authorization honors the
  -- workspace-admin overlay for a session an owner/admin opened without a
  -- grant row. NULL for agents and pre-0055 rows.
  principal_workspace_role TEXT,

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

  -- Both enumerations fail closed — the application decoder and these
  -- CHECKs reject any other value rather than treating it as active or as
  -- terminal success.
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
-- (migration 0049). Every version producer reserves through this ledger and
-- commits or releases through the shared accounting seam
-- (core/v0_content_commit.py); the conditional `released_at IS NULL` update
-- makes release atomic and exactly-once even when cleanup retries.
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
-- (migration 0049). `drives.storage_bytes` is the authoritative per-drive
-- committed logical counter; this row carries the workspace totals. Parity
-- against sum(artifact_versions.size_bytes) is asserted by the accounting
-- seam's parity helper and checked by the GC job.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workspace_storage (
  workspace_id    TEXT PRIMARY KEY,
  committed_bytes BIGINT NOT NULL DEFAULT 0 CHECK (committed_bytes >= 0),
  reserved_bytes  BIGINT NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- v0_uploads — direct-upload sessions (§7)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS v0_uploads (
  id             TEXT PRIMARY KEY CHECK (id ~ '^upld_[a-f0-9]{16}$'),
  drive_id       TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  -- 'artifact' creates a new artifact under parent_id; 'version' appends a
  -- content version to artifact_id.
  target_kind    TEXT NOT NULL CHECK (target_kind IN ('artifact', 'version')),
  parent_id      TEXT,
  artifact_id    TEXT,
  source         TEXT NOT NULL,
  state          TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'completed', 'cancelled')),
  size_bytes     BIGINT NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
  gcs_object     TEXT,
  expires_at     TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT v0_uploads_target_kind_shape CHECK (
    (target_kind = 'artifact' AND artifact_id IS NULL AND parent_id IS NOT NULL)
    OR (target_kind = 'version' AND artifact_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS v0_uploads_drive ON v0_uploads (drive_id) WHERE state = 'active';

-- ---------------------------------------------------------------------------
-- v0_jobs — isolated generic async work (§6.5, §9)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS v0_jobs (
  id           TEXT PRIMARY KEY CHECK (id ~ '^job_[a-f0-9]{16}$'),
  drive_id     TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,
  state        TEXT NOT NULL DEFAULT 'queued'
                 CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  revision     TEXT NOT NULL CHECK (revision ~ '^rev_[a-f0-9]{16}$'),
  input        JSONB NOT NULL DEFAULT '{}'::jsonb,
  result       JSONB,
  error        JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at  TIMESTAMPTZ,
  CONSTRAINT v0_jobs_finished_at_iff_terminal CHECK (
    (state IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS v0_jobs_drive ON v0_jobs (drive_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- v0_job_object_refs — the objects a job reads or writes, for retry and GC.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS v0_job_object_refs (
  job_id      TEXT NOT NULL REFERENCES v0_jobs(id) ON DELETE CASCADE,
  drive_id    TEXT NOT NULL CHECK (drive_id ~ '^drv_[a-f0-9]{16}$'),
  gcs_object  TEXT NOT NULL CHECK (gcs_object <> ''),
  PRIMARY KEY (job_id, drive_id, gcs_object)
);

-- Sheet edit sessions (design §6).
--
-- Two tables holding a LEDGER. The base grid is deliberately not stored: it is
-- derivable from an immutable, content-addressed version, so materialising it
-- here would put cache-class data in the transactional database and pay WAL,
-- replication, backup and PITR for rows discarded within minutes.
--
-- One row per write REQUEST, not per cell. Under the §8 budget of 200,000
-- cells written per session the edit log tops out near 2 MB and 1,000 rows.

CREATE TABLE IF NOT EXISTS sheet_sessions (
  id                   text PRIMARY KEY CHECK (id ~ '^shs_[a-f0-9]{16}$'),
  drive_id             text NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  artifact_id          text NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  -- The concurrency anchor, captured from a required If-Match at create and
  -- enforced at complete. Same shape as the upload session's contract.
  base_version_id      text NOT NULL REFERENCES artifact_versions(id),
  base_revision        text NOT NULL,
  actor_subject_type   text NOT NULL,
  actor_subject        text NOT NULL,
  actor_workspace      text NOT NULL,
  state                text NOT NULL
                         CHECK (state IN ('open','completed','discarded','expired')),
  revision             text NOT NULL,
  format               text NOT NULL CHECK (format IN ('xlsx','csv','tsv')),
  sheet_index          jsonb NOT NULL,
  -- Per-sheet rollup ordered by sheet index, maintained as edits append, so
  -- the console renders "3 of 12 sheets changed" and a per-group count from
  -- ONE session read instead of paginating the whole edit log. Defaulted
  -- rather than nullable: every client reads it, and null-versus-empty would
  -- be a branch in all of them.
  sheets_touched       jsonb NOT NULL DEFAULT '[]'::jsonb,
  edit_count           integer NOT NULL DEFAULT 0,
  cells_written        integer NOT NULL DEFAULT 0,
  lease_expires_at     timestamptz NOT NULL,
  completed_version_id text REFERENCES artifact_versions(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),

  -- Sessions are addressed by the artifact that owns them
  -- (`/artifacts/{a}/sheet-sessions/{s}`), so every lookup is by this pair.
  -- `artifact_versions` carries the same constraint for the same reason:
  -- it lets the nested path be enforced by the database rather than by an
  -- assertion each caller has to remember (migration 0054).
  CONSTRAINT sheet_sessions_artifact_id_key UNIQUE (artifact_id, id)
);

-- Partial: `sheet_sessions_list` filters open sessions per artifact, and the
-- expiry sweep scans by lease. Neither should walk completed history, which
-- is retained for the lifetime of the version it produced.
CREATE INDEX IF NOT EXISTS sheet_sessions_artifact_open
  ON sheet_sessions (artifact_id) WHERE state = 'open';
CREATE INDEX IF NOT EXISTS sheet_sessions_lease
  ON sheet_sessions (lease_expires_at) WHERE state = 'open';
-- Keyset order for the drive-scoped listing.
CREATE INDEX IF NOT EXISTS sheet_sessions_drive_created
  ON sheet_sessions (drive_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS sheet_session_edits (
  session_id    text    NOT NULL REFERENCES sheet_sessions(id) ON DELETE CASCADE,
  seq           integer NOT NULL,
  sheet         text    NOT NULL,
  range_a1      text    NOT NULL,
  values        jsonb   NOT NULL,
  -- Nullable only for mixed-version compatibility: the pre-0061 writer omits
  -- this column. Current writers always populate it; readers derive the exact
  -- type from the old writer subject's authoritative namespace.
  actor_subject_type text,
  actor_subject text    NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  -- `seq` IS replay order: completion applies these in ascending order, so
  -- the primary key is also the contract.
  PRIMARY KEY (session_id, seq)
);

-- Fixed one-minute rate windows for the direct-transfer control surface,
-- moved OUT of process memory.
--
-- They were a module-level dict in `api/v0_uploads.py`, so the configured
-- limits were exact only at one instance. That is not a footnote: it is the
-- stated reason production pins `api_max_instances = 1`
-- (`envs/prod/main.tf`, `modules/agentdrive-environment/variables.tf`), and
-- that pin is why the API tier has no redundancy at all — a crash, an OOM,
-- or a wedged event loop is a full outage with no sibling to absorb it.
-- Shared counters are what let that ceiling rise.
--
-- Postgres rather than Redis, per the ladder Hub already ratified for the
-- same problem (`apps/hub/src/console/rate-limit.ts`): edge IP limiting,
-- then Postgres counters, then "Memorystore/Redis only if (2) shows
-- measurable write pressure". The operations these windows gate ALREADY sit
-- in a Postgres transaction, so the counter costs no extra round trip and
-- check-and-act stays atomic; Redis would add a second datastore to the
-- path, a new fail-open/fail-closed decision, and ~$40+/month to solve
-- contention that does not exist at this volume.
--
-- The `(dimension, ident)` prefix of the primary key is load-bearing: it is
-- what makes the per-key prune in `core/v0_transfer_rate.py` an indexed
-- range delete rather than a scan. Residue is bounded by TENANCY, not by
-- traffic — at most one stale row per key that has ever transferred — which
-- is why this table needs no sweeper phase of its own.
CREATE TABLE IF NOT EXISTS transfer_rate_windows (
  dimension    text    NOT NULL,
  ident        text    NOT NULL,
  -- Epoch MINUTE, not a timestamptz: the window is an integer bucket and
  -- comparing integers keeps the upsert predicate free of any timezone or
  -- rounding question.
  window_start bigint  NOT NULL,
  count        integer NOT NULL CHECK (count > 0),
  PRIMARY KEY (dimension, ident, window_start)
);

-- ---------------------------------------------------------------------------
-- usage metering — atomic multi-scope byte/request windows (migration 0059)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS usage_windows (
  metric       TEXT NOT NULL CHECK (metric IN ('upload_bytes', 'download_bytes', 'public_bytes', 'requests')),
  scope_type   TEXT NOT NULL CHECK (scope_type IN ('workspace', 'drive', 'principal', 'share', 'share_ip')),
  scope_id     TEXT NOT NULL CHECK (scope_id <> ''),
  period       TEXT NOT NULL CHECK (period IN ('ten_seconds', 'minute', 'hour', 'day', 'month')),
  window_start TIMESTAMPTZ NOT NULL,
  used         BIGINT NOT NULL DEFAULT 0 CHECK (used >= 0),
  reserved     BIGINT NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (metric, scope_type, scope_id, period, window_start)
);

CREATE TABLE IF NOT EXISTS usage_operations (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  operation_key   TEXT NOT NULL CHECK (operation_key <> ''),
  metric          TEXT NOT NULL CHECK (metric IN ('upload_bytes', 'download_bytes', 'public_bytes', 'requests')),
  amount          BIGINT NOT NULL CHECK (amount >= 0),
  dimensions_json JSONB NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('reserved', 'committed', 'released')),
  expiry_action   TEXT NOT NULL CHECK (expiry_action IN ('release', 'commit_reserved')),
  expires_at      TIMESTAMPTZ,
  finalized_at    TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (operation_key, metric),
  CONSTRAINT usage_operations_finalization_shape
    CHECK ((state = 'reserved') = (finalized_at IS NULL))
);

CREATE INDEX IF NOT EXISTS usage_operations_live_expirations
  ON usage_operations (expires_at, id) WHERE state = 'reserved';
CREATE INDEX IF NOT EXISTS usage_windows_retention
  ON usage_windows (window_start);

-- The self-hosted install's credentials (AUTH_MODE=local; 2026-09-19
-- open-source design §4.2 as amended 2026-09-21). An install with no Hub
-- mints opaque API keys against its own principals; these two tables are the
-- only identity plane it keeps. Neither is read under AUTH_MODE=hub, where
-- Hub owns principals and credentials.

-- The subjects this install has minted. Not a users table: no credential,
-- no login, no invite — an id, a label, the workspace it belongs to and, for
-- a user, the role the token claims carry. The workspace owner minted by
-- `init` is the sponsor of every agent token in that workspace.
CREATE TABLE IF NOT EXISTS local_principals (
  subject        text        PRIMARY KEY,
  principal_type text        NOT NULL CHECK (principal_type IN ('agent', 'user')),
  name           text        NOT NULL,
  workspace_id   text        NOT NULL,
  workspace_role text        CHECK (workspace_role IN ('owner', 'admin', 'member')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT local_principals_subject_shape CHECK (
       (principal_type = 'agent' AND subject ~ '^tcagt_')
    OR (principal_type = 'user'  AND subject ~ '^tcusr_')
  ),
  CONSTRAINT local_principals_role_by_type CHECK (
    (principal_type = 'user') = (workspace_role IS NOT NULL)
  )
);
CREATE INDEX IF NOT EXISTS local_principals_by_workspace
  ON local_principals (workspace_id, principal_type);
-- One owner per workspace, held by the schema: two concurrent `init`s must
-- not both mint one (the second sees a unique violation and re-reads).
CREATE UNIQUE INDEX IF NOT EXISTS local_principals_one_owner
  ON local_principals (workspace_id) WHERE workspace_role = 'owner';

-- One row per key `agentdrive-keys create` minted (migration 0064, which
-- replaced the issuer's `local_tokens` ledger). The key itself is never
-- stored: `key_hash` is hex sha256 of the whole `adk_…` string, 240 bits of
-- randomness, so lookup is hash equality and no slow KDF is needed. `id` is
-- the display id (`adk_` + the key's first 8 characters) that `list` shows
-- and `revoke` takes, so an operator can name a key without ever seeing it
-- again.
--
-- `scopes` is a space-separated subset of the eight /v0 scopes, FIXED at
-- creation: no code path updates it (§8 decision 11), so a leaked key cannot
-- be widened by anyone, including its operator. `expires_at` is NULL unless
-- `--expires` was given and is enforced per request. No `last_used_at`: it
-- costs a write per request and nothing reads it yet.
CREATE TABLE IF NOT EXISTS local_api_keys (
  id           text        PRIMARY KEY,
  key_hash     text        NOT NULL UNIQUE,
  subject      text        NOT NULL REFERENCES local_principals (subject),
  name         text        NOT NULL,
  scopes       text        NOT NULL,
  workspace_id text        NOT NULL,
  expires_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  revoked_at   timestamptz,
  -- The wire shapes, pinned here as every other id namespace in this schema
  -- is: `adk_` plus the key's 8 base64url display characters, and a hex
  -- sha256. A hand-written row that does not look like a credential fails
  -- loudly at INSERT rather than quietly authenticating something.
  CONSTRAINT local_api_keys_id_shape CHECK (id ~ '^adk_[A-Za-z0-9_-]{8}$'),
  CONSTRAINT local_api_keys_hash_shape CHECK (key_hash ~ '^[0-9a-f]{64}$'),
  -- A key with no scopes authorizes nothing; it would be a credential that
  -- looks live and does nothing, which is worse than a refused INSERT.
  CONSTRAINT local_api_keys_scopes_present CHECK (length(btrim(scopes)) > 0)
);
CREATE INDEX IF NOT EXISTS local_api_keys_by_subject ON local_api_keys (subject);
