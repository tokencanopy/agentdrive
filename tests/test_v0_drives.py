"""Drives vertical (slice 4): the 7 drive operations over real Postgres.

Exercises the composed app through httpx/ASGITransport with
``app.dependency_overrides[v0_actor]`` standing in for token verification;
auth rejections (no bearer) are tested against the real dependency.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
OTHER_AGENT = "tcagt_0000000000000002"
WS_A = "tcws_0000000000000001"
WS_B = "tcws_0000000000000002"


def make_actor(
    *,
    subject: str = AGENT,
    subject_type: str = "agent",
    workspace: str = WS_A,
    scopes: set[str] | None = None,
    sponsor: str | None = SPONSOR,
) -> V0ActorContext:
    scopes = scopes if scopes is not None else {"drives:read", "drives:write", "usage:read"}
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
        sponsor_id=sponsor if is_agent else None,
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
async def _clean_drive_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute(
            "TRUNCATE usage_operations, usage_windows, idempotency_records, "
            "workspace_storage, drives "
            "RESTART IDENTITY CASCADE"
        )


async def _create(http, name: str, key: str, **body_over) -> object:
    body = {"name": name}
    body.update(body_over)
    return await http.post("/v0/drives", json=body, headers={"Idempotency-Key": key})


async def _delete(http, drive_id: str, key: str, etag: str) -> object:
    return await http.request(
        "DELETE",
        f"/v0/drives/{drive_id}",
        headers={"Idempotency-Key": key, "If-Match": etag},
    )


# ---------------------------------------------------------------------------
# auth boundary
# ---------------------------------------------------------------------------


async def test_drive_ops_require_auth(http):
    resp = await http.get("/v0/drives")
    assert resp.status_code == 401
    body = resp.json()
    assert body == {"error": {"code": "AUTHENTICATION_REQUIRED", "message": "missing bearer token"}}
    assert "detail" not in body

    resp = await http.post("/v0/drives", json={"name": "x"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    resp = await http.get("/v0/drives/drv_00000000000000a1/usage")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_drive_returns_201_and_creates_root_and_grant(http, override_actor):
    override_actor(make_actor())
    resp = await _create(http, "primary", "key-create-1", metadata={"env": "dev"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "primary"
    assert body["workspace_id"] == WS_A
    assert body["metadata"] == {"env": "dev"}
    assert body["created_by"] == AGENT
    assert body["id"].startswith("drv_")
    assert body["revision"].startswith("rev_")
    assert body["root_folder_id"].startswith("fld_")
    assert body["storage_bytes"] == 0
    assert body["deleted_at"] is None
    assert resp.headers["location"].endswith(f"/v0/drives/{body['id']}")
    assert resp.headers["etag"] == f'"{body["revision"]}"'

    async with conn() as c:
        root = await c.fetchrow(
            "SELECT id, parent_id, name FROM folders "
            "WHERE drive_id=$1 AND parent_id IS NULL",
            body["id"],
        )
        assert root is not None
        assert root["id"] == body["root_folder_id"]
        assert root["name"] is None

        grants = await c.fetch(
            "SELECT principal_type, principal_id, role FROM grants "
            "WHERE drive_id=$1 AND resource_type='drive' AND resource_id=$1 "
            "AND revoked_at IS NULL",
            body["id"],
        )
        rows = {(g["principal_type"], g["principal_id"], g["role"]) for g in grants}
        assert ("agent", AGENT, "manager") in rows
        assert ("user", SPONSOR, "manager") in rows


async def test_create_drive_is_idempotent_under_a_key(http, override_actor):
    override_actor(make_actor())
    first = await _create(http, "idem", "key-same")
    second = await _create(http, "idem", "key-same")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    async with conn() as c:
        assert await c.fetchval("SELECT count(*) FROM drives") == 1


async def test_create_drive_rejects_key_reuse_for_different_request(http, override_actor):
    override_actor(make_actor())
    await _create(http, "one", "key-reuse")
    resp = await _create(http, "two", "key-reuse")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_create_drive_requires_idempotency_key(http, override_actor):
    override_actor(make_actor())
    resp = await http.post("/v0/drives", json={"name": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    async with conn() as c:
        assert await c.fetchval("SELECT count(*) FROM drives") == 0


async def test_create_drive_enforces_workspace_limit(http, override_actor, monkeypatch):
    from agentdrive.core import v0_drives

    monkeypatch.setattr(v0_drives, "MAX_DRIVES_PER_WORKSPACE", 2)
    override_actor(make_actor())
    for i in range(2):
        resp = await _create(http, f"drive-{i}", f"limit-key-{i}")
        assert resp.status_code == 201
    resp = await _create(http, "third", "limit-key-3")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DRIVE_LIMIT_EXCEEDED"
    async with conn() as c:
        assert await c.fetchval("SELECT count(*) FROM drives") == 2


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_drives_is_workspace_scoped(http, override_actor):
    override_actor(make_actor())
    await _create(http, "A1", "k-a1")
    await _create(http, "A2", "k-a2")

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp = await http.get("/v0/drives")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}

    override_actor(make_actor())
    resp = await http.get("/v0/drives")
    assert resp.status_code == 200
    body = resp.json()
    assert {i["name"] for i in body["items"]} == {"A1", "A2"}
    assert body["next_cursor"] is None


async def test_list_drives_paginates_with_keyset_cursor(http, override_actor):
    override_actor(make_actor())
    for i in range(3):
        await _create(http, f"page-{i}", f"kp-{i}")

    resp = await http.get("/v0/drives", params={"limit": 2})
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]

    page2 = await http.get("/v0/drives", params={"limit": 2, "cursor": body["next_cursor"]})
    body2 = page2.json()
    assert len(body2["items"]) == 1
    assert body2["next_cursor"] is None

    ids = [i["id"] for i in body["items"] + body2["items"]]
    assert len(set(ids)) == 3


async def test_list_drives_state_filter_exposes_deleted_for_restore(
    http, override_actor
):
    override_actor(make_actor())
    created = await _create(http, "life", "k-life")
    drive_id = created.json()["id"]
    await _delete(http, drive_id, "k-life-del", created.headers["etag"])

    active = await http.get("/v0/drives")
    assert active.json()["items"] == []

    deleted = await http.get("/v0/drives", params={"state": "deleted"})
    items = deleted.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == drive_id
    assert items[0]["deleted_at"] is not None


async def test_list_drives_rejects_unknown_query_params(http, override_actor):
    override_actor(make_actor())
    resp = await http.get("/v0/drives", params={"bogus": "1"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_QUERY"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


async def test_read_drive_etag_and_not_modified(http, override_actor):
    override_actor(make_actor())
    created = await _create(http, "read", "k-read")
    drive_id = created.json()["id"]
    etag = created.headers["etag"]

    resp = await http.get(f"/v0/drives/{drive_id}")
    assert resp.status_code == 200
    assert resp.headers["etag"] == etag
    assert resp.json()["id"] == drive_id

    not_mod = await http.get(f"/v0/drives/{drive_id}", headers={"If-None-Match": etag})
    assert not_mod.status_code == 304


async def test_read_drive_404_for_other_workspace_and_deleted(http, override_actor):
    override_actor(make_actor())
    created = await _create(http, "iso", "k-iso")
    drive_id = created.json()["id"]

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp = await http.get(f"/v0/drives/{drive_id}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DRIVE_NOT_FOUND"

    override_actor(make_actor())
    await _delete(http, drive_id, "k-iso-del", created.headers["etag"])
    resp = await http.get(f"/v0/drives/{drive_id}")
    assert resp.status_code == 404


async def test_drive_ops_enforce_token_scope(http, override_actor):
    override_actor(make_actor(scopes={"content:read"}))
    resp = await http.get("/v0/drives")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_malformed_drive_id_is_400(http, override_actor):
    override_actor(make_actor())
    resp = await http.get("/v0/drives/not-a-drive")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------


async def test_patch_drive_requires_and_validates_if_match(http, override_actor):
    override_actor(make_actor())
    created = await _create(http, "patchme", "k-patch")
    drive_id = created.json()["id"]
    etag = created.headers["etag"]

    no_match = await http.patch(
        f"/v0/drives/{drive_id}",
        json={"name": "renamed"},
        headers={"Idempotency-Key": "kp-1"},
    )
    assert no_match.status_code == 428
    assert no_match.json()["error"]["code"] == "PRECONDITION_REQUIRED"

    stale = await http.patch(
        f"/v0/drives/{drive_id}",
        json={"name": "renamed"},
        headers={"Idempotency-Key": "kp-2", "If-Match": '"rev_00000000000000ff"'},
    )
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "PRECONDITION_FAILED"

    # A key whose mutation never executed stays usable for the corrected retry.
    retried = await http.patch(
        f"/v0/drives/{drive_id}",
        json={"name": "retried"},
        headers={"Idempotency-Key": "kp-2", "If-Match": etag},
    )
    assert retried.status_code == 200
    assert retried.json()["name"] == "retried"

    ok = await http.patch(
        f"/v0/drives/{drive_id}",
        json={"name": "renamed", "metadata": {"k": "v"}},
        headers={"Idempotency-Key": "kp-3", "If-Match": retried.headers["etag"]},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["name"] == "renamed"
    assert body["metadata"] == {"k": "v"}
    assert body["created_by"] == AGENT  # immutable attribution survives a PATCH
    assert body["revision"] != created.json()["revision"]
    assert ok.headers["etag"] == f'"{body["revision"]}"'


async def test_patch_drive_requires_idempotency_key(http, override_actor):
    override_actor(make_actor())
    created = await _create(http, "p", "kp")
    drive_id = created.json()["id"]
    resp = await http.patch(
        f"/v0/drives/{drive_id}",
        json={"name": "x"},
        headers={"If-Match": created.headers["etag"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# ---------------------------------------------------------------------------
# delete / restore
# ---------------------------------------------------------------------------


async def test_delete_and_restore_drive_state(http, override_actor):
    override_actor(make_actor())
    created = await _create(http, "del", "kd")
    drive_id = created.json()["id"]

    resp = await _delete(http, drive_id, "kd-1", created.headers["etag"])
    assert resp.status_code == 200
    deleted = resp.json()
    assert deleted["deleted_at"] is not None
    assert deleted["revision"] != created.json()["revision"]

    async with conn() as c:
        row = await c.fetchrow("SELECT deleted_at FROM drives WHERE id=$1", drive_id)
    assert row["deleted_at"] is not None

    gone = await http.get(f"/v0/drives/{drive_id}")
    assert gone.status_code == 404

    # Restore: If-Match is the post-delete revision (the delete response ETag).
    restore = await http.post(
        f"/v0/drives/{drive_id}/restore",
        headers={"Idempotency-Key": "kd-2", "If-Match": resp.headers["etag"]},
    )
    assert restore.status_code == 200
    restored = restore.json()
    assert restored["deleted_at"] is None
    assert restored["revision"] != deleted["revision"]

    back = await http.get(f"/v0/drives/{drive_id}")
    assert back.status_code == 200

    # Restoring an already-active drive is a conflict, not a silent no-op.
    already = await http.post(
        f"/v0/drives/{drive_id}/restore",
        headers={"Idempotency-Key": "kd-3", "If-Match": restore.headers["etag"]},
    )
    assert already.status_code == 409
    assert already.json()["error"]["code"] == "CONFLICT"


async def test_delete_and_restore_require_preconditions_and_no_body(http, override_actor):
    override_actor(make_actor())
    created = await _create(http, "pre", "kp")
    drive_id = created.json()["id"]

    no_match = await _delete(http, drive_id, "kp-1", '"rev_00000000000000ff"')
    assert no_match.status_code == 412
    assert no_match.json()["error"]["code"] == "PRECONDITION_FAILED"

    body_reject = await http.request(
        "DELETE",
        f"/v0/drives/{drive_id}",
        json={"oops": True},
        headers={"Idempotency-Key": "kp-2", "If-Match": created.headers["etag"]},
    )
    assert body_reject.status_code == 400

    await _delete(http, drive_id, "kp-3", created.headers["etag"])
    restore_no_match = await http.post(
        f"/v0/drives/{drive_id}/restore", headers={"Idempotency-Key": "kp-4"}
    )
    assert restore_no_match.status_code == 428
    assert restore_no_match.json()["error"]["code"] == "PRECONDITION_REQUIRED"


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


async def test_usage_reports_byte_counters(http, override_actor):
    """`storage_bytes` reads the LOCKED authoritative counter maintained by
    the shared commit seam (B3 accounting, migration 0049) — version rows
    reach the database only through `v0_content_commit`, so the fixture
    commits through that seam rather than raw-INSERTing rows behind the
    counter's back."""
    from agentdrive.core import v0_content_commit as acct

    override_actor(make_actor())
    created = await _create(http, "usage", "ku")
    drive_id = created.json()["id"]

    async with conn() as c:
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ($1, $2, $3, $4, $5)",
            "art_00000000000000a1",
            drive_id,
            created.json()["root_folder_id"],
            "a.bin",
            "rev_00000000000000a1",
        )
        for version_id, checksum, size, ordinal in (
            ("ver_00000000000000a1", "sha256:1", 10, 1),
            ("ver_00000000000000a2", "sha256:2", 250, 2),
        ):
            async with c.transaction():
                rsv = await acct.reserve_version_bytes(
                    c, workspace_id=WS_A, drive_id=drive_id,
                    principal_id=AGENT, upload_id=None, size_bytes=size,
                )
                await acct.commit_immutable_version(
                    c,
                    acct.ImmutableVersionCommit(
                        drive_id=drive_id,
                        workspace_id=WS_A,
                        artifact_id="art_00000000000000a1",
                        version_id=version_id,
                        parent_version_id=None,
                        ordinal=ordinal,
                        checksum=checksum,
                        content_type="application/octet-stream",
                        size_bytes=size,
                        storage_object="cas/x",
                        storage_bucket=None,
                        storage_generation=None,
                        actor_type="agent",
                        actor_id=AGENT,
                        reservation_id=rsv,
                    ),
                )
        await c.execute("UPDATE drives SET retrieval_bytes = 7 WHERE id=$1", drive_id)
        await c.execute(
            "INSERT INTO storage_reservations "
            "(id, workspace_id, drive_id, principal_id, size_bytes) "
            "VALUES ('rsv_00000000000000a1', $1, $2, $3, 40)",
            WS_A,
            drive_id,
            AGENT,
        )
        await c.execute(
            "UPDATE workspace_storage SET reserved_bytes = 40 WHERE workspace_id = $1",
            WS_A,
        )
        await c.execute(
            "INSERT INTO usage_windows "
            "(metric, scope_type, scope_id, period, window_start, used, reserved) "
            "VALUES "
            "('download_bytes', 'workspace', $1, 'day', "
            " date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC', 7, 3), "
            "('download_bytes', 'workspace', $1, 'month', "
            " date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC', 17, 5)",
            WS_A,
        )

    resp = await http.get(f"/v0/drives/{drive_id}/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["storage_bytes"] == 260
    assert body["retrieval_bytes"] == 7
    assert body["meters"]["drive_storage"] == {
        "scope": "drive",
        "used": 260,
        "reserved": 40,
        "limit": 10 * 1024**3,
        "remaining": 10 * 1024**3 - 300,
        "reset_at": None,
    }
    assert body["meters"]["workspace_storage"] == {
        "scope": "workspace",
        "used": 260,
        "reserved": 40,
        "limit": 50 * 1024**3,
        "remaining": 50 * 1024**3 - 300,
        "reset_at": None,
    }
    assert body["meters"]["workspace_download_day"]["used"] == 7
    assert body["meters"]["workspace_download_day"]["reserved"] == 3
    assert body["meters"]["workspace_download_day"]["limit"] == 50 * 1024**3
    assert body["meters"]["workspace_download_day"]["reset_at"].endswith("Z")
    assert body["meters"]["workspace_download_month"]["used"] == 17
    assert body["meters"]["workspace_download_month"]["reserved"] == 5
    assert body["meters"]["workspace_download_month"]["limit"] == 250 * 1024**3
    assert body["meters"]["workspace_download_month"]["reset_at"].endswith("Z")
    assert body["effective_limits"] == {
        "max_file_bytes": 1024**3,
        "max_inline_file_bytes": 15 * 1024**2,
        "share_default_ttl_seconds": 7 * 24 * 3600,
        "share_max_ttl_seconds": 30 * 24 * 3600,
    }

    # counter/live-sum parity — the accounting design's standing assertion
    async with conn() as c:
        counter, live_sum = await acct.storage_bytes_parity(c, drive_id)
    assert counter == live_sum == 260


async def test_usage_404_outside_workspace(http, override_actor):
    override_actor(make_actor())
    created = await _create(http, "u2", "ku2")
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp = await http.get(f"/v0/drives/{created.json()['id']}/usage")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# concurrency: the drive row must be row-locked before If-Match is judged
# ---------------------------------------------------------------------------


async def test_drive_update_loses_no_concurrent_write(http, override_actor):
    """Gap-1 regression: two concurrent drive mutations must not silently
    last-writer-win.

    The drives vertical reads the row with a plain SELECT, checks If-Match in
    Python, then issues an unconditional ``UPDATE ... WHERE id``. Two writers
    who both read revision R both pass the precondition, and both UPDATE —
    the second clobbers the first, both get 200. The fix locks the drive row
    ``FOR UPDATE`` before the precondition, so the second writer's blocked
    read re-sees the committed revision and fails 412.

    Deterministic: a dedicated connection holds ``FOR UPDATE`` on the drive
    row (a concurrent writer mid-transaction), the PATCH blocks at its own
    row lock, the holder commits a revision bump + name change, and the
    PATCH must then fail 412 — not overwrite the holder's change.
    """
    override_actor(make_actor())
    created = await _create(http, "g1", "kg1")
    drive_id = created.json()["id"]
    etag = created.headers["etag"]  # revision R

    # A concurrent writer (T1) holds the drive row mid-write.
    async with conn() as c_holder, c_holder.transaction():
        await c_holder.execute(
            "SELECT id FROM drives WHERE id=$1 FOR UPDATE", drive_id
        )
        # The PATCH (T2) must block on the row lock T1 holds.
        task = asyncio.create_task(
            http.patch(
                f"/v0/drives/{drive_id}",
                json={"name": "patched-by-t2"},
                headers={"Idempotency-Key": "kg1-patch", "If-Match": etag},
            )
        )
        await asyncio.sleep(0.2)
        assert not task.done(), "PATCH must be blocked on the drive row lock"

        # T1 commits its write first (bumps revision + changes name).
        await c_holder.execute(
            "UPDATE drives SET name = $2, revision = $3 WHERE id = $1",
            drive_id, "committed-by-t1", "rev_ffffffffffffffff",
        )
        # T1's transaction commits here, releasing the row lock.
    resp = await task
    # T2's If-Match (rev R) is now stale; the write must NOT silently win.
    assert resp.status_code == 412, resp.text
    assert resp.json()["error"]["code"] == "PRECONDITION_FAILED"

    final = await http.get(f"/v0/drives/{drive_id}")
    assert final.json()["name"] == "committed-by-t1"


async def test_drive_delete_loses_no_concurrent_write(http, override_actor):
    """Gap-1 regression for DELETE: a stale If-Match must not let a delete
    clobber a concurrent write. The drive row lock serializes; the loser 412s."""
    override_actor(make_actor())
    created = await _create(http, "g2", "kg2")
    drive_id = created.json()["id"]
    etag = created.headers["etag"]

    async with conn() as c_holder, c_holder.transaction():
        await c_holder.execute(
            "SELECT id FROM drives WHERE id=$1 FOR UPDATE", drive_id
        )
        task = asyncio.create_task(
            http.request(
                "DELETE",
                f"/v0/drives/{drive_id}",
                headers={"Idempotency-Key": "kg2-del", "If-Match": etag},
            )
        )
        await asyncio.sleep(0.2)
        assert not task.done(), "DELETE must be blocked on the drive row lock"

        await c_holder.execute(
            "UPDATE drives SET name = $2, revision = $3 WHERE id = $1",
            drive_id, "still-live", "rev_ffffffffffffffff",
        )
    resp = await task
    assert resp.status_code == 412, resp.text
    assert resp.json()["error"]["code"] == "PRECONDITION_FAILED"

    final = await http.get(f"/v0/drives/{drive_id}")
    assert final.status_code == 200
    assert final.json()["name"] == "still-live"


async def _hold_drive_advisory(c, drive_id: str) -> None:
    """Take the drive-scoped advisory lock on an explicit transaction."""
    await c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"v0_drive_namespace:{drive_id}",
    )


