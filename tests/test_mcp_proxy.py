"""HTTP boundary tests for the hosted MCP sidecar proxy."""

import json

import httpx
import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.mcp_proxy import MAX_MCP_REQUEST_BYTES, MCP_ORIGIN_HEADER, proxy_mcp


class _UpstreamResponse:
    status_code = 401
    headers = {
        "content-type": "application/json",
        "www-authenticate": 'Bearer resource_metadata="https://drive.tokencanopy.com/.well-known/oauth-protected-resource"',
        "cache-control": "no-store",
        "content-length": "16",
    }

    async def aiter_bytes(self):
        yield b'{"error":"missing"}'


class _UpstreamStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_exc):
        return None


class _FakeAsyncClient:
    request: tuple[str, str, dict[str, str], bytes] | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    def stream(self, method, url, *, headers, content):
        self.request = (method, url, headers, content)
        type(self).request = self.request
        return _UpstreamStream(_UpstreamResponse())


@pytest.mark.asyncio
async def test_mcp_is_not_claimed_when_the_sidecar_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "mcp_proxy_url", "")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/mcp", content=b"{}")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "NOT_FOUND", "message": "not found"}}


@pytest.mark.asyncio
async def test_mcp_forwards_only_protocol_headers_and_preserves_auth_challenge(monkeypatch):
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    monkeypatch.setattr("agentdrive.mcp_proxy.httpx.AsyncClient", _FakeAsyncClient)
    transport = ASGITransport(app=app)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/mcp?session=ignored",
            json=payload,
            headers={
                "Authorization": "Bearer synthetic-token",
                "Accept": "application/json",
                "MCP-Protocol-Version": "2025-11-25",
                "Cookie": "session=must-not-forward",
                "X-Forwarded-For": "198.51.100.10",
                "X-Request-ID": "request.test",
            },
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer ")
    assert response.json() == {"error": "missing"}
    method, url, headers, body = _FakeAsyncClient.request
    assert method == "POST"
    assert url == "http://127.0.0.1:8081/mcp?session=ignored"
    assert headers == {
        "accept": "application/json",
        "authorization": "Bearer synthetic-token",
        "content-type": "application/json",
        "mcp-protocol-version": "2025-11-25",
        "x-request-id": "request.test",
    }
    assert json.loads(body) == payload


@pytest.mark.asyncio
async def test_mcp_returns_retryable_503_when_the_sidecar_is_unavailable(monkeypatch):
    class _UnavailableClient(_FakeAsyncClient):
        def stream(self, *_args, **_kwargs):
            raise httpx.ConnectError("synthetic sidecar outage")

    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    monkeypatch.setattr("agentdrive.mcp_proxy.httpx.AsyncClient", _UnavailableClient)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/mcp")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "error": {"code": "MCP_UNAVAILABLE", "message": "MCP service unavailable"}
    }


@pytest.mark.asyncio
async def test_mcp_request_body_limit_matches_the_node_transport(monkeypatch):
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    monkeypatch.setattr("agentdrive.mcp_proxy.httpx.AsyncClient", _FakeAsyncClient)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/mcp", content=b"x" * (MAX_MCP_REQUEST_BYTES + 1))

    assert response.status_code == 413
    assert response.json() == {
        "jsonrpc": "2.0",
        "error": {"code": -32000, "message": "MCP request body is too large"},
        "id": None,
    }


@pytest.mark.asyncio
async def test_mcp_rejects_methods_outside_the_transport_surface(monkeypatch):
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/mcp", content=b"{}")

    assert response.status_code == 405
    assert response.json() == {
        "error": {"code": "METHOD_NOT_ALLOWED", "message": "Method Not Allowed"}
    }


