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
