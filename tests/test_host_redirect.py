"""HostRedirectMiddleware behavior tests.

Pure ASGI-level tests — no DB, no auth, no fixtures. The middleware
is intentionally the outermost layer so traffic on legacy hosts never
touches downstream concerns; these tests pin that property by feeding
fake ASGI scopes directly and asserting on the response messages."""

import pytest

from agentdrive.middleware import HostRedirectMiddleware

# The hosted product's retiring alias family, as production configures it
# through LEGACY_HOSTS / LEGACY_REDIRECT_HOST. Literal here on purpose: the
# middleware no longer ships any host of its own.
CANONICAL_HOST = "agentdrive.run"
LEGACY_HOSTS = frozenset({"adrv.ai", "adrive.run", "agentdrive.mnexa.ai"})


async def _noop_app(scope, receive, send):
    """Inner app that records it was reached. Tests fail loudly if the
    middleware forwards a request that should have been redirected."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"forwarded"})


def _scope(host: str, path: str = "/", query: bytes = b""):
    return {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [(b"host", host.encode())],
    }


async def _capture(mw, scope):
    messages = []

    async def send(m):
        messages.append(m)

    async def receive():
        return {"type": "http.disconnect"}

    await mw(scope, receive, send)
    return messages


def _header(messages, name: bytes) -> bytes | None:
    start = next(m for m in messages if m["type"] == "http.response.start")
    for k, v in start["headers"]:
        if k == name:
            return v
    return None


@pytest.fixture
def mw():
    return HostRedirectMiddleware(
        _noop_app,
        canonical_host=CANONICAL_HOST,
        legacy_hosts=LEGACY_HOSTS,
    )


@pytest.mark.asyncio
async def test_canonical_host_passes_through(mw):
    messages = await _capture(mw, _scope("agentdrive.run"))
    assert messages[0]["status"] == 200
    assert messages[-1]["body"] == b"forwarded"


@pytest.mark.asyncio
async def test_api_subdomain_passes_through(mw):
    # `api.agentdrive.run` is a real serving host, not in LEGACY_HOSTS;
    # the middleware must not touch its traffic.
    messages = await _capture(mw, _scope("api.agentdrive.run", path="/v0/health"))
    assert messages[0]["status"] == 200


@pytest.mark.asyncio
async def test_localhost_passes_through(mw):
    # Dev / test environments hit localhost and must not get redirected.
    messages = await _capture(mw, _scope("localhost"))
    assert messages[0]["status"] == 200


@pytest.mark.parametrize("legacy", sorted(LEGACY_HOSTS))
@pytest.mark.asyncio
async def test_legacy_host_redirects_to_canonical(mw, legacy):
    messages = await _capture(mw, _scope(legacy, path="/dashboard"))
    assert messages[0]["status"] == 308
    assert _header(messages, b"location") == b"https://agentdrive.run/dashboard"


@pytest.mark.asyncio
async def test_redirect_preserves_path_and_query(mw):
    messages = await _capture(
        mw, _scope("adrv.ai", path="/v0/agents/x", query=b"foo=bar&baz=1")
    )
    assert messages[0]["status"] == 308
    assert (
        _header(messages, b"location")
        == b"https://agentdrive.run/v0/agents/x?foo=bar&baz=1"
    )


@pytest.mark.asyncio
async def test_redirect_uses_raw_path_when_set(mw):
    # Percent-encoded paths arrive on `raw_path`; we must forward them
    # byte-for-byte so e.g. drive IDs with `%` survive the redirect.
    scope = _scope("adrive.run", path="/drives/has space")
    scope["raw_path"] = b"/drives/has%20space"
    messages = await _capture(mw, scope)
    assert (
        _header(messages, b"location")
        == b"https://agentdrive.run/drives/has%20space"
    )


@pytest.mark.asyncio
async def test_host_header_with_port_still_matches(mw):
    scope = _scope("adrv.ai:443")
    messages = await _capture(mw, scope)
    assert messages[0]["status"] == 308


@pytest.mark.asyncio
async def test_host_header_case_insensitive(mw):
    scope = _scope("ADRV.AI")
    messages = await _capture(mw, scope)
    assert messages[0]["status"] == 308


@pytest.mark.asyncio
async def test_redirect_body_is_empty_with_zero_content_length(mw):
    """308 responses must carry an empty body + `Content-Length: 0`.
    Some HTTP clients reject a redirect with a non-empty body, and
    the cache-control header we set encourages CDNs to cache the
    response — caching a body would balloon storage for zero gain."""
    messages = await _capture(mw, _scope("adrv.ai"))
    body_msg = next(m for m in messages if m["type"] == "http.response.body")
    assert body_msg["body"] == b""
    assert body_msg.get("more_body", False) is False
    assert _header(messages, b"content-length") == b"0"


@pytest.mark.asyncio
async def test_lifespan_scope_passes_through(mw):
    # Non-http scopes (lifespan, websocket) must traverse untouched.
    received = []

    async def inner(scope, receive, send):
        received.append(scope["type"])

    mw2 = HostRedirectMiddleware(
        inner, canonical_host=CANONICAL_HOST, legacy_hosts=LEGACY_HOSTS
    )
    await mw2({"type": "lifespan"}, None, None)
    assert received == ["lifespan"]


@pytest.mark.asyncio
async def test_unconfigured_middleware_redirects_nothing(monkeypatch):
    """Constructed without hosts, the middleware reads the settings — and a
    standalone install configures none, so every Host passes through,
    including the ones the hosted product would redirect."""
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "legacy_hosts", "")
    monkeypatch.setattr(settings, "legacy_redirect_host", "")
    mw = HostRedirectMiddleware(_noop_app)
    for host in ("adrv.ai", "agentdrive.run", "localhost", "example.test"):
        messages = await _capture(mw, _scope(host))
        assert messages[0]["status"] == 200, host


@pytest.mark.asyncio
async def test_settings_driven_middleware_redirects_configured_aliases(monkeypatch):
    """The hosted configuration, expressed as settings rather than constants:
    the same 308, the same Location, resolved at request time so the
    environment — not the module — decides which hosts are aliases."""
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "legacy_hosts", "adrv.ai,ADRIVE.RUN")
    monkeypatch.setattr(settings, "legacy_redirect_host", "agentdrive.run")
    mw = HostRedirectMiddleware(_noop_app)
    messages = await _capture(mw, _scope("Adrive.Run", path="/v0/drives", query=b"limit=1"))
    assert messages[0]["status"] == 308
    assert _header(messages, b"location") == b"https://agentdrive.run/v0/drives?limit=1"
    # The target itself is never redirected, and an unlisted host is untouched.
    assert (await _capture(mw, _scope("agentdrive.run")))[0]["status"] == 200
    assert (await _capture(mw, _scope("agentdrive.mnexa.ai")))[0]["status"] == 200
