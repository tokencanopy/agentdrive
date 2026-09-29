"""Per-principal rate limit on the /v0 surface.

The dependency (`enforce_v0_rate_limit`) is wired onto every v0 router and
returns 429 RATE_LIMITED in the top-level envelope once a principal exceeds
the per-minute budget. This test drives the dependency directly (deterministic
low limit, fresh in-memory bucket) rather than through the real 600/min
ceiling, which no test should trip.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from limits import parse_many
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from agentdrive.api import v0_rate_limit
from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
WS_A = "tcws_0000000000000001"


def make_actor() -> V0ActorContext:
    return V0ActorContext(
        subject=AGENT,
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({"drives:read", "drives:write", "usage:read"}),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id="tcusr_0000000000000009",
        workspace_role=None,
    )


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


@pytest_asyncio.fixture
def low_limit(monkeypatch):
    """A deterministic 3/minute ceiling on a fresh in-memory bucket, isolated
    from the module-global storage and the settings-derived limit cache."""
    monkeypatch.setattr(v0_rate_limit, "_storage", MemoryStorage())
    monkeypatch.setattr(
        v0_rate_limit, "_strategy", FixedWindowRateLimiter(v0_rate_limit._storage)
    )
    monkeypatch.setattr(v0_rate_limit, "_ITEMS", None)
    monkeypatch.setattr(v0_rate_limit, "_limits", lambda: list(parse_many("3/minute")))
    yield


@pytest_asyncio.fixture
def disabled(monkeypatch):
    monkeypatch.setattr(settings, "v0_rate_limit_enabled", False)
    yield


def _auth(actor=None):
    app.dependency_overrides[v0_actor] = lambda: actor or make_actor()


async def test_rate_limit_429_after_budget(http, low_limit):
    """Over-budget principal gets 429 in the top-level envelope with a
    Retry-After; the first requests under budget pass."""
    _auth(make_actor())
    try:
        for _ in range(3):
            resp = await http.get("/v0/drives")
            assert resp.status_code == 200, resp.text
        resp = await http.get("/v0/drives")
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == "RATE_LIMITED"
        assert "detail" not in body
        assert int(resp.headers["retry-after"]) > 0
    finally:
        app.dependency_overrides.pop(v0_actor, None)


async def test_rate_limit_keys_by_principal(http, low_limit):
    """The key is the hashed bearer, so one principal's quota is not shared
    with another — principal B stays under budget after A exhausts its own."""
    _auth(make_actor())
    try:
        for _ in range(3):
            resp = await http.get(
                "/v0/drives", headers={"Authorization": "Bearer tok-agent-a"}
            )
            assert resp.status_code == 200, resp.text
        # Principal A is now at its budget; a different principal passes.
        resp_b = await http.get(
            "/v0/drives", headers={"Authorization": "Bearer tok-agent-b"}
        )
        assert resp_b.status_code == 200
        # Principal A trips on its own next request.
        resp_a = await http.get(
            "/v0/drives", headers={"Authorization": "Bearer tok-agent-a"}
        )
        assert resp_a.status_code == 429
    finally:
        app.dependency_overrides.pop(v0_actor, None)


async def test_rate_limit_can_be_disabled(http, disabled):
    """With the kill switch off, requests pass even past a low budget."""
    _auth(make_actor())
    try:
        for _ in range(5):
            resp = await http.get("/v0/drives")
            assert resp.status_code != 429
    finally:
        app.dependency_overrides.pop(v0_actor, None)
