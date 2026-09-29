"""Wrap a rendered body in a complete HTML document.

The OpenGraph tags are the point: link-unfurling bots fetch this HTML and do
not execute JavaScript, so an artifact's title and description must be in the
first response or the unfurl is generic forever.

`body_html` is injected with `|safe` because Task 3 already produced escaped,
trusted-shape HTML. Every other value goes through Jinja's autoescaping —
`autoescape=` is set explicitly here because a bare `jinja2.Environment` does
NOT escape, and the artifact title is attacker-authored.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import settings
from ..core.kinds import chip_label
from ..rendering.render import RenderedBody

_TEMPLATES = Path(__file__).parent / "templates"
_STATIC = Path(__file__).parent / "static"


def _asset_version(names: tuple[str, ...]) -> str:
    """Short digest of the assets, appended to their URLs as `?v=`.

    Without it the URLs carry no content hash, so a deploy leaves every reader
    on the previous CSS and JS until their cache expires — and a stylesheet
    that no longer matches the markup is worse than a slow one. Computed once
    at import: these files cannot change under a running process.
    """
    h = hashlib.sha256()
    for name in sorted(names):
        f = _STATIC / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


ASSET_V = _asset_version((
    "viewer.css", "viewer.js", "pdf-visitor.js", "reading.js",
    "diagram-visitor.js", "diagrams.js", "diagram-frame.js",
))
SHELL_ASSET_V = _asset_version(("shell.css", "shell.js"))
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=select_autoescape(["html"]),
)


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# The folder listing formats one size per child, so the formatter is reached
# as a template filter rather than pre-computed into every entry dict.
_env.filters["human_size"] = human_size


def render_page(
    *,
    body: RenderedBody,
    title: str,
    kind: str,
    size_bytes: int,
    updated_at: datetime | None,
    canonical_url: str,
    path: str = "",
    description: str | None = None,
    name: str = "",
    content_type: str = "",
    embed: bool = False,
    console_url: str = "",
) -> str:
    """Wrap a rendered body in the viewer shell.

    `name` is the bare filename for the bar; `path` is the fuller display
    path under the heading. `content_type` drives the chip label, which is
    finer-grained than `kind` on purpose — a reader looking at a PDF should
    see `pdf`, not the dispatch bucket it happens to fall in.

    `embed` says this page is being framed by the trusted shell, which draws
    all of that above the frame already: the bar, the document header and the
    machine strip are suppressed and the document is the whole page. Standalone
    — the direct-content link, the unframed renderer origin, the
    direct-renderer rollback — it keeps every one of them.
    """
    return _env.get_template("viewer.html").render(
        asset_v=ASSET_V,
        embed=embed,
        embed_origin=settings.share_base_url if embed else "",
        title=title,
        path=path,
        kind=kind,
        chip_label=chip_label(content_type, name) if content_type else kind,
        name=name or (path.rsplit("/", 1)[-1] if path else title),
        content_type=content_type,
        size_bytes=size_bytes,
        size_human=human_size(size_bytes),
        updated_human=updated_at.strftime("%Y-%m-%d") if updated_at else "",
        canonical_url=canonical_url,
        description=description,
        body_html=body.html,
        body_mode=body.mode,
        diagrams=body.diagrams,
        console_url=console_url,
    )


def render_folder_page(
    *,
    name: str,
    path: str,
    entries: list[dict],
    canonical_url: str,
    description: str | None = None,
    truncated: bool = False,
    embed: bool = False,
) -> str:
    """The listing page for a published folder.

    `entries` arrive already filtered by `core.public_reads` — this function
    renders whatever it is handed and makes no authorization decision, so the
    "listed implies servable" invariant has exactly one owner. Every field is
    autoescaped: `og:type` is `website` rather than `article` because a folder
    is a place, not a document, which is also why there is no `body_html`
    escape hatch on this page at all.
    """
    return _env.get_template("folder.html").render(
        asset_v=ASSET_V,
        embed=embed,
        embed_origin=settings.share_base_url if embed else "",
        # Framed, a child link leaves the frame for the branded host; the entry
        # ids are public permalinks, never a share secret, so this adds no URL
        # the reader could not already reach.
        entry_base=(settings.share_base_url or "").rstrip("/") if embed else "",
        name=name,
        path=path,
        entries=entries,
        count=len(entries),
        canonical_url=canonical_url,
        description=description,
        truncated=truncated,
    )


def render_not_found() -> str:
    return _env.get_template("notfound.html").render(asset_v=ASSET_V)


EMBED_QUERY = "embed=1"
"""The one query the shell is allowed to put on a framed renderer URL.

