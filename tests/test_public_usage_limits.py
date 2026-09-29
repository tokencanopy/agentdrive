from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agentdrive import storage
from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext
from agentdrive.public import routes as public_routes

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000101"
WORKSPACE = "tcws_0000000000000101"


def make_actor() -> V0ActorContext:
    return V0ActorContext(
        subject=AGENT,
        subject_type="agent",
        workspace_id=WORKSPACE,
        membership_id="tcagm_0000000000000101",
        token_id="tctok_0000000000000101",
        scopes=frozenset({
            "drives:read",
            "drives:write",
            "usage:read",
            "content:read",
            "content:write",
            "sharing:read",
            "sharing:write",
        }),
        credential_id="tccred_0000000000000101",
        runtime_id="tcrun_0000000000000101",
        sponsor_id="tcusr_0000000000000101",
        workspace_role=None,
    )


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    app.dependency_overrides[v0_actor] = make_actor
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def clean_usage(app_with_lifespan):
    async with conn() as connection:
        await connection.execute(
            "TRUNCATE usage_operations, usage_windows, idempotency_records, "
            "drives RESTART IDENTITY CASCADE"
        )
    yield
    async with conn() as connection:
        await connection.execute(
            "TRUNCATE usage_operations, usage_windows, idempotency_records, "
            "drives RESTART IDENTITY CASCADE"
        )


