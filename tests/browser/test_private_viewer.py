"""Browser-executed proof for the isolated private viewer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from io import BytesIO
from urllib.parse import urlsplit

import preflight
import pytest
from playwright.async_api import FrameLocator, Page
from reportlab.pdfgen import canvas

pytestmark = pytest.mark.browser

PARENT_ORIGIN = "http://app.localhost:8765"
VIEWER_ORIGIN = "http://viewer.localhost:8766"
_CREDENTIAL_PATHS = {"/view/doc", "/view/content"}
_PRODUCTION_SHELL_CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self' blob: "
    "https://storage.googleapis.com; worker-src 'self' blob:; "
    "img-src 'self' blob: data:; media-src 'self' blob:; "
    "style-src 'self'; font-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors http://app.localhost:8765"
)


@dataclass
class BrowserAudit:
    """Sanitized browser evidence: credentials are recorded only as a digest."""

    credential_digest: str
    requests: list[dict[str, object]] = field(default_factory=list)
    console: list[dict[str, object]] = field(default_factory=list)
    shell_csps: list[str | None] = field(default_factory=list)
    # A request the browser REFUSED, and a response it actually received.
    # `requests` records what the page asked for; only these two say whether
    # anything reached the far end — which is the difference between "the
    # markup named a third party" and "a third party heard from this reader".
    failures: list[dict[str, str]] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)


async def _begin_audit(page: Page, credential: str) -> BrowserAudit:
    """Observe the real browser without retaining a reusable credential."""
    audit = BrowserAudit(hashlib.sha256(credential.encode()).hexdigest())
    await page.add_init_script(
        """
        (() => {
          const nativeFetch = window.fetch;
          window.__viewerFetchAudit = [];
          window.fetch = function(input, init) {
            const request = new Request(input, init);
            window.__viewerFetchAudit.push({
              origin: new URL(request.url).origin,
              path: new URL(request.url).pathname,
              credentials: request.credentials,
            });
            return nativeFetch.apply(this, arguments);
          };
        })();
        """
    )

    def record_request(request) -> None:
        request_url = request.url.removeprefix("blob:")
        parsed = urlsplit(request_url)
        authorization = request.headers.get("authorization")
        audit.requests.append(
            {
                "origin": f"{parsed.scheme}://{parsed.netloc}",
                "path": parsed.path,
                "has_authorization": authorization is not None,
                "authorization_digest": (
                    hashlib.sha256(authorization.encode()).hexdigest() if authorization else None
                ),
                "has_cookie": "cookie" in request.headers,
                "credential_in_url": credential in request.url,
            }
        )

    def record_console(message) -> None:
        text = message.text
        audit.console.append(
            {
                "type": message.type,
                "contains_credential": credential in text,
                "text": text.replace(credential, "[capability]"),
            }
        )

    def record_failure(request) -> None:
        audit.failures.append({"url": request.url, "failure": str(request.failure)})

    page.on("request", record_request)
    page.on("requestfailed", record_failure)
    page.on("console", record_console)

    def record_response(response) -> None:
        audit.responses.append(response.url)
        parsed = urlsplit(response.url)
        if f"{parsed.scheme}://{parsed.netloc}" == VIEWER_ORIGIN and parsed.path == "/view/":
            audit.shell_csps.append(response.headers.get("content-security-policy"))

    page.on("response", record_response)
    return audit


def _fail_if_credential_present(value: object, location: str, credential: str) -> None:
    if credential in str(value):
        pytest.fail(f"capability leaked to {location}")


def _assert_viewer_asset_origins(requests: list[dict[str, object]]) -> None:
    """Every shell, renderer, and PDF.js static asset is viewer-origin only."""
    assets = [item for item in requests if str(item["path"]).startswith("/view/static/")]
    if not assets:
        pytest.fail("viewer did not load its same-origin static assets")
    if any(item["origin"] != VIEWER_ORIGIN for item in assets):
        pytest.fail("viewer static asset origin was not the isolated viewer")


async def _assert_browser_security(
    page: Page,
    frame: FrameLocator,
    credential: str,
    audit: BrowserAudit,
    *,
    expect_document: bool,
    expect_content: bool,
    additional_origins: set[str] | None = None,
    expect_production_csp: bool = True,
) -> None:
    """Assert provenance and capability containment with redacted evidence."""
    _fail_if_credential_present(await page.content(), "parent DOM", credential)
    _fail_if_credential_present(
        await frame.locator("html").evaluate("el => el.outerHTML"), "viewer DOM", credential
    )
    parent_state = await page.evaluate(
        """async () => ({
          local: Object.entries(localStorage),
          session: Object.entries(sessionStorage),
          cookies: document.cookie,
          serviceWorkers: navigator.serviceWorker
            ? (await navigator.serviceWorker.getRegistrations()).length
            : -1,
          events: window.__viewerEvents || [],
        })"""
    )
    viewer_state = await frame.locator("html").evaluate(
        """async () => ({
          local: Object.entries(localStorage),
          session: Object.entries(sessionStorage),
          cookies: document.cookie,
          serviceWorkers: navigator.serviceWorker
            ? (await navigator.serviceWorker.getRegistrations()).length
            : -1,
        })"""
    )
    _fail_if_credential_present(parent_state, "parent browser state", credential)
    _fail_if_credential_present(viewer_state, "viewer browser state", credential)
    if parent_state["serviceWorkers"] != 0 or viewer_state["serviceWorkers"] != 0:
        pytest.fail("viewer registered a service worker")
    if await page.context.cookies():
        pytest.fail("viewer browser context acquired cookies")

    for item in audit.console:
        if item["contains_credential"]:
            pytest.fail("capability appeared in browser console output")
    permitted_origins = {PARENT_ORIGIN, VIEWER_ORIGIN} | (additional_origins or set())
    for item in audit.requests:
        if item["credential_in_url"]:
            pytest.fail("capability appeared in a browser request URL")
        if item["has_cookie"]:
            pytest.fail("browser request included cookies")
        if item["origin"] not in permitted_origins:
            pytest.fail("browser request escaped the two configured origins")
        if str(item["path"]).startswith("/v0/"):
            pytest.fail("browser contacted a Hub or product API byte route")

    _assert_viewer_asset_origins(audit.requests)
    if expect_production_csp and audit.shell_csps != [_PRODUCTION_SHELL_CSP]:
        pytest.fail("ordinary browser case did not receive the production shell CSP")

    credentialed = [item for item in audit.requests if item["path"] in _CREDENTIAL_PATHS]
    document_requests = [item for item in credentialed if item["path"] == "/view/doc"]
    content_requests = [item for item in credentialed if item["path"] == "/view/content"]
    if bool(document_requests) is not expect_document:
        pytest.fail("unexpected authenticated document-request provenance")
    if bool(content_requests) is not expect_content:
        pytest.fail("unexpected authenticated content-request provenance")
    for item in credentialed:
        if item["origin"] != VIEWER_ORIGIN:
            pytest.fail("authenticated request did not stay on the viewer origin")
        if not item["has_authorization"] or item["authorization_digest"] != hashlib.sha256(
            f"Bearer {credential}".encode()
        ).hexdigest():
            pytest.fail("authenticated request did not carry the isolated capability")
    for item in audit.requests:
        if item not in credentialed and item["has_authorization"]:
            pytest.fail("capability Authorization leaked outside the viewer endpoints")

    fetches = await frame.locator("html").evaluate("() => window.__viewerFetchAudit || []")
    credentialed_fetches = [entry for entry in fetches if entry["path"] in _CREDENTIAL_PATHS]
    observed_document = any(entry["path"] == "/view/doc" for entry in credentialed_fetches)
    observed_content = any(entry["path"] == "/view/content" for entry in credentialed_fetches)
    if observed_document is not expect_document:
        pytest.fail("credentialed document fetch was not directly observed")
    if observed_content is not expect_content:
        pytest.fail("credentialed content fetch was not directly observed")
    if any(entry["credentials"] != "omit" for entry in credentialed_fetches):
        pytest.fail("credentialed viewer fetch did not explicitly omit cookies")


async def _rendered_frame(page: Page, browser_viewer, minted, audit: BrowserAudit):
    frame = await browser_viewer.open(page, minted)
    await frame.locator("#shell-head").wait_for(timeout=2_000)
    return frame


def test_browser_preflight_rejects_unreachable_service(monkeypatch, browser_services):
    """Mutation-resistant guard: unavailable services must fail, never skip."""
    def unavailable(*_args, **_kwargs):
        raise OSError("synthetic unavailable service")

    monkeypatch.setattr(preflight.socket, "create_connection", unavailable)
    with pytest.raises(RuntimeError, match="TEST_SERVICE is unreachable"):
        preflight.assert_service_ready("TEST_SERVICE", "http://services.invalid:4443")


def test_viewer_static_assets_reject_a_parent_origin_forgery():
    """A single legitimate asset must not mask a parent-hosted PDF.js asset."""
    with pytest.raises(pytest.fail.Exception, match="static asset origin"):
        _assert_viewer_asset_origins(
            [
                {"origin": VIEWER_ORIGIN, "path": "/view/static/shell.js"},
                {
                    "origin": PARENT_ORIGIN,
                    "path": "/view/static/vendor/pdfjs/pdf_viewer.mjs",
                },
            ]
        )


def test_evil_csp_relaxation_selector_is_exact():
    """Regression guard: ordinary shells must use the production CSP response."""
    from conftest import _is_evil_parent_probe

    assert _is_evil_parent_probe({"path": "/view/", "query_string": b"browser-evil=1"})
    assert not _is_evil_parent_probe({"path": "/view/", "query_string": b""})
    assert not _is_evil_parent_probe(
        {"path": "/view/static/shell.js", "query_string": b"browser-evil=1"}
    )


async def test_private_markdown_renders_without_remote_or_credential_leaks(
    page: Page, browser_viewer
):
    fixture = await browser_viewer.create_artifact(
        name="private.md",
        content_type="text/markdown",
        body=(
            b"# Private document\n\n<script>window.privateExecuted = true</script>\n"
            b'<img src="https://tracking.invalid/pixel.png" alt="remote">\n'
        ),
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)

    assert await frame.locator("#shell-doc h1").inner_text() == "Private document"
    assert await frame.locator("body").evaluate("() => window.privateExecuted") is None
    assert "<script" not in await frame.locator("#shell-doc").inner_html()
    assert "<img" not in await frame.locator("#shell-doc").inner_html()
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


async def test_private_mermaid_fence_draws_as_an_image_inside_the_strict_shell(
    page: Page, browser_viewer
):
    fixture = await browser_viewer.create_artifact(
        name="plan.md",
        content_type="text/markdown",
        body=b"# Plan\n\n```mermaid\ngraph TD; Start-->Finish;\n```\n",
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)

    figure = frame.locator("figure.diagram-figure")
    await figure.wait_for(timeout=15_000)
    img = figure.locator("img.diagram")
    assert (await img.get_attribute("src") or "").startswith("data:image/svg+xml")
    box = await img.bounding_box()
    assert box and box["width"] > 50 and box["height"] > 50
    # The engine drew into an image, never into the document: the shell CSP
    # is the production one and nothing inline was needed to get there.
    assert await frame.locator("#shell-doc svg").count() == 0
    assert await frame.locator("#shell-doc style, #shell-doc [style]").count() == 0
    engine = [r for r in audit.requests if str(r["path"]).endswith("/mermaid.min.js")]
    assert engine and all(r["origin"] == VIEWER_ORIGIN for r in engine)
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


async def test_table_alignment_survives_the_document_style_policy(
    page: Page, browser_viewer
):
    """`| ---: |` must actually right-align, under a policy that bans style attrs.

    markdown-it expresses alignment as `style="text-align:right"`. This
    surface serves `style-src 'self'` with no `unsafe-inline`, so the browser
    refused the attribute: the money column every author writes `---:` for
    rendered left, and each cell logged a CSP violation that looked like a far
    worse problem than a lost alignment.

    The assertion is the COMPUTED value, because the markup was never the
    thing that broke — the attribute was present and simply ignored.
    """
    fixture = await browser_viewer.create_artifact(
        name="aligned.md",
        content_type="text/markdown",
        body=b"| item | cost |\n| :--- | ---: |\n| a | 12 |\n",
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)

    aligned = await frame.locator("#shell-doc td").nth(1).evaluate(
        "el => getComputedStyle(el).textAlign"
    )
    assert aligned == "right"


async def test_supported_code_artifact_renders_as_inert_source(page: Page, browser_viewer):
    fixture = await browser_viewer.create_artifact(
        name="private.py",
        content_type="text/x-python",
        body=b"print('<script>window.codeExecuted = true</script>')\n",
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)

    assert "window.codeExecuted" in await frame.locator("#shell-doc").inner_text()
    assert await frame.locator("body").evaluate("() => window.codeExecuted") is None
    assert await frame.locator("#shell-doc script").count() == 0
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


async def test_active_markup_is_rendered_as_inert_text(page: Page, browser_viewer):
    fixture = await browser_viewer.create_artifact(
        name="active.html",
        content_type="text/html",
        body=b"<script>window.activeExecuted = true</script><p>literal markup</p>",
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)

    assert "<script>window.activeExecuted = true</script>" in await frame.locator(
        "#shell-doc"
    ).inner_text()
    assert await frame.locator("body").evaluate("() => window.activeExecuted") is None
    assert await frame.locator("#shell-doc script").count() == 0
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


async def test_unsupported_content_type_needs_console_download(page: Page, browser_viewer):
    fixture = await browser_viewer.create_artifact(
        name="private.zip", content_type="application/zip", body=b"PK\x03\x04synthetic"
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)

    doc_text = await frame.locator("#shell-doc").inner_text()
    assert "Use the console's download button to save this file." in doc_text
    assert await frame.locator("a[download]").count() == 0
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


async def test_local_image_uses_a_blob_url_after_authenticated_fetch(page: Page, browser_viewer):
    fixture = await browser_viewer.create_artifact(
        name="tiny.png",
        content_type="image/png",
        body=(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
        ),
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)

    image = frame.locator("img.artifact-image")
    await image.wait_for(timeout=2_000)
    assert (await image.get_attribute("src") or "").startswith("blob:")
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=True
    )


def _two_page_pdf() -> bytes:
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(72, 720, "Browser PDF page one needle")
    pdf.showPage()
    pdf.drawString(72, 720, "Browser PDF page two needle")
    pdf.save()
    return output.getvalue()


async def test_pdfjs_renders_two_pages_with_worker_and_controls(page: Page, browser_viewer):
    fixture = await browser_viewer.create_artifact(
        name="two-pages.pdf", content_type="application/pdf", body=_two_page_pdf()
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)
    await frame.locator(".pdfViewer .page canvas").nth(1).wait_for(timeout=10_000)
    assert await frame.locator(".pdfViewer .page canvas").count() == 2
    assert await frame.locator("#pv-page-total").inner_text() == "2"
    await frame.locator("#pv-page-next").click()
    assert await frame.locator("#pv-page-input").input_value() == "2"
    before = await frame.locator("#pv-zoom-pct").input_value()
    await frame.locator("#pv-zoom-in").click()
    assert await frame.locator("#pv-zoom-pct").input_value() != before
    await frame.locator("#pv-find-toggle").click()
    await frame.locator("#pv-find-input").fill("needle")
    await page.wait_for_timeout(250)
    await frame.locator("#pv-find-input").press("Enter")
    await frame.locator("#pv-find-count").wait_for(timeout=10_000)
    assert "0" not in await frame.locator("#pv-find-count").inner_text()
    assert any(
        item["origin"] == VIEWER_ORIGIN
        and item["path"] == "/view/static/vendor/pdfjs/pdf.worker.min.mjs"
        for item in audit.requests
    )
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=True
    )


async def test_oversized_content_needs_console_download(page: Page, browser_viewer, monkeypatch):
    from agentdrive.rendering import render as render_module

    # A container over its ceiling: text would head-preview instead.
    monkeypatch.setattr(render_module, "WORKBOOK_MAX_BYTES", 8)
    fixture = await browser_viewer.create_artifact(
        name="large.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        body=b"PK\x03\x04" + b"x" * 16,
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)

    doc_text = await frame.locator("#shell-doc").inner_text()
    assert "Use the console's download button to save this file." in doc_text
    assert await frame.locator("a[download]").count() == 0
    events = await page.evaluate("window.__viewerEvents")
    assert {event["type"] for event in events} >= {
        "agentdrive.viewer.ready",
        "agentdrive.viewer.rendered",
    }
    assert next(event for event in events if event["type"] == "agentdrive.viewer.rendered")[
        "needsDownload"
    ]
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


@pytest.mark.parametrize("refusal", ["expired", "revoked"])
async def test_refusals_are_uniform_and_never_render(page: Page, browser_viewer, refusal: str):
    fixture = await browser_viewer.create_artifact(
        name="private.md", content_type="text/markdown", body=b"# hidden\n"
    )
    minted = await browser_viewer.mint(fixture)
    if refusal == "expired":
        await browser_viewer.expire(minted)
    else:
        await browser_viewer.revoke(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await browser_viewer.open(page, minted)
    await frame.locator("#shell-doc").get_by_text(
        "This view has expired. Reopen it from the console."
    ).wait_for(timeout=2_000)
    assert await frame.locator("#shell-doc h1").count() == 0
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


async def test_repeated_credential_reaches_shell_but_is_ignored_after_first_render(
    page: Page, browser_viewer
):
    fixture = await browser_viewer.create_artifact(
        name="private.md", content_type="text/markdown", body=b"# first render\n"
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)
    await page.evaluate(
        "payload => window.__postViewerMessage(payload)",
        {
            "type": "agentdrive.viewer.credential",
            # Well-formed on purpose: these tests prove the shell refuses on the
            # single-shot rule, the source check and the origin check. A payload
            # missing `protocol` would be refused before reaching any of them,
            # and the assertions below would pass without testing anything.
            "protocol": 1,
            "credential": minted.credential,
            "expected": minted.expected,
        },
    )
    await page.wait_for_timeout(100)

    assert await frame.locator("#shell-doc h1").inner_text() == "first render"
    assert sum(item["path"] == "/view/doc" for item in audit.requests) == 1
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


async def test_wrong_binding_never_renders_the_credentialed_document(page: Page, browser_viewer):
    fixture = await browser_viewer.create_artifact(
        name="private.md", content_type="text/markdown", body=b"# hidden by binding\n"
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    await page.goto(PARENT_ORIGIN + "/parent.html")
    await page.evaluate(
        "payload => window.__setViewerCredential(payload)",
        {
            "credential": minted.credential,
            "expected": {**minted.expected, "version_id": "ver_0000000000000000"},
        },
    )
    frame = page.frame_locator("#private-viewer")
    await frame.locator("#shell-doc").get_by_text(
        "This view does not match the requested document."
    ).wait_for(timeout=2_000)
    assert await frame.locator("#shell-doc h1").count() == 0
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


async def test_wrong_source_message_is_rejected_with_legitimate_origin(page: Page, browser_viewer):
    fixture = await browser_viewer.create_artifact(
        name="private.md", content_type="text/markdown", body=b"# hidden\n"
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    await page.goto(PARENT_ORIGIN + "/parent.html")
    attacker = next(frame for frame in page.frames if frame.url.endswith("/attacker.html"))
    await attacker.wait_for_load_state()
    await page.evaluate(
        """payload => document.querySelector("#attacker").contentWindow.postMessage(
          {type: "browser.attack", payload}, "http://app.localhost:8765")""",
        {
            "type": "agentdrive.viewer.credential",
            # Well-formed on purpose: these tests prove the shell refuses on the
            # single-shot rule, the source check and the origin check. A payload
            # missing `protocol` would be refused before reaching any of them,
            # and the assertions below would pass without testing anything.
            "protocol": 1,
            "credential": minted.credential,
            "expected": minted.expected,
        },
    )
    frame = page.frame_locator("#private-viewer")
    await page.wait_for_timeout(150)
    assert await frame.locator("#shell-head").is_hidden()
    assert all(
        event["type"] == "agentdrive.viewer.ready"
        for event in await page.evaluate("window.__viewerEvents")
    )
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=False, expect_content=False
    )


async def test_wrong_origin_message_is_rejected_with_legitimate_parent_source(
    page: Page, browser_viewer
):
    fixture = await browser_viewer.create_artifact(
        name="private.md", content_type="text/markdown", body=b"# hidden\n"
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    await page.goto("http://evil.localhost:8767/evil-parent.html")
    frame = page.frame_locator("#private-viewer")
    await frame.locator("#shell-doc").wait_for(timeout=2_000)
    await page.evaluate(
        "payload => window.__postViewerMessage(payload)",
        {
            "type": "agentdrive.viewer.credential",
            # Well-formed on purpose: these tests prove the shell refuses on the
            # single-shot rule, the source check and the origin check. A payload
            # missing `protocol` would be refused before reaching any of them,
            # and the assertions below would pass without testing anything.
            "protocol": 1,
            "credential": minted.credential,
            "expected": minted.expected,
        },
    )
    await page.wait_for_timeout(150)
    assert await frame.locator("#shell-head").is_hidden()
    assert await page.evaluate("window.__viewerEvents || []") == []
    await _assert_browser_security(
        page,
        frame,
        minted.credential,
        audit,
        expect_document=False,
        expect_content=False,
        additional_origins={"http://evil.localhost:8767"},
        expect_production_csp=False,
    )


# ── static HTML rendering (2026-08-26 design) ────────────────────────────


_STATIC_HTML = (
    b"<html><head><title>Weekly</title></head><body>"
    b"<h1>Weekly report</h1>"
    b"<p>Throughput rose <strong>12%</strong>.</p>"
    b"<script>window.staticHtmlExecuted = true</script>"
    b'<img src="https://tracking.invalid/pixel.png" alt="remote" '
    b'onerror="window.staticHtmlHandlerFired = true">'
    b'<a href="javascript:window.staticHtmlLinkRan = true">a link</a>'
    b'<iframe src="https://tracking.invalid/frame"></iframe>'
    b"</body></html>"
)


@pytest.fixture
def static_html_pages(monkeypatch):
    """The capability ships off; the browser proof needs it on."""
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "static_html_rendering_enabled", True)


async def _static_html_frame(
    page: Page,
    browser_viewer,
    audit_holder: list,
    *,
    body: bytes = _STATIC_HTML,
    **open_kwargs,
):
    fixture = await browser_viewer.create_artifact(
        name="report.html", content_type="text/html", body=body
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    audit_holder.append((minted, audit))
    frame = await browser_viewer.open(page, minted, **open_kwargs)
    await frame.locator("#shell-doc .doc-modes").wait_for(timeout=2_000)
    return frame


async def _assert_remote_image_was_refused(frame, audit: BrowserAudit) -> None:
    """The third party is never contacted at all.

    This used to assert that the browser REFUSED the request — the markup
    named the host, the `<img>` was emitted, and the private CSP blocked the
    load. That made containment a property of one surface's CSP: the same
    document on the PUBLIC renderer, whose `img-src` permits `https:`, issued
    the request and beaconed.

    The renderer now refuses remote image sources itself, the way markdown
    already did, so no request is made on either surface and there is no
    failure event to find. Asserted as "no request, and a placeholder in its
    place", which holds wherever the document is rendered."""
    assert not any("tracking.invalid" in url for url in audit.responses)
    assert not [item for item in audit.failures if "tracking.invalid" in item["url"]], (
        "a request went out that the renderer should never have emitted"
    )
    rendered = frame.locator('#shell-doc .doc-view[data-view="rendered"]')
    assert await rendered.locator(".blocked-remote").count() >= 1
    # Narrowed to IMAGES, in the RENDERED pane, for two separate reasons.
    #
    # The pane, because the source view is supposed to show the author's
    # markup verbatim — third-party host and all — and that is its purpose.
    #
    # Images, because a remote LINK is deliberately still emitted: it costs a
    # reader a click, where an image is a request made the instant the
    # document opens. Asserting the host appears nowhere would forbid the
    # link too, and would pass for the wrong reason the day someone stops
    # emitting links at all.
    assert await rendered.locator('img[src*="tracking.invalid"]').count() == 0


