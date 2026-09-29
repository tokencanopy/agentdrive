"""The v0_auth boundary: honest 401 vs 503, JWKS lifecycle, audience split.

The dependency verifies Hub-issued bearers against Hub's published JWKS. That
document has a lifecycle: primed at startup (non-fatal), refreshed once on an
unknown ``kid`` (Hub rotation), rate-limited so a flood cannot stampede Hub,
and — while NO JWKS is available at all — the boundary answers 503
AUTH_UNAVAILABLE (OUR unavailability), not 401 (the caller's fault).

Reject paths run against the real dependency. The acceptance path (claims →
V0ActorContext) is exercised where the point is the boundary, not the
vertical (the vertical suites override ``v0_actor`` for that reason).
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentdrive.api import v0_deps
from agentdrive.api.v0_deps import _bearer_from, v0_actor
from agentdrive.api.v0_errors import V0ApiError
from agentdrive.config import settings


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _fresh_jwks_store():
    """Isolate JWKS state per test: the store is module-global (cached across
    requests by design), so a test that leaves a verifier behind would poison
    the next one's "never fetched" setup."""
    v0_deps.reset()
    yield
    v0_deps.reset()


def _rotated_doc(hub_jwks, rotated_hub_jwks) -> dict:
    """A JWKS after a rotation: both the retired and the new key published."""
    return {
        "keys": [
            hub_jwks.public_jwks["keys"][0],
            rotated_hub_jwks.public_jwks["keys"][0],
        ]
    }


def _derived_origin() -> str:
    """The origin the app itself advertises (v0_discovery's derivation)."""
    return (settings.api_base_url or settings.public_base_url).rstrip("/")


# ---------------------------------------------------------------------------
# Bearer extraction / missing credential
# ---------------------------------------------------------------------------


def test_bearer_from_extracts_only_bearer_tokens():
    assert _bearer_from({}) is None
    assert _bearer_from({"authorization": None}) is None
    assert _bearer_from({"authorization": "Basic xyz"}) is None
    assert _bearer_from({"authorization": "bearer abc.def.ghi"}) == "abc.def.ghi"
    assert _bearer_from({"authorization": "Bearer abc.def.ghi"}) == "abc.def.ghi"


@pytest.mark.asyncio
async def test_v0_actor_rejects_missing_bearer():
    with pytest.raises(V0ApiError) as excinfo:
        await v0_actor(None)
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "AUTHENTICATION_REQUIRED"
    assert excinfo.value.message == "missing bearer token"


@pytest.mark.asyncio
async def test_v0_actor_rejects_non_bearer_header(monkeypatch):
    """A non-bearer header is answered without ever touching the JWKS store —
    a missing credential needs no verification infrastructure."""

    def _no_store():
        raise AssertionError("JWKS store must not be touched for a non-bearer")

    monkeypatch.setattr(v0_deps, "_store", _no_store)
    with pytest.raises(V0ApiError) as excinfo:
        await v0_actor("Basic xyz")
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "AUTHENTICATION_REQUIRED"


@pytest.mark.asyncio
async def test_v0_actor_rejects_garbage_token(monkeypatch, hub_jwks):
    """A malformed token is the caller's fault: 401, once a JWKS is loaded."""
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store().prime()

    with pytest.raises(V0ApiError) as excinfo:
        await v0_actor("Bearer not.a.token")
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "AUTHENTICATION_REQUIRED"
    assert excinfo.value.message == "invalid token"


async def test_unauthenticated_401_carries_bearer_challenge(http):
    """RFC 6750 §3: a request with no credentials gets a challenge with NO
    error attribute, advertising where the RFC 9728 metadata lives."""
    resp = await http.get("/v0/drives")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    origin = _derived_origin()
    assert resp.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource"'
    )


async def test_invalid_token_401_carries_invalid_token_challenge(http, monkeypatch, hub_jwks):
    """RFC 6750 §3: a present-but-invalid token gets a challenge WITH
    error="invalid_token" plus the metadata advertisement."""
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store().prime()

    resp = await http.get(
        "/v0/drives", headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    origin = _derived_origin()
    header = resp.headers["www-authenticate"]
    assert 'error="invalid_token"' in header
    assert (
        f'resource_metadata="{origin}/.well-known/oauth-protected-resource"' in header
    )


# ---------------------------------------------------------------------------
# JWKS lifecycle: unknown-kid refresh (rotation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_start_primes_jwks_before_refresh_gate_opens(
    monkeypatch, hub_jwks, hub_token
):
    """The first fetch is never rate-limited, even during the first 30 seconds.

    ``time.monotonic()`` can be below the refresh-gate duration in a fresh
    Cloud Run process.  That uptime must not make the never-fetched sentinel
    look like a recent failed attempt.
    """
    monkeypatch.setattr(v0_deps.time, "monotonic", lambda: 5.0)
    calls = []

    def _fake(issuer):
        calls.append(issuer)
        return hub_jwks.public_jwks

    monkeypatch.setattr(v0_deps, "_fetch_jwks", _fake)

    await v0_deps.prime_jwks()
    actor = await v0_actor(f"Bearer {hub_token()}")

    assert actor.subject == "tcagt_0000000000000001"
    assert calls == [settings.hub_issuer]


