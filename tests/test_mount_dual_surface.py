"""One service, two accepted path shapes — the mount compatibility contract.

`tests/test_mount_prefix.py` covers the `u()` helper and the startup
self-check; and the `_suite.yml` CI matrix runs the entire suite twice, at
`MOUNT_PREFIX=""` and `MOUNT_PREFIX=/drive`. (The former static
`test_mount_prefix_sweep.py` guard against bare root-absolute URLs was
retired with the `/drive`-mounted server-rendered console it protected —
that human UI moved to the TokenCanopy Next.js app, and the surfaces
agentdrive still serves are either JSON or host-bound root-serving.)

What none of those pin is the ROUTING consequence of running the product
service with `root_path` set while accepting both normalized path shapes:

    /drive/…  → product-scoped/mounted form
    /…        → bare domain-mapping or bounded-compatibility form

Both hit the same process. Every applicable product route must therefore keep
its accepted bare and mounted behavior even when `root_path="/drive"`. This
does not model browser-console hosting: Hub serves that static console and
there is no `/drive*` browser edge route to AgentDrive. The path-shape
asymmetry is invisible in the CI matrix, which exercises one shape per leg.

Static is the deliberate exception, and it is worth knowing why: Starlette's
`Mount` consumes `root_path` during matching while `Route` does not, so
`/static/…` answers only under the mount. That compatibility behavior is
exactly the kind of thing a refactor from `app.mount` to a `Route` would
quietly invert, so it is pinned rather than left to be rediscovered. The B1
share and renderer assets use separate host-bound allowlist routes.
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MOUNT = "/drive"

# Product API surfaces whose accepted bare and mounted forms are pinned here.
# The .well-known/* discovery paths left with the local authorization server
# (v0 contract reset). `/health` is the route that remains in this focused
# list; drive-scoped operations can be added as their dual-path behavior lands.
AGENT_PATHS = [
    "/health",
]

SHARE_HOST = "share.example.test"
PUBLIC_HOST = "public.example.test"
API_HOST = "api.example.test"
ARTIFACT_ID = "art_1111111111111111"
VERSION_ID = "ver_2222222222222222"
FOLDER_ID = "fld_3333333333333333"
SHARE_KEY = "share_secret_4444"


@contextmanager
def _public_surface_client(
    monkeypatch,
    *,
    split: bool = True,
    mount_prefix: str = "",
    artifact_content_type: str = "text/markdown",
    artifact_name: str = "report.md",
    artifact_size_bytes: int | None = None,
    artifact_body: bytes = b"# Renderer Body\n",
):
    """Real HTTP routing over authorized in-memory descriptors.

    The production authorization owners are left in place at the route seam;
    only their database-backed results are substituted. This keeps the host
    matrix executable when Docker is unavailable while still exercising the
    real middleware, router, templates, renderer, response policies, and
    redirect behavior.
    """
    from agentdrive import storage
    from agentdrive.api.v0_errors import V0ApiError, v0_api_error_handler
    from agentdrive.api.v0_rate_limit import enforce_v0_rate_limit
    from agentdrive.config import settings
    from agentdrive.middleware import (
        PUBLIC_RENDERER_PREFIXES,
        SHARE_PREFIXES,
        HostSurfaceMiddleware,
        SurfaceBinding,
    )
    from agentdrive.public import routes

    share_origin = f"https://{SHARE_HOST}"
    public_origin = f"https://{PUBLIC_HOST}" if split else ""
    monkeypatch.setattr(settings, "share_base_url", share_origin)
    monkeypatch.setattr(settings, "public_content_base_url", public_origin)
    monkeypatch.setattr(settings, "public_base_url", f"https://{API_HOST}/drive")
    monkeypatch.setattr(settings, "download_signed_min_bytes", 10_000_000)
    monkeypatch.setattr(settings, "public_usage_limit_mode", "off")

    artifact = {
        "kind": "artifact",
        "storage_object": "objects/synthetic",
        "size_bytes": (
            len(artifact_body)
            if artifact_size_bytes is None
            else artifact_size_bytes
        ),
        "content_type": artifact_content_type,
        "name": artifact_name,
        "etag": VERSION_ID,
        "artifact_id": ARTIFACT_ID,
        "updated_at": None,
        "path": "reports/report.md",
    }
    folder = {
        "folder_id": FOLDER_ID,
        "name": "Reports",
        "path": "reports",
        "entries": [
            {
                "kind": "markdown",
                "chip": "markdown",
                "name": "child-private-name.md",
                "id": ARTIFACT_ID,
                "size_bytes": 17,
                "is_folder": False,
            }
        ],
        "truncated": False,
    }
    authorization_calls = {"artifact": 0, "version": 0, "folder": 0, "share": 0}

    class _Connection:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return None

    async def public_artifact(_connection, artifact_id):
        authorization_calls["artifact"] += 1
        return artifact if artifact_id == ARTIFACT_ID else None

    async def public_version(_connection, artifact_id, version_id):
        authorization_calls["version"] += 1
        if artifact_id == ARTIFACT_ID and version_id == VERSION_ID:
            return artifact
        return None

    async def public_folder(_connection, folder_id):
        authorization_calls["folder"] += 1
        return folder if folder_id == FOLDER_ID else None

    async def resolve_secret(_connection, *, secret):
        authorization_calls["share"] += 1
        return artifact if secret == SHARE_KEY else None

    async def get_object(_storage_object, *, bucket=None, generation=None):
        return artifact_body

    async def stream_object(_storage_object, *, bucket=None, generation=None):
        yield artifact_body

    monkeypatch.setattr(routes, "conn", _Connection)
    monkeypatch.setattr(routes.public_reads, "public_artifact", public_artifact)
    monkeypatch.setattr(routes.public_reads, "public_version", public_version)
    monkeypatch.setattr(routes.public_reads, "public_folder", public_folder)
    monkeypatch.setattr(routes.v0_shares, "resolve_secret", resolve_secret)
    monkeypatch.setattr(storage, "get", get_object)
    monkeypatch.setattr(storage, "stream", stream_object)

    share_prefixes = SHARE_PREFIXES
    if not split:
        share_prefixes += tuple(
            prefix for prefix in PUBLIC_RENDERER_PREFIXES if prefix not in share_prefixes
        )

    test_app = FastAPI(root_path=mount_prefix or None, root_path_in_servers=False)
    test_app.state.authorization_calls = authorization_calls
    test_app.include_router(routes.router)
    test_app.add_exception_handler(V0ApiError, v0_api_error_handler)
    test_app.dependency_overrides[enforce_v0_rate_limit] = lambda: None
    test_app.add_middleware(
        HostSurfaceMiddleware,
        surfaces=[
            SurfaceBinding("share", SHARE_HOST, share_prefixes),
            SurfaceBinding(
                "public-renderer",
                PUBLIC_HOST if split else "",
                PUBLIC_RENDERER_PREFIXES,
            ),
        ],
        mount_prefix=mount_prefix,
    )

    with TestClient(test_app, base_url=f"https://{API_HOST}") as client:
        yield client


def _app_root_path() -> str:
    """`root_path` of the process-wide app.

    It is fixed at FastAPI construction time from `MOUNT_PREFIX`, so it
    cannot be monkeypatched after import — which is why the routing tests
    below are gated on it rather than setting it themselves.
    """
    os.environ.setdefault("DATABASE_URL", "postgresql://x:y@localhost:5432/x")
    os.environ.setdefault("GCS_BUCKET", "x")
    os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
    from agentdrive.app import app

    return app.root_path or ""


# The routing half of this file only means anything when the app was
# constructed WITH a prefix. `_suite.yml` runs the suite twice — once at
# MOUNT_PREFIX="" and once at MOUNT_PREFIX=/drive — so these execute on the
# `drive` leg in CI, and locally with:
#
#     MOUNT_PREFIX=/drive PUBLIC_BASE_URL=http://localhost:8000/drive uv run pytest
#
# The URL-generation and cookie tests below are NOT gated: they monkeypatch
# settings and assert in either leg.
requires_mount = pytest.mark.skipif(
    _app_root_path() != MOUNT,
    reason=(
        f"app was constructed with root_path={_app_root_path()!r}; these "
        f"assert the {MOUNT} routing shape. Runs on the `drive` leg of the "
        "_suite.yml matrix."
    ),
)


@pytest.fixture(scope="module")
def mounted_client():
    """TestClient over the process-wide app (already carrying root_path).

    Used to depend on the `signer` fixture without referencing it -- that
    minted agent_auth JWTs, which the reset archived.
    """
    from agentdrive.app import app

    return TestClient(app)


@pytest.fixture()
def mount_settings(monkeypatch):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "mount_prefix", MOUNT)
    monkeypatch.setattr(settings, "public_base_url", "https://app.staging.tokencanopy.com/drive")
    monkeypatch.setattr(settings, "api_base_url", "https://api.staging.tokencanopy.com/drive")
    return settings


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_split_artifact_html_uses_shell_then_existing_renderer(monkeypatch):
    with _public_surface_client(monkeypatch) as client:
        shell = client.get(
            f"/a/{ARTIFACT_ID}/",
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
        )
        rendered = client.get(
            f"/a/{ARTIFACT_ID}/",
            headers={"Host": PUBLIC_HOST, "Accept": "text/html"},
        )

    assert shell.status_code == 200
    assert f'src="https://{PUBLIC_HOST}/a/{ARTIFACT_ID}/?embed=1"' in shell.text
    assert "report.md" in shell.text
    assert "Renderer Body" not in shell.text
    assert shell.headers["content-security-policy"] == (
        "default-src 'none'; "
        f"frame-src https://{PUBLIC_HOST}; "
        "style-src 'self'; script-src 'self'; "
        "base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    )

    assert rendered.status_code == 200
    assert "Renderer Body" in rendered.text
    assert "<iframe" not in rendered.text
    assert rendered.headers["content-security-policy"].endswith(
        f"frame-ancestors https://{SHARE_HOST}"
    )
    assert rendered.headers["referrer-policy"] == "no-referrer"
    assert rendered.headers["x-content-type-options"] == "nosniff"
    assert "x-frame-options" not in rendered.headers


@pytest.mark.parametrize(
    ("path", "content_type", "name", "size_bytes", "fullscreen"),
    [
        (f"/a/{ARTIFACT_ID}/", "video/mp4", "clip.mp4", 1024, True),
        (
            f"/v/{ARTIFACT_ID}/{VERSION_ID}/",
            "video/mp4",
            "clip.mp4",
            1024,
            True,
        ),
        (f"/s/{SHARE_KEY}/", "video/mp4", "clip.mp4", 1024, True),
        (
            f"/a/{ARTIFACT_ID}/",
            "video/mp4",
            "large-clip.mp4",
            64 * 1024 * 1024 + 1,
            False,
        ),
        (f"/a/{ARTIFACT_ID}/", "audio/mpeg", "track.mp3", 1024, False),
        (
            f"/a/{ARTIFACT_ID}/",
            "text/markdown",
            "report.md",
            1024,
            False,
        ),
    ],
)
def test_split_artifact_shell_delegates_fullscreen_only_to_inline_video(
    monkeypatch, path, content_type, name, size_bytes, fullscreen,
):
    with _public_surface_client(
        monkeypatch,
        artifact_content_type=content_type,
        artifact_name=name,
        artifact_size_bytes=size_bytes,
    ) as client:
        shell = client.get(
            path,
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
        )

    assert shell.status_code == 200
    assert ("allow=\"fullscreen\"" in shell.text) is fullscreen


def test_split_folder_shell_never_exposes_authorized_child_names(monkeypatch):
    with _public_surface_client(monkeypatch) as client:
        shell = client.get(
            f"/f/{FOLDER_ID}/",
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
        )
        rendered = client.get(
            f"/f/{FOLDER_ID}/",
            headers={"Host": PUBLIC_HOST, "Accept": "text/html"},
        )

    assert shell.status_code == 200
    assert f'src="https://{PUBLIC_HOST}/f/{FOLDER_ID}/?embed=1"' in shell.text
    assert "Reports" in shell.text
    assert "1 item" in shell.text
    assert "child-private-name.md" not in shell.text
    assert rendered.status_code == 200
    assert "child-private-name.md" in rendered.text


@pytest.mark.parametrize(
    ("path", "canonical_path"),
    [
        (f"/a/{ARTIFACT_ID}/", f"/a/{ARTIFACT_ID}/"),
        (
            f"/v/{ARTIFACT_ID}/{VERSION_ID}/",
            f"/v/{ARTIFACT_ID}/{VERSION_ID}/",
        ),
    ],
)
def test_split_permalink_shell_keeps_share_canonical_url(monkeypatch, path, canonical_path):
    with _public_surface_client(monkeypatch) as client:
        response = client.get(
            path,
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
        )

    assert response.status_code == 200
    assert f'href="https://{SHARE_HOST}{canonical_path}"' in response.text
    assert f'content="https://{SHARE_HOST}{canonical_path}"' in response.text
    assert f'src="https://{PUBLIC_HOST}{canonical_path}?embed=1"' in response.text


def test_split_share_secret_only_reaches_renderer_possession_url(monkeypatch):
    with _public_surface_client(monkeypatch) as client:
        shell = client.get(
            f"/s/{SHARE_KEY}/",
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
        )
        rendered = client.get(
            f"/s/{SHARE_KEY}/",
            headers={"Host": PUBLIC_HOST, "Accept": "text/html"},
        )

    assert shell.status_code == 200
    assert f'src="https://{PUBLIC_HOST}/s/{SHARE_KEY}/?embed=1"' in shell.text
    assert 'rel="canonical"' not in shell.text
    assert 'property="og:url"' not in shell.text
    # Frame source, the download link beside it in the header, and the
    # fallback link. Three attributes, no fourth appearance — in particular
    # none in text, in metadata, or in a `/share-static/` asset URL.
    assert shell.text.count(SHARE_KEY) == 3
    assert rendered.status_code == 200
    assert "Renderer Body" in rendered.text
    assert "etag" not in rendered.headers


def test_split_share_refusals_are_uniform_per_origin(monkeypatch):
    with _public_surface_client(monkeypatch) as client:
        secrets = (
            "unknown_key",
            "revoked_key",
            "expired_key",
            "deleted_share_key",
            "deleted_target_key",
        )
        shell_refusals = [
            client.get(
                f"/s/{secret}/",
                headers={"Host": SHARE_HOST, "Accept": "text/html"},
            )
            for secret in secrets
        ]
        renderer_refusals = [
            client.get(
                f"/s/{secret}/",
                headers={"Host": PUBLIC_HOST, "Accept": "text/html"},
            )
            for secret in secrets
        ]

    assert all(response.status_code == 404 for response in shell_refusals)
    assert len({response.content for response in shell_refusals}) == 1
    assert len({tuple(response.headers.items()) for response in shell_refusals}) == 1
    assert all(response.status_code == 404 for response in renderer_refusals)
    assert len({response.content for response in renderer_refusals}) == 1
    assert len({tuple(response.headers.items()) for response in renderer_refusals}) == 1
    assert shell_refusals[0].content != renderer_refusals[0].content


@pytest.mark.parametrize(
    ("prefix", "first", "second"),
    [
        ("a", "art_aaaaaaaaaaaaaaaa", "art_bbbbbbbbbbbbbbbb"),
        ("f", "fld_aaaaaaaaaaaaaaaa", "fld_bbbbbbbbbbbbbbbb"),
    ],
)
def test_unpublished_and_absent_permalinks_are_uniform_on_both_origins(
    monkeypatch, prefix, first, second
):
    with _public_surface_client(monkeypatch) as client:
        comparisons = {}
        for host in (SHARE_HOST, PUBLIC_HOST):
            responses = [
                client.get(
                    f"/{prefix}/{resource_id}/",
                    headers={"Host": host, "Accept": "text/html"},
                )
                for resource_id in (first, second)
            ]
            comparisons[host] = responses

    for responses in comparisons.values():
        assert [response.status_code for response in responses] == [404, 404]
        assert responses[0].content == responses[1].content
        assert responses[0].headers == responses[1].headers


def test_trusted_shell_does_not_read_artifact_bytes(monkeypatch):
    from agentdrive import storage

    async def fail_if_fetched(_storage_object, *, bucket=None, generation=None):
        raise AssertionError("trusted shell fetched artifact bytes")

    with _public_surface_client(monkeypatch) as client:
        monkeypatch.setattr(storage, "get", fail_if_fetched)
        response = client.get(
            f"/a/{ARTIFACT_ID}/",
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
        )

    assert response.status_code == 200
    assert "<iframe" in response.text
    assert client.app.state.authorization_calls["artifact"] == 1


def test_split_non_html_share_redirects_after_authorization(monkeypatch):
    with _public_surface_client(monkeypatch) as client:
        redirected = client.get(
            f"/s/{SHARE_KEY}",
            headers={
                "Host": SHARE_HOST,
                "Accept": "application/json",
                "X-Forwarded-Host": "attacker.invalid",
            },
            follow_redirects=False,
        )
        redirected_slash = client.get(
            f"/s/{SHARE_KEY}/",
            headers={"Host": SHARE_HOST, "Accept": "application/json"},
            follow_redirects=False,
        )
        refused = client.get(
            "/s/unknown%20secret",
            headers={"Host": SHARE_HOST, "Accept": "application/json"},
            follow_redirects=False,
        )
        followed = client.get(
            redirected.headers["location"],
            headers={"Host": PUBLIC_HOST, "Accept": "application/json"},
        )

    assert redirected.status_code == 308
    assert redirected.headers["location"] == f"https://{PUBLIC_HOST}/s/{SHARE_KEY}"
    assert redirected_slash.status_code == 308
    assert redirected_slash.headers["location"] == (f"https://{PUBLIC_HOST}/s/{SHARE_KEY}/")
    assert "attacker.invalid" not in redirected.headers["location"]
    assert refused.status_code == 404
    assert followed.status_code == 200
    assert followed.content == b"# Renderer Body\n"


def test_split_content_redirects_to_encoded_configured_renderer_path(monkeypatch):
    hostile = "share secret"
    with _public_surface_client(monkeypatch) as client:
        # The target must come from the configured origin, independently of a
        # request-supplied host or forwarded URL. Segment encoding is pinned
        # separately by the URL-helper test below.
        redirected = client.get(
            f"/a/{ARTIFACT_ID}/content",
            headers={
                "Host": SHARE_HOST,
                "Accept": "image/png",
                "X-Forwarded-Host": hostile,
            },
            follow_redirects=False,
        )
        followed = client.get(
            redirected.headers["location"],
            headers={"Host": PUBLIC_HOST, "Accept": "image/png"},
        )

    assert redirected.status_code == 308
    assert redirected.headers["location"] == (f"https://{PUBLIC_HOST}/a/{ARTIFACT_ID}/content")
    assert hostile not in redirected.headers["location"]
    assert followed.status_code == 200
    assert followed.content == b"# Renderer Body\n"
    assert followed.headers["referrer-policy"] == "no-referrer"
    assert followed.headers["x-content-type-options"] == "nosniff"
    assert followed.headers["content-security-policy"].endswith(
        f"frame-ancestors https://{SHARE_HOST}; sandbox"
    )


def test_split_assets_answer_only_on_their_role(monkeypatch):
    with _public_surface_client(monkeypatch) as client:
        matrix = {
            (SHARE_HOST, "/share-static/shell.css"): 200,
            (SHARE_HOST, "/public-static/viewer.css"): 404,
            (PUBLIC_HOST, "/share-static/shell.css"): 404,
            (PUBLIC_HOST, "/public-static/viewer.css"): 200,
        }
        observed = {
            key: client.get(path, headers={"Host": host}).status_code
            for key in matrix
            for host, path in [key]
        }

    assert observed == matrix


def test_configured_public_and_renderer_urls_encode_segments(monkeypatch):
    from agentdrive.config import settings
    from agentdrive.core import urls

    monkeypatch.setattr(settings, "public_base_url", f"https://{API_HOST}/drive")
    monkeypatch.setattr(settings, "share_base_url", f"https://{SHARE_HOST}")
    monkeypatch.setattr(settings, "public_content_base_url", f"https://{PUBLIC_HOST}")

    assert urls.share_url("secret /?#") == (f"https://{SHARE_HOST}/s/secret%20%2F%3F%23/")
    assert urls.renderer_url("s", "secret /?#", trailing_slash=True) == (
        f"https://{PUBLIC_HOST}/s/secret%20%2F%3F%23/"
    )


def test_no_content_origin_preserves_direct_share_renderer(monkeypatch):
    with _public_surface_client(monkeypatch, split=False) as client:
        page = client.get(
            f"/a/{ARTIFACT_ID}/",
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
        )
        content = client.get(
            f"/a/{ARTIFACT_ID}/content",
            headers={"Host": SHARE_HOST, "Accept": "image/png"},
        )
        public_asset = client.get(
            "/public-static/viewer.css",
            headers={"Host": SHARE_HOST},
        )

    assert page.status_code == 200
    assert "Renderer Body" in page.text
    assert "<iframe" not in page.text
    assert content.status_code == 200
    assert content.content == b"# Renderer Body\n"
    assert public_asset.status_code == 200


@pytest.mark.parametrize("prefix", ["", MOUNT])
def test_split_host_matrix_is_identical_for_bare_and_mounted_paths(monkeypatch, prefix):
    with _public_surface_client(monkeypatch, mount_prefix=MOUNT) as client:
        share = client.get(
            f"{prefix}/a/{ARTIFACT_ID}/",
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
        )
        public = client.get(
            f"{prefix}/a/{ARTIFACT_ID}/",
            headers={"Host": PUBLIC_HOST, "Accept": "text/html"},
        )
        refused = client.get(
            f"{prefix}/a/{ARTIFACT_ID}/",
            headers={"Host": API_HOST, "Accept": "text/html"},
        )

    assert share.status_code == 200
    assert "<iframe" in share.text
    assert public.status_code == 200
    assert "Renderer Body" in public.text
    assert refused.status_code == 404


# ---------------------------------------------------------------------------
# Generated URLs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", AGENT_PATHS)
@requires_mount
def test_agent_surfaces_answer_at_the_bare_path(mounted_client, path):
    """A 404 means the bare compatibility shape went dark when root_path was
    configured.

    `/health` returns 503 without a DB pool — that is a healthy 503, and it
    still proves the route matched. A routing failure is a 404."""
    assert mounted_client.get(path).status_code != 404


@pytest.mark.parametrize("path", AGENT_PATHS)
@requires_mount
def test_agent_surfaces_also_answer_under_the_mount(mounted_client, path):
    """The same routes also resolve in product-scoped `/drive/…` form."""
    assert mounted_client.get(f"{MOUNT}{path}").status_code != 404


@requires_mount
def test_static_is_served_under_the_mount(mounted_client):
    """The legacy mounted static handler follows the configured root path."""
    assert mounted_client.get(f"{MOUNT}/static/agentdrive.css").status_code == 200


@requires_mount
def test_static_at_the_origin_root_is_not_served(mounted_client):
    """The documented consequence of `app.mount` + `root_path`: Starlette
    consumes the root path when matching a Mount, so `/static/…` does NOT
    resolve at the origin root under a prefix.

    Load-bearing to know: anything that emits an unprefixed `/static/...` URL
    404s under a mount. The trusted shell, public renderer, and private viewer
    sidestep this handler: their host-bound, root-answering allowlist routes
    (`/share-static/…`, `/public-static/…`, `/view/static/…`) own their assets."""
    assert mounted_client.get("/static/agentdrive.css").status_code == 404


@requires_mount
def test_openapi_servers_are_absolute_urls(mounted_client):
    """Fetch `/openapi.json` over HTTP — do NOT call `app.openapi()`.

    FastAPI's `root_path_in_servers` injection happens in the
    `/openapi.json` ROUTE HANDLER, reading `scope["root_path"]`, not in the
    schema builder. A test that calls `app.openapi()` therefore cannot see
    it: the builder returns the servers block we passed, while the endpoint
    serves that block with a bare `{"url": "/drive"}` prepended.

    That gap is not hypothetical. The helper's unit tests were green, and the
    staging release smoke gate — which reads the deployed `/openapi.json` —
    failed with `servers[0] is /drive`. SDK generators take servers[0], so a
    generated client would have targeted a relative path.

    The invariant is "absolute", not a specific host: `servers` is built once
    at app construction, so the origin here is whatever this test process
    started with. Which origin gets derived is covered by
    `test_servers_follow_the_deployment_origin`."""
    servers = mounted_client.get("/openapi.json").json()["servers"]

    assert servers, "openapi spec has no servers block"
    for entry in servers:
        assert entry["url"].startswith(("http://", "https://")), (
            f"servers entry {entry['url']!r} is not an absolute URL — SDK "
            f"generators take servers[0] and would emit a client pointing at "
            f"a relative path. Check `root_path_in_servers` on the FastAPI "
            f"app. Full block: {servers}"
        )
    assert all(e["url"] != MOUNT for e in servers), (
        f"root_path leaked into the servers block: {servers}"
    )
