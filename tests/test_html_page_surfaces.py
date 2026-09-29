"""`mode="page"` through both adapters — and what each one adds on its own.

The renderer produces one document for both surfaces; the adapters differ in
exactly two places, and this module pins both:

  * **the public renderer** adds `X-Robots-Tag: noindex` and the
    untrusted-content band. A static page an agent wrote can imitate a brand
    and instruct a stranger out of band; it should not also be indexable, and
    the reader should be told whose words they are reading.
  * **the private viewer** adds neither. The reader is the authorized owner of
    the artifact, inside a console that already frames it as their own file.

And what neither adapter does, which is the larger half of the test: no CSP
moves in either direction, and `text/html` bytes are still an attachment.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"
CONSOLE_ORIGIN = "https://console.example.test"

BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

REPORT = (
    b"<html><head><title>Weekly</title></head><body>"
    b"<h1>Weekly report</h1><p>Throughput rose.</p>"
    b"<script>window.pwned = true</script>"
    b'<a href="javascript:alert(1)">bad</a>'
    b"</body></html>"
)

# Byte-exact, and deliberately duplicated from `test_public_share.py` and
# `test_viewer_surface.py`: this design changes no CSP, so the pins have to be
# asserted with the new mode ACTIVE, not merely inherited from a suite that
# never renders one.
PUBLIC_PAGE_CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "worker-src 'self' blob:; img-src 'self' data: https:; "
    "media-src 'self'; "
    "style-src 'self'; font-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
CONTENT_CSP = (
    "default-src 'none'; script-src 'none'; object-src 'none'; "
    "img-src 'self' data: https:; style-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox"
)
SHELL_CSP_EMBEDDABLE = (
    "default-src 'none'; script-src 'self'; "
    "connect-src 'self' blob: https://storage.googleapis.com; "
    "worker-src 'self' blob:; img-src 'self' blob: data:; "
    "media-src 'self' blob:; "
    "style-src 'self'; font-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'none'; "
    f"frame-ancestors {CONSOLE_ORIGIN}"
)


def make_actor() -> V0ActorContext:
    return V0ActorContext(
        subject=AGENT,
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({
            "drives:read", "drives:write", "content:read", "content:write",
            "sharing:read", "sharing:write",
        }),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id=SPONSOR,
        workspace_role=None,
    )


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def override_actor(app_with_lifespan):
    def _set(actor: V0ActorContext) -> None:
        app.dependency_overrides[v0_actor] = lambda: actor

    yield _set
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


@pytest.fixture
def pages_on(monkeypatch):
    monkeypatch.setattr(settings, "static_html_rendering_enabled", True)


@pytest.fixture
def pages_off(monkeypatch):
    monkeypatch.setattr(settings, "static_html_rendering_enabled", False)


@pytest.fixture(autouse=True)
def _bound_viewer(monkeypatch):
    monkeypatch.setattr(settings, "viewer_base_url", "https://viewer.example.test")


def _multipart(parent_id: str, name: str, content_type: str, body: bytes) -> bytes:
    return (
        b'--b\r\nContent-Disposition: form-data; name="parent_id"\r\n\r\n'
        + parent_id.encode()
        + b'\r\n--b\r\nContent-Disposition: form-data; name="name"\r\n\r\n'
        + name.encode()
        + b'\r\n--b\r\nContent-Disposition: form-data; name="content"; filename="'
        + name.encode()
        + b'"\r\nContent-Type: '
        + content_type.encode()
        + b"\r\n\r\n"
        + body
        + b"\r\n--b--\r\n"
    )


async def _shared(
    http, override_actor, *, body: bytes = REPORT,
    content_type: str = "text/html", name: str = "report.html",
) -> str:
    """A published artifact's share secret."""
    override_actor(make_actor())
    drive = await http.post(
        "/v0/drives", json={"name": "d"}, headers={"Idempotency-Key": "k-drive"}
    )
    assert drive.status_code == 201, drive.text
    payload = drive.json()
    artifact = await http.post(
        f"/v0/drives/{payload['id']}/artifacts",
        content=_multipart(payload["root_folder_id"], name, content_type, body),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": "k-art",
        },
    )
    assert artifact.status_code == 201, artifact.text
    share = await http.post(
        f"/v0/drives/{payload['id']}/shares",
        json={"resource_type": "artifact", "resource_id": artifact.json()["id"]},
        headers={"Idempotency-Key": "k-share"},
    )
    assert share.status_code == 201, share.text
    return share.json()["secret"]