async def create_drive(http, key: str = "usage-drive") -> dict:
    response = await http.post(
        "/v0/drives",
        json={"name": "Public limits"},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def multipart(parent_id: str, body: bytes) -> bytes:
    return (
        b'--b\r\nContent-Disposition: form-data; name="parent_id"\r\n\r\n'
        + parent_id.encode()
        + b'\r\n--b\r\nContent-Disposition: form-data; name="name"\r\n\r\n'
        + b"public.bin\r\n--b\r\nContent-Disposition: form-data; name="
        + b'"content"; filename="public.bin"\r\nContent-Type: '
        + b"application/octet-stream\r\n\r\n"
        + body
        + b"\r\n--b--\r\n"
    )


async def create_artifact(http, drive: dict, body: bytes) -> dict:
    response = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=multipart(drive["root_folder_id"], body),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": "usage-artifact",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_share(http, drive_id: str, resource_type: str, resource_id: str):
    return await http.post(
        f"/v0/drives/{drive_id}/shares",
        json={"resource_type": resource_type, "resource_id": resource_id},
        headers={"Idempotency-Key": f"usage-share-{resource_type}"},
    )


async def test_omitted_expiry_defaults_to_seven_days(http):
    drive = await create_drive(http)
    before = datetime.now(UTC)
    response = await create_share(
        http, drive["id"], "folder", drive["root_folder_id"]
    )
    after = datetime.now(UTC)

    assert response.status_code == 201, response.text
    expires_at = datetime.fromisoformat(response.json()["expires_at"].replace("Z", "+00:00"))
    assert before + timedelta(days=7) <= expires_at <= after + timedelta(days=7)


async def test_share_expiry_cannot_exceed_thirty_days(http):
    drive = await create_drive(http)
    response = await http.post(
        f"/v0/drives/{drive['id']}/shares",
        json={
            "resource_type": "folder",
            "resource_id": drive["root_folder_id"],
            "expires_at": (datetime.now(UTC) + timedelta(days=31)).isoformat(),
        },
        headers={"Idempotency-Key": "usage-share-long"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_public_range_commits_only_yielded_bytes(http, monkeypatch):
    monkeypatch.setattr(settings, "public_usage_limit_mode", "enforce")
    drive = await create_drive(http)
    artifact = await create_artifact(http, drive, b"0123456789abcdefghij")
    share = await create_share(http, drive["id"], "artifact", artifact["id"])
    assert share.status_code == 201, share.text

    response = await http.get(
        f"/s/{share.json()['secret']}/content",
        headers={"Accept": "application/octet-stream", "Range": "bytes=3-7"},
    )

    assert response.status_code == 206, response.text
    assert response.content == b"34567"
    assert "location" not in response.headers
    assert response.headers["content-range"] == "bytes 3-7/20"
    async with conn() as connection:
        used = await connection.fetchval(
            "SELECT used FROM usage_windows WHERE metric='public_bytes' "
            "AND scope_type='share' AND scope_id=$1",
            share.json()["id"],
        )
        retrieval = await connection.fetchval(
            "SELECT retrieval_bytes FROM drives WHERE id=$1", drive["id"]
        )
        dimensions = await connection.fetch(
            "SELECT scope_id FROM usage_windows WHERE metric='requests'"
        )
    assert used == 5
    assert retrieval == 5
    assert all("127.0.0.1" not in row["scope_id"] for row in dimensions)
    assert all(share.json()["secret"] not in row["scope_id"] for row in dimensions)


async def test_unknown_probe_writes_no_usage_row(http, monkeypatch):
    monkeypatch.setattr(settings, "public_usage_limit_mode", "enforce")
    response = await http.get("/s/not-a-real-secret/", headers={"Accept": "text/html"})
    assert response.status_code == 404
    async with conn() as connection:
        count = await connection.fetchval("SELECT count(*) FROM usage_operations")
    assert count == 0


async def align_to_ten_second_window(min_remaining: float) -> None:
    """Wait out a window boundary the burst that follows cannot survive.

    `Period.TEN_SECONDS` windows are wall-clock aligned — `window_bounds`
    floors the current second to a multiple of ten — so they tumble rather than
    slide. A burst that straddles a boundary is counted against two windows,
    the fresh one admits the request the assertion expects to be refused, and
    the failure reads `assert 200 == 429`. The database clock is the authority
    because the meter windows are computed from it, not from this process.
    """
    async with conn() as connection:
        now = await connection.fetchval("SELECT clock_timestamp()")
    remaining = 10 - (now.timestamp() % 10)
    if remaining < min_remaining:
        await asyncio.sleep(remaining + 0.1)


async def test_share_request_limit_is_distributed_and_post_resolution(http, monkeypatch):
    monkeypatch.setattr(settings, "public_usage_limit_mode", "enforce")
    drive = await create_drive(http)
    share = await create_share(http, drive["id"], "folder", drive["root_folder_id"])
    assert share.status_code == 201, share.text
    url = f"/s/{share.json()['secret']}/"
    await align_to_ten_second_window(3)

    for _ in range(20):
        response = await http.get(url, headers={"Accept": "application/json"})
        assert response.status_code == 200, response.text
    refused = await http.get(url, headers={"Accept": "application/json"})

    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    assert int(refused.headers["retry-after"]) >= 1


async def test_hot_share_concurrency_admits_only_the_distributed_ceiling(
    http, monkeypatch,
):
    monkeypatch.setattr(settings, "public_usage_limit_mode", "enforce")
    drive = await create_drive(http)
    share = await create_share(http, drive["id"], "folder", drive["root_folder_id"])
    assert share.status_code == 201, share.text
    url = f"/s/{share.json()['secret']}/"

    await align_to_ten_second_window(5)

    responses = await asyncio.gather(
        *(
            http.get(url, headers={"Accept": "application/json"})
            for _ in range(40)
        )
    )

    assert sum(response.status_code == 200 for response in responses) == 20
    refused = [response for response in responses if response.status_code == 429]
    assert len(refused) == 20
    assert all(response.headers.get("retry-after") for response in refused)


async def test_share_bandwidth_refuses_before_storage(http, monkeypatch):
    monkeypatch.setattr(settings, "public_usage_limit_mode", "enforce")
    drive = await create_drive(http)
    artifact = await create_artifact(http, drive, b"0123456789")
    share = await create_share(http, drive["id"], "artifact", artifact["id"])
    assert share.status_code == 201, share.text
    async with conn() as connection:
        await connection.execute(
            "UPDATE shares SET daily_byte_limit=5 WHERE id=$1", share.json()["id"]
        )

    response = await http.get(
        f"/s/{share.json()['secret']}/content",
        headers={"Accept": "application/octet-stream"},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "BANDWIDTH_LIMIT_EXCEEDED"
    assert int(response.headers["retry-after"]) >= 1


async def test_public_stream_holds_no_database_connection(
    http, monkeypatch,
):
    monkeypatch.setattr(settings, "public_usage_limit_mode", "enforce")
    drive = await create_drive(http)
    artifact = await create_artifact(http, drive, b"streamed")
    share = await create_share(http, drive["id"], "artifact", artifact["id"])
    active_connections = 0
    streamed_with_connection = False
    real_conn = public_routes.conn

    @asynccontextmanager
    async def traced_conn():
        nonlocal active_connections
        async with real_conn() as connection:
            active_connections += 1
            try:
                yield connection
            finally:
                active_connections -= 1

    async def stream(*_args, **_kwargs):
        nonlocal streamed_with_connection
        streamed_with_connection = active_connections > 0
        yield b"streamed"

    monkeypatch.setattr(public_routes, "conn", traced_conn)
    monkeypatch.setattr(storage, "stream", stream)

    response = await http.get(
        f"/s/{share.json()['secret']}/content",
        headers={"Accept": "application/octet-stream"},
    )

    assert response.status_code == 200
    assert response.content == b"streamed"
    assert streamed_with_connection is False


async def test_public_finalize_failure_leaves_conservative_reservation(
    http, monkeypatch,
):
    monkeypatch.setattr(settings, "public_usage_limit_mode", "enforce")
    drive = await create_drive(http)
    artifact = await create_artifact(http, drive, b"conservative")
    share = await create_share(http, drive["id"], "artifact", artifact["id"])

    async def fail_commit(*_args, **_kwargs):
        raise RuntimeError("synthetic finalization failure")

    monkeypatch.setattr(public_routes.usage_gate.meter, "commit", fail_commit)
    response = await http.get(
        f"/s/{share.json()['secret']}/content",
        headers={"Accept": "application/octet-stream"},
    )

    assert response.status_code == 200
    assert response.content == b"conservative"
    async with conn() as connection:
        state = await connection.fetchval(
            "SELECT state FROM usage_operations WHERE metric='public_bytes'"
        )
    assert state == "reserved"
