-- The minting principal's workspace role, snapshotted onto the two session
-- tables whose later re-authorization runs WITHOUT a token.
--
-- The workspace-admin overlay (contract §8, ratified 2026-08-28) gives a
-- human workspace owner/admin implicit `manager` on every drive in their
-- workspace — capability that lives on the verified token
-- (`workspace_role`), not in the grants table. Viewer-session resolution
-- and the upload reconciler re-authorize from a STORED principal
-- (`principal_type`, `principal_id`, `workspace_id`); without this column
-- an owner/admin acting through the overlay could mint a viewer session
-- that never resolves, or start an upload whose reconciled completion is
-- misclassified as unauthorized — the same trap `require_local` documents
-- for public grants: a credential minted under terms its re-check can
-- never satisfy.
--
-- A SNAPSHOT, like token scope, not a live lookup: AgentDrive verifies
-- tokens offline and has no workspace-membership table to consult, so the
-- role recorded at mint is honored for the session's bounded lifetime
-- (viewer sessions expire in minutes; upload sessions are already trusted
-- to finish work their mint authorized). NULL for agents, for humans whose
-- token carried no role, and for every row minted before this migration —
-- all of which simply get no overlay at re-check, exactly as before.

ALTER TABLE viewer_sessions
  ADD COLUMN IF NOT EXISTS principal_workspace_role TEXT;

ALTER TABLE upload_sessions
  ADD COLUMN IF NOT EXISTS principal_workspace_role TEXT;