@pytest.mark.asyncio
async def test_cold_request_fetches_jwks_without_startup_prime(
    monkeypatch, hub_jwks, hub_token
):
    """The request-path fallback also fetches during the first 30 seconds."""
    monkeypatch.setattr(v0_deps.time, "monotonic", lambda: 5.0)
    calls = []

    def _fake(issuer):
        calls.append(issuer)
        return hub_jwks.public_jwks

    monkeypatch.setattr(v0_deps, "_fetch_jwks", _fake)

    actor = await v0_actor(f"Bearer {hub_token()}")

    assert actor.subject == "tcagt_0000000000000001"
    assert calls == [settings.hub_issuer]


@pytest.mark.asyncio
async def test_failed_first_fetch_retries_when_refresh_gate_opens(
    monkeypatch, hub_jwks
):
    """A real failed attempt is gated until the exact 30-second boundary."""
    clock = {"now": 0.0}
    monkeypatch.setattr(v0_deps.time, "monotonic", lambda: clock["now"])
    calls = []

    def _fake(issuer):
        calls.append(issuer)
        if len(calls) == 1:
            raise OSError("hub unreachable")
        return hub_jwks.public_jwks

    monkeypatch.setattr(v0_deps, "_fetch_jwks", _fake)
    store = v0_deps._store()

    assert await store.refresh() is False
    assert calls == [settings.hub_issuer]

    clock["now"] = 29.999
    assert await store.refresh() is False
    assert calls == [settings.hub_issuer]

    clock["now"] = 30.0
    assert await store.refresh() is True
    assert calls == [settings.hub_issuer, settings.hub_issuer]
    assert store.verifier is not None


@pytest.mark.asyncio
async def test_unknown_kid_refreshes_once_and_verifies(
    monkeypatch, hub_jwks, rotated_hub_jwks, hub_claims
):
    """Hub rotates: a token signed by the NEW key is unknown at first, the
    boundary re-fetches exactly ONCE, and the retry verifies."""
    monkeypatch.setattr(v0_deps, "_JWKS_REFRESH_GATE_S", 0.0)
    calls = []

    def _fake(issuer):
        calls.append(issuer)
        if len(calls) == 1:
            return hub_jwks.public_jwks  # pre-rotation: only hub-key-1
        return _rotated_doc(hub_jwks, rotated_hub_jwks)

    monkeypatch.setattr(v0_deps, "_fetch_jwks", _fake)
    await v0_deps._store().prime()
    assert len(calls) == 1

    token = rotated_hub_jwks.sign(hub_claims())  # kid hub-key-2, not yet known
    actor = await v0_actor(f"Bearer {token}")
    assert actor.subject == "tcagt_0000000000000001"
    assert len(calls) == 2, "prime + exactly one refresh"


@pytest.mark.asyncio
async def test_unknown_kid_after_one_refresh_still_lacks_kid_is_401(
    monkeypatch, hub_jwks, rotated_hub_jwks, hub_claims
):
    """Refetch served the SAME document (the new key truly is unknown) →
    401, not 503 and not a refresh loop."""
    monkeypatch.setattr(v0_deps, "_JWKS_REFRESH_GATE_S", 0.0)
    calls = []

    def _fake(issuer):
        calls.append(issuer)
        return hub_jwks.public_jwks  # never advertises hub-key-2

    monkeypatch.setattr(v0_deps, "_fetch_jwks", _fake)
    await v0_deps._store().prime()

    token = rotated_hub_jwks.sign(hub_claims())
    with pytest.raises(V0ApiError) as excinfo:
        await v0_actor(f"Bearer {token}")
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "AUTHENTICATION_REQUIRED"
    assert excinfo.value.message == "invalid token"
    assert len(calls) == 2, "prime + one refresh, then stop — no loop"


