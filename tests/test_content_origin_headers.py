"""Executable response-policy table for the three browser-facing origins.

The production authorization owners are replaced only at their descriptor
return seam.  Requests still cross the real host middleware, FastAPI routes,
templates, renderer, byte-safety adapter, and response constructors.  That
keeps this suite useful without Docker while avoiding a vacuous test of header
constants in isolation.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SHARE_HOST = "share.example.test"
PUBLIC_HOST = "public.example-isolated.test"
VIEWER_HOST = "viewer.example-isolated.test"
API_HOST = "api.example.test"
CONSOLE_ORIGIN = "https://app.example.test"

ARTIFACT_ID = "art_1111111111111111"
VERSION_ID = "ver_2222222222222222"
FOLDER_ID = "fld_3333333333333333"

SHELL_CSP = (
    "default-src 'none'; "
    f"frame-src https://{PUBLIC_HOST}; "
    "style-src 'self'; script-src 'self'; "
    "base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
RENDERER_CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "worker-src 'self' blob:; img-src 'self' data: https:; "
    "media-src 'self'; "
    "style-src 'self'; font-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'none'; "
    f"frame-ancestors https://{SHARE_HOST}"
)
PUBLIC_CONTENT_CSP = (
    "default-src 'none'; script-src 'none'; object-src 'none'; "
    "img-src 'self' data: https:; style-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; "
    f"frame-ancestors https://{SHARE_HOST}; sandbox"
)
PRIVATE_VIEWER_CSP = (
    "default-src 'none'; script-src 'self'; "
    "connect-src 'self' blob: https://storage.googleapis.com; "
    "worker-src 'self' blob:; img-src 'self' blob: data:; "
    "media-src 'self' blob:; "
    "style-src 'self'; font-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'none'; "
    f"frame-ancestors {CONSOLE_ORIGIN}"
)


@contextmanager
def _content_surface_client(
    monkeypatch,
    *,
    version_authorization: dict[str, bool] | None = None,
    artifact_content_type: str = "text/markdown",
    artifact_name: str = "report.md",
    artifact_body: bytes = b"# Synthetic report\n",
):
    """Drive real response construction from authorized synthetic records."""
    from agentdrive import storage
    from agentdrive.api.v0_errors import V0ApiError, v0_api_error_handler
    from agentdrive.api.v0_rate_limit import enforce_v0_rate_limit
    from agentdrive.config import settings
    from agentdrive.middleware import (
        PUBLIC_RENDERER_PREFIXES,
        SHARE_PREFIXES,
        VIEWER_PREFIXES,
        HostSurfaceMiddleware,
        SurfaceBinding,
    )
    from agentdrive.public import routes as public_routes
    from agentdrive.viewer import routes as viewer_routes

    monkeypatch.setattr(settings, "share_base_url", f"https://{SHARE_HOST}")
    monkeypatch.setattr(
        settings,
        "public_content_base_url",
        f"https://{PUBLIC_HOST}",
    )
    monkeypatch.setattr(settings, "public_base_url", f"https://{API_HOST}/drive")
    monkeypatch.setattr(settings, "viewer_base_url", f"https://{VIEWER_HOST}")
    monkeypatch.setattr(settings, "viewer_embed_origins", CONSOLE_ORIGIN)
    monkeypatch.setattr(settings, "download_signed_min_bytes", 10_000_000)
    monkeypatch.setattr(settings, "public_usage_limit_mode", "off")

    version_authorization = version_authorization or {"granted": True}
    artifact = {
        "kind": "artifact",
        "storage_object": "objects/synthetic",
        "size_bytes": len(artifact_body),
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
        "entries": [],
        "truncated": False,
    }

    class _Connection:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return None

    async def public_artifact(_connection, artifact_id):
        return artifact if artifact_id == ARTIFACT_ID else None

    async def public_version(_connection, artifact_id, version_id):
        if (
            version_authorization["granted"]
            and artifact_id == ARTIFACT_ID
            and version_id == VERSION_ID
        ):
            return artifact
        return None

    async def public_folder(_connection, folder_id):
        return folder if folder_id == FOLDER_ID else None

    async def resolve_secret(_connection, *, secret):
        return None

    async def get_object(_storage_object, *, bucket=None, generation=None):
        return artifact_body

    async def stream_object(_storage_object, *, bucket=None, generation=None):
        yield artifact_body

    monkeypatch.setattr(public_routes, "conn", _Connection)
    monkeypatch.setattr(public_routes.public_reads, "public_artifact", public_artifact)
    monkeypatch.setattr(public_routes.public_reads, "public_version", public_version)
    monkeypatch.setattr(public_routes.public_reads, "public_folder", public_folder)
    monkeypatch.setattr(public_routes.v0_shares, "resolve_secret", resolve_secret)
    monkeypatch.setattr(storage, "get", get_object)
    monkeypatch.setattr(storage, "stream", stream_object)

    app = FastAPI()
    app.include_router(public_routes.router)
    app.include_router(viewer_routes.router)
    app.add_exception_handler(V0ApiError, v0_api_error_handler)
    app.dependency_overrides[enforce_v0_rate_limit] = lambda: None
    app.add_middleware(
        HostSurfaceMiddleware,
        surfaces=[
            SurfaceBinding("share", SHARE_HOST, SHARE_PREFIXES),
            SurfaceBinding("public-renderer", PUBLIC_HOST, PUBLIC_RENDERER_PREFIXES),
            SurfaceBinding("viewer", VIEWER_HOST, VIEWER_PREFIXES, private=True),
        ],
    )

    with TestClient(app, base_url=f"https://{API_HOST}") as client:
        yield client


@dataclass(frozen=True)
class HeaderCase:
    name: str
    host: str
    path: str
    status_code: int
    csp: str
    frame_ancestors: str
    cache_control: str
    content_type: str


CASES = (
    HeaderCase(
        "trusted shell",
        SHARE_HOST,
        f"/a/{ARTIFACT_ID}/",
        200,
        SHELL_CSP,
        "'none'",
        "public, max-age=60, must-revalidate",
        "text/html; charset=utf-8",
    ),
    HeaderCase(
        "public artifact",
        PUBLIC_HOST,
        f"/a/{ARTIFACT_ID}/",
        200,
        RENDERER_CSP,
        f"https://{SHARE_HOST}",
        "public, max-age=60, must-revalidate",
        "text/html; charset=utf-8",
    ),
    HeaderCase(
        "public folder",
        PUBLIC_HOST,
        f"/f/{FOLDER_ID}/",
        200,
        RENDERER_CSP,
        f"https://{SHARE_HOST}",
        "public, max-age=60, must-revalidate",
        "text/html; charset=utf-8",
    ),
    HeaderCase(
        "public version",
        PUBLIC_HOST,
        f"/v/{ARTIFACT_ID}/{VERSION_ID}/",
        200,
        RENDERER_CSP,
        f"https://{SHARE_HOST}",
        "public, max-age=0, must-revalidate",
        "text/html; charset=utf-8",
    ),
    HeaderCase(
        "public raw content",
        PUBLIC_HOST,
        f"/a/{ARTIFACT_ID}/content",
        200,
        PUBLIC_CONTENT_CSP,
        f"https://{SHARE_HOST}",
        "public, max-age=60, must-revalidate",
        "text/markdown",
    ),
    HeaderCase(
        "private viewer",
        VIEWER_HOST,
        "/view/",
        200,
        PRIVATE_VIEWER_CSP,
        CONSOLE_ORIGIN,
        "private, no-store",
        "text/html; charset=utf-8",
    ),
    HeaderCase(
        "uniform shell 404",
        SHARE_HOST,
        "/a/art_aaaaaaaaaaaaaaaa/",
        404,
        SHELL_CSP,
        "'none'",
        "private, no-store",
        "text/html; charset=utf-8",
    ),
)


def _frame_ancestors(csp: str) -> str:
    for directive in csp.split(";"):
        name, _, value = directive.strip().partition(" ")
        if name == "frame-ancestors":
            return value
    raise AssertionError("CSP is missing frame-ancestors")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_content_origin_response_policy_table(monkeypatch, case: HeaderCase):
    with _content_surface_client(monkeypatch) as client:
        response = client.get(
            case.path,
            headers={"Host": case.host, "Accept": "text/html"},
        )

    assert response.status_code == case.status_code
    assert response.headers["content-security-policy"] == case.csp
    assert _frame_ancestors(response.headers["content-security-policy"]) == (case.frame_ancestors)
    assert response.headers["cache-control"] == case.cache_control
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-type"] == case.content_type
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in response.headers
    assert "service-worker-allowed" not in response.headers
    assert "x-frame-options" not in response.headers


@pytest.mark.parametrize(
    ("name", "host", "path", "status_code"),
    (
        ("trusted version shell", SHARE_HOST, f"/v/{ARTIFACT_ID}/{VERSION_ID}/", 200),
        ("public version renderer", PUBLIC_HOST, f"/v/{ARTIFACT_ID}/{VERSION_ID}/", 200),
        ("bare version canonical redirect", SHARE_HOST, f"/v/{ARTIFACT_ID}/{VERSION_ID}", 308),
        ("public version content", PUBLIC_HOST, f"/v/{ARTIFACT_ID}/{VERSION_ID}/content", 200),
    ),
)
def test_public_version_paths_revalidate_before_reusing_cached_bytes(
    monkeypatch, name: str, host: str, path: str, status_code: int
):
    """A revoked public grant must be checked before any cached `/v/` reuse."""
    with _content_surface_client(monkeypatch) as client:
        response = client.get(
            path,
            headers={"Host": host, "Accept": "text/html"},
            follow_redirects=False,
        )

    assert response.status_code == status_code, name
    assert response.headers["cache-control"] == "public, max-age=0, must-revalidate"


def test_warm_public_version_urls_recheck_revoked_authorization(monkeypatch):
    authorization = {"granted": True}
    shell_path = f"/v/{ARTIFACT_ID}/{VERSION_ID}/"
    renderer_path = f"/v/{ARTIFACT_ID}/{VERSION_ID}/"
    bare_path = f"/v/{ARTIFACT_ID}/{VERSION_ID}"
    content_path = f"/v/{ARTIFACT_ID}/{VERSION_ID}/content"

    with _content_surface_client(
        monkeypatch,
        version_authorization=authorization,
    ) as client:
        warm_responses = (
            client.get(shell_path, headers={"Host": SHARE_HOST, "Accept": "text/html"}),
            client.get(renderer_path, headers={"Host": PUBLIC_HOST, "Accept": "text/html"}),
            client.get(
                bare_path,
                headers={"Host": SHARE_HOST, "Accept": "text/html"},
                follow_redirects=False,
            ),
            client.get(content_path, headers={"Host": PUBLIC_HOST, "Accept": "text/html"}),
        )
        assert [response.status_code for response in warm_responses] == [200, 200, 308, 200]
        assert {response.headers["cache-control"] for response in warm_responses} == {
            "public, max-age=0, must-revalidate"
        }

        authorization["granted"] = False

        from agentdrive.public.page import render_not_found, render_shell_not_found

        refusal_cases = (
            (SHARE_HOST, shell_path, render_shell_not_found()),
            (PUBLIC_HOST, renderer_path, render_not_found()),
            (PUBLIC_HOST, content_path, render_not_found()),
        )
        for host, path, expected_body in refusal_cases:
            response = client.get(path, headers={"Host": host, "Accept": "text/html"})
            assert response.status_code == 404
            assert response.text == expected_body
            assert response.headers["cache-control"] == "private, no-store"

        bare = client.get(
            bare_path,
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
            follow_redirects=False,
        )
        assert bare.status_code == 308
        assert bare.headers["location"] == f"/v/{ARTIFACT_ID}/{VERSION_ID}/"
        assert bare.headers["cache-control"] == "public, max-age=0, must-revalidate"

        followed = client.get(
            bare_path,
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
            follow_redirects=True,
        )
        assert followed.status_code == 404
        assert followed.text == render_shell_not_found()
        assert followed.headers["cache-control"] == "private, no-store"


SCRIPT_ASSETS = (
    (PUBLIC_HOST, "/public-static/viewer.js"),
    (PUBLIC_HOST, "/public-static/reading.js"),
    (PUBLIC_HOST, "/public-static/pdf-visitor.js"),
    (PUBLIC_HOST, "/public-static/pdfview.js"),
    (PUBLIC_HOST, "/public-static/vendor/pdfjs/pdf.min.mjs"),
    (PUBLIC_HOST, "/public-static/vendor/pdfjs/pdf.worker.min.mjs"),
    (PUBLIC_HOST, "/public-static/vendor/pdfjs/pdf_viewer.mjs"),
    (PUBLIC_HOST, "/public-static/diagram-visitor.js"),
    (PUBLIC_HOST, "/public-static/diagrams.js"),
    (PUBLIC_HOST, "/public-static/diagram-frame.js"),
    (PUBLIC_HOST, "/public-static/vendor/mermaid/mermaid.min.js"),
    (VIEWER_HOST, "/view/static/shell.js"),
    (VIEWER_HOST, "/view/static/reading.js"),
    (VIEWER_HOST, "/view/static/pdfview.js"),
    (VIEWER_HOST, "/view/static/vendor/pdfjs/pdf.min.mjs"),
    (VIEWER_HOST, "/view/static/diagrams.js"),
    (VIEWER_HOST, "/view/static/diagram-frame.js"),
    (VIEWER_HOST, "/view/static/vendor/mermaid/mermaid.min.js"),
    (VIEWER_HOST, "/view/static/vendor/pdfjs/pdf.worker.min.mjs"),
    (VIEWER_HOST, "/view/static/vendor/pdfjs/pdf_viewer.mjs"),
)


def test_content_host_script_table_is_exhaustive():
    from agentdrive.public.routes import _PUBLIC_ASSETS
    from agentdrive.viewer.routes import _VIEWER_ASSETS

    routed_scripts = {
        (PUBLIC_HOST, f"/public-static/{name}")
        for name, (_path, media_type) in _PUBLIC_ASSETS.items()
        if media_type == "text/javascript"
    }
    routed_scripts.update(
        {
            (VIEWER_HOST, f"/view/static/{name}")
            for name, (_path, media_type) in _VIEWER_ASSETS.items()
            if media_type == "text/javascript"
        }
    )

    assert set(SCRIPT_ASSETS) == routed_scripts


def test_pdfjs_loading_spinner_is_served_on_both_content_hosts(monkeypatch):
    """pdf_viewer.css pulls images/loading-icon.gif on every page render —
    the one image it references in our configuration. Both content hosts
    must serve it from their allowlists, as an image, with no cookie."""
    with _content_surface_client(monkeypatch) as client:
        for host, path in (
            (PUBLIC_HOST, "/public-static/vendor/pdfjs/images/loading-icon.gif"),
            (VIEWER_HOST, "/view/static/vendor/pdfjs/images/loading-icon.gif"),
        ):
            response = client.get(path, headers={"Host": host})
            assert response.status_code == 200, (host, path)
            assert response.headers["content-type"].startswith("image/gif")
            assert "Set-Cookie" not in response.headers


@pytest.mark.parametrize(("host", "path"), SCRIPT_ASSETS)
def test_content_host_scripts_cannot_register_a_service_worker(monkeypatch, host, path):
    with _content_surface_client(monkeypatch) as client:
        response = client.get(path, headers={"Host": host})

    assert response.status_code == 200
    assert "serviceworker" not in response.text.lower()
    assert "Service-Worker-Allowed" not in response.headers
    assert "Set-Cookie" not in response.headers


# ── `mode="page"` and the robots directive (2026-08-26 static-HTML design) ──


def _page_mode_client(monkeypatch, **kwargs):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "static_html_rendering_enabled", True)
    return _content_surface_client(
        monkeypatch,
        artifact_content_type="text/html",
        artifact_name="report.html",
        artifact_body=b"<h1>Weekly report</h1><p>Body.</p>",
        **kwargs,
    )


def test_a_rendered_page_is_noindex_on_both_public_origins(monkeypatch):
    """The renderer draws the impersonating body; the SHELL is the URL a
    stranger is sent and a crawler fetches. Neither may be indexable."""
    with _page_mode_client(monkeypatch) as client:
        shell = client.get(
            f"/a/{ARTIFACT_ID}/", headers={"Host": SHARE_HOST, "Accept": "text/html"}
        )
        renderer = client.get(
            f"/a/{ARTIFACT_ID}/", headers={"Host": PUBLIC_HOST, "Accept": "text/html"}
        )

    assert shell.status_code == 200, shell.text
    assert renderer.status_code == 200, renderer.text
    assert shell.headers["x-robots-tag"] == "noindex"
    assert renderer.headers["x-robots-tag"] == "noindex"
    # The shell still carries no artifact bytes; that property is what lets it
    # decide from the descriptor alone.
    assert "Weekly report" not in shell.text
    assert "<h1>Weekly report</h1>" in renderer.text


def test_the_shell_policy_is_otherwise_unchanged_by_page_mode(monkeypatch):
    with _page_mode_client(monkeypatch) as client:
        shell = client.get(
            f"/a/{ARTIFACT_ID}/", headers={"Host": SHARE_HOST, "Accept": "text/html"}
        )
        renderer = client.get(
            f"/a/{ARTIFACT_ID}/", headers={"Host": PUBLIC_HOST, "Accept": "text/html"}
        )
    assert shell.headers["content-security-policy"] == SHELL_CSP
    assert renderer.headers["content-security-policy"] == RENDERER_CSP


def test_a_markdown_artifact_gets_no_robots_directive_on_either_origin(monkeypatch):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "static_html_rendering_enabled", True)
    with _content_surface_client(monkeypatch) as client:
        shell = client.get(
            f"/a/{ARTIFACT_ID}/", headers={"Host": SHARE_HOST, "Accept": "text/html"}
        )
        renderer = client.get(
            f"/a/{ARTIFACT_ID}/", headers={"Host": PUBLIC_HOST, "Accept": "text/html"}
        )
    assert "x-robots-tag" not in shell.headers
    assert "x-robots-tag" not in renderer.headers


def test_the_capability_being_off_leaves_the_shell_untouched(monkeypatch):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "static_html_rendering_enabled", False)
    with _content_surface_client(
        monkeypatch,
        artifact_content_type="text/html",
        artifact_name="report.html",
        artifact_body=b"<h1>Weekly report</h1>",
    ) as client:
        shell = client.get(
            f"/a/{ARTIFACT_ID}/", headers={"Host": SHARE_HOST, "Accept": "text/html"}
        )
    assert "x-robots-tag" not in shell.headers


# ── one header, across the two origins that draw it ─────────────────────────


def test_the_shell_frames_the_chrome_less_page_and_keeps_the_chrome_itself(
    monkeypatch,
):
    """The split, end to end: header on the trusted origin, document on the
    isolated one, and neither drawing the other's half."""
    with _content_surface_client(monkeypatch) as client:
        shell = client.get(
            f"/a/{ARTIFACT_ID}/", headers={"Host": SHARE_HOST, "Accept": "text/html"}
        )
        framed = client.get(
            f"/a/{ARTIFACT_ID}/?embed=1",
            headers={"Host": PUBLIC_HOST, "Accept": "text/html"},
        )
        direct = client.get(
            f"/a/{ARTIFACT_ID}/", headers={"Host": PUBLIC_HOST, "Accept": "text/html"}
        )

    assert f'src="https://{PUBLIC_HOST}/a/{ARTIFACT_ID}/?embed=1"' in shell.text
    assert 'class="shell-bar"' in shell.text
    assert "report.md" in shell.text
    assert "text/markdown" in shell.text  # the strip's `type`, now on the line

    # The framed page: document only.
    assert 'class="viewer-bar"' not in framed.text
    assert 'class="doc-head"' not in framed.text
    assert 'class="machine-strip"' not in framed.text
    assert "Synthetic report" in framed.text

    # The same URL without the flag is still the full standalone page, because
    # that is what the shell's own "open the content directly" link reaches.
    assert 'class="viewer-bar"' in direct.text
    assert 'class="doc-head"' in direct.text


