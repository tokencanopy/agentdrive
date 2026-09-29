"""The page shell carries the OpenGraph tags an unfurl needs.

Slack, iMessage and Discord fetch the URL and read the HTML. They do NOT run
JavaScript, so tags added after hydration arrive after the bot has gone.
"""

import re
from datetime import UTC, datetime
from inspect import signature

import pytest

from agentdrive.public.page import (
    render_not_found,
    render_page,
    render_shell,
    render_shell_not_found,
)
from agentdrive.rendering.render import RenderedBody


def _page(**kw):
    return render_page(
        body=kw.get("body", RenderedBody(html="<p>hi</p>", mode="markdown")),
        title=kw.get("title", "Quarterly Report"),
        kind=kw.get("kind", "md"),
        size_bytes=kw.get("size_bytes", 1234),
        updated_at=kw.get("updated_at", datetime(2026, 8, 7, tzinfo=UTC)),
        canonical_url=kw.get("canonical_url", "https://share.example.test/a/art_1"),
        description=kw.get("description"),
        name=kw.get("name", ""),
        content_type=kw.get("content_type", ""),
        embed=kw.get("embed", False),
        console_url=kw.get(
            "console_url", "https://app.example.test/drive/drv_1/a/art_1/"
        ),
    )


def test_page_carries_opengraph_title_and_url():
    html = _page()
    assert '<meta property="og:title" content="Quarterly Report"' in html
    assert '<meta property="og:url" content="https://share.example.test/a/art_1"' in html
    assert '<meta name="twitter:card" content="summary"' in html


def test_page_title_is_escaped():
    html = _page(title='Bad "><script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in html


def test_body_html_is_embedded_verbatim():
    assert "<p>hi</p>" in _page()


def test_not_found_page_is_a_complete_document_without_detail():
    html = render_not_found()
    assert html.startswith("<!doctype html>")
    # Anti-enumeration: it must not hint at what was missing.
    for leak in ("grant", "revoked", "expired", "permission"):
        assert leak not in html.lower()


def test_every_untrusted_field_is_escaped_in_attribute_position():
    """A title must not be able to break out of an OG `content="..."`.

    Escaping the text is not enough on its own — the quote character is what
    ends the attribute, so `"` has to become an entity too.
    """
    evil = '"><script>alert(1)</script>'
    html = _page(title=evil, kind=evil, canonical_url=evil, description=evil)
    # The page legitimately loads one EXTERNAL script, so the assertion is
    # that no attacker-authored script tag appears — not that none does.
    assert "<script>alert(1)</script>" not in html
    assert "alert(1)" not in html.replace("&lt;script&gt;alert(1)&lt;/script&gt;", "")
    assert "&#34;" in html  # the quote itself was escaped, not just the angles
    for attr in ("og:title", "og:url", "og:description"):
        value = re.search(rf'property="{attr}" content="([^"]*)"', html).group(1)
        assert "<" not in value and '"' not in value


def test_shell_has_no_inline_style_or_script():
    """The CSP is `script-src 'self'` and `style-src 'self'` — no unsafe-inline.

    That distinction is the entire security value of the script directive: an
    attacker who slipped a `<script>` past the renderer still cannot run it,
    because they cannot write a file into our origin. The moment this shell
    needs one inline script or one `onclick=`, the policy has to add
    `'unsafe-inline'` — which permits exactly the injected-inline case the
    directive exists to stop. So external `<script src>` is fine; inline is
    not, and neither is any event-handler attribute.
    """
    for html in (_page(), render_not_found()):
        for tag in re.findall(r"<script\b[^>]*>", html, re.I):
            assert "src=" in tag.lower(), f"inline script in the shell: {tag}"
        assert not re.search(r"<script\b[^>]*>\s*\S", html, re.I) or all(
            "src=" in t.lower() for t in re.findall(r"<script\b[^>]*>", html, re.I)
        )
        assert not re.search(r"<style\b", html, re.I)
        assert ' style="' not in html
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", html, re.I), "inline handler"


def test_shell_does_not_pin_the_colour_scheme():
    """Dark mode is CSS-only, since no script may run to toggle it.

    A hardcoded `data-theme` on <html> would defeat the stylesheet's
    `prefers-color-scheme` block and lock every reader into one scheme.
    """
    for html in (_page(), render_not_found()):
        assert "data-theme=" not in html


@pytest.mark.parametrize("mount_prefix", ["", "/drive"])
def test_the_stylesheet_resolves_in_both_mount_modes(mount_prefix, monkeypatch):
    """The page links `/public-static/viewer.css` at the root, always.

    A `StaticFiles` mount would have been resolved against `root_path`, so
    under MOUNT_PREFIX it answers only at `/drive/public-static/...` — while
    the page is served on the share host, which has no such prefix, and
    `PUBLIC_PREFIXES` would refuse the prefixed form anyway. Every public page
    rendered unstyled in staging because of exactly this. A route matches at
    the root in both modes.
    """
    from fastapi.testclient import TestClient

    from agentdrive.config import settings

    monkeypatch.setattr(settings, "mount_prefix", mount_prefix)

    from agentdrive.app import app

    # No `with`: the context manager runs the lifespan, which wants Postgres.
    # A stylesheet needs none of that.
    r = TestClient(app).get("/public-static/viewer.css")

    assert r.status_code == 200, mount_prefix
    assert r.headers["content-type"].startswith("text/css")
    assert b".doc" in r.content


def test_the_download_button_label_is_not_painted_out_by_the_link_rule():
    """`.doc a` outranks `.btn` on specificity — (0,1,1) vs (0,1,0).

    So a blanket `.doc a { color: var(--canopy) }` wins over the button's own
    `color`, and since `--canopy` is also the button's background the label
    goes invisible: a green rectangle with no text, in both themes. The
    download card is the only way to get bytes for a format we cannot render
    inline, so an unlabelled button is the whole affordance gone.
    """
    from pathlib import Path

    css = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agentdrive"
        / "public"
        / "static"
        / "viewer.css"
    ).read_text()

    assert ".doc a:not(.btn)" in css, "the generic link rule must exclude .btn"
    assert ".doc a {" not in css, "a blanket .doc a rule would repaint the button"


