"""Sheet edit-session schema, ids, and routing (design §6, plan Task 1).

The lockstep rule this pins: an id's generator, its route convertor regex, and
its schema CHECK are one fact expressed in three places, and they must agree.
A `shs_` that the generator mints but the CHECK rejects is a write that fails
in production and nowhere else.
"""

from __future__ import annotations

import re

import asyncpg
import pytest

from agentdrive.core.ids import new_id
from agentdrive.db import conn

# No module-level asyncio mark: `asyncio_mode = auto` picks up the async
# tests, and marking the sync ones warns.

_SHS = re.compile(r"^shs_[a-f0-9]{16}$")


def test_new_sheet_session_id_shape():
    assert _SHS.match(new_id("shs"))


async def test_sheet_session_tables_exist(app_with_lifespan):
    async with conn() as c:
        rows = await c.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('sheet_sessions', 'sheet_session_edits')"
        )
    assert {r["table_name"] for r in rows} == {"sheet_sessions", "sheet_session_edits"}


async def test_id_check_rejects_a_foreign_prefix(app_with_lifespan):
    """The CHECK is the last line of defence if a caller hand-builds an id."""
    async with conn() as c:
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute(
                "INSERT INTO sheet_sessions "
                "(id, drive_id, artifact_id, base_version_id, base_revision, "
                " actor_subject_type, actor_subject, actor_workspace, state, "
                " revision, format, sheet_index, lease_expires_at) "
                "VALUES ('upld_0123456789abcdef', 'drv_0000000000000001', "
                "'art_0000000000000001', 'ver_0000000000000001', 'rev_1', "
                "'agent', 'a', 'w', 'open', 'rev_2', 'xlsx', '[]'::jsonb, now())"
            )


async def test_state_check_rejects_an_unknown_state(app_with_lifespan):
    async with conn() as c:
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute(
                "INSERT INTO sheet_sessions "
                "(id, drive_id, artifact_id, base_version_id, base_revision, "
                " actor_subject_type, actor_subject, actor_workspace, state, "
                " revision, format, sheet_index, lease_expires_at) "
                "VALUES ('shs_0123456789abcdef', 'drv_0000000000000001', "
                "'art_0000000000000001', 'ver_0000000000000001', 'rev_1', "
                "'agent', 'a', 'w', 'paused', 'rev_2', 'xlsx', '[]'::jsonb, now())"
            )


async def test_sheets_touched_defaults_to_an_empty_array(app_with_lifespan):
    """Defaulted rather than nullable: the console reads this on every session
    poll, and `null` versus `[]` would be a branch in every client."""
    async with conn() as c:
        col = await c.fetchrow(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'sheet_sessions' AND column_name = 'sheets_touched'"
        )
    assert col is not None, "sheets_touched column is missing"
    assert col["is_nullable"] == "NO"
    assert "[]" in (col["column_default"] or "")


async def test_edits_cascade_when_a_session_is_deleted(app_with_lifespan):
    """GC deletes expired and discarded sessions; their edits must go with
    them rather than becoming unreachable rows nothing sweeps."""
    async with conn() as c:
        rule = await c.fetchval(
            "SELECT rc.delete_rule FROM information_schema.referential_constraints rc "
            "JOIN information_schema.table_constraints tc "
            "  ON tc.constraint_name = rc.constraint_name "
            "WHERE tc.table_name = 'sheet_session_edits'"
        )
    assert rule == "CASCADE"


def test_convertor_regex_matches_the_generator():
    """Registered as a Starlette convertor so `{session_id:shs_id}` routes.
    Pinned to the generator's output, per the lockstep rule."""
    from agentdrive.api.convertors import ShsIdConvertor

    assert re.fullmatch(ShsIdConvertor.regex, new_id("shs"))
