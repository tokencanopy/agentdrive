"""Tests for the artifact.purged change event emitted during GC sweeper purge."""

from __future__ import annotations

import datetime as dt
import json

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.core.gc import GCSweeper
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

WS = "tcws_0000000000000001"
AGENT = "tcagt_0000000000000001"
VIEWER = "tcusr_0000000000000002"
DRIVE = "drv_00000000000000d1"
ROOT = "fld_0000000000000d01"


class _FakeTransferStorage:
    """Canned observations + scripted delete outcomes."""

    def __init__(self, observations=None, delete_errors=None):
        self.observations = dict(observations or {})
        self.delete_errors = dict(delete_errors or {})
        self.deleted: list[tuple[str, int]] = []

    async def stat_object(self, object_name):
        return self.observations.get(object_name)

    async def delete_generation(self, object_name, generation):
        err = self.delete_errors.get(object_name)
        if err is not None:
            raise err
        self.deleted.append((object_name, generation))
        self.observations.pop(object_name, None)


def _sweeper(transfer_storage=None, **class_over):
    """A GCSweeper with test-friendly zeroed age gates."""
    attrs = {
        "TRANSFER_GRACE": dt.timedelta(0),
        "PURGE_RETENTION": dt.timedelta(0),
        "MARK_SWEEP_AGE": dt.timedelta(0),
        "SCRATCH_SWEEP_AGE": dt.timedelta(0),
        "ORPHAN_SWEEP_AGE": dt.timedelta(0),
        "LATE_FINALIZATION_WINDOW": dt.timedelta(0),
    }
    attrs.update(class_over)
    cls = type("TestSweeper", (GCSweeper,), attrs)
    return cls(transfer_storage=transfer_storage or _FakeTransferStorage())


def _make_actor(
    *,
    subject: str = AGENT,
    subject_type: str = "agent",
    workspace: str = WS,
    scopes: set[str] | None = None,
) -> V0ActorContext:
    scopes = scopes if scopes is not None else {
        "drives:read", "drives:write", "usage:read",
        "content:read", "content:write", "changes:read",
    }
    is_agent = subject_type == "agent"
    return V0ActorContext(
        subject=subject,
        subject_type=subject_type,
        workspace_id=workspace,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(scopes),
        credential_id="tccred_0000000000000001" if is_agent else None,
        runtime_id="tcrun_0000000000000001" if is_agent else None,
        sponsor_id=None,
        workspace_role=None if is_agent else "admin",
    )


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def override_actor(app_with_lifespan):
    def _set(actor: V0ActorContext) -> None:
        app.dependency_overrides[v0_actor] = lambda: actor

    yield _set
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean(app_with_lifespan):
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, storage_reservations, "
            "upload_sessions, workspace_storage, drives "
            "RESTART IDENTITY CASCADE"
        )
    yield
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, storage_reservations, "
            "upload_sessions, workspace_storage, drives "
            "RESTART IDENTITY CASCADE"
        )


async def _seed_drive(c, drive_id=DRIVE, root_id=ROOT) -> None:
    await c.execute(
        "INSERT INTO drives (id, workspace_id, name, revision) VALUES "
        "($1, $2, 'gc-fixture', 'rev_00000000000000d1') "
        "ON CONFLICT (id) DO NOTHING",
        drive_id, WS,
    )
    await c.execute(
        "INSERT INTO folders (id, drive_id, parent_id, name, revision) VALUES "
        "($1, $2, NULL, NULL, 'rev_00000000000000d2') "
        "ON CONFLICT (id) DO NOTHING",
        root_id, drive_id,
    )
    await c.execute(
        "UPDATE drives SET root_folder_id = $2 WHERE id = $1", drive_id, root_id
    )
    await c.execute(
        "INSERT INTO grants "
        "  (id, drive_id, resource_type, resource_id, principal_type, "
        "   principal_id, role, revision) "
        "VALUES ('grn_0000000000000000', $1, 'drive', $1, 'agent', $2, "
        "        'manager', 'rev_0000000000000000') "
        "ON CONFLICT (id) DO NOTHING",
        drive_id, AGENT,
    )


async def test_artifact_purge_appends_artifact_purged_event():
    """Purging an artifact appends exactly one artifact.purged row with system actor and name."""
    art_id = "art_00000000000000a1"
    art_name = "monthly-statement.pdf"

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision, deleted_at) "
            "VALUES ($1, $2, $3, $4, 'rev_00000000000000a1', now() - interval '90 days')",
            art_id, DRIVE, ROOT, art_name,
        )

    result = await _sweeper().run()
    assert result.purged_artifacts == 1

    async with conn() as c:
        remaining = await c.fetchval("SELECT count(*) FROM artifacts WHERE id = $1", art_id)
        assert remaining == 0

        changes = await c.fetch(
            "SELECT type, actor_type, actor_id, resource_type, resource_id, data "
            "FROM drive_changes WHERE drive_id = $1 AND type = 'artifact.purged'",
            DRIVE,
        )
        assert len(changes) == 1
        chg = changes[0]
        assert chg["type"] == "artifact.purged"
        assert chg["actor_type"] == "system"
        assert chg["actor_id"] is None
        assert chg["resource_type"] == "artifact"
        assert chg["resource_id"] == art_id

        data = json.loads(chg["data"]) if isinstance(chg["data"], str) else chg["data"]
        assert data == {"name": art_name}


