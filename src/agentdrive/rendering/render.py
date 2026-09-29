"""Artifact bytes → safe HTML. The shared renderer behind BOTH viewers.

Content is UNTRUSTED: an agent wrote it and a stranger will open it on our
origin. So `html=False` on the markdown parser is load-bearing, not a
preference — with it on, a shared artifact is stored XSS.

Modes, chosen from the content type: `markdown`, highlighted `code`, a
`table` for delimited data, a `sheet` for a workbook, a `document` for a Word
file or a slide deck, `data` for a JSON, YAML or XML tree, an `image`, a `video`, a `pdf`,
a `page` for a `text/html` artifact rendered as a document, an `empty` card
for a file with no content, and a `download` card for anything we cannot show.
The mode is the body's contract with both adapters and with the stylesheet —
`body.mode-{mode}` and `main.doc.{mode}` — so adding one is additive, but
renaming one is a break on three surfaces at once.

`page` is the one mode behind a flag (`static_html_rendering_enabled`,
default off — the 2026-08-26 static-HTML design). It is also the only mode
that renders markup the ARTIFACT wrote rather than markup this module wrote,
which is why `sanitize.py` exists and why it fails closed to `code` — today's
escaped source — rather than to anything the author supplied.

Two adapters consume this module — `public/` (share links and permalinks,
anonymous, non-frameable) and `viewer/` (the console's private viewer on the
isolated embed origin). Authorization and response policy live in those
adapters; nothing in here may branch on which surface is asking, or the two
surfaces' rendering drifts apart.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from dataclasses import dataclass
from html import escape, unescape

import mammoth
import pyarrow.parquet as pq
from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename, guess_lexer
from pygments.util import ClassNotFound

from ..core.kinds import kind_for
from ..sheets.workbook import guard_archive
from . import data as jsondata
from . import slides as slidedeck
from .sanitize import sanitize_html
from .source import ByteSource, PreviewBudgetExceeded, RangedFile

MAX_RENDER_BYTES = 2 * 1024 * 1024

# One ceiling per format, because the cap does three jobs and they want three
# answers. Text — markdown, code, html, csv, json — grows into DOM as it
# grows on disk (highlighted source is 15-25× its input once rendered), so
# its whole-file ceiling stays at `MAX_RENDER_BYTES` and anything larger is
# previewed from its head instead. Container formats whose OUTPUT is bounded
# by their own caps — a workbook to 2,000 rows, a deck to 200 slides and
# 4 MiB of pictures, a document to its picture budget — can be read whole up
# to what one render may hold in memory on a 1 GiB instance with the parser's
# multiplier on top. `render_ceiling` is the single place that decides; the
# routes' `preview_input` and this module's size gate both call it.
JSON_MAX_BYTES = 8 * 1024 * 1024
WORKBOOK_MAX_BYTES = 24 * 1024 * 1024
DOCUMENT_MAX_BYTES = 32 * 1024 * 1024

# Above its ceiling a whole-object fetch is refused, but several formats can
# still be previewed from a fraction of the file when the caller hands over a
# `ByteSource` instead of bytes: parquet from its footer and one row group,
# and every text format from its first `HEAD_PREVIEW_BYTES` — a csv as a
# table, json lines as records, markdown and code as highlighted source. The
# download card stays the answer for every other oversized format, and for
# these when no source is offered — a renderer given `b""` and a size still
# cards.
HEAD_PREVIEW_BYTES = 1024 * 1024

# Videos above this render as a download card. The embedded viewer has no
# ranged streaming — it fetches the bytes through a credentialed request into a
# blob — so this is a ceiling on what one tab is asked to hold in memory, not a
# statement about what the store will keep.
MAX_INLINE_VIDEO_BYTES = 64 * 1024 * 1024

# Table caps. The byte ceiling above bounds bytes, not elements, and a narrow
# CSV is almost all rows: 2 MiB of `a,b\n` is ~350,000 of them.
_TABLE_MAX_ROWS = 500
_TABLE_MAX_COLUMNS = 40

# Workbook-wide, because a `sheet` document carries EVERY tab: twelve sheets
# at the per-sheet cap would be 6,000 rows in one DOM. Per-sheet truncation
# is stated on the sheet it applies to, exactly like a csv's.
_SHEET_MAX_TOTAL_ROWS = 2_000

# Absolute http(s) — the only src shape the viewers' CSP actually refuses.
_REMOTE_URL = re.compile(r"^https?://", re.IGNORECASE)

# The only document-relative src both surfaces actually serve: the artifact's
# own bytes, at `content` beside the document. Anything else relative resolves
# against the shell URL, where nothing is mounted.
_RESOLVABLE_SRC = frozenset({"content"})

# Which kinds we can show inline, and how. Everything absent from this map
# gets a download card. Keyed by `kind_for`, so the body and the kind chip on
# the same page can never disagree about what an artifact is.
_TEXTUAL_KINDS = frozenset({"code", "dataset"})

# Spreadsheet containers, which are ZIP archives and therefore never decode as
# UTF-8. Before this mode they fell straight through to the download card.
_WORKBOOK_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
})
_WORKBOOK_SUFFIXES = (".xlsx", ".xlsb", ".xls")

# Word documents — the other OOXML container an agent routinely writes. Only
# the plain `.docx`: the macro-enabled `.docm` is refused exactly as `.xlsm`
# is, and the legacy binary `.doc` is not a format mammoth can read, so both
# keep the download card rather than be listed here and fail inside.
_DOCUMENT_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
})
_DOCUMENT_SUFFIXES = (".docx",)

# JSON, which until this mode rendered as highlighted source: correct, and the
# wrong view for a 500-line payload. `data.py` decides between a table (an
# array of flat records) and a collapsible tree (everything else). JSON Lines
# is the same decision over one record per line.
_JSON_TYPES = frozenset({"application/json", "application/geo+json", "application/ld+json"})
_JSONL_TYPES = frozenset({"application/x-ndjson", "application/x-jsonl", "application/jsonl"})
_JSON_SUFFIXES = (".json", ".geojson")
_JSONL_SUFFIXES = (".jsonl", ".ndjson")
# YAML and XML take the same tree. YAML because it IS the JSON data model
# with a friendlier syntax; XML because an element tree folds the same way
# an object tree does, and a reader wants the structure, not the angle
# brackets. `image/svg+xml` is an image and never reaches here; `text/html`
# is a page, not XML.
_YAML_TYPES = frozenset({"application/yaml", "application/x-yaml", "text/yaml", "text/x-yaml"})
_YAML_SUFFIXES = (".yaml", ".yml")
_XML_TYPES = frozenset({"application/xml", "text/xml"})
_XML_SUFFIXES = (".xml", ".xsd", ".xsl", ".xslt", ".plist", ".rss", ".atom", ".svg")
# Slide decks — rendered as their content, slide by slide, in `document` mode
# (see `slides.py`). Plain `.pptx` only: `.pptm` is refused like `.docm`, and
# the legacy binary `.ppt` has no reader here. Decks are big — a few photos
# put one past `MAX_RENDER_BYTES` — so they are the one non-tabular format
# `previews_when_large` admits, read whole in one ranged GET up to this
# ceiling, and a card above it.
_SLIDES_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
})
_SLIDES_SUFFIXES = (".pptx",)
SLIDES_MAX_BYTES = 32 * 1024 * 1024
# Parquet — the dataset format a data agent actually writes, and until this
# branch the one whose chip said `parquet` over a download card. Columnar and
# compressed, so it can only ever fail the UTF-8 decode below.
_PARQUET_TYPES = frozenset({"application/vnd.apache.parquet", "application/x-parquet"})
_PARQUET_SUFFIX = ".parquet"

# Decoding bound for the ONE row group the preview reads. `total_byte_size` is
# the row group's declared uncompressed size, read from the footer before any
# page is inflated — the same shape as the zip guard's central-directory
# check: a bomb is refused, not expanded. The 2 MiB byte ceiling upstream
# bounds the file; this bounds what the file claims to become.
_PARQUET_MAX_ROW_GROUP_BYTES = 64 * 1024 * 1024

# A cell is bounded too. The table caps bound rows and columns, but one string
# column can hold the whole 2 MiB in 500 cells; a preview cell past this is
# elided, and the download has the rest.
_PARQUET_MAX_CELL_CHARS = 500


@dataclass(frozen=True)
class RenderedBody:
    """The body and its mode.

    There was briefly a `source_html` field here, carrying `page` mode's
    Pygments source so an adapter could place it. Nothing ever read it: the
    mode strip embeds both views inside `html`, which is what makes one
    implementation serve both surfaces. A second live copy of the highlighted
    source bought nothing and roughly doubled the payload for the largest
    documents the renderer accepts, so it is gone.
    """

    html: str
    mode: str
    title: str | None = None
    # True when `html` carries a ```mermaid fence for the client to draw. The
    # adapters use it to load the diagram engine for THIS document only — 3.5
    # MB of JavaScript has no business loading for a plain report — the same
    # way pdf.js loads only for a pdf.
    diagrams: bool = False


def _highlight_fence(code: str, lang: str, _attrs: str) -> str:
    """Pygments for a fenced block inside a markdown document.

    Without this, markdown-it emits a bare `<pre><code class="language-x">`,
    which none of the stylesheet's token rules match — they are all scoped to
    `.highlight`. The same Python would then render coloured as a `.py`
    artifact and monochrome inside a report, which reads as a bug in the
    viewer rather than a difference in how the code arrived.

    Returning "" on an unknown language is the documented way to fall back to
    markdown-it's own escaping, so an unrecognised fence stays safe rather
    than unstyled-and-raw.

    The `<pre …>` wrapper is built here rather than taken from Pygments'
    `cssclass=`, which emits `<div class="highlight"><pre>`. markdown-it only
    adopts a highlighter's output verbatim when it starts with `<pre`;
    anything else it nests inside its own `<pre><code>`, which would produce
    `<pre><code><div><pre>` — invalid nesting, and a code block drawn inside a
    second code block.
    """
    if not lang:
        return ""
    if lang.strip().lower() == "mermaid":
        return _diagram_fence(code)
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return ""
    inner = highlight(code, lexer, HtmlFormatter(nowrap=True))
    return f'<pre class="highlight"><code>{inner}</code></pre>'


# The marker the viewers' `diagrams.js` looks for. An attribute rather than a
# class because the sanitiser drops every `data-*` and namespaces every class
# an author writes, so a `text/html` artifact cannot forge one: the ONLY way
# to emit this is a ```mermaid fence, which this module renders itself.
DIAGRAM_MARKER = 'data-diagram="mermaid"'


def _diagram_fence(code: str) -> str:
    """A ```mermaid fence — the diagram's source, marked for the client.

    Rendering happens in the browser, never here: mermaid is a JavaScript
    engine, and the server has no DOM to measure text in. So the document
    carries the escaped source in a `<pre>` (which is also the complete,
    readable fallback when script is off or the engine cannot draw it), and
    each viewer's `diagrams.js` replaces it with the drawn diagram. Starts with
    `<pre` because that is the one shape markdown-it adopts verbatim.
    """
    return f'<pre class="diagram-source" {DIAGRAM_MARKER}><code>{escape(code)}</code></pre>'


def _placeholder(alt: str, reason: str, extra_class: str = "") -> str:
    """The stand-in for an image the reader is never going to see.

    One shape for every unshowable image, because the reader's question is the
    same in each case — "is this document broken, or is this deliberate?" — and
    a broken-image icon answers it wrongly every time.
    """
    label = escape(alt) if alt else "image"
    classes = "blocked-remote" + (f" {extra_class}" if extra_class else "")
    return (
        f'<span class="{classes}" role="img" '
        f'aria-label="{escape(reason)}: {label}">'
        f"<span>{label}</span>"
        f"<span>{escape(reason.lower())}</span>"
        "</span>"
    )


def _image_rule(tokens, idx, options, env):  # noqa: ANN001, ARG001 - markdown-it rule signature
    """Decide what an `![alt](src)` in an agent-written document becomes.

    Exactly two srcs resolve inside a rendered document, and everything else
    is a broken-image icon dressed up as content:

    * `content` — the artifact's own bytes, the one path both surfaces serve.
    * a `data:` URI — carried in the document, fetched from nowhere. Both
      CSPs allow `data:` in `img-src`.

    **Remote** is refused on privacy grounds, not capability. An image URL in
    an agent-written document is a beacon that reports when and where a
    private artifact was read; letting the `<img>` through means the browser
    enforces the CSP and the reader sees the policy working, presented as the
    document being broken. So the refusal is stated.

    **Everything else** — `chart.png`, `/img/x.png`, `//host/x.png` — is a
    document addressing a file the viewer has no way to fetch. A sibling
    artifact is the common case and the interesting one: relative srcs
    resolved against the shell URL and 404'd, so "write a report and the
    chart beside it" produced a report with a broken icon in it. Naming a
    sibling is real work (it needs an addressing scheme and a capability that
    covers more than one artifact); until that exists, saying so is strictly
    better than a browser's idea of a missing file.
    """
    token = tokens[idx]
    src = (token.attrGet("src") or "").strip()
    alt = token.content or ""
    if _REMOTE_URL.match(src):
        return _placeholder(alt, "Remote image blocked")
    if src in _RESOLVABLE_SRC or src.lower().startswith("data:"):
        return options.get("_default_image")(tokens, idx, options, env)
    return _placeholder(alt, "Image unavailable", "image-unavailable")


# markdown-it expresses a table's column alignment as `style="text-align:…"`.
# Both viewers serve `style-src 'self'` with no `unsafe-inline`, so the
# browser refuses the attribute outright: the alignment is dropped and a CSP
# violation is logged per cell. A right-aligned money column — the reason
# anyone writes `| ---: |` — silently rendered left, and a table with a
# handful of aligned columns filled the console with errors that looked like
# something far worse than a lost text-align.
#
# The same alignment as a class survives the policy untouched.
_ALIGN_CLASS = {
    "text-align:left": "ta-left",
    "text-align:center": "ta-center",
    "text-align:right": "ta-right",
}


def _aligned_cell(tokens, idx, options, env):  # noqa: ANN001, ARG001 - markdown-it rule signature
    """`th`/`td` with the alignment moved from a style attribute to a class."""
    token = tokens[idx]
    style = (token.attrGet("style") or "").replace(" ", "")
    align = _ALIGN_CLASS.get(style)
    if align:
        # Drop the refused attribute rather than leaving it to be blocked:
        # an attribute the browser will not honour is not documentation, it
        # is a console error with no reader-visible effect.
        token.attrs.pop("style", None)
        token.attrJoin("class", align)
    return options["_default_cell"](tokens, idx, options, env)


def _md() -> MarkdownIt:
    # html=False escapes raw HTML instead of passing it through, which is the
    # whole security story for this module. The commonmark preset also leaves
    # linkify/smartquotes off; we keep it that way, so bare URLs in untrusted
    # text stay inert text rather than becoming anchors we did not author.
    #
    # The highlighter is safe under that rule: Pygments escapes the code it is
    # given, and the fallback path is markdown-it's own escaping.
    md = MarkdownIt("commonmark", {"html": False, "highlight": _highlight_fence})
    # Tables, and ONLY tables, on top of commonmark.
    #
    # CommonMark has no table syntax, so a pipe table rendered as a paragraph
    # of pipes and dashes — the exact shape agents write most, since every
    # model emits GFM. The `default` preset would fix it and also switch on
    # linkify and smartquotes, which the comment above deliberately refuses:
    # bare URLs in untrusted text must stay inert text, not become anchors we
    # did not author. Enabling the one rule keeps that refusal intact.
    md.enable("table")
    # Strikethrough and task lists, for the same reason tables are on: every
    # model emits GFM, so `~~gone~~` and `- [x] done` are the shapes agents
    # actually write. Without them a checklist renders as literal `[x]` and
    # a correction renders as literal tildes — markup, shown as text, in the
    # place a reader expects the document.
    #
    # Both are markup-level and carry no author-controlled HTML: the
    # checkbox is generated, `disabled`, and never reflects back anywhere.
    # `html=False` above is untouched, which is the whole security story.
    md.enable("strikethrough")
    md.use(tasklists_plugin)
    # Keep a handle on the built-in so the rule can defer to it for local
    # sources rather than reimplementing image rendering.
    md.options["_default_image"] = md.renderer.rules.get("image", _default_image)
    md.renderer.rules["image"] = _image_rule
    # Same passthrough shape as the image rule: markdown-it has no `th_open`
    # or `td_open` entry by default (both go through `renderToken`), so the
    # fallback is the shared default rather than a reimplementation.
    md.options["_default_cell"] = md.renderer.rules.get("th_open", _default_token)
    md.renderer.rules["th_open"] = _aligned_cell
    md.renderer.rules["td_open"] = _aligned_cell
    return md


def _default_token(tokens, idx, options, env):  # noqa: ANN001, ARG001
    """The built-in token renderer, as a plain function to defer to."""
    from markdown_it.renderer import RendererHTML

    return RendererHTML().renderToken(tokens, idx, options, env)


def _default_image(tokens, idx, options, env):  # noqa: ANN001, ARG001
    """markdown-it-py has no `image` entry in `renderer.rules` by default — the
    token is handled by `renderToken` — so this stands in as the passthrough."""
    from markdown_it.renderer import RendererHTML

    return RendererHTML().renderToken(tokens, idx, options, env)


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def render_body(
    data: bytes,
    content_type: str,
    name: str,
    *,
    size_bytes: int | None = None,
    print_button: bool = False,
    source: ByteSource | None = None,
) -> RenderedBody:
    """Render `data` for display. Never raises on bad input.

    `size_bytes` is the artifact's real size, for callers that already know it
    and deliberately did not fetch the bytes. Without it the size test reads
    `len(data)`, which silently mis-reads a skipped fetch as a zero-length
    document: the caller passes `b""` to avoid pulling 3 MiB into memory, this
    function sees a small empty file, and an oversized artifact renders as a
    blank page instead of a download card.

    `print_button` draws the PDF toolbar's print control. It is off by default
    because the button is only honest on a surface that has wired it: the
    control is inert markup on its own, and `pdfview.js` treats every toolbar
    element as optional, so an unwired button is a click that does nothing at
    all. The public share page wires it; the embedded viewer does not, because
    its `window.open` is refused by the console's iframe sandbox.
    """
    kind = kind_for(content_type, name)
    base_type = (content_type or "").split(";", 1)[0].strip().lower()

    # Images are served by the `/content` sub-route rather than inlined, so
    # this returns before the size check: there are no bytes to render.
    if kind == "image":
        return RenderedBody(
            html=f'<img class="artifact-image" alt="{escape(name)}" src="content" />',
            mode="image",
        )

    # Video plays inline, for the same reason images do: `video` is one of the
    # eight kinds with a glyph drawn for it, and answering a video with a
    # download card makes the kind a label for something the viewer cannot
    # actually show.
    #
    # Bounded, unlike images, because the embedded viewer fetches the bytes
    # into a blob before handing them to the element — there is no ranged
    # streaming through a credentialed fetch, so an unbounded video is an
    # unbounded allocation in the reader's tab. Above the bound the download
    # card is the honest answer.
    if kind == "video" or base_type.startswith("audio/"):
        media = "audio" if base_type.startswith("audio/") else "video"
        actual = len(data) if size_bytes is None else size_bytes
        if actual <= MAX_INLINE_VIDEO_BYTES:
            return RenderedBody(
                html=(
                    f'<{media} class="artifact-{media}" controls preload="metadata" '
                    f'src="content" aria-label="{escape(name)}"></{media}>'
                ),
                mode=media,
            )
        return RenderedBody(html=_download_card(name), mode="download")

    # PDFs render through pdf.js — the same engine the LaTeX preview uses,
    # reused from `static/pdfview.js` rather than reimplemented.
    #
    # Legacy chose this over the browser's native `<embed>` on purpose, and
    # its comment says why: pdf.js gives selectable text, find-in-page and
    # clickable links, which `<embed>` does not. Independently confirmed here
    # that `<embed>` also simply does not render on this surface — it failed
    # with `object-src`/`frame-src` granted AND with the page CSP removed
    # entirely, so it was never a policy problem.
    #
    # The element ids below are the ones `pdfview.js`'s attach* helpers look
    # for, so the wrapper ports unchanged. Before the size check: pdf.js
    # streams the document itself from `/content`.
    if base_type == "application/pdf":
        return RenderedBody(
            html=_pdf_shell(name, print_button=print_button), mode="pdf"
        )

    if (len(data) if size_bytes is None else size_bytes) > render_ceiling(content_type, name):
        # Over this format's ceiling. Some formats preview from a fraction of
        # the file when the caller offers ranged reads — see
        # `previews_when_large`; every other oversized artifact, and these
        # without a source, is a card.
        if source is not None:
            partial = _large_preview(source, content_type, name)
            if partial is not None:
                return partial
        return RenderedBody(html=_download_card(name), mode="download")

    # Parquet, BEFORE the UTF-8 decode below, for the same reason workbooks
    # are: compressed columnar bytes can only ever fail that decode. It shares
    # the csv's `table` mode and markup — a reader should not learn two shapes
    # for "rows and columns" because of how the agent chose to serialise them.
    if _is_parquet(content_type, name):
        table = _parquet_table(data)
        if table is not None:
            return RenderedBody(html=_view("rendered", table), mode="table")
        return RenderedBody(html=_download_card(name), mode="download")

    # Workbooks, BEFORE the UTF-8 decode below: an xlsx is a ZIP archive, so
    # it can only ever fail that decode, and until this mode existed every
    # spreadsheet in the product answered with a download card.
    if _is_workbook(content_type, name):
        document = _sheet_document(data, content_type, name)
        if document is not None:
            return RenderedBody(html=document, mode="sheet")
        return RenderedBody(html=_download_card(name), mode="download")

    # Word documents, for the same reason and in the same place: a docx is a
    # ZIP too, so the decode below can only ever refuse it.
    if _is_document(content_type, name):
        return _word_document(data, name)

    if _is_slides(content_type, name):
        return _slides_document(data, name)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return RenderedBody(html=_download_card(name), mode="download")

    # An empty file is a fact worth stating. Rendering it as an empty <pre>
    # produces a blank rectangle indistinguishable from a viewer that failed —
    # the reader cannot tell "there is nothing here" from "we could not show
    # it", and only one of those is worth retrying.
    if not text.strip():
        return RenderedBody(html=_empty_card(name), mode="empty")

    # `text/html` — and only `text/html` — can render as a document rather
    # than as its own source. XHTML, XML and SVG are untouched (an SVG still
    # renders through `<img>`, where script does not run), and a `.html`
    # filename under a generic type is not enough: the declared type is what
    # decides, exactly as it does for every other mode.
    #
    # Reading a config flag here rather than taking a parameter is deliberate.
    # This is a product-wide capability, not a per-surface policy like
    # `print_button`, and the module's contract is that the two surfaces
    # cannot drift — a parameter is precisely how one adapter ends up
    # rendering a page while the other renders source.
    if base_type == "text/html" and _static_html_enabled():
        return _html_page(text, name)

    if kind == "md":
        rendered = _md().render(text)
        return RenderedBody(
            html=(_mode_strip() + _view("rendered", rendered)
                  + _view("source", f"<pre>{escape(text)}</pre>", hidden=True)),
            mode="markdown",
            title=_first_heading(text),
            # Decided on the RENDERED markup, where the marker can only have
            # come from `_diagram_fence`: the source pane holds the author's
            # text, escaped, and a literal `data-diagram` written there is
            # prose, not a diagram.
            diagrams=DIAGRAM_MARKER in rendered,
        )

    # Tabular data renders as a table. Pygments has no CSV lexer, so a dataset
    # previously fell through to `guess_lexer` and landed in a monospace block
    # with no alignment — the one shape where the columns are the whole point.
    if kind == "dataset":
        table = _delimited_table(text, name, content_type)
        if table is not None:
            return RenderedBody(
                html=(_mode_strip("Table", "Raw text") + _view("rendered", table)
                      + _view("source", f"<pre>{escape(text)}</pre>", hidden=True)),
                mode="table",
            )

    # JSON gets a shape before it gets colour: a table when it is records, a
    # tree the reader can fold otherwise. Invalid JSON falls through to the
    # highlighted source below, which is what it always got.
    flavour = _data_flavour(content_type, name)
    if flavour is not None:
        body = _data_body(text, name, flavour)
        if body is not None:
            return body

    if kind in _TEXTUAL_KINDS:
        return RenderedBody(html=_highlighted_source(text, name), mode="code")

    return RenderedBody(html=_download_card(name), mode="download")


def _data_flavour(content_type: str, name: str) -> str | None:
    """`"json"`, `"lines"`, `"yaml"`, `"xml"`, or None. Declared type first,
    filename second, the same order every other dispatch here uses. A generic
    `application/*+xml` (Atom, RSS, XHTML, …) is XML; SVG never arrives here
    because `kind_for` made it an image first."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    lowered = (name or "").lower()
    if base in _JSONL_TYPES or lowered.endswith(_JSONL_SUFFIXES):
        return "lines"
    if base in _JSON_TYPES or lowered.endswith(_JSON_SUFFIXES):
        return "json"
    if base in _YAML_TYPES or lowered.endswith(_YAML_SUFFIXES):
        return "yaml"
    if base in _XML_TYPES or base.endswith("+xml") or lowered.endswith(_XML_SUFFIXES):
        return "xml"
    return None