def test_the_embed_flag_is_refused_where_no_frame_can_exist(monkeypatch):
    """Only the isolated renderer is ever framed, so only it honours the flag.

    On the share host the same parameter must change nothing at all: that host
    serves the shell, and a request that could talk it out of its own header
    would be a way to hand a reader a branded page with no provenance on it.
    """
    with _content_surface_client(monkeypatch) as client:
        shell = client.get(
            f"/a/{ARTIFACT_ID}/?embed=1",
            headers={"Host": SHARE_HOST, "Accept": "text/html"},
        )

    assert shell.status_code == 200
    assert 'class="shell-bar"' in shell.text
    assert shell.headers["content-security-policy"] == SHELL_CSP


def test_a_download_request_arrives_as_an_attachment(monkeypatch):
    """The Download control lives on the shell now, one origin away from the
    bytes — and `download` on a cross-origin anchor is ignored, so a plain link
    would have opened this markdown in the tab instead of saving it. The flag
    is what makes it a download, and its absence must leave the inline view
    (which is what `<img src="content">` on the rendered page depends on).
    """
    with _content_surface_client(monkeypatch) as client:
        saved = client.get(
            f"/a/{ARTIFACT_ID}/content?download=1", headers={"Host": PUBLIC_HOST}
        )
        viewed = client.get(f"/a/{ARTIFACT_ID}/content", headers={"Host": PUBLIC_HOST})

    assert saved.status_code == 200
    assert saved.headers["content-disposition"].startswith("attachment")
    assert "report.md" in saved.headers["content-disposition"]
    assert viewed.headers["content-disposition"].startswith("inline")
    # It only ever tightens: the sandboxed byte CSP and nosniff are unchanged.
    assert saved.headers["content-security-policy"] == PUBLIC_CONTENT_CSP
    assert saved.headers["x-content-type-options"] == "nosniff"


