"""Each configured surface answers on its own host and nowhere else.

The security matrix in this file deliberately wraps a tiny ASGI recorder
instead of the database-backed application. Host selection, role assignment,
and refusal happen before routing, so exercising that seam directly keeps the
assertions runnable when the local Postgres container is unavailable. The
real FastAPI stack still has a focused wiring assertion at the end.
"""

import json
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from agentdrive.app import _surface_bindings, app
from agentdrive.config import settings
from agentdrive.middleware import (
    MCP_PREFIXES,
    PUBLIC_RENDERER_PREFIXES,
    SHARE_PREFIXES,
    VIEWER_PREFIXES,
    HostSurfaceMiddleware,
    ServiceSurfaceMiddleware,
    SurfaceBinding,
)

SHARE_HOST = "share.example.test"
PUBLIC_HOST = "public.example-isolated.test"
VIEWER_HOST = "viewer.example-isolated.test"
CONSOLE_HOST = "app.example.test"
API_HOST = "api.example.test"
MCP_HOST = "drive.mcp.example.test"


async def _echo_app(scope, receive, send):
    """Return only state written before routing; no database or app fixtures."""
    state = scope.get("state", {})
    body = json.dumps(
        {
            "surface_role": state.get("surface_role"),
            "unrelated_state": state.get("unrelated_state"),
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _bindings(*, share_host=SHARE_HOST, public_host=PUBLIC_HOST, viewer_host=VIEWER_HOST):
    return [
        SurfaceBinding("share", share_host, SHARE_PREFIXES),
        SurfaceBinding("public-renderer", public_host, PUBLIC_RENDERER_PREFIXES),
        SurfaceBinding("viewer", viewer_host, VIEWER_PREFIXES, private=True),
    ]


@asynccontextmanager
async def _client(bindings, *, mount_prefix=""):
    middleware = HostSurfaceMiddleware(
        _echo_app,
        surfaces=bindings,
        mount_prefix=mount_prefix,
    )

    async def seeded_state_app(scope, receive, send):
        scope.setdefault("state", {})["unrelated_state"] = "preserved"
        await middleware(scope, receive, send)

    transport = ASGITransport(app=seeded_state_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.parametrize(
    "host,path,expected_role",
    (
        (SHARE_HOST, "/a/art_0000000000000000", "share"),
        (SHARE_HOST, "/f/fld_0000000000000000", "share"),
        (SHARE_HOST, "/s/share_example", "share"),
        (SHARE_HOST, "/v/art_0000000000000000/1", "share"),
        (SHARE_HOST, "/share-static/shell.css", "share"),
        (PUBLIC_HOST, "/a/art_0000000000000000", "public-renderer"),
        (PUBLIC_HOST, "/f/fld_0000000000000000", "public-renderer"),
        (PUBLIC_HOST, "/s/share_example", "public-renderer"),
        (PUBLIC_HOST, "/v/art_0000000000000000/1", "public-renderer"),
        (PUBLIC_HOST, "/public-static/viewer.css", "public-renderer"),
        (VIEWER_HOST, "/view/doc", "viewer"),
    ),
)
async def test_host_is_selected_before_prefix_and_sets_the_surface_role(host, path, expected_role):
    async with _client(_bindings()) as client:
        response = await client.get(path, headers={"host": host})

    assert response.status_code == 200
    assert response.json() == {
        "surface_role": expected_role,
        "unrelated_state": "preserved",
    }


@pytest.mark.parametrize(
    "path",
    (
        "/a/art_0000000000000000",
        "/f/fld_0000000000000000",
        "/s/share_example",
        "/v/art_0000000000000000/1",
        "/share-static/shell.css",
        "/public-static/viewer.css",
        "/view/doc",
    ),
)
@pytest.mark.parametrize("host", (API_HOST, CONSOLE_HOST))
async def test_configured_surface_paths_fail_closed_on_api_and_console_hosts(host, path):
    async with _client(_bindings()) as client:
        response = await client.get(path, headers={"host": host})

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "NOT_FOUND", "message": "not found"}}


@pytest.mark.parametrize(
    "host,path",
    (
        (SHARE_HOST, "/public-static/viewer.css"),
        (SHARE_HOST, "/view/doc"),
        (PUBLIC_HOST, "/share-static/shell.css"),
        (PUBLIC_HOST, "/view/doc"),
        (VIEWER_HOST, "/a/art_0000000000000000"),
        (VIEWER_HOST, "/share-static/shell.css"),
        (VIEWER_HOST, "/public-static/viewer.css"),
        (SHARE_HOST, "/v0/drives"),
        (PUBLIC_HOST, "/v0/drives"),
        (VIEWER_HOST, "/v0/drives"),
    ),
)
async def test_a_bound_host_serves_only_its_own_prefixes(host, path):
    async with _client(_bindings()) as client:
        response = await client.get(path, headers={"host": host})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_health_is_shared_without_assigning_a_surface_role():
    async with _client(_bindings()) as client:
        for host in (SHARE_HOST, PUBLIC_HOST, VIEWER_HOST, CONSOLE_HOST, API_HOST):
            response = await client.get("/health", headers={"host": host})
            assert response.status_code == 200, host
            assert response.json()["surface_role"] is None, host


async def test_exact_mount_prefixed_health_is_shared():
    async with _client(_bindings(), mount_prefix="/drive") as client:
        for host in (SHARE_HOST, PUBLIC_HOST, VIEWER_HOST, CONSOLE_HOST, API_HOST):
            response = await client.get("/drive/health", headers={"host": host})
            assert response.status_code == 200, host
            assert response.json()["surface_role"] is None, host


@pytest.mark.parametrize("host", (SHARE_HOST, PUBLIC_HOST, VIEWER_HOST))
@pytest.mark.parametrize(
    "path,mount_prefix",
    (
        ("/healthz", ""),
        ("/health-anything", ""),
        ("/drive/healthz", "/drive"),
        ("/drive/health-anything", "/drive"),
    ),
)
async def test_health_near_misses_do_not_bypass_a_bound_host(host, path, mount_prefix):
    async with _client(_bindings(), mount_prefix=mount_prefix) as client:
        response = await client.get(path, headers={"host": host})

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "NOT_FOUND", "message": "not found"}}


