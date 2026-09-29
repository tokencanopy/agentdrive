-- Preserve the exact writer identity on each sheet edit. Historical rows
-- used the session creator's type when rendered, so retain that interpretation
-- for the backfill while all new writes store their own actor type.
ALTER TABLE sheet_session_edits
  ADD COLUMN actor_subject_type text;

UPDATE sheet_session_edits AS edit
   SET actor_subject_type = session.actor_subject_type
  FROM sheet_sessions AS session
 WHERE session.id = edit.session_id;

ALTER TABLE sheet_session_edits
  ALTER COLUMN actor_subject_type SET NOT NULL;