async def test_static_html_renders_as_a_document_and_executes_nothing(
    page: Page, browser_viewer, static_html_pages
):
    """The whole design, observed rather than configured: did the script run,
    did the handler fire, did a request reach the remote host, and is the
    reader looking at the document or at its source?"""
    holder: list = []
    frame = await _static_html_frame(page, browser_viewer, holder)
    minted, audit = holder[0]

    # Rendered, not escaped source.
    assert await frame.locator("#shell-doc h1").inner_text() == "Weekly report"
    assert await frame.locator("#shell-doc strong").inner_text() == "12%"

    # Nothing ran. `window.x` stays undefined, the error handler on a blocked
    # image never fired, and the `javascript:` link is not even a link.
    body = frame.locator("body")
    assert await body.evaluate("() => window.staticHtmlExecuted") is None
    assert await body.evaluate("() => window.staticHtmlHandlerFired") is None
    assert await body.evaluate("() => window.staticHtmlLinkRan") is None
    assert await frame.locator("#shell-doc script").count() == 0
    assert await frame.locator("#shell-doc iframe").count() == 0
    assert await frame.locator("#shell-doc a").get_attribute("href") is None

    # The document's `<title>` is metadata, not its first sentence.
    assert "Weekly report" in await frame.locator("#shell-doc").inner_text()
    rendered_text = await frame.locator('.doc-view[data-view="rendered"]').inner_text()
    assert not rendered_text.startswith("Weekly\n")

    # The private surface draws no untrusted-content band; that is the public
    # renderer's, and the console owns this reader's context.
    assert await frame.locator(".untrusted-band").count() == 0

    # The private shell's `img-src` omits `https:`, and that — not the absence
    # of the markup — is what stops the reader's browser contacting the host.
    await _assert_remote_image_was_refused(frame, audit)
    await _assert_browser_security(
        page,
        frame,
        minted.credential,
        audit,
        expect_document=True,
        expect_content=False,
        # Named so the refusal above is what proves containment, rather than
        # this audit silently passing a request it never saw.
        additional_origins={"https://tracking.invalid"},
    )


