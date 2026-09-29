"""`mode="page"` — the one mode `text/html` gains, and nothing else changes.

An HTML artifact is the one type this product stores faithfully and cannot
show: both viewers render it as escaped source. `page` mode renders the
sanitised markup instead and keeps the source behind a control.

The tests below are in the order the risk runs:

  1. the flag is off, so today's behaviour is today's behaviour;
  2. with it on, `text/html` renders as a document and carries the source it
     replaced — *the same bytes* the flag-off path produces, because the
     source view is not new code;
  3. every other artifact type renders byte-identically either way, which is
     the regression that matters most when a shared seam grows a mode;
  4. the failure paths fail to SOURCE, never to raw author markup.
"""

from __future__ import annotations

import pytest
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, guess_lexer
from pygments.util import ClassNotFound

from agentdrive.config import settings
from agentdrive.rendering import render as render_module
from agentdrive.rendering.render import MAX_RENDER_BYTES, RenderedBody, render_body

REPORT = (
    b"<!doctype html><html><head><title>Weekly</title></head><body>"
    b"<h1>Weekly report</h1>"
    b"<p>Throughput rose <strong>12%</strong>.</p>"
    b'<script>window.pwned = true</script>'
    b'<a href="javascript:alert(1)">bad</a>'
    b'<p>See <a href="https://example.test/runs">the runs</a>.</p>'
    b"</body></html>"
)


@pytest.fixture
def pages_on(monkeypatch):
    monkeypatch.setattr(settings, "static_html_rendering_enabled", True)


@pytest.fixture
def pages_off(monkeypatch):
    monkeypatch.setattr(settings, "static_html_rendering_enabled", False)


_SOURCE_PANE = '<div class="doc-view" data-view="source" hidden>'


def rendered_pane(html: str) -> str:
    """Just the rendered half — the strip names both views, so splitting on
    the bare attribute would cut at the Source *button*."""
    assert _SOURCE_PANE in html
    return html.split(_SOURCE_PANE)[0]


# ── The flag ─────────────────────────────────────────────────────────────


def test_the_capability_ships_off():
    """Declared before it is activated, matching the codebase's flag pattern.

    The FIELD default, not the live settings object: a developer with
    `STATIC_HTML_RENDERING_ENABLED=true` in their `.env` is not a failing
    build, and asserting the process's environment here would say otherwise.
    """
    from agentdrive.config import Settings

    assert Settings.model_fields["static_html_rendering_enabled"].default is False


def test_html_renders_as_escaped_source_while_the_flag_is_off(pages_off):
    out = render_body(REPORT, "text/html", "report.html")
    assert out.mode == "code"
    assert "doc-modes" not in out.html
    assert "<h1>" not in out.html


def test_flag_off_output_is_exactly_todays_highlighted_source(pages_off):
    """Pinned against Pygments directly, so a changed formatter argument is a
    failure here rather than a silent difference in what "source" means."""
    text = REPORT.decode()
    expected = highlight(
        text,
        get_lexer_for_filename("report.html", text),
        HtmlFormatter(nowrap=False, cssclass="highlight"),
    )
    assert render_body(REPORT, "text/html", "report.html").html == expected


# ── The page ─────────────────────────────────────────────────────────────


def test_html_renders_as_a_document_when_enabled(pages_on):
    out = render_body(REPORT, "text/html", "report.html")
    assert out.mode == "page"
    assert "<h1>Weekly report</h1>" in out.html
    assert "<strong>12%</strong>" in out.html
    assert '<a href="https://example.test/runs">the runs</a>' in out.html


def test_the_page_carries_no_script_and_no_refused_url(pages_on):
    rendered = rendered_pane(render_body(REPORT, "text/html", "report.html").html)
    assert "<script" not in rendered.lower()
    assert "window.pwned" not in rendered
    assert "javascript:" not in rendered.lower()
    assert ">bad<" in rendered  # the link text survives; only the href went


def test_a_documents_title_element_is_not_printed_into_the_page(pages_on):
    rendered = rendered_pane(render_body(REPORT, "text/html", "report.html").html)
    assert "Weekly</" not in rendered
    assert "<h1>Weekly report</h1>" in rendered


@pytest.mark.parametrize("content_type", ["text/html", "text/html; charset=utf-8"])
def test_the_content_type_parameter_does_not_defeat_page_mode(pages_on, content_type):
    assert render_body(REPORT, content_type, "report.html").mode == "page"


