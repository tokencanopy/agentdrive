-- Accept `service` in every principal/actor column a Service Account can
-- reach — WITHOUT adding a writer.
--
-- Token Canopy service account design (tokencanopy/tokencanopy#331) §7.1,
-- §15 step 6. Readers and constraints first, writers in the next slice: a
-- widened writer meeting a check constraint that still refuses `service`
-- fails at INSERT, and the request it fails is somebody's.
--
-- ADDITIVE THROUGHOUT. `agent`, `user`, `system`, `workspace`, and `public`
-- keep their exact meanings; `system` in particular is the maintenance actor
-- and is NOT dropped. Every constraint below is replaced by one that accepts
-- a strict superset, so no existing row can become invalid.
--
-- `viewer_sessions.principal_type` is deliberately NOT widened. A private
-- viewer session is a narrow browser console capability minted by the Human
-- BFF path (§7.1) — a Service Account has no browser, and giving it one
-- would turn a console affordance into a backend interface.

-- grants: the principal a grant names.
ALTER TABLE grants DROP CONSTRAINT IF EXISTS grants_principal_type_check;
ALTER TABLE grants
  ADD CONSTRAINT grants_principal_type_check
  CHECK (principal_type IN ('agent', 'user', 'service', 'workspace', 'public'));

-- The id SHAPE is per principal_type, so a `service` grant naming a `tcagt_`
-- id would record an agent's access under the wrong kind — invisible to a
-- reader and wrong to the matcher.
ALTER TABLE grants DROP CONSTRAINT IF EXISTS grants_principal_id_shape;
ALTER TABLE grants
  ADD CONSTRAINT grants_principal_id_shape CHECK (
       (principal_type = 'agent'     AND principal_id ~ '^tcagt_')
    OR (principal_type = 'user'      AND principal_id ~ '^tcusr_')
    OR (principal_type = 'service'   AND principal_id ~ '^tcsvc_')
    OR (principal_type = 'workspace' AND principal_id IS NOT NULL)
    OR (principal_type = 'public'    AND principal_id IS NULL)
  );

-- artifact_versions: server-observed authorship of one immutable version.
ALTER TABLE artifact_versions
  DROP CONSTRAINT IF EXISTS artifact_versions_actor_type_check;
ALTER TABLE artifact_versions
  ADD CONSTRAINT artifact_versions_actor_type_check
  CHECK (actor_type IN ('agent', 'user', 'service', 'system'));

-- drive_changes: the change feed's actor. Its serialized shape is frozen;
-- this adds a value to the vocabulary, not a field to the response.
ALTER TABLE drive_changes DROP CONSTRAINT IF EXISTS drive_changes_actor_type_check;
ALTER TABLE drive_changes
  ADD CONSTRAINT drive_changes_actor_type_check
  CHECK (actor_type IN ('agent', 'user', 'service', 'system'));

-- upload_sessions: principal-bound direct uploads, so a Service holding the
-- existing content-write scopes uses the standard large-upload path rather
-- than a second one built for it (§7.1).
ALTER TABLE upload_sessions
  DROP CONSTRAINT IF EXISTS upload_sessions_principal_type_check;
ALTER TABLE upload_sessions
  ADD CONSTRAINT upload_sessions_principal_type_check
  CHECK (principal_type IN ('agent', 'user', 'service'));
