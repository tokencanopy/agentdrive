-- 0050 — B3 transfer-readiness partial index (expand-only).
--
-- Governing contract: TokenCanopy
-- docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md §7/§9.
--
-- Packet 3's deferred follow-up: `core.v0_uploads.transfer_readiness` runs
-- its unresolved-generation-row predicate on EVERY transfer control and
-- download mint (deliberately uncached, review round 3). This partial index
-- makes that gate O(unresolved rows) — an index that is empty exactly when
-- the deployment is ready — instead of a per-request scan of every version
-- row. The predicate text matches the readiness query verbatim so the
-- planner can use the index for it.
--
-- Expand-only; no data change; the feature remains disabled by default.

CREATE INDEX IF NOT EXISTS artifact_versions_unresolved_coordinates
  ON artifact_versions (id)
  WHERE storage_generation IS NULL OR storage_bucket IS NULL
     OR storage_bucket = '';