@pytest.mark.parametrize(
    ("content_type", "name", "mode"),
    [
        # Scope is `text/html` and nothing else. XHTML and XML are shown as
        # source (or as an element tree when well-formed), never as a page,
        # and a `.html` filename under a generic type is not enough: the
        # declared type decides.
        ("application/xhtml+xml", "report.xhtml", "code"),
        ("application/xml", "report.xml", "code"),
        ("text/plain", "report.html", "code"),
        ("image/svg+xml", "report.svg", "image"),
    ],
)
def test_only_text_html_becomes_a_page(pages_on, content_type, name, mode):
    assert render_body(REPORT, content_type, name).mode == mode


def test_page_mode_does_not_invent_a_title(pages_on):
    """The adapters fall back to the artifact's name, as they do for code."""
    assert render_body(REPORT, "text/html", "report.html").title is None


def test_the_guessed_lexer_path_is_unchanged(pages_off):
    """`get_lexer_for_filename` fails on an extensionless name, so this is the
    `guess_lexer` branch — the second of the three the extraction moved."""
    text = "def f():\n    return 1\n"
    out = render_body(text.encode(), "text/x-python", "script")
    assert out.mode == "code"
    assert out.html == highlight(
        text, guess_lexer(text), HtmlFormatter(nowrap=False, cssclass="highlight")
    )


def test_the_unguessable_source_path_is_unchanged(pages_off, monkeypatch):
    """The last branch: neither the filename nor the content names a language,
    so the source is escaped into a bare `<pre>` rather than left unrendered."""
    def unguessable(_text):
        raise ClassNotFound("synthetic")

    monkeypatch.setattr(render_module, "guess_lexer", unguessable)
    out = render_body(b"<b>not a language</b>", "text/x-unknown", "mystery")
    assert out.mode == "code"
    assert out.html == "<pre>&lt;b&gt;not a language&lt;/b&gt;</pre>"


# ── The rendered ⇄ source strip ──────────────────────────────────────────


def test_the_page_carries_the_source_it_replaced(monkeypatch):
    """Byte-for-byte the flag-off rendering: the source view is current
    behaviour kept and moved behind a control, not a second implementation."""
    monkeypatch.setattr(settings, "static_html_rendering_enabled", False)
    off = render_body(REPORT, "text/html", "report.html")
    monkeypatch.setattr(settings, "static_html_rendering_enabled", True)
    on = render_body(REPORT, "text/html", "report.html")
    # The source pane is exactly the flag-off document, embedded rather than
    # carried in a second field: today's behaviour kept, moved behind the
    # control. Asserted against `html` because the field it used to duplicate
    # had no reader and is gone.
    assert off.html in on.html


def test_the_mode_strip_is_part_of_the_document(pages_on):
    """Emitted by the renderer so both surfaces get one implementation and
    neither console needs a new control."""
    html = render_body(REPORT, "text/html", "report.html").html
    assert '<nav class="doc-modes" aria-label="View">' in html
    assert '<button type="button" class="doc-mode is-current" data-view="rendered"' in html
    assert 'data-view="source"' in html
    assert html.index('data-view="rendered"') < html.index('data-view="source"')


def test_the_source_pane_starts_hidden_and_the_rendered_one_does_not(pages_on):
    html = render_body(REPORT, "text/html", "report.html").html
    assert '<div class="doc-view" data-view="rendered">' in html
    assert '<div class="doc-view" data-view="source" hidden>' in html


def test_the_toggle_is_keyed_on_an_attribute_no_author_can_emit(pages_on):
    """`data-view` is outside the attribute allowlist and `button` is outside
    the ELEMENT allowlist, so a document cannot forge its own strip.

    Asserted as "no button is ever emitted" rather than "button is dropped
    with its contents": the second is one mechanism that delivered the first,
    and it changed when `button` had to leave `DROPPED_WITH_CONTENTS` to stop
    an unclosed one truncating the document. The property is what matters, and
    it is the allowlist — not the drop set — that provides it."""
    from agentdrive.rendering.sanitize import (
        ALLOWED_ATTRIBUTES,
        ALLOWED_ELEMENTS,
        sanitize_html,
    )

    assert "data-view" not in ALLOWED_ATTRIBUTES
    assert "button" not in ALLOWED_ELEMENTS
    impostor = sanitize_html(
        '<nav class="doc-modes"><button type="button" class="doc-mode" '
        'data-view="source">Source</button></nav>'
    )
    assert "<button" not in impostor
    assert "data-view" not in impostor
    assert 'class="doc-modes"' not in impostor
    forged = (
        b'<div class="doc-modes"><div class="doc-mode" data-view="source">x</div></div>'
        b"<button data-view=\"rendered\">y</button>"
    )
    rendered = rendered_pane(render_body(forged, "text/html", "forged.html").html)
    strip, _, document = rendered.partition('<div class="doc-view" data-view="rendered">')
    # The strip names each view exactly once; the forged `data-view` pair and
    # the forged `<button>` are both gone from the document below it.
    assert strip.count('data-view="source"') == 1
    assert strip.count('data-view="rendered"') == 1
    assert "data-view" not in document
    assert "<button" not in document
    # The author keeps its class TEXT, namespaced — so it can neither bind the
    # toggle (which keys on `data-view`) nor borrow the strip's styling
    # (which keys on the same attribute and on the un-namespaced class).
    from agentdrive.rendering.sanitize import CLASS_PREFIX

    assert f'class="{CLASS_PREFIX}doc-mode"' in document
    assert 'class="doc-mode"' not in document


