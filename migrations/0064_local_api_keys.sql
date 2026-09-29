-- Opaque local API keys replace the local JWT issuer's token records
-- (AUTH_MODE=local; 2026-09-19 open-source design §4.2 as amended
-- 2026-09-21). `local_principals` is unchanged and carries over; the
-- issued-token ledger does not, because there is no longer a signed token
-- to bind to a row — the key IS the row.
--
-- Dropping `local_tokens` is safe on every deployed database: it is read and
-- written ONLY under AUTH_MODE=local, and every hosted environment runs
-- AUTH_MODE=hub, so the table is empty and unread there. The expand-contract
-- rule (migrations/README.md §4) is about the revision still serving traffic
-- while this runs; that revision never touches this table in hub mode.

-- One row per key `agentdrive-keys create` minted. The key itself is never
-- stored: `key_hash` is hex sha256 of the whole `adk_…` string, which is 240
-- bits of randomness, so lookup is hash equality and no slow KDF is needed.
-- `id` is the key's display id (`adk_` + its first 8 characters), which is
-- what `list` shows and `revoke` takes — an operator can name a key without
-- ever seeing it again.
--
-- `scopes` is a space-separated subset of the eight /v0 scopes, fixed at
-- creation: there is deliberately NO code path that updates it (Josh,
-- 2026-09-21, §8 decision 11). Changing what a client may do is `revoke`
-- plus `create`, so a leaked key cannot be widened by anyone, including its
-- operator. `expires_at` is NULL by default — a key on the operator's own
-- box, with `revoke` one command away — and is enforced per request when set.
-- There is no `last_used_at`: it costs a write per request and nothing reads
-- it yet.
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

DROP TABLE IF EXISTS local_tokens;