@pytest.mark.parametrize(
    "host,path,expected_status,expected_role",
    (
        (SHARE_HOST, "/drive/a/art_0000000000000000", 200, "share"),
        (PUBLIC_HOST, "/drive/a/art_0000000000000000", 200, "public-renderer"),
        (VIEWER_HOST, "/drive/view/doc", 200, "viewer"),
        (CONSOLE_HOST, "/drive/a/art_0000000000000000", 404, None),
        (API_HOST, "/drive/public-static/viewer.css", 404, None),
        (SHARE_HOST, "/drive/view/doc", 404, None),
        (PUBLIC_HOST, "/drive/share-static/shell.css", 404, None),
        (VIEWER_HOST, "/drive/s/share_example", 404, None),
    ),
)
async def test_mount_prefixed_paths_cannot_bypass_host_binding(
    host, path, expected_status, expected_role
):
    async with _client(_bindings(), mount_prefix="/drive") as client:
        response = await client.get(path, headers={"host": host})

    assert response.status_code == expected_status
    if expected_role is not None:
        assert response.json()["surface_role"] == expected_role
    else:
        assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_duplicate_prefixes_are_valid_on_distinct_hosts():
    bindings = [
        SurfaceBinding("share", SHARE_HOST, ("/same/",)),
        SurfaceBinding("public-renderer", PUBLIC_HOST, ("/same/",)),
    ]
    async with _client(bindings) as client:
        share = await client.get("/same/value", headers={"host": SHARE_HOST})
        public = await client.get("/same/value", headers={"host": PUBLIC_HOST})

    assert share.json()["surface_role"] == "share"
    assert public.json()["surface_role"] == "public-renderer"


def test_duplicate_configured_hosts_are_rejected():
    with pytest.raises(RuntimeError, match="same host"):
        HostSurfaceMiddleware(
            _echo_app,
            surfaces=[
                SurfaceBinding("share", SHARE_HOST, SHARE_PREFIXES),
                SurfaceBinding("public-renderer", SHARE_HOST, PUBLIC_RENDERER_PREFIXES),
            ],
        )


@pytest.mark.parametrize("bad_host", ("localhost:8000", "example.test/drive", "[::1]"))
def test_malformed_configured_hosts_are_rejected(bad_host):
    with pytest.raises(RuntimeError, match="bare hostname"):
        HostSurfaceMiddleware(
            _echo_app,
            surfaces=[SurfaceBinding("viewer", bad_host, VIEWER_PREFIXES, private=True)],
        )