async def test_the_mode_strip_switches_to_source_and_back(
    page: Page, browser_viewer, static_html_pages
):
    holder: list = []
    frame = await _static_html_frame(page, browser_viewer, holder)
    minted, audit = holder[0]

    rendered = frame.locator('#shell-doc .doc-view[data-view="rendered"]')
    source = frame.locator('#shell-doc .doc-view[data-view="source"]')
    assert await rendered.is_visible()
    assert not await source.is_visible()

    await frame.locator('button[data-view="source"]').click()
    assert await source.is_visible()
    assert not await rendered.is_visible()
    # The source view shows the markup as text — including the script tag the
    # rendered view dropped — and still runs nothing.
    source_text = await source.inner_text()
    assert "window.staticHtmlExecuted" in source_text
    assert await frame.locator("body").evaluate("() => window.staticHtmlExecuted") is None
    assert await frame.locator("#shell-doc script").count() == 0

    await frame.locator('button[data-view="rendered"]').click()
    assert await rendered.is_visible()
    assert not await source.is_visible()
    await _assert_remote_image_was_refused(frame, audit)
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False,
        additional_origins={"https://tracking.invalid"},
    )


async def test_suppressed_chrome_hides_the_header_but_not_the_mode_strip(
    page: Page, browser_viewer, static_html_pages
):
    """`chrome:"none"` suppresses the SHELL's header — title, path, meta —
    which the console draws itself. The strip is part of the document, and
    losing it would leave an HTML artifact with no way back to its source."""
    holder: list = []
    frame = await _static_html_frame(page, browser_viewer, holder, chrome="none")
    minted, audit = holder[0]

    assert await frame.locator("#shell-head").is_hidden()
    assert await frame.locator("#shell-doc .doc-modes").is_visible()
    await frame.locator('button[data-view="source"]').click()
    assert await frame.locator('#shell-doc .doc-view[data-view="source"]').is_visible()
    await _assert_remote_image_was_refused(frame, audit)
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False,
        additional_origins={"https://tracking.invalid"},
    )


