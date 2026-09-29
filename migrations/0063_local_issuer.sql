-- The local token issuer (AUTH_MODE=local; 2026-09-19 open-source design
-- §4.2). A self-hosted install mints its own bearer tokens with the claim
-- shape Hub's carry; these two tables are the only identity plane it keeps.
-- Neither is read under AUTH_MODE=hub, where Hub owns principals and tokens.

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

-- Every token `create` issued, so `list` can name them and `revoke` can
-- refuse one. The verifier consults this row after the signature check on
-- every local-mode request and requires the verified claims to MATCH it --
-- subject, workspace, scopes within `scopes`, and the audience the token was
-- minted for -- so a signed token whose jti is absent here, or whose claims
-- outgrew its row, is refused: a leaked signing key plus a live jti does not
-- mint an escalated token. `audience` is `api` (the product `/v0` bearer) or
-- `mcp` (what the MCP transport verifies); the two are mutually refusing.
-- `expires_at` is informational for `list`; the JWT's own `exp` governs.
CREATE TABLE IF NOT EXISTS local_tokens (
  jti          text        PRIMARY KEY,
  subject      text        NOT NULL REFERENCES local_principals (subject),
  name         text        NOT NULL,
  scopes       text        NOT NULL,
  audience     text        NOT NULL CHECK (audience IN ('api', 'mcp')),
  workspace_id text        NOT NULL,
  expires_at   timestamptz NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  revoked_at   timestamptz
);
CREATE INDEX IF NOT EXISTS local_tokens_by_subject ON local_tokens (subject);
