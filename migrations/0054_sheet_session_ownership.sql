-- A session is addressable by the artifact that owns it.
--
-- The session routes moved under `/artifacts/{artifact_id}/sheet-sessions`,
-- so every lookup is now by the PAIR (artifact_id, id) rather than by id
-- alone. `artifact_versions` carries the same constraint for the same
-- reason — it is what lets a nested path be enforced by the database
-- instead of by a hand-written assertion each caller has to remember.
--
-- Why the shape changed at all: `require_local(..., "artifact",
-- "artifact_id")` reads the artifact from the request PATH. The seven flat
-- routes had no artifact in the path, so the declarative grant check the
-- rest of the content surface relies on could not attach to them, and
-- nothing replaced it — a same-workspace caller holding no grant could
-- list, read, write into, complete and discard sessions on artifacts it
-- could not open. Nesting closes that by construction rather than by
-- remembering to check.
--
-- The id stays the primary key: a session id is still globally unique, and
-- this is an additional uniqueness guarantee, not a new identity.

ALTER TABLE sheet_sessions
  ADD CONSTRAINT sheet_sessions_artifact_id_key UNIQUE (artifact_id, id);