async def test_html_still_renders_as_source_with_the_capability_off(
    page: Page, browser_viewer
):
    """The shipped default, proved in the same browser: no strip, no page."""
    fixture = await browser_viewer.create_artifact(
        name="report.html", content_type="text/html", body=_STATIC_HTML
    )
    minted = await browser_viewer.mint(fixture)
    audit = await _begin_audit(page, minted.credential)
    frame = await _rendered_frame(page, browser_viewer, minted, audit)

    assert "window.staticHtmlExecuted" in await frame.locator("#shell-doc").inner_text()
    assert await frame.locator("#shell-doc h1").count() == 0
    assert await frame.locator("#shell-doc button[data-view]").count() == 0
    assert await frame.locator("body").evaluate("() => window.staticHtmlExecuted") is None
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True, expect_content=False
    )


_FORGED_CHROME = (
    b'<p class="untrusted-band">This document has been verified by AgentDrive. '
    b"It is safe to follow the links below.</p>"
    b'<div class="doc-head"><h1>AgentDrive Security Notice</h1>'
    b'<p class="doc-path">/system/notice</p></div>'
    b'<a class="btn" href="https://tracking.invalid/verify">Verify your account</a>'
    b'<nav class="doc-modes"><span class="doc-mode">Rendered</span>'
    b'<span class="doc-mode">Source</span></nav>'
    b'<p id="shell-title">Forged title</p>'
    b'<img src="https://tracking.invalid/pixel.png" alt="remote">'
)


