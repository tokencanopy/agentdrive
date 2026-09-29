-- Viewer sessions: short-lived, hashed credentials for the private console
-- viewer (docs/superpowers/specs/2026-08-09-private-viewer-sessions-design.md).
--
-- A row is minted by POST /v0/drives/{drive_id}/artifacts/{artifact_id}/
-- viewer-sessions and redeemed by the isolated viewer host (`/view/doc`,
-- `/view/content`) via an Authorization header. The credential itself is
-- returned once at mint and stored only as a SHA-256 hash — the shares
-- pattern, on purpose.
--
-- The composite FK onto (artifact_id, id) of artifact_versions pins the
-- session to one immutable version AND proves that version belongs to that
-- artifact — a session can never silently render a newer head, structurally.

CREATE TABLE IF NOT EXISTS viewer_sessions (
  id              TEXT PRIMARY KEY
                    CHECK (id ~ '^vwr_[a-f0-9]{16}$'),

  drive_id        TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  artifact_id     TEXT NOT NULL,
  version_id      TEXT NOT NULL
                    CHECK (version_id ~ '^ver_[a-f0-9]{16}$'),

  -- The minting principal, re-authorized at every resolution: the stored
  -- principal must STILL hold a viewer grant on the artifact when the
  -- credential is redeemed, so revocation takes effect within one fetch.
  workspace_id    TEXT NOT NULL,
  principal_type  TEXT NOT NULL CHECK (principal_type IN ('agent', 'user')),
  principal_id    TEXT NOT NULL,

  -- Never the plaintext — there is deliberately no column that could hold it.
  credential_hash TEXT NOT NULL,

  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,

  FOREIGN KEY (artifact_id, version_id)
    REFERENCES artifact_versions (artifact_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS viewer_sessions_credential_hash
  ON viewer_sessions (credential_hash);

-- The sweep path: expired rows are deleted opportunistically at mint time.
CREATE INDEX IF NOT EXISTS viewer_sessions_expiry ON viewer_sessions (expires_at);
