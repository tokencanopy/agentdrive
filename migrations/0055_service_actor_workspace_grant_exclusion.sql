-- Exclude Service actors from `workspace` grants.
--
-- Token Canopy service account design (tokencanopy/tokencanopy#331) §7.1. A
-- `workspace` grant means "everyone in this workspace", which for humans and
-- agents is their workspace membership. A Service Account has NO workspace
-- membership: it BELONGS TO a workspace without being a member of one. Its
-- Drive access is exactly the explicit `service` grants it holds, plus drives
-- it created.
--
-- The exclusion goes in the central matcher rather than at each call site
-- because that is the whole reason this function exists: search, navigation,
-- content, changes, and sharing all ask it the same question and therefore
-- cannot drift apart. An exclusion added to five queries would be an
-- exclusion missing from the sixth.
--
-- Additive and behavior-preserving for every actor that exists today: no
-- token carries `actor_type = 'service'` until Hub's `tck_*` issuance is
-- enabled, and `user`, `agent`, `workspace`, and `public` are untouched.

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
    OR (principal_type = 'workspace' AND principal_id = actor_workspace
        AND actor_type <> 'service')
    OR (principal_type = 'public' AND principal_id IS NULL)
$$;