def _data_body(
    text: str, name: str, flavour: str, *, extent: str | None = None
) -> RenderedBody | None:
    """`mode="table"` for records, `mode="data"` for a tree; None when the
    text does not parse as its flavour, so the caller keeps today's
    highlighted source.

    The source pane is the same Pygments rendering the file had before this
    mode existed — kept, and moved behind a control, exactly as `page` keeps
    the HTML source: a reader comparing against a spec still wants the bytes.
    """
    # A head preview has no source pane — the bytes on hand are not the file —
    # and states its extent where a whole file's caption would state totals.
    source = "" if extent else _view("source", _highlighted_source(text, name), hidden=True)
    strip = (lambda a, b: "") if extent else _mode_strip
    extent_note = (
        f'<p class="doc-note">Showing {escape(extent)} — download the file for all of it.</p>'
        if extent else ""
    )
    if flavour == "xml":
        tree = jsondata.xml_tree(text)
        if tree is None:
            return None
        return RenderedBody(
            html=strip("Tree", "Source")
            + _view("rendered", tree.html + _cut_note(tree) + extent_note) + source,
            mode="data",
        )
    if flavour == "yaml":
        value = jsondata.parse_yaml(text)
    else:
        value = jsondata.parse(text, lines=flavour == "lines")
    if value is None:
        return None
    rows = jsondata.as_rows(value)
    if rows is not None:
        table = _data_table(
            rows.head, rows.body, total_rows=rows.total_rows, total_columns=rows.total_columns
        )
        return RenderedBody(
            html=strip("Table", "Source") + _view("rendered", table + extent_note) + source,
            mode="table",
        )
    tree = jsondata.tree(value)
    return RenderedBody(
        html=(strip("Tree", "Source")
              + _view("rendered", tree.html + _cut_note(tree) + extent_note) + source),
        mode="data",
    )


