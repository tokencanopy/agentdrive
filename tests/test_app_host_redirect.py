"""Full-stack integration check for HostRedirectMiddleware ordering.

The unit tests in `test_host_redirect.py` exercise the middleware in
isolation against a stub inner app. These tests construct the real
FastAPI app and prove (a) the middleware is actually wired in
`app.py` and (b) it runs BEFORE the request-context + rate-limit
middlewares — which is the whole point of installing it as the
outermost layer. The negative signal is the absence of side effects
from inner middlewares: no `X-Request-Id` minted, no rate-limit
headers stamped, no DB pool work attempted.
"""

from __future__ import annotations

import os
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def _hosted_alias_configuration():
    """The hosted product's alias family, as its Terraform configures it.
    Module-scoped: the app object is built once per process, so the
    middleware must resolve these at request time for this to work — which
    is exactly the property these tests pin beside the ordering one."""
    os.environ.setdefault("DATABASE_URL", "postgresql://x:y@localhost:5432/x")
    os.environ.setdefault("GCS_BUCKET", "x")
    os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
    from agentdrive.config import settings

    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "legacy_hosts", "adrv.ai,adrive.run,agentdrive.mnexa.ai")
    mp.setattr(settings, "legacy_redirect_host", "agentdrive.run")
    yield
    mp.undo()


@pytest.fixture(scope="module")
def legacy_host_client(_hosted_alias_configuration):
    """TestClient pinned to `http://adrv.ai` so every request carries
    `Host: adrv.ai` — the input that triggers HostRedirectMiddleware.
    Disables auto-follow so we can inspect the 308 directly.

    Lives outside `tests/identity/agent_auth/` so it doesn't depend on
    the agent-auth signer fixtures — we only exercise the redirect
    path, which never reaches the auth routes."""
    from agentdrive.app import app

    return TestClient(app, base_url="http://adrv.ai", follow_redirects=False)


@pytest.fixture(scope="module")
def canonical_host_client(_hosted_alias_configuration):
    """TestClient pinned to `http://agentdrive.run` — proves
    canonical traffic is left untouched by the redirect middleware
    so the rest of the app behaves normally."""
    from agentdrive.app import app

    return TestClient(app, base_url="http://agentdrive.run", follow_redirects=False)


def test_full_stack_redirects_legacy_host(legacy_host_client):
    """Smoke: middleware is wired and rewrites `adrv.ai` → canonical."""
    r = legacy_host_client.get("/dashboard")
    assert r.status_code == 308
    assert r.headers["location"] == "https://agentdrive.run/dashboard"


def test_redirect_preserves_query_through_full_stack(legacy_host_client):
    r = legacy_host_client.get("/v0/agents/x?foo=bar&baz=1")
    assert r.status_code == 308
    assert r.headers["location"] == "https://agentdrive.run/v0/agents/x?foo=bar&baz=1"


def test_redirect_runs_before_request_context_middleware(legacy_host_client):
    """Outermost-ordering guard: `RequestContextMiddleware` stamps
    `X-Request-Id` on every response it sees. If the host-redirect
    ever drifts to an inner position, this header will appear on the
    308 — and that's the signal that DB pool / quota / auth work
    started getting spent on traffic we should have shed immediately.
    """
    r = legacy_host_client.get("/health")
    assert r.status_code == 308
    assert "x-request-id" not in {k.lower() for k in r.headers}
    # Same negative check for rate-limit headers — proves
    # `RateLimitHeadersMiddleware` (innermost of the three) also
    # didn't see this request.
    assert not any(
        k.lower().startswith("x-ratelimit-") for k in r.headers
    )


def test_redirect_body_is_empty_through_full_stack(legacy_host_client):
    r = legacy_host_client.get("/anything")
    assert r.status_code == 308
    assert r.content == b""
    assert r.headers.get("content-length") == "0"


def test_canonical_host_is_not_redirected(canonical_host_client):
    """Counterfactual: same path, canonical host, no redirect.
    Confirms the middleware's gate is `host in LEGACY_HOSTS` and not
    something accidentally broader (e.g. "any host not localhost")."""
    r = canonical_host_client.get("/health")
    # /health may 503 in this stripped env (no DB pool), so we only
    # assert the absence of a 308 — anything else proves the redirect
    # short-circuit didn't fire.
    assert r.status_code != 308
    assert "location" not in {k.lower() for k in r.headers}