def test_the_viewer_bar_carries_the_visitor_affordances_only():
    """Ported chrome, minus everything that needed a session.

    The legacy bar also held sharing controls, a drive breadcrumb and an Edit
    button — all gated on `is_owner`. A share-link recipient is not the owner
    and this surface has no session at all, so those cannot appear here; the
    console is where they belong.
    """
    html = _page()

    assert 'class="viewer-bar"' in html
    assert "data-theme-toggle" in html  # theme toggle
    assert "data-copy-url" in html  # copy link
    assert 'href="content" download' in html  # download
    for owner_only in ("data-share-open", "bar-edit", "/dashboard", "share-pill"):
        assert owner_only not in html, owner_only


def test_the_chip_shows_the_format_not_the_dispatch_bucket():
    """`pdf`, not `bundle`. The chip is display; `kind_for` is dispatch."""
    from agentdrive.public.page import render_page as rp

    html = rp(
        body=RenderedBody(html="", mode="pdf"),
        title="report.pdf",
        kind="bundle",
        size_bytes=10,
        updated_at=None,
        canonical_url="https://share.example.test/a/art_1/",
        name="report.pdf",
        content_type="application/pdf",
    )
    assert '<span class="kind" data-k="bundle">pdf</span>' in html


def test_the_machine_strip_carries_provenance_for_agent_readers():
    html = _page()
    assert 'class="machine-strip"' in html
    assert "<dt>type</dt>" in html and "<dt>size</dt>" in html