def _cut_note(tree: jsondata.Tree) -> str:
    if not tree.truncated:
        return ""
    return (
        '<p class="doc-note">Showing '
        + ", ".join(tree.truncated)
        + " — download the file for all of it.</p>"
    )


def _highlighted_source(text: str, name: str) -> str:
    """A document's own source, highlighted. The `code` mode's whole body.

    Also the `page` mode's source view, which is the point of it being one
    function: the source pane is not a second rendering of the markup, it is
    the rendering `text/html` has always had, kept and moved behind a control.

    Falls back through filename → content guess → escaped plain text, so an
    unrecognised language is unstyled rather than unrendered.
    """
    try:
        lexer = get_lexer_for_filename(name, text)
    except ClassNotFound:
        try:
            lexer = guess_lexer(text)
        except ClassNotFound:
            return f"<pre>{escape(text)}</pre>"
    return highlight(text, lexer, HtmlFormatter(nowrap=False, cssclass="highlight"))


def _static_html_enabled() -> bool:
    """The flag, read at CALL time rather than imported at module scope.

    Reading a config flag rather than taking a parameter stays deliberate —
    see `render_body` — but importing `settings` here made this module
    unimportable without a database URL, a bucket and a session secret.
    That is backwards for the one module in the package whose contract is
    "bytes in, markup out": it broke a comparison harness that only wanted
    to diff two revisions' output, and it would break any script, notebook
    or test that wants the renderer without an environment. The read still
    happens exactly once per render, and the two surfaces still cannot drift.
    """
    from ..config import settings

    return settings.static_html_rendering_enabled