# ── Only `page` gets a mode strip ────────────────────────────────────────

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _workbook() -> bytes:
    """A real xlsx, built in memory so no binary fixture lives in the tree."""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    sheet = wb.create_sheet("Model")
    for row in ([["Region", "Q1"], ["EMEA", 1200]]):
        sheet.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _artifact_case_ids(cases) -> list[str]:
    """Stable pytest ids: generated archive bytes contain wall-clock metadata."""
    return [case[2] for case in cases]


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx() -> bytes:
    """A real docx, built in memory — the minimum package mammoth will open."""
    import io
    import zipfile

    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Memo</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def _parquet() -> bytes:
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.table({"region": ["EMEA"], "q1": [1200]}), buf)
    return buf.getvalue()


def _pptx() -> bytes:
    import io

    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Memo"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


_OTHER_MODES = [
    (b"# Title\n\ntext\n", "text/markdown", "a.md", "markdown"),
    (b"def f():\n    return 1\n", "text/x-python", "a.py", "code"),
    (b"a,b\n1,2\n", "text/csv", "a.csv", "table"),
    (b"a\tb\n1\t2\n", "text/tab-separated-values", "a.tsv", "table"),
    (b"", "image/png", "a.png", "image"),
    (b"clip", "video/mp4", "a.mp4", "video"),
    (b"%PDF-1.4", "application/pdf", "a.pdf", "pdf"),
    (b"\x00\x01", "application/octet-stream", "a.bin", "download"),
    (b"   \n", "text/plain", "a.txt", "empty"),
    (_workbook(), XLSX, "a.xlsx", "sheet"),
    (_docx(), DOCX, "a.docx", "document"),
    (_parquet(), "application/vnd.apache.parquet", "a.parquet", "table"),
    (b'{"a": {"b": 1}}', "application/json", "a.json", "data"),
    (b'[{"id": 1}, {"id": 2}]', "application/json", "records.json", "table"),
    (b'{"id": 1}\n{"id": 2}\n', "application/x-ndjson", "a.jsonl", "table"),
    (b"a:\n  b: 1\n", "application/yaml", "a.yaml", "data"),
    (b"<r><c>x</c></r>", "application/xml", "a.xml", "data"),
    (_pptx(), "application/vnd.openxmlformats-officedocument.presentationml.presentation",
     "a.pptx", "document"),
]


def test_collection_ids_ignore_generated_payload_bytes():
    first = (b"first archive bytes", XLSX, "a.xlsx", "sheet")
    second = (b"different archive bytes", XLSX, "a.xlsx", "sheet")
    assert _artifact_case_ids([first]) == _artifact_case_ids([second]) == ["a.xlsx"]


@pytest.mark.parametrize(
    ("data", "content_type", "name", "mode"),
    _OTHER_MODES,
    ids=_artifact_case_ids(_OTHER_MODES),
)
def test_only_page_mode_populates_the_source_field(pages_on, data, content_type, name, mode):
    out = render_body(data, content_type, name)
    assert out.mode == mode


def test_the_dataclass_default_keeps_every_existing_construction_valid():
    assert "doc-mode" not in RenderedBody(html="<p>x</p>", mode="markdown").html


# ── The regression that matters most ────────────────────────────────────