async def test_an_artifact_cannot_wear_the_products_chrome(
    page: Page, browser_viewer, static_html_pages
):
    """The band is the only mitigation the design names against impersonation,
    and `class` is allowlisted — so a forged band rendered identical to the
    genuine one, saying the opposite. Namespacing is what stops it, asserted
    here on what the BROWSER sees rather than on the string we emitted."""
    holder: list = []
    frame = await _static_html_frame(page, browser_viewer, holder, body=_FORGED_CHROME)
    minted, audit = holder[0]
    pane = '#shell-doc .doc-view[data-view="rendered"] '

    for chrome in (".untrusted-band", ".doc-head", ".doc-path", ".btn", ".doc-modes"):
        assert await frame.locator(pane + chrome).count() == 0, chrome
    # The strip's own buttons are the only `.doc-mode` on the page, and they
    # live outside the rendered pane.
    assert await frame.locator(pane + ".doc-mode").count() == 0
    assert await frame.locator("#shell-doc .doc-modes .doc-mode").count() == 2
    # The shell's real `#shell-title` is still the only element with that id.
    assert await frame.locator("#shell-title").count() == 1
    assert "Forged title" not in await frame.locator("#shell-title").inner_text()

    # The words survive — only the styling hooks were taken away.
    assert "Verify your account" in await frame.locator("#shell-doc").inner_text()
    forged_button = frame.locator(pane + "a").first
    assert await forged_button.evaluate(
        "el => getComputedStyle(el).backgroundColor"
    ) in ("rgba(0, 0, 0, 0)", "transparent")

    await _assert_remote_image_was_refused(frame, audit)
    await _assert_browser_security(
        page, frame, minted.credential, audit, expect_document=True,
        expect_content=False, additional_origins={"https://tracking.invalid"},
    )