def _html_page(text: str, name: str) -> RenderedBody:
    """`mode="page"` — sanitised markup, with the source a click away.

    Fails closed to SOURCE. If sanitising raises for any reason the answer is
    today's escaped-source document, byte-identical to the flag-off path —
    never the author's markup, which is the one output that must be
    impossible. A document that sanitises to nothing (one that is all
    `<script>`) gets the empty card rather than a blank page: a reader cannot
    tell an empty document from a broken viewer, and only one of those is
    worth retrying.
    """
    source = _highlighted_source(text, name)
    try:
        rendered = sanitize_html(
            text,
            # The same answer markdown gives, from the same function, because
            # a reader looking at two documents in one drive should not have
            # to learn two conventions for "this image is not here".
            image_placeholder=lambda alt, reason: (
                _placeholder(alt, "Remote image blocked")
                if reason == "remote"
                else _placeholder(alt, "Image unavailable", "image-unavailable")
            ),
            # Same refusal markdown already makes, for the same reason: an
            # image URL an agent wrote is a request to a third party the
            # moment a reader opens the document.
            allow_remote_images=False,
        )
    except Exception:
        return RenderedBody(html=source, mode="code")
    if not rendered.strip():
        return RenderedBody(html=_empty_card(name), mode="empty")
    return RenderedBody(
        html=_mode_strip() + _view("rendered", rendered) + _view("source", source, hidden=True),
        mode="page",
    )


