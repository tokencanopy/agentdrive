"""Fixtures shared by the conformance suites that drive the real surface.

Deliberately none of these are autouse: `test_isolation.py` and
`test_v0_smoke.py` own their own setup and cleanup, and a package-wide autouse
TRUNCATE would reach into them.

`enabled_transfer` and `fake_storage` are re-exported from the upload suite
rather than duplicated. Direct transfer is off by default and the five B3
controls answer `503 TRANSFER_DISABLED` before doing anything else, so without
them those operations cannot be exercised at all — and the upload suite already
owns a complete, boot-validated §9 policy plus the provider seam.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentdrive.api.v0_deps import v0_actor
from agentdrive.config import settings
from agentdrive.db import conn
from tests.conformance.harness import ALL_SCOPES, make_actor, seed_resources
from tests.test_v0_uploads import enabled_transfer, fake_storage  # noqa: F401

__all__ = [
    "bound_viewer",
    "enabled_transfer",
    "fake_storage",
    "http",
    "seeded",
    "set_actor",
]


@pytest.fixture
def bound_viewer(monkeypatch):
    """Bind a viewer host so `viewer_sessions_create` reaches its handler.

    Same shape and reason as `enabled_transfer`: the mint fails closed with
    `503 VIEWER_DISABLED` while `viewer_base_url` is empty, and a suite that
    stopped at that gate would prove nothing about the code paths behind it.
    """
    monkeypatch.setattr(settings, "viewer_base_url", "https://viewer.example.test")


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def set_actor(app_with_lifespan):
    def _set(scopes: frozenset[str], subject_type: str = "agent") -> None:
        app_with_lifespan.dependency_overrides[v0_actor] = lambda: make_actor(
            scopes, subject_type
        )

    yield _set
    app_with_lifespan.dependency_overrides.clear()


@pytest_asyncio.fixture(params=("agent", "user"))
async def seeded(request, http, set_actor) -> tuple[str, dict[str, str]]:
    """A seeded drive per subject type, torn down after the test.

    Parametrized over agent and human tokens because authorization conditioned
    on `actor.is_agent` would otherwise pass every case unnoticed.
    """
    subject_type: str = request.param
    set_actor(ALL_SCOPES, subject_type)
    made = await seed_resources(http, set_actor, subject_type)
    yield subject_type, made
    async with conn() as c:
        # `workspace_storage` too: the accounting row survives a `drives`
        # TRUNCATE and would leak committed bytes into later suites.
        await c.execute(
            "TRUNCATE idempotency_records, drives, workspace_storage RESTART IDENTITY CASCADE"
        )
