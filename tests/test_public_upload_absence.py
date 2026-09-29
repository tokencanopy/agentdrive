from __future__ import annotations

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio


def read_only_actor() -> V0ActorContext:
    return V0ActorContext(
        subject="tcagt_0000000000000201",
        subject_type="agent",
        workspace_id="tcws_0000000000000201",
        membership_id="tcagm_0000000000000201",
        token_id="tctok_0000000000000201",
        scopes=frozenset({"content:read"}),
        credential_id="tccred_0000000000000201",
        runtime_id="tcrun_0000000000000201",
        sponsor_id="tcusr_0000000000000201",
        workspace_role=None,
    )


@pytest_asyncio.fixture
async def http(app_with_lifespan, monkeypatch, hub_jwks):
    from httpx import ASGITransport, AsyncClient

    from agentdrive.api import v0_deps

    monkeypatch.setattr(settings, "direct_transfer_enabled", True)
    v0_deps.reset()
    monkeypatch.setattr(
        v0_deps, "_fetch_jwks", lambda _issuer: hub_jwks.public_jwks
    )
    await v0_deps.prime_jwks()
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def clean_state(app_with_lifespan):
    async with conn() as connection:
        await connection.execute(
            "TRUNCATE usage_operations, usage_windows, upload_sessions, "
            "storage_reservations, idempotency_records, drives RESTART IDENTITY CASCADE"
        )
    yield


async def assert_no_upload_state() -> None:
    async with conn() as connection:
        counts = await connection.fetchrow(
            "SELECT (SELECT count(*) FROM upload_sessions) AS uploads, "
            "(SELECT count(*) FROM storage_reservations) AS reservations, "
            "(SELECT count(*) FROM usage_operations) AS operations"
        )
    assert dict(counts) == {"uploads": 0, "reservations": 0, "operations": 0}


async def test_anonymous_and_share_credentials_cannot_begin_uploads(http):
    path = "/v0/drives/drv_0000000000000201/uploads"
    for headers in (
        {},
        {"Authorization": "Bearer synthetic-share-secret"},
    ):
        response = await http.post(path, headers=headers, json={})
        assert response.status_code == 401
    await assert_no_upload_state()


async def test_read_only_product_token_cannot_begin_upload(http):
    app.dependency_overrides[v0_actor] = read_only_actor
    response = await http.post(
        "/v0/drives/drv_0000000000000201/uploads",
        headers={"Idempotency-Key": "read-only-upload"},
        json={},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    await assert_no_upload_state()


async def test_public_routes_expose_no_upload_operation(http):
    for path in (
        "/s/synthetic-share-secret/uploads",
        "/a/art_0000000000000201/uploads",
        "/v/art_0000000000000201/ver_0000000000000201/uploads",
    ):
        response = await http.post(path, json={})
        assert response.status_code == 404

    schema = app.openapi()
    upload_operations = [
        operation
        for path, methods in schema["paths"].items()
        if "/uploads" in path
        for operation in methods.values()
    ]
    assert upload_operations
    assert all(operation.get("security") for operation in upload_operations)
    await assert_no_upload_state()
