"""Service grants over HTTP, not just through the core functions.

An earlier suite called `create_grant` directly, which is how a whole layer of
request validation went unexercised: the API model and the list filter both
enumerate principal types independently of `core/v0_grants.py`, so widening the
core left the HTTP surface refusing the very grants §7.1 makes the required
path to Service access.

That is the same shape of gap twice now — a domain-level test passing while the
boundary rejects the row — so these assert on the wire.
"""

from __future__ import annotations

import os
import socket

import pytest
import pytest_asyncio

WS = "tcws_0000000000000001"
SERVICE = "tcsvc_0000000000000001"


def _postgres_reachable() -> bool:
    host = os.environ.get("POSTGRES_TEST_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_TEST_PORT", "5432"))
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="local Postgres is not reachable"
)


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    from agentdrive.api.v0_deps import v0_actor
    from agentdrive.app import app
    from agentdrive.identity.actor import V0ActorContext

    # A human manager: the caller §7.3 describes sharing a drive WITH a
    # Service Account. The grant's target is the Service; the caller is not.
    app.dependency_overrides[v0_actor] = lambda: V0ActorContext(
        subject="tcusr_0000000000000001",
        subject_type="user",
        workspace_id=WS,
        membership_id="tcmem_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(
            {"drives:read", "drives:write", "sharing:read", "sharing:write"}
        ),
        workspace_role="admin",
    )
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    from agentdrive.db import conn

    yield
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE"
        )


async def _drive(http) -> str:
    response = await http.post(
        "/v0/drives",
        json={"name": "svc-http"},
        headers={"Idempotency-Key": "svc-http-drive"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_a_service_grant_can_be_created_over_http(http):
    drive_id = await _drive(http)

    response = await http.post(
        f"/v0/drives/{drive_id}/grants",
        json={
            "principal_type": "service",
            "principal_id": SERVICE,
            "resource_type": "drive",
            "resource_id": drive_id,
            "role": "editor",
        },
        headers={"Idempotency-Key": "svc-http-grant"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["principal_type"] == "service"
    assert body["principal_id"] == SERVICE


async def test_a_service_grant_id_must_carry_the_tcsvc_prefix(http):
    """The per-type prefix check covers `service` too. A `service` grant naming
    an agent would record that agent's access under the wrong kind — inert
    against every token, and invisible to whoever granted it."""
    drive_id = await _drive(http)

    response = await http.post(
        f"/v0/drives/{drive_id}/grants",
        json={
            "principal_type": "service",
            "principal_id": "tcagt_0000000000000001",
            "resource_type": "drive",
            "resource_id": drive_id,
            "role": "editor",
        },
        headers={"Idempotency-Key": "svc-http-grant-bad"},
    )

    assert response.status_code == 422, response.text


async def test_grants_can_be_filtered_by_principal_type_service(http):
    drive_id = await _drive(http)
    await http.post(
        f"/v0/drives/{drive_id}/grants",
        json={
            "principal_type": "service",
            "principal_id": SERVICE,
            "resource_type": "drive",
            "resource_id": drive_id,
            "role": "viewer",
        },
        headers={"Idempotency-Key": "svc-http-filter"},
    )

    response = await http.get(
        f"/v0/drives/{drive_id}/grants?principal_type=service"
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["principal_id"] for item in items] == [SERVICE]


async def test_an_unknown_principal_type_filter_is_still_refused(http):
    drive_id = await _drive(http)

    response = await http.get(f"/v0/drives/{drive_id}/grants?principal_type=robot")

    assert response.status_code == 400, response.text
