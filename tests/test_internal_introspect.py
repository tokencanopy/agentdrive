"""`POST /_internal/introspect` — how the MCP sidecar authenticates an opaque
API key in a self-hosted install (§4.2 as amended 2026-09-21).

It is a LOCAL-MODE route and it is not mounted at all under `hub`: the
hosted sidecar verifies a Hub JWT itself and has no use for it. What makes
it safe is what makes the rest of the ingress safe — the per-boot proof gates
the port, and the answer is produced by the SAME `resolve_actor` the `/v0`
boundary runs, so there is no second authorization implementation to drift.
It authorizes nothing by itself: every `tools/call` still arrives at a `/v0`
route with the key and is resolved again.
"""

from __future__ import annotations

import argparse

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentdrive import keys as cli
from agentdrive.api import v0_deps
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.api_keys import generate_key
from agentdrive.identity.internal_proof import INTERNAL_PROOF_HEADER
from agentdrive.internal_ingress import build_internal_app

PROOF = "synthetic-per-boot-proof-0123456789abcdefXY"
INTROSPECT = "/_internal/introspect"


def _create_args(**over) -> argparse.Namespace:
    base = {
        "subject_type": "agent",
        "name": "claude-code",
        "workspace": "default",
        "scopes": "all",
        "expires": None,
        "role": None,
        "subject": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


@pytest_asyncio.fixture
async def local_ingress(app_with_lifespan, monkeypatch):
    """The loopback ingress built in local mode, over the suite's pool."""
    monkeypatch.setattr(settings, "auth_mode", "local")
    v0_deps.reset()
    async with conn() as c:
        await c.execute("DELETE FROM local_api_keys")
        await c.execute("DELETE FROM local_principals")
    app = build_internal_app(PROOF)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://ingress"
    ) as client:
        yield client
    v0_deps.reset()


async def test_a_live_key_introspects_to_its_actor(local_ingress):
    owner = (await cli._init("default", "owner"))["owner"]
    minted = await cli._create(_create_args(scopes="drives:read,content:read"))
    response = await local_ingress.post(
        INTROSPECT,
        headers={
            INTERNAL_PROOF_HEADER: PROOF,
            "Authorization": f"Bearer {minted['key']}",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "subject": minted["subject"],
        "principal_type": "agent",
        "workspace_id": "default",
        "scopes": ["content:read", "drives:read"],
        "workspace_role": None,
        "sponsor_id": owner,
        "key_id": minted["id"],
    }


async def test_a_user_key_carries_its_role_and_no_sponsor(local_ingress):
    await cli._init("default", "owner")
    minted = await cli._create(_create_args(subject_type="user", name="me", role="owner"))
    body = (
        await local_ingress.post(
            INTROSPECT,
            headers={
                INTERNAL_PROOF_HEADER: PROOF,
                "Authorization": f"Bearer {minted['key']}",
            },
        )
    ).json()
    assert body["principal_type"] == "user"
    assert body["workspace_role"] == "owner"
    assert body["sponsor_id"] is None


@pytest.mark.parametrize(
    "bearer",
    ["missing", "unknown", "malformed", "not-a-bearer"],
    ids=["missing", "unknown", "malformed", "wrong-scheme"],
)
async def test_a_bearer_that_is_not_a_live_key_is_401(local_ingress, bearer):
    await cli._init("default", "owner")
    headers = {INTERNAL_PROOF_HEADER: PROOF}
    if bearer == "unknown":
        headers["Authorization"] = f"Bearer {generate_key()}"
    elif bearer == "malformed":
        headers["Authorization"] = "Bearer adk_short"
    elif bearer == "not-a-bearer":
        headers["Authorization"] = f"Token {generate_key()}"
    response = await local_ingress.post(INTROSPECT, headers=headers)
    assert response.status_code == 401, response.text
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_a_revoked_key_stops_introspecting(local_ingress):
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())
    headers = {INTERNAL_PROOF_HEADER: PROOF, "Authorization": f"Bearer {minted['key']}"}
    assert (await local_ingress.post(INTROSPECT, headers=headers)).status_code == 200
    await cli._revoke(minted["id"])
    assert (await local_ingress.post(INTROSPECT, headers=headers)).status_code == 401


async def test_without_the_boot_proof_the_route_is_a_404(local_ingress):
    """The same answer a closed route gives. A process that merely reached
    the loopback port learns nothing about what is behind it — and in
    particular does not learn that a key it holds is valid."""
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())
    for proof in ({}, {INTERNAL_PROOF_HEADER: "wrong-" + PROOF[6:]}):
        response = await local_ingress.post(
            INTROSPECT, headers={**proof, "Authorization": f"Bearer {minted['key']}"}
        )
        assert response.status_code == 404, response.text
        assert "WWW-Authenticate" not in response.headers


async def test_a_non_ascii_proof_header_is_the_same_404_not_a_500(local_ingress):
    """`hmac.compare_digest` raises TypeError on a code point above U+00FF,
    and a header is caller-controlled bytes decoded as latin-1. A 500 here
    would be both an unhandled error on an auth path and a signal that
    something IS behind this port."""
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())
    response = await local_ingress.post(
        INTROSPECT,
        # Raw latin-1 bytes, which is what reaches ASGI: 0xFF decodes to
        # U+00FF, and `compare_digest` refuses any str that is not ASCII.
        headers={
            INTERNAL_PROOF_HEADER: b"\xff" * 43,
            "Authorization": f"Bearer {minted['key']}",
        },
    )
    assert response.status_code == 404, response.text


async def test_a_database_outage_during_introspection_is_a_503(local_ingress, monkeypatch):
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())

    class _Down:
        async def __aenter__(self):
            raise ConnectionError("pool gone")

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("agentdrive.db.conn", lambda: _Down())
    response = await local_ingress.post(
        INTROSPECT,
        headers={INTERNAL_PROOF_HEADER: PROOF, "Authorization": f"Bearer {minted['key']}"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTH_UNAVAILABLE"
    assert response.headers["Retry-After"]


async def test_the_route_does_not_exist_under_hub_mode(app_with_lifespan):
    """Hosted has no use for it, so it is not mounted — not mounted and then
    refused, but absent from the route table."""
    assert settings.auth_mode == "hub"
    app = build_internal_app(PROOF)
    assert INTROSPECT not in {route.path for route in app.routes}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://ingress"
    ) as client:
        response = await client.post(
            INTROSPECT,
            headers={INTERNAL_PROOF_HEADER: PROOF, "Authorization": f"Bearer {generate_key()}"},
        )
    assert response.status_code == 404