def test_the_script_is_external_and_loads_before_paint():
    """No defer/async: the theme must resolve before the first paint.

    An async script would let the page paint light and then switch, which is
    the flash the legacy inline pre-paint script existed to avoid — and we
    cannot use an inline one without `'unsafe-inline'`.
    """
    html = _page()
    tag = re.search(r'<script\b[^>]*src="/public-static/viewer\.js\?v=[a-f0-9]+"[^>]*>', html)
    assert tag, "the viewer script must be linked"
    assert "defer" not in tag.group(0) and "async" not in tag.group(0)


@pytest.mark.parametrize(
    ("mode", "expect_pdfjs"), [("pdf", True), ("markdown", False), ("code", False)]
)
def test_pdfjs_loads_only_on_pdf_pages(mode, expect_pdfjs):
    """~1.9 MB of engine has no business loading for a markdown document."""
    from agentdrive.public.page import render_page as rp

    html = rp(
        body=RenderedBody(html="", mode=mode),
        title="x",
        kind="bundle",
        size_bytes=10,
        updated_at=None,
        canonical_url="https://share.example.test/a/art_1/",
        name="x",
        content_type="application/pdf" if mode == "pdf" else "text/markdown",
    )
    assert ("pdf-visitor.js" in html) is expect_pdfjs
    assert ("pdf_viewer.css" in html) is expect_pdfjs


def test_asset_urls_carry_a_content_version():
    """A deploy must not leave readers on the previous CSS.

    The asset URLs have no content hash of their own, so without `?v=` a
    stylesheet that no longer matches the markup is served for up to the
    max-age. That is exactly what happened while testing the PDF viewer: the
    page shipped new markup against cached CSS and pdf.js threw.
    """
    from agentdrive.public.page import ASSET_V

    html = _page()
    assert len(ASSET_V) == 12
    assert f"viewer.css?v={ASSET_V}" in html
    assert f"viewer.js?v={ASSET_V}" in html
    assert f"viewer.css?v={ASSET_V}" in render_not_found()


def test_artifact_shell_carries_only_escaped_metadata_and_exact_renderer_url(
    monkeypatch,
):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")
    title = 'Quarterly "><img src=x onerror=alert(1)>'
    description = "Review & <strong>approve</strong>"
    canonical_url = "https://share.example.test/a/art_abc%2Fdef/"
    renderer_url = "https://public.example.test/a/art_abc%2Fdef/"

    html = render_shell(
        title=title,
        description=description,
        canonical_url=canonical_url,
        renderer_url=renderer_url,
        og_type="article",
    )

    assert "<title>Quarterly &#34;&gt;&lt;img" in html
    assert 'property="og:title" content="Quarterly &#34;&gt;&lt;img' in html
    assert 'property="og:description" content="Review &amp; &lt;strong&gt;' in html
    assert f'<link rel="canonical" href="{canonical_url}"' in html
    assert f'property="og:url" content="{canonical_url}"' in html
    # The frame asks for the chrome-less page; the "open the content directly"
    # link deliberately does not, because a reader who follows it has left the
    # shell behind and needs the renderer's own header back.
    assert re.search(rf'<iframe\s+src="{re.escape(renderer_url)}\?embed=1"', html)
    assert f'<a href="{renderer_url}"' in html
    assert 'title="View Quarterly &#34;&gt;&lt;img' in html
    assert "<img src=x onerror=alert(1)>" not in html
    assert "<strong>approve</strong>" not in html


@pytest.mark.parametrize("fullscreen", [True, False])
def test_shell_delegates_explicit_fullscreen_permission(monkeypatch, fullscreen):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")
    html = render_shell(
        title="Shared artifact",
        description="A public artifact",
        canonical_url="https://share.example.test/a/art_0123456789abcdef/",
        renderer_url="https://public.example.test/a/art_0123456789abcdef/",
        og_type="article",
        allow_fullscreen=fullscreen,
    )

    iframe_attrs = re.search(r"<iframe\b([^>]*)>", html, re.S).group(1)
    assert ("allow=\"fullscreen\"" in iframe_attrs) is fullscreen