async def test_drive_delete_waits_on_advisory_lock(http, override_actor):
    """Option-1 regression: `delete_drive` must take the drive advisory lock
    so it serializes with content writes (which all take it before mutating).

    Pre-fix, `delete_drive` takes no advisory lock — a delete can interleave
    with a content write's drive-liveness check. Deterministic: the holder
    owns the advisory, so a delete that takes it must block."""
    override_actor(make_actor())
    created = await _create(http, "da", "kda")
    drive_id = created.json()["id"]
    etag = created.headers["etag"]

    async with conn() as c_holder, c_holder.transaction():
        await _hold_drive_advisory(c_holder, drive_id)
        task = asyncio.create_task(
            http.request(
                "DELETE",
                f"/v0/drives/{drive_id}",
                headers={"Idempotency-Key": "kda-2", "If-Match": etag},
            )
        )
        await asyncio.sleep(0.15)
        assert not task.done(), "delete must be blocked on the drive advisory lock"
    resp = await task
    assert resp.status_code == 200


async def test_create_folder_does_not_land_in_deleted_drive(http, override_actor):
    """Gap-2-class regression on the drive boundary: a content write must not
    land in a drive that is soft-deleted concurrently.

    Pre-fix, `create_folder` checks drive liveness with a plain read BEFORE
    the advisory lock, and `delete_drive` takes no advisory — a delete can
    land between the liveness check and the INSERT, orphaning the folder.
    Post-fix, `delete_drive` takes the advisory AND `create_folder` checks
    liveness under the advisory, so the create re-sees the deleted drive and
    404s.

    Deterministic: the holder simulates the delete (it owns the advisory),
    the create blocks at the advisory, the holder soft-deletes the drive,
    then releases — the create must re-check.
    """
    override_actor(make_actor())
    created = await _create(http, "dl", "kdl")
    drive_id = created.json()["id"]

    # The folder create needs content:write (the drive create used drives scopes).
    override_actor(make_actor(scopes={"content:read", "content:write"}))

    body = {"parent_id": created.json()["root_folder_id"], "name": "orphan"}
    async with conn() as c_holder, c_holder.transaction():
        await _hold_drive_advisory(c_holder, drive_id)
        task = asyncio.create_task(
            http.post(
                f"/v0/drives/{drive_id}/folders",
                json=body,
                headers={"Idempotency-Key": "kdl-2"},
            )
        )
        await asyncio.sleep(0.15)
        assert not task.done(), "create must be blocked on the advisory lock"

        # Simulate delete_drive's effect: soft-delete the drive.
        await c_holder.execute(
            "UPDATE drives SET deleted_at = now(), revision = $2, updated_at = now() "
            "WHERE id = $1",
            drive_id, "rev_ffffffffffffffff",
        )
        # holder transaction commits here → advisory released, drive deleted
    resp = await task
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "DRIVE_NOT_FOUND"

    async with conn() as c:
        orphan = await c.fetchval(
            "SELECT count(*) FROM folders WHERE drive_id = $1 AND name = 'orphan'",
            drive_id,
        )
        assert orphan == 0, "a folder was created in a soft-deleted drive"