@pytest.mark.asyncio
async def test_path_scoped_resource_metadata_reaches_the_sidecar(monkeypatch):
    """RFC 9728 lets a client discover the resource at `/mcp` by inserting the
    well-known segment BEFORE the path, and shipping coding agents do exactly
    that. Only the ROOT document was served, so this 404ed and the client fell
    back to the root form — which names the `/v0` product resource, not the
    `/mcp` one, so the client went on to request a token for the wrong
    audience. (At the time the two scope lists also differed; today they
    coincide but are still maintained separately — see `v0_discovery.py`.)

    The sidecar is the authority on its own document, so this must FORWARD
    rather than be answered locally from a second copy of the scope list.
    """
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    monkeypatch.setattr("agentdrive.mcp_proxy.httpx.AsyncClient", _FakeAsyncClient)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code != 404
    method, url, _headers, _body = _FakeAsyncClient.request
    assert method == "GET"
    # The sidecar answers at its origin root, so the upstream path is the
    # well-known path itself — never the transport path, and never carrying a
    # FastAPI mount prefix.
    assert url == "http://127.0.0.1:8081/.well-known/oauth-protected-resource/mcp"


@pytest.mark.asyncio
async def test_path_scoped_resource_metadata_is_404_without_a_sidecar(monkeypatch):
    """No sidecar means no `/mcp` resource, and the honest answer is 404.

    This used to fall back to the ROOT document, which was defensible while
    the two shared an audience. Since the 2026-08-28 split they do not: the
    root document names `<origin>`, so serving it here would tell a client the
    `/mcp` resource IS the product origin — and a client that believed it
    would go get a product-audience token that the MCP transport refuses. A
    404 says the true thing: this deployment has no `/mcp` resource.
    """
    monkeypatch.setattr(settings, "mcp_proxy_url", "")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    # And emphatically NOT the root document under another name.
    assert "resource" not in response.json()


@pytest.mark.asyncio
async def test_root_resource_metadata_is_still_served_locally(monkeypatch):
    """The ROOT document describes the `/v0` API and stays FastAPI's own — it
    must not be forwarded to the MCP sidecar."""
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    assert response.json()["resource"]


class _CannedResponse:
    """An upstream response with real `httpx.Headers` semantics.

    The header container matters: `httpx.Headers.items()` yields lowercase
    names whatever casing the sidecar sent, and that is what let a
    title-cased `setdefault("Cache-Control", …)` slip a second header past
    the forwarded `cache-control`. A plain dict would only reproduce the bug
    if the test author remembered to lowercase the key.
    """

    def __init__(self, status_code: int, headers: dict[str, str], body: bytes):
        self.status_code = status_code
        self.headers = httpx.Headers(headers)
        self._body = body

    async def aiter_bytes(self):
        yield self._body


def _fake_client_returning(status_code: int, headers: dict[str, str], body: bytes):
    """A `_FakeAsyncClient` whose upstream answers with the given response."""

    class _Client(_FakeAsyncClient):
        def stream(self, method, url, *, headers, content):
            _FakeAsyncClient.request = (method, url, headers, content)
            return _UpstreamStream(_CannedResponse(status_code, headers, body))

    return _Client


@pytest.mark.asyncio
async def test_proxied_discovery_document_is_never_cacheable(monkeypatch):
    """`GET /.well-known/oauth-protected-resource/mcp` used to leave with TWO
    `Cache-Control` headers — the sidecar's `public, max-age=300` beside the
    edge's `no-store` — because httpx lowercases header names and the edge's
    `setdefault("Cache-Control", …)` never matched the forwarded
    `cache-control`. Two conflicting directives leave caching to whichever
    one an intermediary reads, and a cached discovery document is how a
    client ends up acting on a stale resource identifier. Exactly one header,
    and it is `no-store`, whatever the sidecar sent.
    """
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(
        "agentdrive.mcp_proxy.httpx.AsyncClient",
        _fake_client_returning(
            200,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "public, max-age=300",
                "Content-Length": "2",
            },
            b"{}",
        ),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.headers.get_list("cache-control") == ["no-store"]


@pytest.mark.asyncio
async def test_proxied_auth_challenge_carries_exactly_one_cache_control(monkeypatch):
    """The 401 on `/mcp` is the other spelling of the same defect: the sidecar
    already says `no-store`, and the edge appended its own, so the challenge
    carried `no-store` twice. Same-valued or not, it must be one header."""
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    monkeypatch.setattr("agentdrive.mcp_proxy.httpx.AsyncClient", _FakeAsyncClient)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )

    assert response.status_code == 401
    assert response.headers.get_list("cache-control") == ["no-store"]
    # The challenge itself still comes through untouched.
    assert response.headers["www-authenticate"].startswith("Bearer resource_metadata=")