def test_folder_shell_has_no_children_or_artifact_rendering_input(monkeypatch):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")
    html = render_shell(
        title="Launch <folder>",
        description="Parent folder · 2 items",
        canonical_url="https://share.example.test/f/fld_0123456789abcdef/",
        renderer_url="https://public.example.test/f/fld_0123456789abcdef/",
        og_type="website",
    )

    assert 'property="og:type" content="website"' in html
    assert "Launch &lt;folder&gt;" in html
    assert "private-child-name.txt" not in html
    assert "artifact-authored-body" not in html
    # Every parameter is display metadata off an already-authorized descriptor
    # or a URL this module validates against the configured renderer origin.
    # There is still no body, no entries and no bytes: that absence is what
    # keeps artifact-authored markup out of the share-origin document, and it
    # is the reason this assertion is exhaustive rather than a subset check.
    assert set(signature(render_shell).parameters) == {
        "title",
        "description",
        "canonical_url",
        "renderer_url",
        "og_type",
        "name",
        "kind",
        "content_type",
        "meta_line",
        "allow_fullscreen",
        "download_url",
        "console_url",
    }


def test_shell_requires_description_and_renders_an_explicit_empty_value(monkeypatch):
    from typing import get_type_hints

    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")

    with pytest.raises(TypeError, match="description"):
        render_shell(
            title="Empty description",
            canonical_url="https://share.example.test/a/art_0123456789abcdef/",
            renderer_url="https://public.example.test/a/art_0123456789abcdef/",
            og_type="article",
        )

    assert get_type_hints(render_shell)["description"] is str
    html = render_shell(
        title="Empty description",
        description="",
        canonical_url="https://share.example.test/a/art_0123456789abcdef/",
        renderer_url="https://public.example.test/a/art_0123456789abcdef/",
        og_type="article",
    )
    assert '<meta name="description" content=""' in html
    assert '<meta property="og:description" content=""' in html
    assert '<meta name="twitter:description" content=""' in html
    assert re.search(r'<div class="shell-metadata">.*<p></p>', html, re.S)


def test_share_shell_keeps_possession_secret_out_of_metadata_text_and_assets(
    monkeypatch,
):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")
    secret = "shr_synthetic-secret"
    renderer_url = f"https://public.example.test/s/{secret}/"
    download_url = f"https://public.example.test/s/{secret}/content?download=1"
    html = render_shell(
        title='Shared "><folder>',
        description="Private handoff & notes",
        canonical_url="",
        renderer_url=renderer_url,
        download_url=download_url,
        og_type="article",
    )

    # The copy control has no URL to advertise on a capability link, so it
    # falls back to the address bar at click time rather than to anything the
    # response carries.
    assert 'data-copy-url=""' in html

    assert "Shared &#34;&gt;&lt;folder&gt;" in html
    assert "Private handoff &amp; notes" in html
    assert 'rel="canonical"' not in html
    assert 'property="og:url"' not in html

    secret_attributes = re.findall(rf'(src|href)="([^"]*{re.escape(secret)}[^"]*)"', html)
    # Header first (the download), then the frame, then the recovery link.
    assert secret_attributes == [
        ("href", download_url),
        ("src", f"{renderer_url}?embed=1"),
        ("href", renderer_url),
    ]
    assert secret not in re.sub(r"<[^>]+>", "", html)
    for asset_url in re.findall(r'(?:src|href)="([^"]+)"', html):
        if asset_url.startswith("/share-static/"):
            assert secret not in asset_url


