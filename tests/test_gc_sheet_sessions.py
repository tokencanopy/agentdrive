"""Sheet-session collection in the live GC sweep (plan Task 10).

Two properties this pins, and the second is the one that would rot silently:
an abandoned session stops counting against the per-drive cap without waiting
for a caller who never returns, and a COMPLETED session is never swept — its
edit log is the change feed's cell-level provenance.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.core.gc import GCSweeper
from agentdrive.db import conn

from .test_v0_sheets_read import VALUES, XLSX, build_xlsx, make_actor

pytestmark = pytest.mark.asyncio


def _key() -> str:
    return uuid.uuid4().hex


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    app.dependency_overrides[v0_actor] = lambda: make_actor()
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lifespan), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, storage_reservations, upload_sessions, "
            "sheet_sessions, workspace_storage, drives RESTART IDENTITY CASCADE"
        )


@pytest_asyncio.fixture
async def base(http):
    """Seeded through the real API on purpose: the drive/root-folder pair is a
    circular FK that only `create_drive` orders correctly, and duplicating that
    in a fixture is how it drifts from the schema."""
    drive = (
        await http.post(
            "/v0/drives", json={"name": "gc"}, headers={"Idempotency-Key": _key()}
        )
    ).json()
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        files={"content": ("q.xlsx", build_xlsx(VALUES), XLSX)},
        data={"parent_id": drive["root_folder_id"], "name": "q.xlsx"},
        headers={"Idempotency-Key": _key()},
    )
    assert created.status_code == 201, created.text
    art = created.json()
    async with conn() as c:
        version = await c.fetchval(
            "SELECT head_version_id FROM artifacts WHERE id = $1", art["id"]
        )
    return {
        "drive": drive["id"],
        "artifact": art["id"],
        "revision": art["revision"],
        "version": version,
        "workspace": make_actor().workspace_id,
    }


async def _session(
    base: dict, state: str, *, lease_minutes: int, updated_days_ago: float = 0.0
) -> str:
    # `shs_` + exactly 16 hex characters: the schema CHECK, the convertor regex
    # and the id generator all agree on that shape.
    sid = "shs_" + uuid.uuid4().hex[:16]
    async with conn() as c:
        await c.execute(
            "INSERT INTO sheet_sessions (id, drive_id, artifact_id, base_version_id, "
            "base_revision, actor_subject_type, actor_subject, actor_workspace, state, "
            "revision, format, sheet_index, lease_expires_at, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,'agent','a',$6,$7,$8,'xlsx','[]'::jsonb, "
            "now() + ($9 || ' minutes')::interval, now() - ($10 || ' days')::interval)",
            sid,
            base["drive"],
            base["artifact"],
            base["version"],
            base["revision"],
            base["workspace"],
            state,
            "rev_" + uuid.uuid4().hex[:16],
            str(lease_minutes),
            str(updated_days_ago),
        )
    return sid


async def _states() -> dict[str, str]:
    async with conn() as c:
        rows = await c.fetch("SELECT id, state FROM sheet_sessions")
    return {r["id"]: r["state"] for r in rows}


async def _sweep():
    return await GCSweeper(sessions_only=True).run()


async def test_an_overdue_open_session_is_terminalized(base):
    """The backstop for sessions nobody comes back to. Expiry at USE is the
    primary path; this is what frees the per-drive cap slot regardless."""
    sid = await _session(base, "open", lease_minutes=-5)
    result = await _sweep()
    assert result.sheet_sessions_expired >= 1
    assert (await _states())[sid] == "expired"


async def test_a_live_session_is_left_alone(base):
    sid = await _session(base, "open", lease_minutes=30)
    await _sweep()
    assert (await _states())[sid] == "open"


async def test_terminal_sessions_are_collected_after_retention(base):
    old = await _session(base, "discarded", lease_minutes=-60, updated_days_ago=2)
    fresh = await _session(base, "expired", lease_minutes=-60, updated_days_ago=0)
    result = await _sweep()
    states = await _states()
    assert old not in states, "a terminal session past retention is collected"
    assert fresh in states, "a recent one stays readable so the agent learns why"
    assert result.sheet_sessions_removed >= 1


async def test_a_completed_session_is_never_collected(base):
    """Its edit log is the change feed's cell-level provenance, bounded by the
    version pruner rather than by age."""
    sid = await _session(base, "completed", lease_minutes=-60, updated_days_ago=400)
    await _sweep()
    assert (await _states())[sid] == "completed"


async def test_collection_cascades_to_the_edit_log(base):
    sid = await _session(base, "discarded", lease_minutes=-60, updated_days_ago=2)
    async with conn() as c:
        await c.execute(
            "INSERT INTO sheet_session_edits "
            "(session_id, seq, sheet, range_a1, values, actor_subject_type, "
            "actor_subject) VALUES ($1, 1, 'Q3', 'A1', '[[1]]'::jsonb, "
            "'agent', 'a')",
            sid,
        )
    await _sweep()
    async with conn() as c:
        left = await c.fetchval(
            "SELECT count(*) FROM sheet_session_edits WHERE session_id = $1", sid
        )
    assert left == 0, "edits must not outlive their session"


async def test_the_sweep_reports_its_work(base):
    await _session(base, "open", lease_minutes=-5)
    await _session(base, "discarded", lease_minutes=-60, updated_days_ago=3)
    result = await _sweep()
    assert not result.failed
    payload = result.as_dict()
    assert payload["sheet_sessions_expired"] >= 1
    assert payload["sheet_sessions_removed"] >= 1
