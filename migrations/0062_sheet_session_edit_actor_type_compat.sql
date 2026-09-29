-- 0061's NOT NULL end state is not compatible with the previous serving
-- revision, whose edit INSERT omits actor_subject_type. Migrations publish as
-- one pending batch, so a database below 0061 never exposes that intermediate
-- state. Keep this nullable through the mixed-version and rollback window;
-- current writers still populate it and readers derive an omitted value from
-- the writer subject's authoritative namespace.
ALTER TABLE sheet_session_edits
  ALTER COLUMN actor_subject_type DROP NOT NULL;

-- 0061 copied the SESSION CREATOR's type onto historical edits. Another
-- authorized principal can write an edit, so correct those rows from the
-- edit's own actor_subject. These are the same closed subject namespaces the
-- product-token boundary validates; an unrecognized historical value is left
-- unchanged rather than guessed.
UPDATE sheet_session_edits
   SET actor_subject_type = CASE
     WHEN actor_subject LIKE 'tcagt\_%' ESCAPE '\' THEN 'agent'
     WHEN actor_subject LIKE 'tcusr\_%' ESCAPE '\' THEN 'user'
     WHEN actor_subject LIKE 'tcsvc\_%' ESCAPE '\' THEN 'service'
     ELSE actor_subject_type
   END;