def _mode_strip(rendered_label: str = "Rendered", source_label: str = "Source") -> str:
    """The rendered ⇄ source control, emitted as part of the DOCUMENT.

    One implementation for both surfaces, and no new control for the console
    to draw. It is document-level, not chrome: `chrome:"none"` suppresses the
    shell's header (title, path, meta) and must not take this with it.

    Keyed on `data-view`, which is deliberately outside the sanitiser's
    attribute allowlist — and `button` is dropped with its contents — so an
    artifact cannot forge a strip of its own for a reader to click.
    """
    return (
        '<nav class="doc-modes" aria-label="View">'
        '<button type="button" class="doc-mode is-current" data-view="rendered" '
        f'aria-pressed="true">{rendered_label}</button>'
        '<button type="button" class="doc-mode" data-view="source" '
        f'aria-pressed="false">{source_label}</button>'
        "</nav>"
    )


def _view(name: str, html: str, *, hidden: bool = False) -> str:
    """One pane of a `page` document. Both ship in the same response, so the
    toggle costs no second round trip — which matters on the private surface,
    where the shell drops its credential as soon as the document paints and
    could not make one."""
    return (
        f'<div class="doc-view" data-view="{name}"{" hidden" if hidden else ""}>'
        f"{html}</div>"
    )


def _btn(el_id: str, label: str, glyph: str) -> str:
    return (
        f'<button type="button" class="pv-btn" id="{el_id}" '
        f'aria-label="{label}">{glyph}</button>'
    )


def _pdf_shell(name: str, *, print_button: bool = False) -> str:
    """Empty viewer shell — `pdf-visitor.js` owns everything inside it.

    The element ids are the ones `pdfview.js`'s attach* helpers query, so the
    wrapper ports across unchanged. `pv-print` is the one id drawn only on
    request: see `render_body` for why an unwired print button is worse than
    no print button.

    `<noscript>` matters: the CSP permits our own script, but a reader with
    JavaScript disabled would otherwise get a blank frame, so they get the
    download instead.
    """
    safe = escape(name)
    toolbar = (
        '<div class="pdf-toolbar">'
        + _btn("pv-page-prev", "Previous page", "&#8249;")
        + '<input class="pv-page" id="pv-page-input" aria-label="Page" value="1" />'
        + '<span class="pv-total" id="pv-page-total"></span>'
        + _btn("pv-page-next", "Next page", "&#8250;")
        + '<span class="bar-sep"></span>'
        + _btn("pv-zoom-out", "Zoom out", "&#8722;")
        + '<input class="pv-pct" id="pv-zoom-pct" aria-label="Zoom percent" value="100%" />'
        + _btn("pv-zoom-in", "Zoom in", "+")
        + _btn("pv-fit", "Fit page", "Fit")
        + _btn("pv-rotate", "Rotate", "&#8635;")
        + (_btn("pv-print", "Print", "Print") if print_button else "")
        + '<span class="grow"></span>'
        + _btn("pv-find-toggle", "Find in document", "Find")
        + "</div>"
    )
    find = (
        '<div class="pdf-find" id="pv-find" hidden>'
        '<input id="pv-find-input" aria-label="Find in document" placeholder="Find" />'
        '<span id="pv-find-count"></span>'
        + _btn("pv-find-prev", "Previous match", "&#8249;")
        + _btn("pv-find-next", "Next match", "&#8250;")
        + _btn("pv-find-close", "Close find", "&#215;")
        + "</div>"
    )
    return (
        '<div class="pdf-doc" id="pdf-doc" data-pdf-url="content">'
        + toolbar
        + find
        # Legacy's exact nesting, and it is load-bearing: pdf.js throws
        # "The `container` must be absolutely positioned" otherwise. An
        # absolute host inside a relative body is what satisfies it.
        + '<div class="pdf-doc-body">'
        '<div class="pdf-host" id="pv-container">'
        '<div id="pv-viewer" class="pdfViewer"></div></div></div>'
        "</div>"
        f'<noscript><div class="download-card"><p>{safe}</p>'
        '<p><a class="btn" href="content" download>Download</a></p></div></noscript>'
    )


def _is_workbook(content_type: str, name: str) -> bool:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base in _WORKBOOK_TYPES:
        return True
    # Macro containers are deliberately absent from both lists: we refuse to
    # open them at all, so they keep the download card.
    return name.lower().endswith(_WORKBOOK_SUFFIXES)


