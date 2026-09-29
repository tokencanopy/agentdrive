-- Finite beta share capabilities: expiry and snapshotted daily egress limit.
ALTER TABLE shares
  ADD COLUMN IF NOT EXISTS daily_byte_limit BIGINT;

UPDATE shares
   SET daily_byte_limit = 5368709120
 WHERE daily_byte_limit IS NULL;

UPDATE shares
   SET expires_at = LEAST(
     COALESCE(
       expires_at,
       GREATEST(
         created_at + interval '30 days',
         timestamptz '2026-09-09 07:30:00+00' + interval '7 days'
       )
     ),
     GREATEST(
       created_at + interval '30 days',
       timestamptz '2026-09-09 07:30:00+00' + interval '7 days'
     )
   )
 WHERE revoked_at IS NULL;

UPDATE shares
   SET expires_at = COALESCE(expires_at, created_at + interval '30 days')
 WHERE expires_at IS NULL;

ALTER TABLE shares
  ALTER COLUMN daily_byte_limit SET NOT NULL,
  ALTER COLUMN expires_at SET NOT NULL,
  ADD CONSTRAINT shares_daily_byte_limit_check CHECK (daily_byte_limit > 0);

CREATE INDEX IF NOT EXISTS shares_active_by_drive
  ON shares (drive_id, resource_type, resource_id)
  WHERE revoked_at IS NULL;
