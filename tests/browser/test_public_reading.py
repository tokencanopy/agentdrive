"""Public shell and renderer assets share a contained reading viewport."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import expect

from agentdrive.config import settings
from agentdrive.public.page import render_page, render_shell
from agentdrive.rendering.render import render_body

pytestmark = pytest.mark.browser


async def _open_public_document(page, monkeypatch, *, embedded=True, html=True):
    share = "http://share.example.test"
    content = "http://content.example.test"
    monkeypatch.setattr(settings, "share_base_url", share)
    monkeypatch.setattr(settings, "public_content_base_url", content)
    monkeypatch.setattr(settings, "static_html_rendering_enabled", True)
    text = "\n\n".join(f"## Section {i}\n\nSynthetic paragraph." for i in range(120))
    if html:
        text = "".join(f"<h2>Section {i}</h2><p>Synthetic paragraph.</p>" for i in range(120))
    body = render_body(
        text.encode(),
        "text/html" if html else "text/markdown",
        "report.html" if html else "report.md",
    )
    document = render_page(
        body=body,
        title="Report",
        kind="text",
        size_bytes=1000,
        updated_at=None,
        canonical_url="",
        embed=embedded,
    )
    shell = render_shell(
        title="Report",
        description="Synthetic report",
        canonical_url="",
        renderer_url=content + "/report",
        og_type="article",
    )
    assets = Path(__file__).parents[2] / "src/agentdrive/public/static"

    async def serve(route):
        path = urlsplit(route.request.url).path
        if path == "/":
            await route.fulfill(body=shell, content_type="text/html")
        elif path == "/report":
            await route.fulfill(body=document, content_type="text/html")
        elif path.startswith(("/public-static/", "/share-static/")):
            asset = assets / path.split("/", 2)[2]
            await route.fulfill(
                body=asset.read_bytes(),
                content_type="text/css" if asset.suffix == ".css" else "text/javascript",
            )
        else:
            await route.abort()

    await page.route("http://*.example.test/**", serve)
    await page.goto(share if embedded else content + "/report")
    frame = page.frame_locator("iframe") if embedded else page
    await frame.get_by_role("searchbox").wait_for()
    return frame


async def test_public_video_shell_delegates_fullscreen_permission(page, monkeypatch):
    share = "http://localhost:8000"
    content = "http://localhost:8001"
    monkeypatch.setattr(settings, "share_base_url", share)
    monkeypatch.setattr(settings, "public_content_base_url", content)

    body = render_body(b"synthetic video", "video/mp4", "clip.mp4")
    document = render_page(
        body=body,
        title="Clip",
        kind="video",
        size_bytes=15,
        updated_at=None,
        canonical_url="",
        embed=True,
    )
    shell = render_shell(
        title="Clip",
        description="Synthetic video",
        canonical_url="",
        renderer_url=f"{content}/clip",
        og_type="article",
        allow_fullscreen=True,
    )
    assets = Path(__file__).parents[2] / "src/agentdrive/public/static"

    async def serve(route):
        path = urlsplit(route.request.url).path
        if path == "/":
            await route.fulfill(body=shell, content_type="text/html")
        elif path == "/clip":
            await route.fulfill(body=document, content_type="text/html")
        elif path.startswith(("/public-static/", "/share-static/")):
            asset = assets / path.split("/", 2)[2]
            await route.fulfill(
                body=asset.read_bytes(),
                content_type="text/css" if asset.suffix == ".css" else "text/javascript",
            )
        else:
            await route.abort()

    await page.route(f"{share}/**", serve)
    await page.route(f"{content}/**", serve)
    await page.goto(share)

    video = page.frame_locator("iframe").locator("video")
    await video.wait_for()
    assert await video.evaluate("video => document.fullscreenEnabled")
    # The synthetic body is not a decodable clip, so the element falls back to
    # the browser's 300x150 placeholder — smaller than the floor, which is the
    # case the floor exists for: a clip too small for the native controls
    # (and so for the fullscreen button the permission above enables) is
    # widened to the usable minimum and letterboxed rather than shown as a
    # thumbnail. 20rem at the 16px root.
    assert await video.evaluate("video => video.getBoundingClientRect().width") >= 320


@pytest.mark.parametrize("embedded", [True, False])
@pytest.mark.parametrize("html", [False, True])
async def test_public_search_keeps_match_and_controls_in_view(page, monkeypatch, embedded, html):
    frame = await _open_public_document(page, monkeypatch, embedded=embedded, html=html)
    await frame.get_by_role("searchbox").fill("Section 10")
    next_match = frame.get_by_role("button", name="Next match")
    await next_match.click()
    await expect(frame.get_by_role("heading", name="Section 10", exact=True)).to_be_in_viewport()
    # Keyboard activation does not scroll the offscreen toolbar into view first.
    await next_match.press("Enter")
    await expect(frame.get_by_role("status")).to_have_text("2 of 11")
    await expect(frame.get_by_role("heading", name="Section 100", exact=True)).to_be_in_viewport()
    if embedded:
        await expect(frame.get_by_role("searchbox")).to_be_in_viewport()
        assert await page.evaluate("document.documentElement.scrollHeight <= innerHeight")
        await frame.locator(".reading-content").evaluate("el => el.scrollTop = el.scrollHeight")
        final_heading = frame.get_by_role("heading", name="Section 119", exact=True)
        await expect(final_heading).to_be_in_viewport()
        await expect(frame.locator('.doc-view[data-view="rendered"] p').last).to_be_in_viewport()
    await frame.get_by_role("button", name="Source", exact=True).click()
    await expect(frame.locator('[data-view="source"] pre')).to_contain_text("Section 119")


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("font", [None, "Verdana, sans-serif"])
async def test_public_reader_aligns_controls_and_keeps_notice_compact(
    page,
    monkeypatch,
    width,
    font,
):
    await page.set_viewport_size({"width": width, "height": 844})
    frame = await _open_public_document(page, monkeypatch)
    if font:
        # Wider fallback fonts must not consume another row on mobile.
        await page.locator("body").evaluate("(el, font) => el.style.fontFamily = font", font)
    summary = frame.locator(".untrusted-band summary")
    await expect(summary).to_have_text("From the file’s author, not AgentDrive")
    await expect(frame.get_by_role("note")).not_to_be_visible()
    controls = await frame.get_by_role("button", name="Rendered", exact=True).bounding_box()
    document = await frame.locator('.doc-view[data-view="rendered"]').bounding_box()
    assert abs(controls["x"] - document["x"]) <= 1
    viewport = await frame.locator(".reading-content").bounding_box()
    assert viewport["height"] >= 480
    footer = await page.locator("footer").bounding_box()
    assert footer["height"] <= 50
    await summary.click()
    await expect(frame.get_by_role("note")).to_be_visible()
    await expect(frame.get_by_role("note")).to_contain_text("requests for information")
    await summary.click()
    await frame.get_by_role("searchbox").fill("Section 100")
    await frame.get_by_role("button", name="Next match").click()
    await expect(frame.get_by_role("heading", name="Section 100", exact=True)).to_be_in_viewport()
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")


async def _open_public_markdown(page, monkeypatch, markdown: bytes, *, embedded: bool = False):
    """A public markdown page under the REAL renderer CSP, both origins routed.

    The CSP is the point of this helper: `style-src 'self'` with nothing
    inline is exactly what a diagram engine that emits `<style>` would trip
    over, so the diagram path is only proven if the page it runs on carries
    the production policy.
    """
    from agentdrive.public.routes import _renderer_csp, _renderer_frame_ancestor, diagram_frame_csp

    share = "http://share.example.test"
    content = "http://content.example.test"
    monkeypatch.setattr(settings, "share_base_url", share)
    monkeypatch.setattr(settings, "public_content_base_url", content)
    body = render_body(markdown, "text/markdown", "plan.md")
    document = render_page(
        body=body,
        title="Plan",
        kind="md",
        size_bytes=len(markdown),
        updated_at=None,
        canonical_url="",
        embed=embedded,
    )
    shell = render_shell(
        title="Plan",
        description="Synthetic plan",
        canonical_url="",
        renderer_url=content + "/plan",
        og_type="article",
    )
    # The engine frame's policy as the asset route would emit it, with the
    # renderer's own ancestor: the browser checks the WHOLE chain, so a frame
    # that named only 'self' would be refused inside the share shell.
    frame_csp = diagram_frame_csp(_renderer_frame_ancestor())
    public_static = Path(__file__).parents[2] / "src/agentdrive/public/static"
    app_static = Path(__file__).parents[2] / "src/agentdrive/static"

    async def serve(route):
        path = urlsplit(route.request.url).path
        if path == "/":
            await route.fulfill(body=shell, content_type="text/html")
        elif path == "/plan":
            await route.fulfill(
                body=document,
                content_type="text/html",
                headers={"Content-Security-Policy": _renderer_csp()},
            )
        elif path.startswith("/public-static/"):
            rel = path.split("/", 2)[2]
            asset = (app_static if rel.startswith("vendor/") else public_static) / rel
            types = {".css": "text/css", ".html": "text/html"}
            is_frame = asset.suffix == ".html"
            headers = {"Content-Security-Policy": frame_csp} if is_frame else {}
            await route.fulfill(
                body=asset.read_bytes(),
                content_type=types.get(asset.suffix, "text/javascript"),
                headers=headers,
            )
        else:
            await route.abort()

    await page.route("http://*.example.test/**", serve)
    await page.goto(share if embedded else content + "/plan")
    return body, (page.frame_locator("iframe") if embedded else page)


@pytest.mark.parametrize("embedded", [True, False])
async def test_public_mermaid_fence_draws_under_the_production_csp(page, monkeypatch, embedded):
    violations = []
    page.on(
        "console",
        lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None,
    )
    body, page = await _open_public_markdown(
        page,
        monkeypatch,
        b"# Plan\n\n```mermaid\ngraph TD; Start-->Finish;\n```\n\n"
        b"```mermaid\nnot a diagram {{{\n```\n",
        embedded=embedded,
    )
    assert body.diagrams is True
    figure = page.locator("figure.diagram-figure")
    await expect(figure).to_have_count(1, timeout=15_000)
    img = figure.locator("img.diagram")
    await expect(img).to_be_visible()
    src = await img.get_attribute("src")
    assert src.startswith("data:image/svg+xml")
    # The drawing has a real size — a zero-height image is a diagram that
    # "rendered" and shows nothing.
    box = await img.bounding_box()
    assert box and box["width"] > 50 and box["height"] > 50
    # Nothing from the engine landed in the page tree: no inline SVG, no style.
    assert await page.locator("main.doc svg").count() == 0
    assert await page.locator("main.doc style, main.doc [style]").count() == 0
    # The source is still there, a click away.
    await expect(figure.locator("details summary")).to_have_text("Diagram source")
    assert "Start--&gt;Finish" in await figure.locator("details pre").inner_html()
    # The unparseable fence stays a code block with a note, and does not stop
    # the good one from drawing.
    failed = page.locator("pre.diagram-failed")
    await expect(failed).to_have_count(1)
    await expect(page.locator("p.diagram-note")).to_contain_text("could not be drawn")
    # The strict policy stayed strict: the page reported no violation while
    # drawing. If this ever fires, the engine started touching the document.
    assert violations == [], violations


async def test_public_markdown_without_a_fence_never_fetches_the_engine(page, monkeypatch):
    fetched = []
    page.on("request", lambda r: fetched.append(urlsplit(r.url).path))
    body, _ = await _open_public_markdown(page, monkeypatch, b"# Plain\n\nNo diagram here.\n")
    assert body.diagrams is False
    await page.locator("main.doc h1").wait_for()
    assert not any("mermaid" in p or "diagram" in p for p in fetched), fetched
