-- Atomic, idempotent usage windows for beta quota enforcement.
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
