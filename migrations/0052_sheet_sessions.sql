-- Sheet edit sessions (design §6).
--
-- Two tables holding a LEDGER. The base grid is deliberately not stored: it is
-- derivable from an immutable, content-addressed version, so materialising it
-- here would put cache-class data in the transactional database and pay WAL,
-- replication, backup and PITR for rows discarded within minutes.
--
-- One row per write REQUEST, not per cell. Under the §8 budget of 200,000
-- cells written per session the edit log tops out near 2 MB and 1,000 rows.

CREATE TABLE sheet_sessions (
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
  updated_at           timestamptz NOT NULL DEFAULT now()
);

-- Partial: `sheet_sessions_list` filters open sessions per artifact, and the
-- expiry sweep scans by lease. Neither should walk completed history, which
-- is retained for the lifetime of the version it produced.
CREATE INDEX sheet_sessions_artifact_open
  ON sheet_sessions (artifact_id) WHERE state = 'open';
CREATE INDEX sheet_sessions_lease
  ON sheet_sessions (lease_expires_at) WHERE state = 'open';
-- Keyset order for the drive-scoped listing.
CREATE INDEX sheet_sessions_drive_created
  ON sheet_sessions (drive_id, created_at DESC, id DESC);

CREATE TABLE sheet_session_edits (
  session_id    text    NOT NULL REFERENCES sheet_sessions(id) ON DELETE CASCADE,
  seq           integer NOT NULL,
  sheet         text    NOT NULL,
  range_a1      text    NOT NULL,
  values        jsonb   NOT NULL,
  actor_subject text    NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  -- `seq` IS replay order: completion applies these in ascending order, so
  -- the primary key is also the contract.
  PRIMARY KEY (session_id, seq)
);