def test_trusted_shell_has_no_active_or_artifact_authored_elements(monkeypatch):
    """One external script of ours is allowed; nothing else active is.

    The shell gained `script-src 'self'` when the theme control, the clipboard
    and frame-sizing moved up here out of the frame. What that grant is worth
    depends entirely on the two things this test pins: the script is an
    external file on our own origin, and there is no inline script and no
    event-handler attribute anywhere — because `'unsafe-inline'` is exactly the
    directive that would let an injected inline script run, and this document
    would then be the one place an escaping failure could run it. It contains
    no artifact-authored markup at all, which is what makes the grant narrow;
    keeping it narrow is what this assertion is for.
    """
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")
    html = render_shell(
        title="Safe title",
        description="Safe description",
        canonical_url="https://share.example.test/a/art_0123456789abcdef/",
        renderer_url="https://public.example.test/a/art_0123456789abcdef/",
        og_type="article",
    )

    for tag in re.findall(r"<script\b[^>]*>", html, re.I):
        assert 'src="/share-static/shell.js?v=' in tag, f"inline script in the shell: {tag}"
    assert len(re.findall(r"<script\b", html, re.I)) == 1

    for forbidden in (
        r"<form\b",
        r"<object\b",
        r"<embed\b",
        r"\ssrcdoc\s*=",
        r"service\s*worker",
    ):
        assert not re.search(forbidden, html, re.I), forbidden
    assert not re.search(r"<style\b", html, re.I)
    assert ' style="' not in html
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", html, re.I)
    assert re.search(r'href="/share-static/shell\.css\?v=[a-f0-9]{12}"', html)
    assert re.search(r'src="/share-static/shell\.js\?v=[a-f0-9]{12}"', html)


def test_shell_assets_share_one_content_digest(monkeypatch):
    from hashlib import sha256
    from pathlib import Path

    from agentdrive.config import settings
    from agentdrive.public.page import SHELL_ASSET_V

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")
    static = (
        Path(__file__).resolve().parents[1] / "src" / "agentdrive" / "public" / "static"
    )
    # Both files, in the order `_asset_version` hashes them. A digest over the
    # stylesheet alone would leave every reader on the previous shell.js after
    # a deploy that only changed behaviour — the exact staleness the version
    # parameter exists to prevent.
    shell_assets = (static / "shell.css").read_bytes() + (static / "shell.js").read_bytes()
    html = render_shell(
        title="Safe title",
        description="Safe description",
        canonical_url="https://share.example.test/a/art_0123456789abcdef/",
        renderer_url="https://public.example.test/a/art_0123456789abcdef/",
        og_type="article",
    )

    assert sha256(shell_assets).hexdigest()[:12] == SHELL_ASSET_V
    assert f"/share-static/shell.css?v={SHELL_ASSET_V}" in html
    assert f"/share-static/shell.js?v={SHELL_ASSET_V}" in html


def test_shell_links_keep_semantic_anchors_with_44px_touch_targets():
    from pathlib import Path

    public_dir = Path(__file__).resolve().parents[1] / "src" / "agentdrive" / "public"
    template = (public_dir / "templates" / "shell.html").read_text()
    css = (public_dir / "static" / "shell.css").read_text()

    assert '<a class="shell-lockup" href="https://tokencanopy.com">' in template
    assert '<a href="{{ renderer_url }}" referrerpolicy="no-referrer">' in template
    for selector in (".shell-lockup", ".shell-fallback a"):
        match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", css, re.S)
        assert match, f"missing {selector} rule"
        declarations = match.group(1)
        assert "display: inline-flex" in declarations
        assert "min-width: 44px" in declarations
        assert "min-height: 44px" in declarations


def test_shell_controls_that_need_script_stay_hidden_without_it():
    """`[hidden]` is a UA rule; the author `display` on `.shell-btn` outranks it.

    Without an explicit rule the copy and theme controls — which ship `hidden`
    precisely because only script can make them work — rendered for a reader
    with script off, dead, and took a phone header's width with them.
    """
    from pathlib import Path

    css = (
        Path(__file__).resolve().parents[1]
        / "src" / "agentdrive" / "public" / "static" / "shell.css"
    ).read_text()

    assert re.search(r"\.shell-btn\[hidden\]\s*\{[^}]*display:\s*none", css)