async def test_restored_artifact_produces_no_event():
    """An artifact restored between candidate SELECT and DELETE produces no purge event."""
    art_id = "art_00000000000000a2"
    art_name = "resurrected.pdf"

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision, deleted_at) "
            "VALUES ($1, $2, $3, $4, 'rev_00000000000000a2', now() - interval '90 days')",
            art_id, DRIVE, ROOT, art_name,
        )

    class RestoreBetweenSelectAndDelete(GCSweeper):
        TRANSFER_GRACE = dt.timedelta(0)
        PURGE_RETENTION = dt.timedelta(0)
        MARK_SWEEP_AGE = dt.timedelta(0)
        SCRATCH_SWEEP_AGE = dt.timedelta(0)
        ORPHAN_SWEEP_AGE = dt.timedelta(0)

        async def _purge_artifact_candidates(self, c, cutoff):
            rows = await super()._purge_artifact_candidates(c, cutoff)
            if rows:
                async with conn() as other:
                    await other.execute(
                        "UPDATE artifacts SET deleted_at = NULL WHERE id = $1",
                        art_id,
                    )
            return rows

    result = await RestoreBetweenSelectAndDelete(
        transfer_storage=_FakeTransferStorage()
    ).run()
    assert result.purged_artifacts == 0

    async with conn() as c:
        survived = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE id = $1 AND deleted_at IS NULL",
            art_id,
        )
        assert survived == 1

        purged_events = await c.fetch(
            "SELECT id FROM drive_changes WHERE drive_id = $1 AND type = 'artifact.purged'",
            DRIVE,
        )
        assert len(purged_events) == 0


async def test_artifact_purge_dry_run_does_not_persist_event():
    """A dry run previews the purge but rolls back both the deletion and the event."""
    art_id = "art_00000000000000a3"

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision, deleted_at) "
            "VALUES ($1, $2, $3, 'dry-run.bin', 'rev_00000000000000a3', "
            "        now() - interval '90 days')",
            art_id, DRIVE, ROOT,
        )

    result = await _sweeper().run(dry_run=True)
    assert result.purged_artifacts == 1

    async with conn() as c:
        remaining = await c.fetchval("SELECT count(*) FROM artifacts WHERE id = $1", art_id)
        assert remaining == 1

        events = await c.fetch(
            "SELECT id FROM drive_changes WHERE drive_id = $1 AND type = 'artifact.purged'",
            DRIVE,
        )
        assert len(events) == 0


async def test_changes_endpoint_exposes_artifact_purged_as_content_event(http, override_actor):
    """artifact.purged is readable via /v0/drives/{drive_id}/changes and is a content event."""
    override_actor(_make_actor())
    art_id = "art_00000000000000a4"
    art_name = "feed-check.txt"

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision, deleted_at) "
            "VALUES ($1, $2, $3, $4, 'rev_00000000000000a4', now() - interval '90 days')",
            art_id, DRIVE, ROOT, art_name,
        )

    result = await _sweeper().run()
    assert result.purged_artifacts == 1

    # 1. Query with type filter specifically for artifact.purged
    resp = await http.get(
        f"/v0/drives/{DRIVE}/changes",
        params={"start": "beginning", "type": "artifact.purged"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["type"] == "artifact.purged"
    assert item["actor"] == {"type": "system", "id": None}
    assert item["resource"] == {"type": "artifact", "id": art_id}
    assert item["data"] == {"name": art_name}

    # 2. Query as a non-manager viewer (content event is not filtered out)
    # Manager-only permission events are stripped for viewers, but content events stay visible.
    async with conn() as c:
        await c.execute(
            "INSERT INTO grants (id, drive_id, principal_type, principal_id, "
            "resource_type, resource_id, role, revision) VALUES "
            "('grn_0000000000000001', $1, 'user', $2, 'drive', $1, "
            " 'viewer', 'rev_0000000000000001')",
            DRIVE, VIEWER,
        )

    override_actor(_make_actor(subject=VIEWER, subject_type="user", scopes={"changes:read"}))
    resp_viewer = await http.get(
        f"/v0/drives/{DRIVE}/changes",
        params={"start": "beginning", "type": "artifact.purged"},
    )
    assert resp_viewer.status_code == 200, resp_viewer.text
    viewer_items = resp_viewer.json()["items"]
    assert len(viewer_items) == 1
    assert viewer_items[0]["type"] == "artifact.purged"


async def test_multiple_artifacts_purged_emits_ordered_events():
    """Purging multiple artifacts creates ordered events with dense sequences and clean data."""
    art1 = "art_00000000000000b1"
    art2 = "art_00000000000000b2"

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision, deleted_at) VALUES "
            "($1, $3, $4, 'file1.txt', 'rev_00000000000000b1', now() - interval '90 days'), "
            "($2, $3, $4, 'file2.txt', 'rev_00000000000000b2', now() - interval '90 days')",
            art1, art2, DRIVE, ROOT,
        )

    result = await _sweeper().run()
    assert result.purged_artifacts == 2

    async with conn() as c:
        changes = await c.fetch(
            "SELECT sequence, type, actor_type, actor_id, resource_id, data "
            "FROM drive_changes WHERE drive_id = $1 AND type = 'artifact.purged' "
            "ORDER BY sequence ASC",
            DRIVE,
        )
        assert len(changes) == 2
        ev1, ev2 = changes[0], changes[1]
        assert ev1["sequence"] < ev2["sequence"]

        by_id = {ev["resource_id"]: ev for ev in changes}
        assert art1 in by_id
        assert art2 in by_id

        raw_data1 = by_id[art1]["data"]
        raw_data2 = by_id[art2]["data"]
        data1 = json.loads(raw_data1) if isinstance(raw_data1, str) else raw_data1
        data2 = json.loads(raw_data2) if isinstance(raw_data2, str) else raw_data2
        assert data1 == {"name": "file1.txt"}
        assert data2 == {"name": "file2.txt"}
        for data in (data1, data2):
            forbidden = {
                "bytes", "size_bytes", "checksum", "storage_object", "bucket", "generation",
            }
            assert not (set(data.keys()) & forbidden)