async def _minted(
    http, override_actor, *, body: bytes = REPORT,
    content_type: str = "text/html", name: str = "report.html",
) -> dict:
    override_actor(make_actor())
    drive = await http.post(
        "/v0/drives", json={"name": "d"}, headers={"Idempotency-Key": "k-drive"}
    )
    assert drive.status_code == 201, drive.text
    payload = drive.json()
    artifact = await http.post(
        f"/v0/drives/{payload['id']}/artifacts",
        content=_multipart(payload["root_folder_id"], name, content_type, body),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": "k-art",
        },
    )
    assert artifact.status_code == 201, artifact.text
    minted = await http.post(
        f"/v0/drives/{payload['id']}/artifacts/{artifact.json()['id']}/viewer-sessions",
        json={},
        headers={"Idempotency-Key": "k-mint"},
    )
    assert minted.status_code == 200, minted.text
    return minted.json()


def _auth(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


# ── the public renderer ──────────────────────────────────────────────────


async def test_public_html_renders_as_a_page_when_enabled(http, override_actor, pages_on):
    secret = await _shared(http, override_actor)
    r = await http.get(f"/s/{secret}/", headers=BROWSER)

    assert r.status_code == 200, r.text
    assert '<main class="doc page">' in r.text
    assert '<body class="mode-page">' in r.text
    assert "<h1>Weekly report</h1>" in r.text
    assert "window.pwned" not in r.text.split('data-view="source" hidden')[0]


async def test_public_page_responses_are_noindex(http, override_actor, pages_on):
    """A static page an agent wrote can imitate a brand; it must not also be
    a search result."""
    secret = await _shared(http, override_actor)
    r = await http.get(f"/s/{secret}/", headers=BROWSER)
    assert r.headers["x-robots-tag"] == "noindex"


async def test_public_page_carries_the_untrusted_content_band(
    http, override_actor, pages_on
):
    secret = await _shared(http, override_actor)
    r = await http.get(f"/s/{secret}/", headers=BROWSER)
    assert 'class="untrusted-band"' in r.text
    # Above the document, not below it: a warning after the content it
    # qualifies has already done its work on the reader.
    assert r.text.index("untrusted-band") < r.text.index("<h1>Weekly report</h1>")


async def test_a_refused_link_is_not_painted_as_a_working_one(
    http, override_actor, pages_on
):
    """The sanitiser keeps the text and drops the href; the stylesheet has to
    finish the job, or a `javascript:` link still LOOKS clickable."""
    secret = await _shared(http, override_actor)
    page = await http.get(f"/s/{secret}/", headers=BROWSER)
    # Exactly this: the text kept, the attribute gone. A disjunction that also
    # accepted `>bad<` would pass for an anchor that KEPT its `javascript:`
    # href, which is the regression this exists to catch.
    assert "<a>bad</a>" in page.text
    assert "javascript:" not in page.text.split('data-view="source" hidden')[0]

    # The styling half is proved in the browser (`tests/browser/`), where a
    # computed style can be read; here we only pin that the rule ships.
    css = await http.get("/public-static/viewer.css")
    assert css.status_code == 200, css.text
    assert ".doc a:not([href])" in css.text


async def test_no_other_mode_gets_the_band_or_the_noindex(http, override_actor, pages_on):
    """Markdown is not affected by an HTML feature, flag or no flag."""
    secret = await _shared(
        http, override_actor, body=b"# Quarterly\n", content_type="text/markdown",
        name="report.md",
    )
    r = await http.get(f"/s/{secret}/", headers=BROWSER)
    assert r.status_code == 200
    assert "untrusted-band" not in r.text
    assert "x-robots-tag" not in r.headers
    assert 'data-view="source"' in r.text  # Markdown also has a safe source view.


async def test_with_the_flag_off_the_public_page_is_unchanged(
    http, override_actor, pages_off
):
    secret = await _shared(http, override_actor)
    r = await http.get(f"/s/{secret}/", headers=BROWSER)
    assert r.status_code == 200
    assert '<body class="mode-code">' in r.text
    assert "untrusted-band" not in r.text
    assert "doc-modes" not in r.text
    assert "x-robots-tag" not in r.headers


async def test_the_public_page_csp_is_unchanged_by_page_mode(
    http, override_actor, pages_on
):
    """This design changes no CSP. `style-src 'self'` still refuses the
    author's own CSS, which is the intended outcome, not a gap."""
    secret = await _shared(http, override_actor)
    r = await http.get(f"/s/{secret}/", headers=BROWSER)
    assert r.headers["content-security-policy"] == PUBLIC_PAGE_CSP
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


async def test_html_bytes_are_still_an_attachment_when_pages_are_on(
    http, override_actor, pages_on
):
    """Rendering and downloading are separate decisions. A navigation straight
    at the bytes must still download rather than execute them."""
    secret = await _shared(http, override_actor)
    r = await http.get(f"/s/{secret}/content", headers=BROWSER)
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["content-security-policy"] == CONTENT_CSP
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_the_permalink_page_gets_the_same_treatment(
    http, override_actor, pages_on
):
    """One adapter, three routes: `/s/`, `/a/` and `/v/` must not disagree."""
    override_actor(make_actor())
    drive = await http.post(
        "/v0/drives", json={"name": "d"}, headers={"Idempotency-Key": "k-drive"}
    )
    payload = drive.json()
    artifact = await http.post(
        f"/v0/drives/{payload['id']}/artifacts",
        content=_multipart(payload["root_folder_id"], "report.html", "text/html", REPORT),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": "k-art",
        },
    )
    assert artifact.status_code == 201, artifact.text
    published = await http.post(
        f"/v0/drives/{payload['id']}/grants",
        json={
            "resource_type": "artifact",
            "resource_id": artifact.json()["id"],
            "principal_type": "public",
            "role": "viewer",
        },
        headers={"Idempotency-Key": "k-grant"},
    )
    assert published.status_code == 201, published.text

    r = await http.get(f"/a/{artifact.json()['id']}/", headers=BROWSER)
    assert r.status_code == 200, r.text
    assert r.headers["x-robots-tag"] == "noindex"
    assert "untrusted-band" in r.text
    assert r.headers["content-security-policy"] == PUBLIC_PAGE_CSP