def test_shell_bar_can_shrink_inside_the_page_grid():
    """The page's column is `minmax(0, 1fr)`, not `auto`.

    An auto column is floored at its widest item's min-content — here the
    wordmark plus an un-ellipsised filename plus every action — so the bar grew
    past the viewport, never had negative free space, and never ellipsised the
    name it was already told to ellipsise.
    """
    from pathlib import Path

    css = (
        Path(__file__).resolve().parents[1]
        / "src" / "agentdrive" / "public" / "static" / "shell.css"
    ).read_text()

    match = re.search(r"\.shell-page\s*\{([^}]+)\}", css)
    assert match, "missing .shell-page rule"
    assert "grid-template-columns: minmax(0, 1fr)" in match.group(1)


def test_phone_header_gives_the_document_actions_a_row_of_their_own():
    """Four nowrap buttons never fit one phone row — that is the overflow.

    `Auto` was cut off past the right edge and, because the group is
    right-aligned, the overflow ran left as well and painted `Open in console`
    over the wordmark. The theme control therefore sits OUTSIDE `.shell-actions`
    in the markup: it rides with the brand, and the three actions that describe
    the document get the full width of a row.
    """
    from pathlib import Path

    public_dir = Path(__file__).resolve().parents[1] / "src" / "agentdrive" / "public"
    template = (public_dir / "templates" / "shell.html").read_text()
    css = (public_dir / "static" / "shell.css").read_text()

    actions = template[template.index('<div class="shell-actions">') :]
    actions = actions[: actions.index("</div>")]
    assert "data-theme-toggle" not in actions
    assert "data-theme-toggle" in template

    narrow = css[css.index("@media (max-width: 48rem)") :]
    rule = re.search(r"\.shell-actions\s*\{([^}]+)\}", narrow)
    assert rule, "the narrow bar must place .shell-actions itself"
    declarations = rule.group(1)
    # Its own row, the full width of the bar, and wrapping rather than
    # overflowing if a future action ever makes the row too long.
    assert "grid-column: 1 / -1" in declarations
    assert "flex-wrap: wrap" in declarations

    theme = re.search(r"\.shell-theme\s*\{([^}]+)\}", narrow)
    assert theme, "the narrow bar must place .shell-theme itself"
    assert "grid-row: 1" in theme.group(1)


def test_narrow_bar_drops_the_endorsement_before_it_drops_the_filename():
    """The name is the flex item that shrinks, so the brand yields first.

    Otherwise a tablet or a narrow laptop window ellipsised the filename away
    to nothing while `by Token Canopy` — which the reader can also see in the
    address bar — kept its full width.
    """
    from pathlib import Path

    css = (
        Path(__file__).resolve().parents[1]
        / "src" / "agentdrive" / "public" / "static" / "shell.css"
    ).read_text()

    wide = css[css.index("@media (max-width: 64rem)") : css.index("@media (max-width: 48rem)")]
    assert re.search(r"\.shell-lockup-by\s*\{[^}]*display:\s*none", wide)


def test_trusted_shell_rejects_a_renderer_url_from_another_origin(monkeypatch):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")

    with pytest.raises(ValueError, match="configured public content origin"):
        render_shell(
            title="Safe title",
            description="Safe description",
            canonical_url="https://share.example.test/a/art_0123456789abcdef/",
            renderer_url="https://attacker.example.test/a/art_0123456789abcdef/",
            og_type="article",
        )


def test_share_shell_follows_the_visitor_system_colour_scheme():
    from pathlib import Path

    css = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agentdrive"
        / "public"
        / "static"
        / "shell.css"
    ).read_text()

    assert "color-scheme: light dark" in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert "--bg: #17171a" in css


def test_viewer_page_has_a_console_link():
    html = _page()

    assert 'class="bar-btn bar-console"' in html
    assert 'href="https://app.example.test/drive/drv_1/a/art_1/"' in html


def test_share_shell_has_a_console_link(monkeypatch):
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")
    html = render_shell(
        title="Report",
        description="Report · dataset · 1 KB",
        canonical_url="https://share.example.test/a/art_1/",
        renderer_url="https://public.example.test/a/art_1/",
        og_type="article",
        console_url="https://app.example.test/drive/drv_1/a/art_1/",
    )

    assert 'class="shell-btn shell-console"' in html
    assert 'href="https://app.example.test/drive/drv_1/a/art_1/"' in html