@pytest.mark.parametrize(
    "headers",
    (
        {"host": ""},
        {"host": "share.example.test/drive"},
        {"host": "share.example.test:not-a-port"},
    ),
)
async def test_malformed_request_hosts_fail_closed(headers):
    async with _client(_bindings()) as client:
        response = await client.get("/a/art_0000000000000000", headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_unbound_private_viewer_fails_closed_once_an_origin_is_bound():
    bindings = _bindings(viewer_host="")
    async with _client(bindings) as client:
        for host in (SHARE_HOST, PUBLIC_HOST, CONSOLE_HOST, API_HOST):
            response = await client.get("/view/doc", headers={"host": host})
            assert response.status_code == 404, host
            assert response.json()["error"]["code"] == "NOT_FOUND", host


async def test_no_configured_hosts_keeps_local_single_origin_mode_inert():
    bindings = _bindings(share_host="", public_host="", viewer_host="")
    async with _client(bindings) as client:
        for path in (
            "/a/art_0000000000000000",
            "/public-static/viewer.css",
            "/view/doc",
            "/v0/drives",
        ):
            response = await client.get(path, headers={"host": "localhost:8000"})
            assert response.status_code == 200, path
            assert response.json()["surface_role"] is None, path


def test_validated_origins_build_the_four_distinct_app_bindings(monkeypatch):
    monkeypatch.setattr(settings, "share_base_url", f"https://{SHARE_HOST}")
    monkeypatch.setattr(settings, "public_content_base_url", f"https://{PUBLIC_HOST}")
    monkeypatch.setattr(settings, "viewer_base_url", f"https://{VIEWER_HOST}")
    monkeypatch.setattr(settings, "mcp_origin_base_url", f"https://{MCP_HOST}")

    assert _surface_bindings() == [
        SurfaceBinding("share", SHARE_HOST, SHARE_PREFIXES),
        SurfaceBinding("public-renderer", PUBLIC_HOST, PUBLIC_RENDERER_PREFIXES),
        SurfaceBinding("viewer", VIEWER_HOST, VIEWER_PREFIXES, private=True),
        SurfaceBinding("mcp", MCP_HOST, MCP_PREFIXES, exclusive=False),
    ]


async def test_share_only_configuration_preserves_the_direct_renderer_rollback(monkeypatch):
    monkeypatch.setattr(settings, "share_base_url", f"https://{SHARE_HOST}")
    monkeypatch.setattr(settings, "public_content_base_url", "")
    monkeypatch.setattr(settings, "viewer_base_url", "")

    async with _client(_surface_bindings()) as client:
        for path in (
            "/a/art_0000000000000000",
            "/share-static/shell.css",
            "/public-static/viewer.css",
        ):
            accepted = await client.get(path, headers={"host": SHARE_HOST})
            refused = await client.get(path, headers={"host": API_HOST})
            assert accepted.status_code == 200, path
            assert accepted.json()["surface_role"] == "share", path
            assert refused.status_code == 404, path


async def test_cookie_tripwire_applies_only_to_the_share_role():
    cookie = "tc_session=synthetic-secret"
    async with _client(_bindings()) as client:
        mismatch = await client.get("/a/art_0000000000000000", headers={"host": API_HOST})
        share = await client.get(
            "/a/art_0000000000000000",
            headers={"host": SHARE_HOST, "cookie": cookie},
        )
        public = await client.get(
            "/a/art_0000000000000000",
            headers={"host": PUBLIC_HOST, "cookie": cookie},
        )
        viewer = await client.get(
            "/view/doc",
            headers={"host": VIEWER_HOST, "cookie": cookie},
        )

    assert share.status_code == 404
    assert share.content == mismatch.content
    assert share.headers == mismatch.headers
    assert cookie.encode() not in share.content
    assert public.status_code == 200
    assert public.json()["surface_role"] == "public-renderer"
    assert viewer.status_code == 200
    assert viewer.json()["surface_role"] == "viewer"


async def test_request_headers_cannot_select_or_override_a_surface_role():
    async with _client(_bindings()) as client:
        refused = await client.get(
            "/a/art_0000000000000000",
            headers={"host": API_HOST, "x-surface-role": "share"},
        )
        accepted = await client.get(
            "/a/art_0000000000000000",
            headers={"host": SHARE_HOST, "x-surface-role": "public-renderer"},
        )

    assert refused.status_code == 404
    assert accepted.json()["surface_role"] == "share"


def test_middleware_is_wired_with_all_four_named_surface_bindings():
    middleware = next(m for m in app.user_middleware if m.cls is HostSurfaceMiddleware)
    bindings = middleware.kwargs["surfaces"]

    assert [binding.name for binding in bindings] == [
        "share",
        "public-renderer",
        "viewer",
        "mcp",
    ]
    assert all(isinstance(binding, SurfaceBinding) for binding in bindings)
    # The MCP binding is the one non-exclusive surface: the legacy transport
    # must keep answering on the API host while the origin is being added.
    assert [binding.exclusive for binding in bindings] == [True, True, True, False]


# ---------------------------------------------------------------------------
# ADR-0002: the single-purpose MCP origin
# ---------------------------------------------------------------------------


def _bindings_with_mcp(mcp_host=MCP_HOST):
    return [*_bindings(), SurfaceBinding("mcp", mcp_host, MCP_PREFIXES, exclusive=False)]


@pytest.mark.parametrize(
    "path, expected",
    (
        ("/mcp", 200),
        ("/.well-known/oauth-protected-resource/mcp", 200),
        ("/health", 200),
        # Single-purpose means single-purpose: no product API, no root
        # discovery document (it describes /v0, which this host does not
        # serve), no share, no viewer, nothing at the root.
        ("/v0/drives", 404),
        ("/.well-known/oauth-protected-resource", 404),
        ("/s/abc", 404),
        ("/view/x", 404),
        ("/", 404),
    ),
)
async def test_the_mcp_host_serves_the_transport_and_nothing_else(path, expected):
    async with _client(_bindings_with_mcp()) as client:
        response = await client.get(path, headers={"host": MCP_HOST})
    assert response.status_code == expected
    if path.startswith("/mcp") or path.startswith("/.well-known/oauth-protected-resource/mcp"):
        assert response.json()["surface_role"] == "mcp"


@pytest.mark.parametrize(
    "path", ("/mcp", "/.well-known/oauth-protected-resource/mcp")
)
async def test_the_legacy_transport_keeps_answering_on_the_api_host(path):
    """Additive, not a move: binding the origin must not take /mcp away from
    the API host, where Codex and every existing grant still point."""
    async with _client(_bindings_with_mcp()) as client:
        response = await client.get(path, headers={"host": API_HOST})
    assert response.status_code == 200
    assert response.json()["surface_role"] is None


@pytest.mark.parametrize("host", (SHARE_HOST, PUBLIC_HOST, VIEWER_HOST))
async def test_the_transport_does_not_leak_onto_the_content_hosts(host):
    async with _client(_bindings_with_mcp()) as client:
        response = await client.get("/mcp", headers={"host": host})
    assert response.status_code == 404


async def test_retiring_the_legacy_transport_makes_the_origin_binding_exclusive(monkeypatch):
    """ADR-0002 end state: /mcp answers on the origin and nowhere else."""
    monkeypatch.setattr(settings, "share_base_url", f"https://{SHARE_HOST}")
    monkeypatch.setattr(settings, "public_content_base_url", f"https://{PUBLIC_HOST}")
    monkeypatch.setattr(settings, "viewer_base_url", f"https://{VIEWER_HOST}")
    monkeypatch.setattr(settings, "mcp_origin_base_url", f"https://{MCP_HOST}")
    monkeypatch.setattr(settings, "mcp_legacy_retired", True)
    bindings = _surface_bindings()
    assert bindings[-1] == SurfaceBinding("mcp", MCP_HOST, MCP_PREFIXES, exclusive=True)

    async with _client(bindings) as client:
        for path in ("/mcp", "/.well-known/oauth-protected-resource/mcp"):
            legacy = await client.get(path, headers={"host": API_HOST})
            assert legacy.status_code == 404, path
            origin = await client.get(path, headers={"host": MCP_HOST})
            assert origin.status_code == 200, path
        # The root discovery document stays on the API host: it describes /v0.
        root = await client.get("/.well-known/oauth-protected-resource", headers={"host": API_HOST})
        assert root.status_code == 200


async def test_an_unconfigured_mcp_origin_is_inert():
    async with _client(_bindings_with_mcp(mcp_host="")) as client:
        api = await client.get("/mcp", headers={"host": API_HOST})
        share = await client.get("/mcp", headers={"host": SHARE_HOST})
    assert api.status_code == 200
    # A bound content host still answers only its own surface.
    assert share.status_code == 404


async def test_public_renderer_service_refuses_every_other_surface():
    wrapped = ServiceSurfaceMiddleware(_echo_app, role="public-renderer")
    async with AsyncClient(
        transport=ASGITransport(app=wrapped), base_url="http://service.test"
    ) as client:
        for path in ("/a/art_demo", "/v/ver_demo", "/public-static/viewer.css"):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.json()["surface_role"] == "public-renderer"
        assert (await client.get("/health")).status_code == 200
        for path in ("/v0/drives", "/view/session", "/mcp", "/"):
            assert (await client.get(path)).status_code == 404, path


async def test_default_service_role_remains_unrestricted():
    wrapped = ServiceSurfaceMiddleware(_echo_app)
    async with AsyncClient(
        transport=ASGITransport(app=wrapped), base_url="http://service.test"
    ) as client:
        assert (await client.get("/v0/drives")).status_code == 200