def _is_slides(content_type: str, name: str) -> bool:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base in _SLIDES_TYPES:
        return True
    return name.lower().endswith(_SLIDES_SUFFIXES)


def _slides_document(data: bytes, name: str) -> RenderedBody:
    """`mode="document"` — a deck as its slides' content, see `slides.py`.

    Shares the docx mode and typography on purpose: both are "a document an
    agent wrote, shown as what it says". The zip guard runs first, as it does
    for every OOXML container; anything the reader cannot open is the
    download card, never a 500 and never a partial page dressed as a deck.
    """
    try:
        guard_archive(data)
        deck = slidedeck.render_deck(data)
    except Exception:  # noqa: BLE001 - untrusted bytes: any failure is a download
        deck = None
    if deck is None:
        return RenderedBody(html=_download_card(name), mode="download")
    if not deck.html.strip():
        return RenderedBody(html=_empty_card(name), mode="empty")
    note = ""
    if deck.omitted:
        note = (
            '<p class="doc-note">Not shown in this preview: '
            f"{escape('; '.join(deck.omitted))}. Download the file to see them.</p>"
        )
    return RenderedBody(
        html=_view("rendered", f'<div class="deck">{deck.html}</div>' + note),
        mode="document",
        title=deck.title,
    )


def _is_document(content_type: str, name: str) -> bool:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base in _DOCUMENT_TYPES:
        return True
    return name.lower().endswith(_DOCUMENT_SUFFIXES)