# ---------------------------------------------------------------------------
# ADR-0002: labelling the origin for the sidecar
# ---------------------------------------------------------------------------


def _surface_app(surface_role: str | None):
    """The proxy as HostSurfaceMiddleware would reach it, with the role set."""

    async def asgi(scope, receive, send):
        if surface_role is not None:
            scope.setdefault("state", {})["surface_role"] = surface_role
        response = await proxy_mcp(Request(scope, receive))
        await response(scope, receive, send)

    return asgi


@pytest.mark.asyncio
async def test_the_edge_labels_requests_from_the_mcp_origin_and_only_those(monkeypatch):
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(settings, "mcp_origin_base_url", "https://drive.mcp.example.test")
    monkeypatch.setattr("agentdrive.mcp_proxy.httpx.AsyncClient", _FakeAsyncClient)

    # From the per-product origin: labelled with the CONFIGURED origin, not
    # with anything the client sent — its own copy of the header is dropped
    # with every other non-protocol header.
    transport = ASGITransport(app=_surface_app("mcp"))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/mcp",
            content=b"{}",
            headers={MCP_ORIGIN_HEADER: "https://attacker.example.test"},
        )
    _method, _url, headers, _body = _FakeAsyncClient.request
    assert headers[MCP_ORIGIN_HEADER] == "https://drive.mcp.example.test"

    # From the API host (no surface role): unlabelled, so the sidecar answers
    # for its primary — the legacy — resource, and a client cannot relabel.
    transport = ASGITransport(app=_surface_app(None))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/mcp",
            content=b"{}",
            headers={MCP_ORIGIN_HEADER: "https://drive.mcp.example.test"},
        )
    _method, _url, headers, _body = _FakeAsyncClient.request
    assert MCP_ORIGIN_HEADER not in {name.lower() for name in headers}


@pytest.mark.asyncio
async def test_no_label_is_sent_when_no_origin_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(settings, "mcp_origin_base_url", "")
    monkeypatch.setattr("agentdrive.mcp_proxy.httpx.AsyncClient", _FakeAsyncClient)
    transport = ASGITransport(app=_surface_app("mcp"))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mcp", content=b"{}")
    _method, _url, headers, _body = _FakeAsyncClient.request
    assert MCP_ORIGIN_HEADER not in {name.lower() for name in headers}


async def test_local_mode_forwards_the_clients_host_so_discovery_names_the_right_origin(
    monkeypatch,
):
    """Adversarial review of #728: a self-hosted install's sidecar built its
    RFC 9728 resource and its challenge URL from the Host it saw — its own
    loopback address — and told every client `http://127.0.0.1:8081/mcp`.
    Local mode forwards the Host the client reached and the edge's scheme;
    hub mode keeps `Host` out of the forwarded set exactly as before."""
    monkeypatch.setattr(settings, "mcp_proxy_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(settings, "mcp_origin_base_url", "")
    monkeypatch.setattr("agentdrive.mcp_proxy.httpx.AsyncClient", _FakeAsyncClient)

    monkeypatch.setattr(settings, "auth_mode", "local")
    transport = ASGITransport(app=_surface_app(None))
    async with AsyncClient(transport=transport, base_url="http://localhost:8080") as client:
        await client.post("/mcp", content=b"{}", headers={"x-forwarded-proto": "https, http"})
    _method, _url, headers, _body = _FakeAsyncClient.request
    lowered = {name.lower(): value for name, value in headers.items()}
    assert lowered["host"] == "localhost:8080"
    assert lowered["x-forwarded-proto"] == "https"  # the first hop's word, not the edge's

    async with AsyncClient(transport=transport, base_url="http://localhost:8080") as client:
        await client.post("/mcp", content=b"{}")
    _method, _url, headers, _body = _FakeAsyncClient.request
    assert {n.lower(): v for n, v in headers.items()}["x-forwarded-proto"] == "http"

    monkeypatch.setattr(settings, "auth_mode", "hub")
    async with AsyncClient(transport=transport, base_url="http://localhost:8080") as client:
        await client.post("/mcp", content=b"{}")
    _method, _url, headers, _body = _FakeAsyncClient.request
    lowered = {name.lower() for name in headers}
    assert "host" not in lowered and "x-forwarded-proto" not in lowered

