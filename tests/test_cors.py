"""CORS is what lets the console exist at all.

The console is served from `app.tokencanopy.com` and calls
`drive.tokencanopy.com/v0` — a different origin, so before it may send an
`Authorization` header the browser demands a preflight. Without this the
console cannot make a single call, and the failure surfaces only in the
browser's console: the server sees a well-formed OPTIONS it answers 405 to,
and nothing looks wrong from its side.

Fixture shape follows `test_host_surfaces.py`. `agentdrive.app.app` is built
at import time, when CORS_ALLOWED_ORIGINS is unset, so the instance wired
into it pins an empty allowlist and stays inert no matter what a test
patches afterwards. Re-wrapping the same app the way `app.py` wraps it is
how a test drives a CONFIGURED allowlist; `test_middleware_is_wired` covers
the wiring, and `test_cors_is_inert_when_unconfigured` covers the shipped
default through the real stack.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentdrive.api.cors import ScopedCORSMiddleware, cors_kwargs
from agentdrive.app import app
from agentdrive.config import settings

APP_ORIGIN = "https://app.tokencanopy.com"
STAGING_ORIGIN = "https://app.staging.tokencanopy.com"


@pytest.fixture
def allowed_origins(monkeypatch):
    monkeypatch.setattr(
        settings, "cors_allowed_origins", f"{APP_ORIGIN},{STAGING_ORIGIN}"
    )


@pytest_asyncio.fixture
async def cors_client(app_with_lifespan, allowed_origins):
    """A client over the app wrapped exactly as `app.py` wraps it."""
    wrapped = ScopedCORSMiddleware(app_with_lifespan)
    transport = ASGITransport(app=wrapped)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def plain_client(app_with_lifespan):
    """The real app exactly as it ships — no allowlist configured."""
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_preflight_succeeds_for_an_allowed_origin(cors_client):
    r = await cors_client.options(
        "/v0/drives",
        headers={
            "Origin": APP_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,idempotency-key",
        },
    )

    assert r.status_code in (200, 204), r.text
    assert r.headers["access-control-allow-origin"] == APP_ORIGIN
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed
    assert "idempotency-key" in allowed


async def test_both_configured_origins_are_allowed(cors_client):
    """Staging and prod consoles are separate origins; one allowlist serves
    both, and a single-origin implementation would pass the test above while
    breaking staging."""
    for origin in (APP_ORIGIN, STAGING_ORIGIN):
        r = await cors_client.options(
            "/v0/drives",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert r.headers.get("access-control-allow-origin") == origin, origin


async def test_the_mutation_headers_v0_requires_are_allowed(cors_client):
    """`Idempotency-Key` and `If-Match` are not CORS-safelisted.

    Every v0 mutation carries the first, and every mutation of existing state
    carries the second — so if they are missing from the allowlist the
    console can read but never write, and the preflight is where that fails.
    """
    r = await cors_client.options(
        "/v0/drives/drv_0000000000000001",
        headers={
            "Origin": APP_ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "authorization,if-match,idempotency-key",
        },
    )

    allowed = r.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "if-match", "idempotency-key"):
        assert header in allowed, header
    assert "PATCH" in r.headers["access-control-allow-methods"]


async def test_etag_is_exposed_or_every_if_match_flow_breaks(cors_client):
    """`ETag` is not a CORS-safelisted RESPONSE header.

    Without `Access-Control-Expose-Headers` the browser hides it from
    JavaScript even though the server sent it, so the console cannot read the
    value it must echo back in `If-Match` — and every update fails a
    precondition for a reason nothing in the UI can explain.
    """
    r = await cors_client.get("/v0/drives", headers={"Origin": APP_ORIGIN})

    exposed = r.headers.get("access-control-expose-headers", "").lower()
    assert "etag" in exposed


async def test_an_unlisted_origin_is_not_granted_access(cors_client):
    r = await cors_client.options(
        "/v0/drives",
        headers={
            "Origin": "https://evil.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


async def test_credentials_are_not_allowed(cors_client):
    """The console authenticates with a bearer token, not a cookie.

    Allowing credentials would widen the surface for no gain, and it is what
    makes a wildcard origin catastrophic rather than merely sloppy.
    """
    r = await cors_client.get("/v0/drives", headers={"Origin": APP_ORIGIN})

    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}


async def test_cors_is_inert_when_unconfigured(plain_client):
    """Default-deny: an unset allowlist grants nothing, rather than `*`.

    These endpoints take a bearer token, so a wildcard would let any page on
    the internet spend one it tricked a browser into attaching.
    """
    r = await plain_client.get("/v0/drives", headers={"Origin": APP_ORIGIN})

    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_no_wildcard_origin_is_reachable(monkeypatch):
    """A `*` in the setting must not become a wildcard allowlist."""
    monkeypatch.setattr(settings, "cors_allowed_origins", "*")

    assert "*" not in cors_kwargs()["allow_origins"]


def test_middleware_is_wired():
    """CORS ships in the real stack, not only in this module."""
    assert any(m.cls is ScopedCORSMiddleware for m in app.user_middleware)


@pytest.mark.parametrize(
    "path",
    ["/health", "/s/somekey", "/a/art_0000000000000000/", "/v1/agenttag/objects"],
)
async def test_cors_applies_to_v0_and_nowhere_else(cors_client, path):
    """Scoped, not global — and each of these is a distinct reason why.

    `/health` and `/v1/agenttag/*` have their own rules, and an app-wide
    policy answers their preflights before they can apply them; that is how
    this was caught, with a global install breaking an existing agenttag
    test. `/s/` and `/a/` are the public read surface: same-origin HTML that
    needs no CORS, where granting cross-origin reads would hand out a
    capability nobody asked for.
    """
    r = await cors_client.options(
        path,
        headers={"Origin": APP_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    lowered = {k.lower() for k in r.headers}
    assert "access-control-allow-origin" not in lowered, path
    assert "access-control-allow-methods" not in lowered, path