A constant, and checked against as a constant, so the trusted shell can never
grow the ability to pass an arbitrary parameter to the renderer: the value is
literal, and `_require_renderer_url` accepts nothing else.
"""

DOWNLOAD_QUERY = "download=1"
"""The one query the shell is allowed to put on a renderer CONTENT URL.

The download moved out of the frame with the rest of the chrome, and a link
that crosses an origin cannot use the `download` attribute — the browser
ignores it, so a JSON artifact would have opened on the content origin instead
of saving. This asks the byte route for `Content-Disposition: attachment`
instead, which is a header the reader's browser will honour from anywhere.
"""


def _require_renderer_url(url: str, *, query: str = "") -> None:
    """Refuse any URL that is not exactly on the configured renderer origin.

    Every URL the shell emits toward the renderer passes through here: the
    iframe source, the direct-content link, and the download link. The origin
    comes only from validated process configuration, and `query` is one of the
    two literal constants above — never a caller-composed string — so the
    shell cannot be talked into pointing at somebody else's host or into
    forwarding a parameter nobody designed.
    """
    configured_origin = settings.public_content_base_url
    if not configured_origin:
        raise RuntimeError("PUBLIC_CONTENT_BASE_URL is required for the trusted shell")

    parts = urlsplit(url)
    configured_parts = urlsplit(configured_origin)
    try:
        origin = (parts.scheme, parts.hostname, parts.port)
        expected_origin = (
            configured_parts.scheme,
            configured_parts.hostname,
            configured_parts.port,
        )
    except ValueError as exc:
        raise ValueError("renderer URL must use the configured public content origin") from exc
    if (
        origin != expected_origin
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith("/")
        or parts.query != query
        or parts.fragment
    ):
        raise ValueError("renderer URL must use the configured public content origin")


def render_shell(
    *,
    title: str,
    description: str,
    canonical_url: str,
    renderer_url: str,
    og_type: str,
    name: str = "",
    kind: str = "",
    content_type: str = "",
    meta_line: str = "",
    allow_fullscreen: bool = False,
    download_url: str = "",
    console_url: str = "",
) -> str:
    """Render trusted metadata around an isolated public renderer.

    This is now the page's ONLY header. The framed renderer draws the document
    and nothing else, so everything a reader needs to know about what they are
    looking at — the brand, the kind, the name, the path, the size, the date —
    and everything they can do with it are stated once, here, by the origin
    their address bar shows. Every one of those values is display metadata off
    the already-authorized descriptor: the interface still has no artifact body
    and no listing entries, and Jinja autoescaping remains explicit on this
    module's shared environment, because an artifact name is attacker-authored.

    A share-capability route passes an empty ``canonical_url`` so its
    possession secret never enters canonical or OpenGraph metadata — and, now,
    so the copy control falls back to the address bar rather than to a URL the
    response would have had to carry.

    ``allow_fullscreen`` is supplied by the route after it has decided that the
    artifact will render an inline video. Keeping that decision outside this
    metadata-only renderer prevents a download-card response from receiving a
    permission it cannot use.
    """
    _require_renderer_url(renderer_url)
    if download_url:
        _require_renderer_url(download_url, query=DOWNLOAD_QUERY)

    return _env.get_template("shell.html").render(
        asset_v=SHELL_ASSET_V,
        title=title,
        description=description,
        canonical_url=canonical_url,
        renderer_url=renderer_url,
        # The frame's own URL, and the only place the embed flag is minted.
        # The direct-content link deliberately keeps the bare URL: a reader who
        # follows it has left the shell behind and needs the chrome back.
        embed_url=f"{renderer_url}?{EMBED_QUERY}",
        allow_fullscreen=allow_fullscreen,
        download_url=download_url,
        console_url=console_url,
        chip=chip_label(content_type, name or title) if content_type else kind,
        meta_line=meta_line,
        og_type="website" if og_type == "website" else "article",
    )


def render_shell_not_found() -> str:
    """Render the one detail-free refusal used by the trusted shell."""
    return _env.get_template("shell-notfound.html").render(asset_v=SHELL_ASSET_V)