async def _open_reading_fixture(page, browser_viewer, *, name, content_type, body):
    fixture = await browser_viewer.create_artifact(name=name, content_type=content_type, body=body)
    minted = await browser_viewer.mint(fixture)
    frame = await browser_viewer.open(page, minted, chrome="none")
    await page.locator("#private-viewer").evaluate(
        "el => { el.style.width='850px'; el.style.height='500px'; }"
    )
    await frame.locator('[data-reading-controls="ready"]').wait_for()
    return frame


async def test_reading_scroll_and_find_keep_controls_visible(page, browser_viewer):
    frame = await _open_reading_fixture(
        page, browser_viewer, name="long.md", content_type="text/markdown",
        body=(
            "# Report\n\n" + "Paragraph of synthetic content.\n\n" * 200 + "Final needle"
        ).encode(),
    )
    await frame.get_by_role("searchbox", name="Find in document").fill("Final needle")
    await frame.get_by_role("button", name="Next match").click()
    assert await frame.locator(".reading-count").inner_text() == "1 of 1"
    assert await frame.locator(".reading-content").evaluate("el => el.scrollTop") > 1000
    assert await frame.locator(".doc-modes").evaluate("el => el.getBoundingClientRect().top") == 0
    assert await frame.locator("html").evaluate("el => el.scrollHeight === el.clientHeight")
    # The controls start where the capped reading column does (850px frame,
    # 46rem column), not at the frame's edge.
    assert await frame.locator(".doc-modes > :first-child").evaluate(
        "el => Math.round(el.getBoundingClientRect().left)"
    ) == await frame.locator('.doc-view[data-view="rendered"]').evaluate(
        "el => Math.round(el.getBoundingClientRect().left)"
    )
    await frame.get_by_role("button", name="Source", exact=True).click()
    assert await frame.locator('.doc-view[data-view="source"]').is_visible()
    assert await frame.locator('.doc-view[data-view="rendered"]').is_hidden()