_HTML_H1 = re.compile(r"<h1(?:\s[^>]*)?>(.*?)</h1>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def _first_html_heading(html: str) -> str | None:
    """The first `<h1>`'s text, the way `_first_heading` reads a markdown
    title, so a Word report is named by its own title rather than its
    filename. Tags inside the heading are stripped; entities stay encoded
    because the caller escapes the title again."""
    match = _HTML_H1.search(html)
    if match is None:
        return None
    return unescape(_TAG.sub("", match.group(1))).strip() or None


def _word_document(data: bytes, name: str) -> RenderedBody:
    """`mode="document"` — a `.docx` as semantic HTML, through the sanitiser.

    mammoth is deliberate over the alternatives: it maps Word styles to
    headings, lists, tables and emphasis and drops the visual formatting, so
    the output is a document in OUR typography — the same standing markdown
    has — rather than a rendering of the author's page. That is also why this
    is not `page`: the untrusted-content band and `noindex` exist because
    static HTML can imitate a brand pixel for pixel, and a docx stripped to
    semantics cannot.

    The markup mammoth emits is still built from author-controlled text, links
    and images, so it goes through `sanitize_html` unchanged — the same
    allowlist, the same remote-image refusal, the same `data:` image rule
    (mammoth inlines pictures as data URIs, which is exactly the one `src`
    shape the sanitiser admits for `<img>`).

    There is no source pane. Unlike `text/html`, the bytes have no text form
    a reader could want, so a document that fails anywhere falls to the
    download card: never to the converter's unsanitised output, and never to
    an escaped dump of a ZIP archive.
    """
    try:
        guard_archive(data)
        converted = mammoth.convert_to_html(io.BytesIO(data))
        rendered = sanitize_html(
            converted.value,
            image_placeholder=lambda alt, reason: (
                _placeholder(alt, "Remote image blocked")
                if reason == "remote"
                else _placeholder(alt, "Image unavailable", "image-unavailable")
            ),
            allow_remote_images=False,
        )
    except Exception:  # noqa: BLE001 - untrusted bytes: any failure is a download
        # mammoth's failure set on malformed input is as wide and undocumented
        # as openpyxl's (`Unparseable` from the guard, `BadZipFile`, `KeyError`
        # from a missing part, XML errors from a corrupt one), and none of it
        # is worth a 500 for what is really a bad upload.
        return RenderedBody(html=_download_card(name), mode="download")
    if not rendered.strip():
        return RenderedBody(html=_empty_card(name), mode="empty")
    return RenderedBody(
        html=_view("rendered", rendered + _omitted_note(converted.messages)),
        mode="document",
        title=_first_html_heading(rendered),
    )


# mammoth reports every construct it dropped as a warning of this shape. Only
# these are stated to the reader: an unrecognised *style* loses formatting and
# keeps the words, whereas an ignored *element* is content that is not there.
_IGNORED_ELEMENT = re.compile(r"^An unrecognised element was ignored: (?:\{[^}]*\})?(\w+)$")
_OMITTED_NAMES = {
    "oMath": "equations",
    "oMathPara": "equations",
    "sdt": "form fields",
    "fldChar": "fields",
    "object": "embedded objects",
    "pict": "legacy pictures",
}


def _omitted_note(messages) -> str:  # noqa: ANN001 - mammoth's Message list
    """What the renderer left out, stated the way a truncated table states its
    cap: a report whose equations are simply missing is a report that lies
    about the data, and a reader cannot tell "not shown" from "not there"."""
    kinds: list[str] = []
    for message in messages:
        match = _IGNORED_ELEMENT.match(getattr(message, "message", "") or "")
        if match is None:
            continue
        label = _OMITTED_NAMES.get(match.group(1), match.group(1))
        if label not in kinds:
            kinds.append(label)
    if not kinds:
        return ""
    return (
        '<p class="doc-note">Not shown in this preview: '
        f"{escape(', '.join(kinds))}. Download the file to see them.</p>"
    )


def _column_label(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA. The spreadsheet column alphabet."""
    out = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _sheet_cell(value: object) -> str:
    """Excel's own rendering, deliberately not locale-aware: these files are
    reviewed against a source document, so a stable rendering beats one that
    shifts per reader. An empty cell is empty, not a dash — a spreadsheet is
    mostly empty cells and dashes everywhere would be noise."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _sheet_document(data: bytes, content_type: str, name: str) -> str | None:
    """A workbook: every sheet, with a tab strip that needs no JavaScript.

    None rather than raising on anything openpyxl refuses — a workbook we
    cannot parse is still worth offering as a download, and that beats an
    error page.

    **Tabs are in-document fragments, and that choice is load-bearing.**
    An earlier version put the tab in the query string (`?sheet=Costs`) and
    rendered one sheet per response. That works on the public surface and
    cannot work in the console's viewer: its shell URL is deliberately
    parameter-free so the iframe src leaks nothing, and the shell drops its
    credential the moment the document is painted, so there is nothing left
    to authenticate a second fetch with. Making it work meant intercepting
    the click in `shell.js` AND holding the credential open for the frame's
    life — a real security cost, to buy a control that silently does
    nothing if the intercept ever regresses.

    `#sheet-2` costs neither. `interceptLinks` already passes in-document
    hrefs straight through ("in-document navigation stays local"), `:target`
    does the switching in CSS, and both surfaces behave identically with
    scripting disabled. A specific tab stays deep-linkable, which is the
    property the design actually asked for.

    The cost is every sheet in one payload, so the caps below are
    workbook-wide as well as per-sheet. Truncation is always STATED.
    """
    # Imported here rather than at module scope: `rendering` is on the hot
    # path for every artifact, and openpyxl costs ~50ms to import for the
    # small fraction of artifacts that are workbooks.
    from ..sheets import workbook as wb

    try:
        index = wb.read_index(data, content_type=content_type, name=name)
        grid = wb.read_grid(data, content_type=content_type, name=name)
    except Exception:
        # `Unparseable`, a zip-bomb refusal, or anything openpyxl raises on a
        # malformed archive. Untrusted bytes: the catch is deliberately wide.
        return None

    names = [s.name for s in index.sheets if s.name in grid]
    if not names:
        return None

    parts: list[str] = ['<div class="workbook">']
    budget = _SHEET_MAX_TOTAL_ROWS

    for position, sheet_name in enumerate(names):
        rows = grid.get(sheet_name, [])
        total_rows = len(rows)
        total_columns = max((len(r) for r in rows), default=0)
        # Per-sheet cap first, then whatever the workbook budget has left, so
        # a twelve-tab model does not become a hundred-thousand-cell DOM.
        allowance = max(0, min(_TABLE_MAX_ROWS, budget))
        shown = rows[:allowance]
        budget -= len(shown)
        width = min(total_columns, _TABLE_MAX_COLUMNS)

        parts.append(
            f'<section class="workbook-sheet" id="sheet-{position}" '
            f'aria-label="{escape(sheet_name, quote=True)}">'
        )
        parts.append('<div class="table-scroll"><table class="data-table workbook-grid">')
        # The column letters are a header row of their own. A spreadsheet's
        # first row is data as often as it is labels, so promoting it to <th>
        # would be a guess — the letters are always true.
        parts.append(
            '<thead><tr><th class="workbook-corner" scope="col">'
            '<span class="sr-only">Row</span></th>'
        )
        parts.extend(f'<th scope="col">{_column_label(c)}</th>' for c in range(width))
        parts.append("</tr></thead><tbody>")
        for r, row in enumerate(shown):
            parts.append(f'<tr><th class="workbook-rownum" scope="row">{r + 1}</th>')
            parts.extend(
                f"<td>{escape(_sheet_cell(row[c] if c < len(row) else None))}</td>"
                for c in range(width)
            )
            parts.append("</tr>")
        parts.append("</tbody>")

        notes = []
        if total_rows > len(shown):
            notes.append(f"{len(shown)} of {total_rows} rows")
        if total_columns > width:
            notes.append(f"{width} of {total_columns} columns")
        if notes:
            parts.append(
                f'<caption class="table-note">Showing {" and ".join(notes)} — '
                "download the file for all of it.</caption>"
            )
        parts.append("</table></div>")
        # Each panel carries its OWN copy of the strip, with its own tab
        # marked current. Only the visible panel's strip is on screen, so
        # the current tab is correct with no script and no `:has()` gymnastics
        # trying to style an anchor from the element it points at. A few
        # anchors per sheet is a cheap price for that.
        if len(names) > 1:
            parts.append('<nav class="workbook-tabs" aria-label="Sheets in this workbook">')
            for other, other_name in enumerate(names):
                current = other == position
                cls = "workbook-tab is-current" if current else "workbook-tab"
                aria = ' aria-current="page"' if current else ""
                parts.append(
                    f'<a class="{cls}" href="#sheet-{other}"{aria}>'
                    f"{escape(other_name)}</a>"
                )
            parts.append("</nav>")
        parts.append("</section>")

    parts.append("</div>")
    return "".join(parts)


def _download_card(name: str) -> str:
    safe = escape(name)
    return (
        '<div class="download-card">'
        f"<p>{safe}</p>"
        '<p><a class="btn" href="content" download>Download</a></p>'
        "</div>"
    )


def _empty_card(name: str) -> str:
    """`mode="empty"` — this file has no content, and says so.

    Deliberately carries no download button: there is nothing to download, and
    offering one would invite the reader to test that for themselves.
    """
    safe = escape(name)
    return (
        '<div class="download-card empty-card">'
        f"<p>{safe}</p>"
        "<p>This file is empty.</p>"
        "</div>"
    )


def _delimiter_for(content_type: str, name: str) -> str | None:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    lowered = (name or "").lower()
    if base == "text/tab-separated-values" or lowered.endswith(".tsv"):
        return "\t"
    if base == "text/csv" or lowered.endswith(".csv"):
        return ","
    return None


def _delimited_table(
    text: str, name: str, content_type: str, *, extent: str | None = None
) -> str | None:
    """CSV/TSV → a real table, or None to fall back to the text path.

    Returns None rather than raising on anything it cannot parse cleanly: a
    dataset with a broken row is still worth showing as text, and the caller's
    highlighted-code path is a strictly better answer than an error.

    Bounded on BOTH axes. The row cap keeps a 2 MiB CSV from becoming a
    200,000-row DOM that locks the reader's tab — the size ceiling upstream
    bounds bytes, not elements, and a narrow file is mostly rows. The column
    cap does the same for the pathological wide file. Truncation is stated in
    the caption, never silent: a table that quietly stops at row 500 is a table
    that lies about the data.
    """
    delimiter = _delimiter_for(content_type, name)
    if delimiter is None:
        return None

    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return None
    rows = [row for row in rows if row]
    if not rows:
        return None

    total_columns = max(len(row) for row in rows)
    head, *body = rows[: _TABLE_MAX_ROWS + 1]  # +1: the header is not a data row
    return _data_table(
        head, body,
        # A head read cannot know the file's row count; the caption then
        # states the extent read instead of a total it would have to invent.
        total_rows=None if extent else len(rows) - 1,
        total_columns=total_columns,
        extent=extent,
    )


def _data_table(
    head: list[str],
    body: list[list[str]],
    *,
    total_rows: int | None,
    total_columns: int,
    head_titles: list[str] | None = None,
    extent: str | None = None,
) -> str:
    """The one grid every tabular source emits — csv, tsv and parquet alike.

    `head` and `body` are already row-capped; `total_rows` and `total_columns`
    are the source's true extent, so the caption can say what was left out.
    `head_titles` is a per-column `title=` for the header — where the source
    knows its column types, a hover says so without a second header row.
    """
    truncated_columns = total_columns > _TABLE_MAX_COLUMNS
    width = min(total_columns, _TABLE_MAX_COLUMNS)

    def cells(row: list[str], tag: str, titles: list[str] | None = None) -> str:
        trimmed = row[:_TABLE_MAX_COLUMNS]
        # Pad short rows so the grid stays rectangular; a ragged CSV otherwise
        # renders as a staircase and reads as a rendering bug.
        trimmed += [""] * (width - len(trimmed))
        if titles is None:
            return "".join(f"<{tag}>{escape(cell)}</{tag}>" for cell in trimmed)
        return "".join(
            f'<{tag} title="{escape(title)}">{escape(cell)}</{tag}>'
            for cell, title in zip(trimmed, titles[:width], strict=False)
        )

    parts = [
        '<div class="table-scroll">',
        '<table class="data-table">',
        '<thead><tr><th scope="col" class="row-number">Row</th>'
        f"{cells(head, 'th', head_titles)}</tr></thead>",
        "<tbody>",
        *(f'<tr><th class="row-number" scope="row">{i}</th>{cells(row, "td")}</tr>'
          for i, row in enumerate(body, 1)),
        "</tbody>",
    ]

    shown_rows = len(body)
    notes = []
    if total_rows is None:
        notes.append(f"{shown_rows} rows from {extent}")
    elif total_rows > shown_rows:
        notes.append(f"{shown_rows} of {total_rows} rows")
    if truncated_columns:
        notes.append(f"{_TABLE_MAX_COLUMNS} of {total_columns} columns")
    if notes:
        parts.append(
            f'<caption class="table-note">Showing {" and ".join(notes)} — '
            "download the file for all of it.</caption>"
        )
    parts.append("</table></div>")
    return "".join(parts)


def render_ceiling(content_type: str, name: str) -> int:
    """The largest artifact of this format the renderer reads WHOLE.

    Read at call time from the module constants, so a test can pin any one
    of them. Order matters only where formats overlap, and they do not: each
    predicate below is exclusive of the others.
    """
    if _is_slides(content_type, name):
        return SLIDES_MAX_BYTES
    if _is_document(content_type, name):
        return DOCUMENT_MAX_BYTES
    if _is_workbook(content_type, name):
        return WORKBOOK_MAX_BYTES
    if _data_flavour(content_type, name) in ("json", "lines"):
        return JSON_MAX_BYTES
    return MAX_RENDER_BYTES


def _is_text_like(content_type: str, name: str) -> bool:
    """Anything the head-preview can honestly show as highlighted source."""
    kind = kind_for(content_type, name)
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return kind in ("md", "code") or base.startswith("text/") or base == "text/html"


def previews_when_large(content_type: str, name: str) -> bool:
    """Whether an artifact over its ceiling is worth offering a `ByteSource`
    for. Callers use this to skip the whole-object fetch AND the empty-bytes
    shortcut, handing the renderer ranged reads instead. Parquet and every
    text format qualify; containers (workbooks, documents, decks) do not —
    over their ceiling they are a card, unfetched."""
    return _is_parquet(content_type, name) or _is_text_like(content_type, name)


def _large_preview(source: ByteSource, content_type: str, name: str) -> RenderedBody | None:
    """A `table` from a fraction of an oversized file, or None for the card.

    Parquet reads the footer and the first row group through `RangedFile`,
    which bounds the total however the reader walks the file. Delimited text
    reads its first `TABLE_HEAD_BYTES`, drops the line the cut fell in, and
    says how much of the file that was — the footer tells parquet its true
    row count; a csv's is unknowable without reading it all, and the caption
    says so rather than guess.
    """
    try:
        if _is_parquet(content_type, name):
            table = _parquet_table(RangedFile(source))
            return None if table is None else RenderedBody(
                html=_view("rendered", table), mode="table"
            )
        if not _is_text_like(content_type, name):
            return None
        head = source.read_range(0, HEAD_PREVIEW_BYTES)
    except PreviewBudgetExceeded:
        return None
    except Exception:  # noqa: BLE001 - a failed ranged read is a download card
        return None
    text = head.decode("utf-8", errors="ignore")
    if len(head) >= HEAD_PREVIEW_BYTES and "\n" in text:
        text = text[: text.rfind("\n")]  # the cut line is not a row
    extent = f"the first {_human_size(len(head))} of a {_human_size(source.size)} file"
    if _delimiter_for(content_type, name) is not None:
        table = _delimited_table(text, name, content_type, extent=extent)
        if table is not None:
            return RenderedBody(html=_view("rendered", table), mode="table")
    if _data_flavour(content_type, name) == "lines":
        partial = _data_body(text, name, "lines", extent=extent)
        if partial is not None:
            return partial
    # Everything else textual: the head as highlighted source, stated as a
    # head. Not markdown, even for a `.md` — a 50 MB markdown file is a
    # transcript or a log, and rendering a cut-off document as prose invents
    # structure the cut destroyed; source is the honest view of a fragment.
    note = (
        f'<p class="doc-note">Showing {escape(extent)} — download the file for all of it.</p>'
    )
    return RenderedBody(html=note + _highlighted_source(text, name), mode="code")


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _is_parquet(content_type: str, name: str) -> bool:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return base in _PARQUET_TYPES or name.lower().endswith(_PARQUET_SUFFIX)


def _parquet_table(data: bytes | RangedFile) -> str | None:
    """Parquet → the shared `table`, or None for the download card.

    Reads the footer first and decodes at most ONE row group, limited to the
    first `_TABLE_MAX_COLUMNS` columns and `_TABLE_MAX_ROWS` rows: the preview
    never inflates more than the guard above admits, and a file with a
    thousand row groups costs the same as a file with one. The true row count
    comes from the footer, so the caption states it exactly where the csv path
    has to count.

    Returns None on anything pyarrow refuses. Untrusted bytes: a bad upload is
    a download card, never a 500 — and never a stack trace shaped like data.
    """
    try:
        pf = pq.ParquetFile(io.BytesIO(data) if isinstance(data, bytes) else data)
        meta = pf.metadata
        total_rows = meta.num_rows
        names = list(pf.schema_arrow.names)
        total_columns = len(names)
        if total_columns == 0:
            return None
        columns = names[:_TABLE_MAX_COLUMNS]
        types = [str(pf.schema_arrow.field(n).type) for n in columns]
        body: list[list[str]] = []
        if total_rows > 0 and meta.num_row_groups > 0:
            if meta.row_group(0).total_byte_size > _PARQUET_MAX_ROW_GROUP_BYTES:
                return None
            batch = pf.read_row_group(0, columns=columns).slice(0, _TABLE_MAX_ROWS)
            pylists = [column.to_pylist() for column in batch.columns]
            body = [
                [_parquet_cell(pylists[c][r]) for c in range(len(columns))]
                for r in range(batch.num_rows)
            ]
    except PreviewBudgetExceeded:
        raise
    except Exception:  # noqa: BLE001 - untrusted bytes: any failure is a download
        return None
    return _data_table(
        columns, body, total_rows=total_rows, total_columns=total_columns, head_titles=types
    )


def _parquet_cell(value: object) -> str:
    """A typed value as the reader would write it. Stable, not locale-aware,
    for the same reason `_sheet_cell` is; null is empty, not "None", because
    a dataset is mostly nulls in some column and the word everywhere is noise.
    Binary shows its size, nested values show as JSON, and every cell is cut
    at `_PARQUET_MAX_CELL_CHARS` so one long text column cannot be the page."""
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, bytes | bytearray | memoryview):
        text = f"{len(value)} bytes"
    elif isinstance(value, dt.datetime | dt.date | dt.time):
        text = value.isoformat()
    elif isinstance(value, list | dict | tuple):
        text = json.dumps(value, default=str, ensure_ascii=False)
    else:
        text = str(value)
    if len(text) > _PARQUET_MAX_CELL_CHARS:
        return text[:_PARQUET_MAX_CELL_CHARS] + "…"
    return text