@pytest.mark.asyncio
async def test_refresh_is_rate_limited_within_the_gate_window(
    monkeypatch, hub_jwks, rotated_hub_jwks, hub_claims
):
    """A flood of bad-kid tokens must not stampede Hub: two back-to-back
    requests trigger at most ONE refetch within the gate window (the second is
    served from the cached document and 401s)."""
    calls = []

    def _fake(issuer):
        calls.append(issuer)
        return hub_jwks.public_jwks

    monkeypatch.setattr(v0_deps, "_fetch_jwks", _fake)
    await v0_deps._store().prime()
    # Open the gate so the FIRST request's refresh actually fetches; the
    # second request then lands inside the (30s) window and must not.
    v0_deps._store()._last_fetch = 0.0

    token = rotated_hub_jwks.sign(hub_claims())
    for _ in range(2):
        with pytest.raises(V0ApiError) as excinfo:
            await v0_actor(f"Bearer {token}")
        assert excinfo.value.status_code == 401

    assert len(calls) == 2, "prime + exactly one refetch for two bad-kid requests"


# ---------------------------------------------------------------------------
# Honest 503: no JWKS available at all (Hub outage) + recovery
# ---------------------------------------------------------------------------


async def test_jwks_unavailable_is_503_then_recovers(
    http, monkeypatch, hub_jwks, hub_token
):
    """While the JWKS cannot be fetched, /v0 answers 503 AUTH_UNAVAILABLE
    with Retry-After (OUR unavailability, not the caller's fault) — and the
    boundary recovers without a restart once the fetch starts succeeding."""
    monkeypatch.setattr(v0_deps, "_JWKS_REFRESH_GATE_S", 0.0)
    state = {"hub_down": True}

    def _fake(issuer):
        if state["hub_down"]:
            raise OSError("hub unreachable")
        return hub_jwks.public_jwks

    monkeypatch.setattr(v0_deps, "_fetch_jwks", _fake)

    resp = await http.get("/v0/drives", headers={"Authorization": "Bearer whatever"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "AUTH_UNAVAILABLE"
    assert body["error"]["message"] == "token verification is temporarily unavailable"
    assert "detail" not in body
    assert resp.headers["retry-after"] == "30"

    # Hub recovers: the very next request re-fetches and the boundary works.
    state["hub_down"] = False
    ok = await http.get(
        "/v0/drives", headers={"Authorization": f"Bearer {hub_token()}"}
    )
    assert ok.status_code == 200, ok.text

    # A genuinely bad token is back to being the caller's fault.
    bad = await http.get("/v0/drives", headers={"Authorization": "Bearer not.a.jwt"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


# ---------------------------------------------------------------------------
# Audience split: the product verifier requires HUB_PRODUCT_AUDIENCE
# ---------------------------------------------------------------------------


async def test_product_audience_is_enforced(http, monkeypatch, hub_jwks, hub_claims):
    """A token minted with the archived sign-in client id as `aud` is rejected
    (401) under the new default; the contract audience verifies (200)."""
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store().prime()

    legacy = hub_jwks.sign(hub_claims(aud="agentdrive"))
    resp = await http.get("/v0/drives", headers={"Authorization": f"Bearer {legacy}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    product = hub_jwks.sign(hub_claims(aud=settings.hub_product_audience))
    ok = await http.get("/v0/drives", headers={"Authorization": f"Bearer {product}"})
    assert ok.status_code == 200, ok.text


async def test_public_v0_rejects_an_mcp_audience_token(
    http, monkeypatch, hub_jwks, hub_claims
):
    """The load-bearing half of the 2026-08-28 audience split.

    The MCP transport and public `/v0` shared one audience until then, so a
    token minted for a coding agent's MCP session was a fully valid product
    token: the reviewed bounded MCP surface was not a boundary, because any
    holder could call `/v0` directly and reach operations the MCP never
    exposes.

    Public `/v0` verifies the PRODUCT audience and nothing else. It does not
    accept both — accepting both is the defect.
    """
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store().prime()

    mcp = hub_jwks.sign(hub_claims(aud=settings.hub_mcp_audience))
    resp = await http.get("/v0/drives", headers={"Authorization": f"Bearer {mcp}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    # And an audience listing BOTH is refused too: a token good everywhere is
    # exactly what the split exists to prevent.
    both = hub_jwks.sign(
        hub_claims(aud=[settings.hub_product_audience, settings.hub_mcp_audience])
    )
    combined = await http.get(
        "/v0/drives", headers={"Authorization": f"Bearer {both}"}
    )
    assert combined.status_code == 401