def test_the_share_host_carries_a_download_request_across_to_the_renderer(
    monkeypatch,
):
    """A byte request on the branded host is authorized there and moved to the
    renderer. Dropping the flag in that hop would land the reader on an inline
    view of the file they asked to save."""
    with _content_surface_client(monkeypatch) as client:
        moved = client.get(
            f"/a/{ARTIFACT_ID}/content?download=1",
            headers={"Host": SHARE_HOST},
            follow_redirects=False,
        )

    assert moved.status_code == 308
    assert (
        moved.headers["location"]
        == f"https://{PUBLIC_HOST}/a/{ARTIFACT_ID}/content?download=1"
    )


def test_the_diagram_engine_frame_names_each_hosts_ancestors(monkeypatch):
    """`diagram-frame.html` is the one asset that is a page, and its policy
    must let the page that creates it — and THAT page's own ancestors — frame
    it, because the browser checks the whole chain: share shell → renderer →
    engine frame, and console → viewer shell → engine frame. `'self'` alone
    would refuse the engine everywhere it is actually used."""
    from agentdrive.public.routes import diagram_frame_csp

    with _content_surface_client(monkeypatch) as client:
        for host, path, ancestors in (
            (PUBLIC_HOST, "/public-static/diagram-frame.html", f"https://{SHARE_HOST}"),
            (VIEWER_HOST, "/view/static/diagram-frame.html", CONSOLE_ORIGIN),
        ):
            response = client.get(path, headers={"Host": host})
            assert response.status_code == 200, (host, path)
            assert response.headers["content-type"].startswith("text/html")
            csp = response.headers["content-security-policy"]
            assert csp == diagram_frame_csp(ancestors), (host, csp)
            assert csp.endswith(f"frame-ancestors 'self' {ancestors}")
            assert "script-src 'self'" in csp
            assert "Set-Cookie" not in response.headers
