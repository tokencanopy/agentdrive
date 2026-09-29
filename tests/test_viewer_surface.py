"""The `/view/` surface — the private viewer the console iframes.

The properties that matter, in order:

  * **The credential rides only in the Authorization header.** No route on
    this surface accepts one in a path or query string, so infrastructure
    request logs can never capture it.
  * **Uniform refusal.** Unknown, expired, deleted-target, and revoked-grant
    credentials are indistinguishable: one 404, one code, one body. A missing
    header is the one distinct answer (401) — a protocol error, not a probe.
  * **The shell is embeddable by the configured console origins only**, and
    the policy fails closed to `frame-ancestors 'none'` when unconfigured.
    The public surface remains pinned to the exact B1 public-origin contract,
    including its non-frameable policy (test_public_*.py), and is untouched.
  * **Bytes stay byte-safe.** The same nosniff / Content-Disposition /
    sandboxed-CSP levers as the public surface, from the same shared module.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext
from agentdrive.rendering import render as render_module

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
AGENT_B = "tcagt_0000000000000002"
WS_A = "tcws_0000000000000001"

CONSOLE_ORIGIN = "https://console.example.test"

# Pinned literally: the embeddable shell policy. frame-ancestors is the whole
# point of this surface; everything else must stay as strong as the public
# page it derives from.
SHELL_CSP_EMBEDDABLE = (
    "default-src 'none'; script-src 'self'; "
    "connect-src 'self' blob: https://storage.googleapis.com; "
    "worker-src 'self' blob:; img-src 'self' blob: data:; "
    "media-src 'self' blob:; "
    "style-src 'self'; font-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'none'; "
    f"frame-ancestors {CONSOLE_ORIGIN}"
)

# The credentialed byte responses keep the strong public content policy —
# script-less, sandboxed, non-frameable (they are fetched, never navigated).
CONTENT_CSP = (
    "default-src 'none'; script-src 'none'; object-src 'none'; "
    "img-src 'self' data: https:; style-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox"
)


def make_actor(subject: str = AGENT) -> V0ActorContext:
    return V0ActorContext(
        subject=subject,
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({
            "drives:read", "drives:write",
            "content:read", "content:write",
            "sharing:read", "sharing:write",
        }),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id="tcusr_0000000000000009",
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
        await c.execute(
            "TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE"
        )


@pytest.fixture(autouse=True)
def _bound_viewer(monkeypatch):
    """The mint this module's fixtures rely on fails closed with 503
    VIEWER_DISABLED while `viewer_base_url` is empty; bind a synthetic
    host so the surface under test is reachable."""
    monkeypatch.setattr(settings, "viewer_base_url", "https://viewer.example.test")


async def _create_drive(http, name: str, key: str) -> dict:
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


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


async def _create_artifact(
    http, drive: dict, *, name: str, body: bytes, key: str,
    content_type: str = "text/markdown",
) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart(drive["root_folder_id"], name, content_type, body),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": key,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _minted(
    http, override_actor, *, body: bytes = b"# hello\n",
    content_type: str = "text/markdown", name: str = "doc.md",
) -> tuple[dict, dict, dict]:
    override_actor(make_actor())
    drive = await _create_drive(http, "d", "k-drive")
    artifact = await _create_artifact(
        http, drive, name=name, body=body, key="k-art", content_type=content_type
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/viewer-sessions",
        json={},
        headers={"Idempotency-Key": "k-mint"},
    )
    assert resp.status_code == 200, resp.text
    return drive, artifact, resp.json()


def _auth(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


# ── the shell ────────────────────────────────────────────────────────────────


async def test_shell_fails_closed_when_no_embed_origin_is_configured(http):
    resp = await http.get("/view/")
    assert resp.status_code == 200
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


async def test_shell_policy_is_exact_when_configured(http, monkeypatch):
    monkeypatch.setattr(settings, "viewer_embed_origins", CONSOLE_ORIGIN)
    resp = await http.get("/view/")
    assert resp.headers["content-security-policy"] == SHELL_CSP_EMBEDDABLE
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # The configured origins reach the shell script as inert JSON data.
    assert CONSOLE_ORIGIN in resp.text


async def test_the_private_shell_refuses_remote_images(http, monkeypatch):
    """A remote image in a PRIVATE document is a read receipt for its author.

    The public page allows `https:` images because its content is already
    published to anyone; on a private artifact the same directive would let
    an author embed a tracking pixel and silently learn who opened their
    document, from which IP, and when — with `Referrer-Policy` doing nothing
    to stop the request itself. Deliberately stricter than the public page,
    and pinned so parity-seeking never quietly restores it.
    """
    monkeypatch.setattr(settings, "viewer_embed_origins", CONSOLE_ORIGIN)
    csp = (await http.get("/view/")).headers["content-security-policy"]
    img_src = csp.split("img-src")[1].split(";")[0]
    assert "https:" not in img_src
    assert "http:" not in img_src
    # The artifact's OWN image still renders: it arrives as a credentialed
    # fetch swapped in as an object URL.
    assert "blob:" in img_src


async def test_shell_ignores_wildcard_and_junk_origins(http, monkeypatch):
    """Only a bare `scheme://host[:port]` survives.

    `https://*` is the one that matters: CSP honours it as "any https
    origin may frame this", so filtering only the bare `*` would leave a
    wildcard one character away. A trailing path is dropped too — CSP
    ignores it while the shell's own `event.origin` check does not, which
    would leave the page framable by an origin the script then refuses.
    """
    monkeypatch.setattr(
        settings,
        "viewer_embed_origins",
        "*, https://*, https://*.evil.test, javascript:alert(1), "
        f"https://a.test/path, https://b.test;script-src *, {CONSOLE_ORIGIN}",
    )
    resp = await http.get("/view/")
    csp = resp.headers["content-security-policy"]
    ancestors = csp.split("frame-ancestors")[1]
    assert ancestors.strip() == CONSOLE_ORIGIN
    for rejected in ("*", "javascript:", "evil.test", "a.test", "b.test"):
        assert rejected not in ancestors, rejected
    # And the shell only accepts a credential from the surviving origin.
    assert "evil.test" not in resp.text


async def test_shell_accepts_an_origin_with_a_port(http, monkeypatch):
    """Local dev consoles run on a port; that must still be expressible."""
    monkeypatch.setattr(settings, "viewer_embed_origins", "http://localhost:3000")
    resp = await http.get("/view/")
    assert "frame-ancestors http://localhost:3000" in (
        resp.headers["content-security-policy"]
    )


async def test_assets_are_allowlisted(http):
    for asset in (
        "shell.js",
        "viewer.css",
        "pdfview.js",
        "vendor/pdfjs/pdf.min.mjs",
        # pdf_viewer.css's page-loading spinner: the one image it pulls.
        "vendor/pdfjs/images/loading-icon.gif",
        "diagrams.js",
        "diagram-frame.html",
        "diagram-frame.js",
        "vendor/mermaid/mermaid.min.js",
    ):
        resp = await http.get(f"/view/static/{asset}")
        assert resp.status_code == 200, asset
    for missing in ("nope.js", "../../config.py", "..%2F..%2Fconfig.py"):
        resp = await http.get(f"/view/static/{missing}")
        assert resp.status_code == 404, missing


# ── credential transport and refusal ─────────────────────────────────────────


async def test_missing_header_is_401_and_bad_credential_is_the_uniform_404(http):
    for path in ("/view/doc", "/view/content"):
        resp = await http.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

        resp = await http.get(path, headers=_auth("not-a-credential"))
        assert resp.status_code == 404, path
        assert resp.json()["error"]["code"] == "VIEWER_SESSION_NOT_FOUND"
        assert resp.headers["cache-control"] == "private, no-store"


async def test_credential_is_never_accepted_in_a_query_string(http, override_actor):
    """The transport rule, asserted from the refusing side: a credential in
    the URL is not merely unsupported, it is indistinguishable from garbage —
    and the query parameter is not even read."""
    _, _, minted = await _minted(http, override_actor)
    resp = await http.get(f"/view/doc?credential={minted['credential']}")
    assert resp.status_code == 401


async def test_expired_and_unknown_are_indistinguishable(http, override_actor):
    _, _, minted = await _minted(http, override_actor)
    async with conn() as c:
        await c.execute(
            "UPDATE viewer_sessions SET expires_at = now() - interval '1 second' "
            "WHERE id=$1",
            minted["id"],
        )
    expired = await http.get("/view/doc", headers=_auth(minted["credential"]))
    unknown = await http.get("/view/doc", headers=_auth("A" * 43))
    assert expired.status_code == unknown.status_code == 404
    assert expired.json() == unknown.json()


async def test_deleted_artifact_stops_the_credential(http, override_actor):
    drive, artifact, minted = await _minted(http, override_actor)
    deleted = await http.delete(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}",
        headers={
            "Idempotency-Key": "k-del",
            "If-Match": f'"{artifact["revision"]}"',
        },
    )
    assert deleted.status_code == 200, deleted.text
    resp = await http.get("/view/doc", headers=_auth(minted["credential"]))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VIEWER_SESSION_NOT_FOUND"


async def test_grant_revocation_after_mint_stops_the_credential(http, override_actor):
    """Revoking the local grant takes effect at the NEXT resolution, not at
    the credential's expiry — the resolver re-checks the stored principal's
    current grant."""
    override_actor(make_actor())
    drive = await _create_drive(http, "d", "k-drive")
    artifact = await _create_artifact(
        http, drive, name="doc.md", body=b"# hello\n", key="k-art"
    )
    grant = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent", "principal_id": AGENT_B,
            "resource_type": "artifact", "resource_id": artifact["id"],
            "role": "viewer",
        },
        headers={"Idempotency-Key": "k-grant"},
    )
    assert grant.status_code == 201, grant.text

    override_actor(make_actor(subject=AGENT_B))
    minted = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/viewer-sessions",
        json={}, headers={"Idempotency-Key": "k-mint-b"},
    )
    assert minted.status_code == 200, minted.text
    credential = minted.json()["credential"]

    # The credential works while the grant lives...
    assert (await http.get("/view/doc", headers=_auth(credential))).status_code == 200

    # ...and dies the moment it is revoked.
    override_actor(make_actor())
    revoked = await http.delete(
        f"/v0/drives/{drive['id']}/grants/{grant.json()['id']}",
        headers={
            "Idempotency-Key": "k-revoke",
            "If-Match": f'"{grant.json()["revision"]}"',
        },
    )
    assert revoked.status_code == 200, revoked.text
    resp = await http.get("/view/doc", headers=_auth(credential))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VIEWER_SESSION_NOT_FOUND"


# ── the rendered document ────────────────────────────────────────────────────


async def test_doc_renders_markdown_with_raw_html_escaped(http, override_actor):
    _, artifact, minted = await _minted(
        http, override_actor,
        body=b"# Title\n\n<script>alert(1)</script>\n",
    )
    resp = await http.get("/view/doc", headers=_auth(minted["credential"]))
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["mode"] == "markdown"
    assert doc["title"] == "Title"
    assert doc["binding"] == {
        "drive_id": minted["drive_id"],
        "artifact_id": artifact["id"],
        "version_id": minted["version_id"],
    }
    # The renderer escapes; the raw tag never survives into the payload.
    assert "<script>alert(1)</script>" not in doc["html"]
    assert "&lt;script&gt;" in doc["html"]
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"


async def test_doc_renders_html_artifacts_as_escaped_source(http, override_actor):
    _, _, minted = await _minted(
        http, override_actor,
        body=b"<html><script>alert(1)</script></html>",
        content_type="text/html", name="page.html",
    )
    resp = await http.get("/view/doc", headers=_auth(minted["credential"]))
    doc = resp.json()
    # With `static_html_rendering_enabled` off — the shipped default — an
    # active document type is shown as highlighted, escaped source, exactly as
    # on the public surface. `tests/test_html_page_surfaces.py` pins what
    # changes, and what does not, when the flag is on.
    assert doc["mode"] == "code"
    assert "<script>alert(1)</script>" not in doc["html"]


async def test_oversized_containers_fall_back_to_download(http, override_actor, monkeypatch):
    # A container over its ceiling cannot be read in part: card, never fetched.
    from agentdrive import storage

    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    _, _, minted = await _minted(
        http, override_actor, body=b"PK\x03\x04" + b"x" * 40, content_type=docx, name="big.docx",
    )
    monkeypatch.setattr(render_module, "DOCUMENT_MAX_BYTES", 8)

    async def fail_if_fetched(*_args, **_kwargs):
        raise AssertionError("oversized container was fetched")

    monkeypatch.setattr(storage, "get", fail_if_fetched)
    resp = await http.get("/view/doc", headers=_auth(minted["credential"]))
    assert resp.status_code == 200
    assert resp.json()["mode"] == "download"


async def test_oversized_text_previews_its_head(http, override_actor, monkeypatch):
    _, _, minted = await _minted(http, override_actor, body=b"# big\n" * 10)
    monkeypatch.setattr(render_module, "MAX_RENDER_BYTES", 8)
    monkeypatch.setattr(render_module, "HEAD_PREVIEW_BYTES", 30)
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert doc["mode"] == "code"
    assert "# big" in doc["html"]
    assert "Showing the first 30 B of a" in doc["html"]


async def test_doc_pins_the_minted_version_after_the_head_moves(http, override_actor):
    drive, artifact, minted = await _minted(http, override_actor)
    append = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/versions",
        content=(
            b'--b\r\nContent-Disposition: form-data; name="content"; '
            b'filename="doc.md"\r\nContent-Type: text/markdown\r\n\r\n'
            b"# changed\n\r\n--b--\r\n"
        ),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": "k-append",
            "If-Match": f'"{artifact["revision"]}"',
        },
    )
    assert append.status_code == 201, append.text

    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert doc["binding"]["version_id"] == minted["version_id"]
    assert "hello" in doc["html"]
    assert "changed" not in doc["html"]

    content = await http.get("/view/content", headers=_auth(minted["credential"]))
    assert content.content == b"# hello\n"


# ── the bytes ────────────────────────────────────────────────────────────────


async def test_content_carries_the_byte_safety_headers(http, override_actor):
    _, _, minted = await _minted(
        http, override_actor, name="Résumé 研究.md"
    )
    resp = await http.get("/view/content", headers=_auth(minted["credential"]))
    assert resp.status_code == 200
    assert resp.content == b"# hello\n"
    assert resp.headers["content-security-policy"] == CONTENT_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"].startswith(
        'inline; filename="Rsum .md"; '
    )
    assert (
        "filename*=UTF-8''R%C3%A9sum%C3%A9%20%E7%A0%94%E7%A9%B6.md"
        in resp.headers["content-disposition"]
    )
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.headers["etag"] == f'"{minted["version_id"]}"'


async def test_active_content_downloads_as_attachment(http, override_actor):
    _, _, minted = await _minted(
        http, override_actor,
        body=b"<svg onload=alert(1)></svg>",
        content_type="image/svg+xml", name="Résumé 研究.svg",
    )
    resp = await http.get("/view/content", headers=_auth(minted["credential"]))
    assert resp.headers["content-disposition"].startswith(
        'attachment; filename="Rsum .svg"; '
    )
    assert (
        "filename*=UTF-8''R%C3%A9sum%C3%A9%20%E7%A0%94%E7%A9%B6.svg"
        in resp.headers["content-disposition"]
    )
    assert resp.headers["content-security-policy"] == CONTENT_CSP


async def test_pdf_mode_serves_the_pdfjs_shell(http, override_actor):
    _, _, minted = await _minted(
        http, override_actor,
        body=b"%PDF-1.4 fake", content_type="application/pdf", name="paper.pdf",
    )
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert doc["mode"] == "pdf"
    assert 'id="pdf-doc"' in doc["html"]
    # The engine the shell boots is served from this surface's own allowlist.
    assert (await http.get("/view/static/pdfview.js")).status_code == 200


async def test_the_embedded_shell_offers_print_for_the_console_to_gate(
    http, override_actor
):
    """Print is a console action here, not a frame action.

    `view.print()` is `window.open`, and the console embeds this frame with
    `sandbox="allow-scripts allow-same-origin"` — no `allow-popups` — so the
    frame cannot open the tab itself. The button is drawn anyway and the
    click is forwarded to the console, which holds the bytes and opens them
    on its own origin.

    The server draws it unconditionally because it cannot know which console
    build is on the other side of the frame. `shell.js` removes it unless
    the credential message declared `print: true`, so an older console gets
    no button rather than a dead one — asserted in the shell asset below.
    """
    _, _, minted = await _minted(
        http, override_actor,
        body=b"%PDF-1.4 fake", content_type="application/pdf", name="paper.pdf",
    )
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert 'id="pv-print"' in doc["html"]


async def test_the_shell_fails_print_closed_without_a_declaring_console(http):
    """The runtime half of the contract above.

    A dead button is the one outcome this surface must not produce, and the
    only thing standing between the drawn markup and that outcome is the
    shell removing it. Pin both sides of the branch so a refactor cannot
    quietly drop the removal and ship a control that does nothing.
    """
    shell = (await http.get("/view/static/shell.js")).text

    assert "canPrint = data.print === true" in shell
    assert "printBtn.remove()" in shell
    assert 'post("agentdrive.viewer.print")' in shell
    # The bytes the console prints from, and the guard on handing them over.
    assert 'post("agentdrive.viewer.printable", { blob })' in shell
    assert "blob.size > MAX_PRINTABLE_BYTES" in shell
    # The button is gated on the bytes ARRIVING, not merely on the console
    # declaring support: an oversized PDF is never pushed, and a button whose
    # click finds nothing on the other side is the same dead control.
    assert "if (canPrint && printOffered)" in shell
    assert "printOffered = true" in shell


async def test_a_markdown_diagram_tells_the_shell_to_draw_it(http, override_actor):
    _, _, minted = await _minted(
        http, override_actor,
        body=b"# Plan\n\n```mermaid\ngraph TD; A-->B;\n```\n",
        content_type="text/markdown", name="plan.md",
    )
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert doc["mode"] == "markdown"
    assert doc["diagrams"] is True
    assert 'data-diagram="mermaid"' in doc["html"]
    # The engine the shell loads on that signal is served from this surface's
    # own allowlist, like pdf.js.
    assert (await http.get("/view/static/vendor/mermaid/mermaid.min.js")).status_code == 200


async def test_a_plain_document_does_not_ask_the_shell_for_the_engine(http, override_actor):
    _, _, minted = await _minted(
        http, override_actor, body=b"# Plain\n", content_type="text/markdown", name="p.md",
    )
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert doc["diagrams"] is False


async def test_the_diagram_engine_frame_carries_its_own_policy(http, monkeypatch):
    """The one asset that is a page. It may use inline styles — mermaid needs
    them to measure text — and may run only this origin's scripts; the page
    that frames it keeps `style-src 'self'` untouched. Its ancestors are the
    shell's own plus `'self'`, because the browser checks the whole chain:
    console → shell → engine frame."""
    from agentdrive.public.routes import diagram_frame_csp

    monkeypatch.setattr(settings, "viewer_embed_origins", CONSOLE_ORIGIN)
    resp = await http.get("/view/static/diagram-frame.html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    csp = resp.headers["content-security-policy"]
    assert csp == diagram_frame_csp(CONSOLE_ORIGIN)
    assert "script-src 'self'" in csp and "style-src 'unsafe-inline'" in csp
    assert csp.endswith(f"frame-ancestors 'self' {CONSOLE_ORIGIN}")
    # Every other asset is policy-free bytes, as before.
    assert "content-security-policy" not in (await http.get("/view/static/diagrams.js")).headers


async def test_the_engine_frame_fails_closed_without_configured_ancestors(http, monkeypatch):
    monkeypatch.setattr(settings, "viewer_embed_origins", "")
    csp = (await http.get("/view/static/diagram-frame.html")).headers["content-security-policy"]
    assert csp.endswith("frame-ancestors 'self'")


async def test_an_oversized_csv_previews_through_ranged_reads(http, override_actor, monkeypatch):
    from agentdrive import storage
    from agentdrive.rendering import render as render_module

    monkeypatch.setattr(render_module, "MAX_RENDER_BYTES", 64)
    monkeypatch.setattr(render_module, "HEAD_PREVIEW_BYTES", 256)

    async def fail_if_fetched(*_args, **_kwargs):
        raise AssertionError("oversized csv was fetched whole")

    monkeypatch.setattr(storage, "get", fail_if_fetched)
    body = b"id,name\n" + b"".join(f"{i},row-{i}\n".encode() for i in range(200))
    _, _, minted = await _minted(
        http, override_actor, body=body, content_type="text/csv", name="big.csv",
    )
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert doc["mode"] == "table"
    assert "<td>0</td><td>row-0</td>" in doc["html"]
    assert "rows from the first 256 B of a" in doc["html"]


async def test_an_oversized_markdown_previews_its_head_and_is_never_fetched_whole(
    http, override_actor, monkeypatch
):
    from agentdrive import storage
    from agentdrive.rendering import render as render_module

    monkeypatch.setattr(render_module, "MAX_RENDER_BYTES", 64)
    monkeypatch.setattr(render_module, "HEAD_PREVIEW_BYTES", 100)

    async def fail_if_fetched(*_args, **_kwargs):
        raise AssertionError("oversized markdown was fetched whole")

    monkeypatch.setattr(storage, "get", fail_if_fetched)
    _, _, minted = await _minted(
        http, override_actor, body=b"# Big\n" + b"x" * 500, content_type="text/markdown",
        name="big.md",
    )
    doc = (await http.get("/view/doc", headers=_auth(minted["credential"]))).json()
    assert doc["mode"] == "code"  # a fragment is source, not a cut-off document
    assert "Showing the first 100 B of a" in doc["html"]