_CORPUS = [
    *[(data, content_type, name) for data, content_type, name, _mode in _OTHER_MODES],
    (b"# Heading\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", "text/markdown", "t.md"),
    (b'{"a": 1}', "application/json", "a.json"),
    (b"\xff\xfe\x00bad", "text/markdown", "b.md"),
    (b"a" * (MAX_RENDER_BYTES + 1), "text/markdown", "big.md"),
    (b"<p>markup in markdown</p>\n", "text/markdown", "raw.md"),
    (b"<h1>x</h1>", "image/svg+xml", "a.svg"),
    (b"<note>x</note>", "application/xml", "a.xml"),
]


@pytest.mark.parametrize(
    ("data", "content_type", "name"),
    _CORPUS,
    ids=_artifact_case_ids(_CORPUS),
)
def test_every_other_type_renders_identically_with_the_flag_either_way(
    monkeypatch, data, content_type, name
):
    """Adding a mode to a shared seam is exactly when the other modes drift.

    What this proves and what it does not, stated plainly because the two are
    easy to confuse: it proves the flag is inert for everything that is not
    `text/html` — that no SECOND flag read appeared somewhere in the seam. It
    cannot prove byte-identity with `main`, because it compares the branch to
    itself. That claim rests on `tests/test_public_render.py`, which pins each
    mode's output directly, plus the three source-path pins below, which cover
    the one function this change actually moved.
    """
    monkeypatch.setattr(settings, "static_html_rendering_enabled", False)
    off = render_body(data, content_type, name, size_bytes=len(data))
    monkeypatch.setattr(settings, "static_html_rendering_enabled", True)
    on = render_body(data, content_type, name, size_bytes=len(data))
    assert off == on


def test_the_pdf_print_button_still_follows_its_argument(pages_on):
    assert 'id="pv-print"' not in render_body(b"%PDF", "application/pdf", "a.pdf").html
    assert (
        'id="pv-print"'
        in render_body(b"%PDF", "application/pdf", "a.pdf", print_button=True).html
    )


# ── Failure paths fail to source ────────────────────────────────────────


def test_oversized_html_is_a_download_card_with_no_strip_and_no_source(pages_on):
    out = render_body(b"<p>x</p>", "text/html", "big.html", size_bytes=MAX_RENDER_BYTES + 1)
    assert out.mode == "download"
    assert "doc-modes" not in out.html


def test_undecodable_html_is_a_download_card(pages_on):
    out = render_body(b"\xff\xfe\x00bad", "text/html", "b.html")
    assert out.mode == "download"


def test_an_empty_html_file_is_still_an_empty_card(pages_on):
    out = render_body(b"   \n", "text/html", "blank.html")
    assert out.mode == "empty"


def test_a_document_with_a_stylesheet_link_is_not_an_empty_card(pages_on):
    """The shape a model actually emits — `<head>` with a `<link>` in it —
    reaching the empty card is the worst outcome this mode has: it is wrong,
    it says the file has no content, and it takes the source toggle with it."""
    out = render_body(
        b"<!DOCTYPE html><html><head><title>Q3</title>"
        b"<link rel='stylesheet' href='https://fonts.example.test/i.css'>"
        b"</head><body><h1>Q3 report</h1><p>Revenue grew.</p></body></html>",
        "text/html",
        "q3.html",
    )
    assert out.mode == "page"
    assert "<h1>Q3 report</h1>" in out.html


def test_html_that_sanitises_to_nothing_is_an_empty_card_not_a_blank_page(pages_on):
    out = render_body(b"<script>window.pwned = true</script>", "text/html", "all.html")
    assert out.mode == "empty"
    assert "window.pwned" not in out.html


def test_a_sanitiser_failure_falls_back_to_source_never_to_raw_markup(pages_on, monkeypatch):
    def explode(_markup: str) -> str:
        raise RuntimeError("synthetic sanitiser failure")

    monkeypatch.setattr(render_module, "sanitize_html", explode)
    out = render_body(REPORT, "text/html", "report.html")
    assert out.mode == "code"
    assert "<h1>Weekly report</h1>" not in out.html  # nothing rendered as markup
    assert "&lt;" in out.html  # …and the author's angle brackets escaped
    assert "Weekly report" in out.html  # …with the document itself still readable


def test_a_sanitiser_failure_produces_exactly_the_flag_off_document(monkeypatch):
    def explode(_markup: str) -> str:
        raise ValueError("synthetic sanitiser failure")

    monkeypatch.setattr(settings, "static_html_rendering_enabled", False)
    expected = render_body(REPORT, "text/html", "report.html")
    monkeypatch.setattr(settings, "static_html_rendering_enabled", True)
    monkeypatch.setattr(render_module, "sanitize_html", explode)
    assert render_body(REPORT, "text/html", "report.html") == expected