# ---------------------------------------------------------------------------
# list is grant-visibility-filtered (matches drives_read)
# ---------------------------------------------------------------------------


async def _grant_viewer(
    http, override_actor, drive_id: str, key: str, principal_id: str
) -> None:
    """As a drive manager (creator), grant `principal_id` viewer on the drive."""
    override_actor(make_actor(scopes={
        "drives:read", "drives:write", "usage:read", "sharing:read", "sharing:write",
    }))
    resp = await http.post(
        f"/v0/drives/{drive_id}/grants",
        json={
            "principal_type": "agent", "principal_id": principal_id,
            "resource_type": "drive", "resource_id": drive_id, "role": "viewer",
        },
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text


async def test_drives_list_hides_drives_without_a_grant(http, override_actor):
    """A same-workspace principal with drives:read but NO grant sees an empty
    list (currently returns every workspace drive)."""
    override_actor(make_actor())
    await _create(http, "vis-d1", "kvis-1")

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    resp = await http.get("/v0/drives")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_drives_list_shows_exactly_the_granted_drive(http, override_actor):
    """Granting viewer on ONE of two workspace drives shows exactly that one,
    and drives_read agrees (200 on it, 404 on the other)."""
    override_actor(make_actor())
    d1 = await _create(http, "vis-1", "kvis-2")
    await _create(http, "vis-2", "kvis-3")

    await _grant_viewer(http, override_actor, d1.json()["id"], "kvis-4", OTHER_AGENT)

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    listed = await http.get("/v0/drives")
    assert listed.status_code == 200
    names = [d["name"] for d in listed.json()["items"]]
    assert names == ["vis-1"]

    visible = await http.get(f"/v0/drives/{d1.json()['id']}")
    assert visible.status_code == 200

    hidden_id = await _second_drive_id(http, override_actor)
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    hidden = await http.get(f"/v0/drives/{hidden_id}")
    assert hidden.status_code == 404


async def _second_drive_id(http, override_actor) -> str:
    """The other drive's id (the one OTHER_AGENT was NOT granted)."""
    override_actor(make_actor())
    listed = await http.get("/v0/drives")
    for d in listed.json()["items"]:
        if d["name"] == "vis-2":
            return d["id"]
    raise AssertionError("vis-2 not found")


async def test_drives_list_state_filter_still_works_for_manager(http, override_actor):
    """A manager still discovers a soft-deleted drive via state=deleted."""
    override_actor(make_actor())
    created = await _create(http, "vis-del", "kvis-5")
    did = created.json()["id"]
    etag = created.headers["etag"]

    deleted = await _delete(http, did, "kvis-6", etag)
    assert deleted.status_code == 200

    listed = await http.get("/v0/drives", params={"state": "deleted"})
    assert listed.status_code == 200
    ids = [d["id"] for d in listed.json()["items"]]
    assert did in ids