async def test_text_wrap_and_unicode_search(page, browser_viewer):
    frame = await _open_reading_fixture(
        page, browser_viewer, name="notes.txt", content_type="text/plain",
        body=("İ Unicode prefix\n" + "Very long source line " * 100 + " needle").encode(),
    )
    viewport = frame.locator(".reading-content")
    assert await viewport.evaluate("el => el.scrollWidth > el.clientWidth")
    await frame.get_by_role("button", name="Wrap lines").click()
    assert await viewport.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    await frame.get_by_role("searchbox", name="Find in document").fill("needle")
    await frame.get_by_role("button", name="Next match").click()
    assert await frame.locator(".reading-count").inner_text() == "1 of 1"
    assert await viewport.evaluate("el => el.ownerDocument.getSelection().toString()") == "needle"


async def test_find_keeps_typing_after_a_match_is_highlighted(page, browser_viewer):
    """Stepping to a match hands the document selection to the viewport; the
    field must still take the next keystroke, at the caret it had. Before the
    fix the field kept focus but every further key inserted nothing."""
    frame = await _open_reading_fixture(
        page, browser_viewer, name="notes.txt", content_type="text/plain",
        body=b"alpha needle one\nbeta needle two\n",
    )
    box = frame.get_by_role("searchbox", name="Find in document")
    await box.click()
    await page.keyboard.press("n")
    await page.keyboard.press("Enter")  # highlights the first "n"; the field keeps focus
    assert (await frame.locator(".reading-count").inner_text()).startswith("1 of ")
    await page.keyboard.type("eedle")
    assert await box.input_value() == "needle"
    await page.keyboard.press("Enter")
    assert await frame.locator(".reading-count").inner_text() == "1 of 2"
    # The step buttons leave focus in the field, so stepping then typing works.
    await frame.get_by_role("button", name="Next match").click()
    assert await frame.locator(".reading-count").inner_text() == "2 of 2"
    await page.keyboard.press("Backspace")
    assert await box.input_value() == "needl"
    assert await box.evaluate("el => document.activeElement === el")