# ── the private viewer ───────────────────────────────────────────────────


async def test_private_doc_renders_the_page_and_its_strip(http, override_actor, pages_on):
    minted = await _minted(http, override_actor)
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()

    assert doc["mode"] == "page"
    assert '<nav class="doc-modes"' in doc["html"]
    assert "<h1>Weekly report</h1>" in doc["html"]
    assert "window.pwned" not in doc["html"].split('data-view="source" hidden')[0]


async def test_the_private_viewer_has_no_untrusted_band(http, override_actor, pages_on):
    """Public-only, deliberately: the private reader is the artifact's own
    authorized owner, looking at it inside their own console."""
    minted = await _minted(http, override_actor)
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert "untrusted-band" not in doc["html"]

    shell = await http.get("/view/")
    assert "untrusted-band" not in shell.text


async def test_the_private_viewer_headers_are_unchanged_by_page_mode(
    http, override_actor, monkeypatch, pages_on
):
    monkeypatch.setattr(settings, "viewer_embed_origins", CONSOLE_ORIGIN)
    minted = await _minted(http, override_actor)

    shell = await http.get("/view/")
    assert shell.headers["content-security-policy"] == SHELL_CSP_EMBEDDABLE

    doc = await http.get("/view/doc", headers=_auth(minted["credential"]))
    assert doc.headers["cache-control"] == "private, no-store"
    assert doc.headers["referrer-policy"] == "no-referrer"
    assert "x-robots-tag" not in doc.headers

    content = await http.get("/view/content", headers=_auth(minted["credential"]))
    assert content.headers["content-security-policy"] == CONTENT_CSP
    assert content.headers["content-disposition"].startswith("attachment;")


async def test_with_the_flag_off_the_private_viewer_is_unchanged(
    http, override_actor, pages_off
):
    minted = await _minted(http, override_actor)
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert doc["mode"] == "code"
    assert "doc-modes" not in doc["html"]