def test_shell_not_found_is_one_uniform_complete_document():
    first = render_shell_not_found()
    second = render_shell_not_found()

    assert first == second
    assert first.startswith("<!doctype html>")
    assert set(signature(render_shell_not_found).parameters) == set()
    for leak in ("grant", "revoked", "expired", "permission", "artifact", "folder"):
        assert leak not in first.lower()


@pytest.mark.parametrize(
    "cache_control",
    [
        "private, no-store",
        "public, max-age=60, must-revalidate",
        "public, max-age=0, must-revalidate",
    ],
)
def test_shell_headers_frame_only_the_configured_public_origin(cache_control, monkeypatch):
    from agentdrive.config import settings
    from agentdrive.public.routes import shell_response_headers

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")

    assert shell_response_headers(cache_control=cache_control) == {
        "Cache-Control": cache_control,
        "Content-Security-Policy": (
            "default-src 'none'; frame-src https://public.example.test; "
            "style-src 'self'; script-src 'self'; "
            "base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def test_shell_headers_fail_closed_without_a_public_renderer(monkeypatch):
    from agentdrive.config import settings
    from agentdrive.public.routes import shell_response_headers

    monkeypatch.setattr(settings, "public_content_base_url", "")

    with pytest.raises(RuntimeError, match="PUBLIC_CONTENT_BASE_URL"):
        shell_response_headers(cache_control="private, no-store")


# ── one header ──────────────────────────────────────────────────────────────
#
# The share page used to draw three: the trusted shell's metadata bar, the
# framed renderer's own sticky bar, and the document heading under it — name,
# path and size stated three times before a reader reached a single byte of
# content, and on a PDF, three bars above a pane that then scrolled inside its
# own scrollbar. The header is now the shell's alone, and these pin both halves
# of that: the frame gives its chrome up, and the shell picks all of it up.


def test_the_framed_renderer_draws_no_chrome_of_its_own():
    """Framed, this page is a document and nothing else.

    Deliberately NOT inferred from `window !== top`: the server says so, so a
    page cannot arrange to be chrome-less for a reader who reached it directly.
    """
    embedded = _page(embed=True)

    assert 'class="viewer-bar"' not in embedded
    assert 'class="doc-head"' not in embedded
    assert 'class="machine-strip"' not in embedded
    assert "data-theme-toggle" not in embedded
    assert "<p>hi</p>" in embedded  # the document itself is untouched

    # The flags the stylesheet and the pre-paint script need, on <html>,
    # because a full-height PDF chain starts at the root and the script runs
    # before <body> exists.
    assert re.search(r"<html lang=\"en\" data-embedded data-embed-origin=", embedded)
    assert 'data-mode="markdown"' in embedded
    assert 'class="mode-markdown embedded embedded-public"' in embedded


def test_the_standalone_renderer_keeps_every_affordance():
    """The direct-content link, an unframed origin and the direct-renderer
    rollback all land on this page with no shell above it, so it has to remain
    a complete document surface — bar, header, provenance and all."""
    standalone = _page()

    assert 'class="viewer-bar"' in standalone
    assert 'class="doc-head"' in standalone
    assert 'class="machine-strip"' in standalone
    assert "data-embedded" not in standalone
    assert 'href="content" download' in standalone


def test_the_untrusted_content_band_is_not_chrome_and_survives_the_embed():
    """The band is a statement about the BYTES, so it belongs to the document,
    not to the header that was suppressed. Losing it inside the frame would
    remove the only mitigation the design names against a rendered agent-
    authored page imitating somebody."""
    for html in (
        _page(body=RenderedBody(html="<p>page</p>", mode="page"), embed=True),
        _page(body=RenderedBody(html="<p>page</p>", mode="page")),
    ):
        assert 'class="untrusted-band"' in html


def test_the_shell_header_states_the_facts_the_frame_stopped_stating(monkeypatch):
    """Everything the reader lost when the frame gave up its chrome — the kind,
    the name, the path, the media type, the size, the date, and the two things
    they can do — is here, once, on the origin the address bar shows."""
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")
    html = render_shell(
        title="data.json",
        description="reports/data.json · code · 63 B · updated 2026-08-28",
        canonical_url="https://share.example.test/a/art_0123456789abcdef/",
        renderer_url="https://public.example.test/a/art_0123456789abcdef/",
        download_url="https://public.example.test/a/art_0123456789abcdef/content?download=1",
        og_type="article",
        name="data.json",
        content_type="application/json",
        meta_line="reports/data.json · application/json · 63 B · updated 2026-08-28",
    )

    assert '<span class="shell-kind">json</span>' in html
    assert '<span class="shell-name">data.json</span>' in html
    assert "reports/data.json · application/json · 63 B · updated 2026-08-28" in html
    assert ">Download</a>" in html
    assert "data-copy-url=" in html and "data-theme-toggle" in html
    # And it is a header, not a second document: no rendered body, no listing,
    # and no leftover provenance block under the frame.
    assert "machine-strip" not in html and "doc-foot" not in html


def test_the_shell_refuses_a_download_link_it_did_not_mint(monkeypatch):
    """The download URL crosses to the byte origin, so it gets the same
    exact-origin check the frame source gets — plus its one allowed query.
    A shell that could be handed an arbitrary URL here would be a way to point
    a branded page's Download button at somebody else's host."""
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "public_content_base_url", "https://public.example.test")
    renderer_url = "https://public.example.test/a/art_0123456789abcdef/"

    for bad in (
        "https://attacker.example.test/a/art_0123456789abcdef/content?download=1",
        "https://public.example.test/a/art_0123456789abcdef/content?redirect=evil",
        "https://public.example.test/a/art_0123456789abcdef/content",
    ):
        with pytest.raises(ValueError, match="configured public content origin"):
            render_shell(
                title="Safe title",
                description="Safe description",
                canonical_url="https://share.example.test/a/art_0123456789abcdef/",
                renderer_url=renderer_url,
                download_url=bad,
                og_type="article",
            )


def test_the_framed_listing_sends_a_child_click_out_of_the_frame():
    """A child link resolved inside the frame would load that artifact under a
    shell header still describing the folder, on an origin the address bar does
    not show. Framed, the link is absolute onto the branded host and targets
    the whole window; standalone it stays relative, where it is already right.
    """
    from agentdrive.config import settings
    from agentdrive.public.page import render_folder_page

    entries = [{"kind": "md", "name": "q1.md", "id": "art_1", "size_bytes": 1, "is_folder": False}]
    kw = dict(
        name="Reports",
        path="reports",
        entries=entries,
        canonical_url="https://share.example.test/f/fld_1/",
        description="reports · 1 item",
    )

    standalone = render_folder_page(**kw)
    assert '<a href="/a/art_1/">' in standalone
    assert "target=" not in standalone

    original = settings.share_base_url
    settings.share_base_url = "https://share.example.test"
    try:
        embedded = render_folder_page(**kw, embed=True)
    finally:
        settings.share_base_url = original

    assert '<a href="https://share.example.test/a/art_1/" target="_top">' in embedded
    assert 'class="doc-head"' not in embedded


@pytest.mark.parametrize("diagrams", [True, False])
def test_the_diagram_engine_loads_only_for_documents_that_carry_one(diagrams):
    """3.5 MB of engine has no business loading for a report without a diagram."""
    from agentdrive.public.page import render_page as rp

    html = rp(
        body=RenderedBody(html="", mode="markdown", diagrams=diagrams),
        title="x",
        kind="md",
        size_bytes=10,
        updated_at=None,
        canonical_url="https://share.example.test/a/art_1/",
        name="x.md",
        content_type="text/markdown",
    )
    assert ("diagram-visitor.js" in html) is diagrams
    # The bundle itself is never named by the page: `diagrams.js` fetches it,
    # so a document without a fence cannot even reference it.
    assert "mermaid.min.js" not in html