async def test_csv_headers_scroll_and_raw_view(page, browser_viewer):
    frame = await _open_reading_fixture(
        page, browser_viewer, name="rows.csv", content_type="text/csv",
        body=("name,value\n" + "\n".join(f"row-{i},{i}" for i in range(650))).encode(),
    )
    from playwright.async_api import expect

    await expect(frame.locator(".reading-limit")).to_be_in_viewport()
    grid = frame.locator(".table-scroll")
    await grid.evaluate("el => el.scrollTop = 9000")
    assert await grid.evaluate("el => el.scrollTop") > 1000
    header = frame.locator("thead th").first
    assert await header.evaluate("el => el.getBoundingClientRect().top") >= 0
    assert "500 of 650 rows" in await frame.locator(".table-note").inner_text()
    await frame.get_by_role("button", name="Raw text").click()
    assert "row-649,649" in await frame.locator('.doc-view[data-view="source"]').inner_text()


async def test_workbook_row_numbers_scroll_with_rows_and_tabs_stay_visible(page, browser_viewer):
    from openpyxl import Workbook
    from playwright.async_api import expect

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "First"
    for number in range(1, 401):
        sheet.append([f"Record {number}", number])
    workbook.create_sheet("Second").append(["Other sheet"])
    output = BytesIO()
    workbook.save(output)
    frame = await _open_reading_fixture(
        page, browser_viewer, name="rows.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        body=output.getvalue(),
    )
    await frame.get_by_role("searchbox").fill("Record 390")
    await frame.get_by_role("button", name="Next match").click()
    row = frame.get_by_role("row", name="390 Record 390 390", exact=True)
    first = await row.get_by_role("rowheader").bounding_box()
    value = await row.get_by_role("cell").first.bounding_box()
    assert abs(first["y"] - value["y"]) < 1
    # An earlier row number must leave the viewport with its row, rather than
    # piling up on the sticky column header.
    await expect(frame.get_by_role("rowheader", name="1", exact=True)).not_to_be_in_viewport()
    await expect(frame.get_by_role("link", name="Second", exact=True)).to_be_in_viewport()
    await frame.get_by_role("link", name="Second", exact=True).click()
    await expect(frame.get_by_role("cell", name="Other sheet")).to_be_visible()


async def test_short_mobile_reader_keeps_content_reachable(page, browser_viewer):
    from playwright.async_api import expect

    frame = await _open_reading_fixture(
        page, browser_viewer, name="rows.csv", content_type="text/csv",
        body=("name,value\n" + "\n".join(f"row-{i},{i}" for i in range(650))).encode(),
    )
    await page.locator("#private-viewer").evaluate(
        "el => { el.style.width='340px'; el.style.height='140px'; }"
    )
    viewport = frame.locator(".reading-content")
    assert await viewport.evaluate("el => el.clientHeight") >= 80
    await viewport.scroll_into_view_if_needed()
    await expect(frame.get_by_role("cell", name="row-0", exact=True)).to_be_in_viewport()
    await frame.get_by_role("button", name="Raw text").click()
    await expect(frame.locator('.doc-view[data-view="source"] pre')).to_contain_text("row-649,649")


async def test_focused_viewer_relays_escape_to_its_locked_parent(page, browser_viewer):
    frame = await _open_reading_fixture(
        page, browser_viewer, name="escape.txt", content_type="text/plain", body=b"Synthetic text",
    )
    await page.evaluate("""() => {
      window.escapeMessages = [];
      addEventListener('message', event => {
        if (event.data?.type === 'agentdrive.viewer.escape') {
          window.escapeMessages.push({protocol: event.data.protocol, origin: event.origin,
            fromViewer: event.source === document.querySelector('iframe').contentWindow});
        }
      });
    }""")
    await frame.get_by_role("searchbox").focus()
    await page.keyboard.press("Escape")
    await page.wait_for_function("window.escapeMessages.length === 1")
    assert await page.evaluate("window.escapeMessages") == [
        {"protocol": 1, "origin": VIEWER_ORIGIN, "fromViewer": True}
    ]


async def test_short_pdf_keeps_last_page_reachable_with_find_open(page, browser_viewer):
    from playwright.async_api import expect

    frame = await _open_reading_fixture(
        page, browser_viewer, name="short.pdf", content_type="application/pdf",
        body=_two_page_pdf(),
    )
    await frame.locator(".pdfViewer .page canvas").nth(1).wait_for(timeout=10_000)
    await page.locator("#private-viewer").evaluate(
        "el => { el.style.width='340px'; el.style.height='140px'; }"
    )
    await frame.locator("#pv-find-toggle").click()
    await frame.locator("body").evaluate("el => el.scrollTop = el.scrollHeight")
    host = frame.locator(".pdf-host")
    await expect(host).to_be_in_viewport(ratio=1)
    await host.evaluate("el => el.scrollTop = el.scrollHeight")
    last_page = frame.locator(".pdfViewer .page").last
    assert await last_page.evaluate("el => el.getBoundingClientRect().bottom <= innerHeight")
