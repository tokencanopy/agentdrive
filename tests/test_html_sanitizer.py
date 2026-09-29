"""The allowlist sanitiser — the pure function `mode="page"` rests on.

Artifact HTML is written by an agent and rendered on our own origin, so this
module is the first of the three independent layers that keep it inert (the
CSP and `innerHTML`'s refusal to run scripts are the other two). Its contract
is narrow and absolute:

  * **emit only what the allowlist permits** — every element, attribute and
    URL scheme is named, and anything unnamed is dropped by construction
    rather than by pattern-matching, so a novel evasion shape has nothing to
    evade;
  * **never emit unbalanced markup** — the output is the sanitiser's own tags,
    not a filtered copy of the author's byte stream;
  * **preserve text** — an element we do not know is dropped, its words are
    not, except for the handful of elements whose contents are code.

The evasion cases below are the classic ones; they are regression tests, not
illustrations. Every fixture here is synthetic (`example.test`/`.invalid`).
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentdrive.rendering.sanitize import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_ELEMENTS,
    CLASS_PREFIX,
    DROPPED_VOID,
    DROPPED_WITH_CONTENTS,
    MAX_OPEN_ELEMENTS,
    VOID_ELEMENTS,
    sanitize_html,
)


def _url_is_permitted(tag: str, attribute: str, value: str) -> bool:
    """The URL rule, restated independently of the implementation.

    Deliberately not `sanitize._safe_url`: a checker that calls the code under
    test agrees with it by construction, including about its bugs. Written
    from the design's §5.2 rule — http/https/mailto, `data:` for image sources,
    plus a same-document fragment — and applied to what a *browser* would see,
    which is the value with tab/CR/LF removed.
    """
    cleaned = "".join(ch for ch in value if ch not in "\t\r\n" and ord(ch) > 0x1F).strip()
    if cleaned.startswith("#"):
        return True
    scheme, sep, _rest = cleaned.partition(":")
    if not sep or "/" in scheme or "?" in scheme or "#" in scheme:
        return False  # relative, protocol-relative, or empty: refused
    scheme = scheme.lower()
    if scheme in ("http", "https"):
        return True
    if scheme == "mailto":
        return attribute == "href"
    if scheme == "data":
        return (
            attribute == "src"
            and tag == "img"
            and cleaned.lower().startswith("data:image/")
            and not cleaned.lower().startswith("data:image/svg+xml")
        )
    return False


class _Balance(HTMLParser):
    """Re-parses sanitiser output and records structure violations."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_ELEMENTS:
            self.problems.append(f"emitted disallowed element {tag!r}")
        for name, value in attrs:
            if name not in ALLOWED_ATTRIBUTES:
                self.problems.append(f"emitted disallowed attribute {name!r}")
            if name in ("href", "src") and not _url_is_permitted(tag, name, value or ""):
                self.problems.append(f"emitted disallowed {name} {value!r}")
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            self.problems.append(f"end tag {tag!r} with nothing open")
            return
        if self.stack[-1] != tag:
            self.problems.append(f"end tag {tag!r} closed {self.stack[-1]!r}")
            return
        self.stack.pop()


def assert_well_formed(markup: str) -> None:
    """Balanced, allowlisted, and closed in the order it was opened."""
    checker = _Balance()
    checker.feed(markup)
    checker.close()
    assert not checker.problems, checker.problems
    assert not checker.stack, f"unclosed elements: {checker.stack}"


def clean(markup: str) -> str:
    out = sanitize_html(markup)
    assert_well_formed(out)
    return out


# ── The allowlist is the design's, verbatim ──────────────────────────────


def test_the_allowlist_matches_the_design():
    """§5.2's lists, pinned. Widening one is a security decision, not a tidy-up."""
    elements = {
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "br", "hr",
        "ul", "ol", "li", "dl", "dt", "dd",
        "table", "thead", "tbody", "tr", "th", "td", "caption",
        "a", "img", "code", "pre", "blockquote",
        "strong", "em", "b", "i", "s", "del", "ins", "sub", "sup", "small",
        "span", "div", "figure", "figcaption",
        "section", "article", "header", "footer", "main", "nav", "aside",
    }
    attributes = {
        "href", "src", "alt", "title", "colspan", "rowspan",
        "id", "class", "width", "height", "lang", "dir",
    }
    dropped = {
        "script", "style", "iframe", "object", "embed", "link", "base",
        "form", "input", "button", "template", "svg", "math",
    }
    assert set(ALLOWED_ELEMENTS) == elements
    assert set(ALLOWED_ATTRIBUTES) == attributes
    # Every §5.2 drop is unemittable. Stated as "never in the output" rather
    # than "in one of the two drop sets", because there are now THREE ways an
    # element fails to be emitted and only the first two are drop sets:
    #
    #   * DROPPED_WITH_CONTENTS — raw text and foreign content, where
    #     swallowing to the end tag is what a browser does too;
    #   * DROPPED_VOID — no end tag exists to wait for;
    #   * simply absent from ALLOWED_ELEMENTS — the tag goes, children stay.
    #
    # `form`, `button` and `object` moved from the first to the third when an
    # unclosed one turned out to truncate the document. Nothing about what
    # reaches a reader changed: the allowlist alone guarantees it. Pinning the
    # mechanism instead of the property is what made that fix look like a
    # policy change.
    assert dropped.isdisjoint(ALLOWED_ELEMENTS)
    # An element cannot be both emitted and dropped. One line, so an allowlist
    # edit that collides reports itself instead of silently winning.
    assert set(ALLOWED_ELEMENTS).isdisjoint(DROPPED_WITH_CONTENTS)
    assert set(ALLOWED_ELEMENTS).isdisjoint(DROPPED_VOID)
    assert set(DROPPED_WITH_CONTENTS).isdisjoint(DROPPED_VOID)


# ── Dropped entirely, contents and all ───────────────────────────────────


@pytest.mark.parametrize("element", sorted(DROPPED_WITH_CONTENTS))
def test_dropped_elements_take_their_contents_with_them(element):
    out = clean(f"<p>before</p><{element}>SECRET PAYLOAD</{element}><p>after</p>")
    assert "SECRET PAYLOAD" not in out
    assert f"<{element}" not in out
    assert "<p>before</p>" in out
    assert "<p>after</p>" in out


@pytest.mark.parametrize("element", sorted(DROPPED_VOID))
def test_a_void_dropped_element_does_not_swallow_what_follows(element):
    """`link`, `base`, `input` and `embed` have NO end tag — not an optional
    one. Waiting for a `</link>` that HTML forbids means everything after it
    is discarded, and the fixture that supplies one is testing itself."""
    out = clean(f'<p>before</p><{element} href="x">after the void<p>tail</p>')
    assert "<p>before</p>" in out
    assert "after the void" in out
    assert "<p>tail</p>" in out
    assert f"<{element}" not in out


def test_the_document_shape_a_model_actually_emits_survives():
    """A stylesheet `<link>` in `<head>` is in nearly every complete HTML
    document an agent writes. Losing the body to it is the modal case, not an
    edge case — and it lands the reader on a card claiming the file is empty."""
    out = clean(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Q3 report</title>"
        "<link rel='stylesheet' href='https://fonts.example.test/css?family=Inter'>"
        "<style>body{color:#000}</style>"
        "</head><body><h1>Q3 report</h1><p>Revenue grew 12%.</p>"
        "<table><tr><td>EMEA</td><td>120</td></tr></table>"
        "</body></html>"
    )
    assert "<h1>Q3 report</h1>" in out
    assert "<p>Revenue grew 12%.</p>" in out
    assert "<td>EMEA</td>" in out
    assert "Q3 report</" in out  # the h1, not the <title>
    assert out.count("Q3 report") == 1


def test_an_artifact_cannot_choose_where_the_render_stops():
    """A truncation an author controls is a content-integrity primitive: the
    bytes and the source view carry text the rendered view silently drops."""
    out = clean(
        "<h1>Invoice</h1><p>Amount due: <strong>$10.00</strong></p>"
        "<link rel='icon' href='/f.ico'>"
        "<p>CORRECTION: the real amount due is $10,000.00</p>"
    )
    assert "CORRECTION" in out


@pytest.mark.parametrize("element", ["script", "style", "template", "iframe", "title"])
def test_an_xhtml_self_closed_drop_still_takes_its_contents(element):
    """`<script/>` does not close a script in HTML — a browser reads on to
    `</script>`. Treating the slash as a close turns script text into rendered
    markup, which is a smuggling channel even when nothing executes."""
    out = clean(f"<{element}/>window.PWN=1<b>smuggled</b></{element}><p>after</p>")
    assert "window.PWN" not in out
    assert "smuggled" not in out
    assert "<p>after</p>" in out


@pytest.mark.parametrize("element", ["svg", "math"])
def test_a_self_closed_foreign_element_does_not_swallow_the_document(element):
    """Foreign content is the one place HTML honours the self-closing slash,
    so `<svg/>` is an empty element and the document continues."""
    out = clean(f"<p>before</p><{element}/><p>after</p>")
    assert "<p>before</p>" in out
    assert "<p>after</p>" in out


def test_script_body_never_survives_in_any_form():
    out = clean("<script>window.pwned = true</script>")
    assert out.strip() == ""
    assert "window.pwned" not in out


def test_nested_drops_do_not_leak_when_they_close():
    """A dropped element inside a dropped element must not re-open output."""
    out = clean("<style><script>x</script>p{color:red}</style><p>kept</p>")
    assert out == "<p>kept</p>"


# ── Attributes ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("element", sorted(ALLOWED_ELEMENTS))
def test_event_handlers_are_dropped_from_every_permitted_element(element):
    out = clean(f'<{element} onclick="steal()" onerror="steal()">text</{element}>')
    assert "onclick" not in out.lower()
    assert "onerror" not in out.lower()
    assert "steal()" not in out


@pytest.mark.parametrize(
    ("markup", "kept", "dropped"),
    [
        ('<td colspan="2" style="color:red">c</td>', "colspan", "style"),
        ('<div class="report" data-x="1">c</div>', "class", "data-x"),
        ('<p id="top" contenteditable="true">c</p>', "id", "contenteditable"),
        ('<img alt="chart" srcset="https://a.example.test/x 2x">', "alt", "srcset"),
        ('<a title="t" target="_blank">c</a>', "title", "target"),
        ('<span lang="en" hidden="">c</span>', "lang", "hidden"),
    ],
)
def test_only_allowlisted_attributes_survive(markup, kept, dropped):
    out = clean(markup)
    assert kept in out
    assert dropped not in out.lower()


def test_a_style_attribute_never_survives():
    """`style-src 'self'` refuses it anyway; the sanitiser must not rely on that."""
    out = clean('<div style="position:fixed;top:0;left:0;width:100vw">x</div>')
    assert "style" not in out.lower()
    assert "100vw" not in out


# ── URLs ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "  javascript:alert(1)",
        "jav\tascript:alert(1)",
        "jav&#x09;ascript:alert(1)",
        "&#106;avascript:alert(1)",
        "java\nscript:alert(1)",
        "\x00javascript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "data:image/svg+xml,<svg onload=alert(1)>",
        "file:///etc/passwd",
        "blob:https://viewer.example.test/abcd",
        "//evil.example.test/x",
        "/absolute/path",
        "relative/path",
    ],
)
def test_refused_href_schemes_lose_the_attribute_but_keep_the_text(href):
    out = clean(f'<a href="{href}">click me</a>')
    assert "href" not in out.lower()
    assert "click me" in out
    assert "javascript" not in out.lower()


@pytest.mark.parametrize(
    "href",
    [
        "https://example.test/report",
        "http://example.test/report",
        "mailto:someone@example.test",
    ],
)
def test_permitted_href_schemes_survive(href):
    out = clean(f'<a href="{href}">link</a>')
    assert f'href="{href}"' in out


def test_a_same_document_fragment_survives_namespaced():
    """Permitted, because it can neither issue a request nor leave the
    document — and rewritten in step with the ids it targets."""
    out = clean('<a href="#section-2">link</a>')
    assert f'href="#{CLASS_PREFIX}section-2"' in out


@pytest.mark.parametrize(
    "src",
    [
        "https://images.example.test/chart.png",
        "http://images.example.test/chart.png",
        "data:image/png;base64,iVBORw0KGgo=",
        # A media type is case-insensitive; a legitimate image must not be
        # dropped for spelling its scheme in capitals.
        "DATA:image/PNG;base64,iVBORw0KGgo=",
    ],
)
def test_permitted_image_sources_survive(src):
    out = clean(f'<img src="{src}" alt="chart">')
    assert f'src="{src}"' in out


@pytest.mark.parametrize(
    "src",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "data:application/javascript,alert(1)",
        "data:,plain",
        # A second markup language with its own surface. The element form is
        # dropped with its contents; the URL form is the same capability —
        # in whatever case it is written, because a media type is
        # case-insensitive to the browser that resolves it.
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
        "data:image/SVG+XML;base64,PHN2Zz48L3N2Zz4=",
        "DATA:IMAGE/SVG+XML;base64,PHN2Zz48L3N2Zz4=",
    ],
)
def test_refused_image_sources_lose_the_attribute(src):
    out = clean(f'<img src="{src}" alt="chart">')
    assert "src" not in out.lower()
    assert 'alt="chart"' in out


def test_a_data_url_is_refused_on_href_even_when_it_names_an_image():
    """`data:` is permitted for image sources only — never for navigation."""
    out = clean('<a href="data:image/png;base64,iVBORw0KGgo=">x</a>')
    assert "href" not in out.lower()


# ── Classic evasion shapes ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "markup",
    [
        "<scr<script>ipt>alert(1)</script>",
        "<script/src=https://evil.example.test/x.js></script>",
        "<SCRIPT>alert(1)</SCRIPT>",
        "<scri\x00pt>alert(1)</scri\x00pt>",
        "<IMG SRC=x onerror=alert(1)>",
        "<IMG SRC=`x` ONERROR=alert(1)>",
        '<img src=x onerror="alert(1)">',
        '<a hr&#101;f="javascript:alert(1)">t</a>',
        '<a HREF="javascript:alert(1)">t</a>',
        "<svg><script>alert(1)</script></svg>",
        '<math><mtext><script>alert(1)</script></mtext></math>',
        '<iframe src="javascript:alert(1)"></iframe>',
        '<form action="https://evil.example.test"><input name="p"></form>',
        '<object data="data:text/html,<script>alert(1)</script>"></object>',
        '<base href="https://evil.example.test/">',
        '<meta http-equiv="refresh" content="0;url=https://evil.example.test">',
        "<template><script>alert(1)</script></template>",
        '<body onload="alert(1)">t</body>',
        '<div><!--<script>alert(1)</script>--></div>',
    ],
)
def test_classic_evasion_shapes_produce_no_executable_markup(markup):
    """`clean` re-parses the output and fails on any element or attribute
    outside the allowlist, so the assertions below are the belt to that
    braces. Note the deliberate absence of `"alert(1)" not in out`: a payload
    that survives as *escaped text* is the sanitiser working, not failing."""
    out = clean(markup)
    lowered = out.lower()
    assert "<script" not in lowered
    assert "onerror" not in lowered
    assert "onload" not in lowered
    assert "javascript:" not in lowered
    assert "evil.example.test" not in lowered


@pytest.mark.parametrize(
    "markup",
    [
        '<p>kept</p><a href="unterminated attribute',
        "<p>kept</p><a href='unterminated attribute",
        "<p>kept</p><!-- unterminated comment",
        "<p>kept</p><script>unterminated script",
    ],
)
def test_an_unterminated_construct_ends_the_document_as_a_browser_would(markup):
    """Not a defect: a browser consumes the rest of the input the same way for
    all three. Pinned so the behaviour is a decision rather than a surprise —
    the artifact's bytes and its source view still carry the whole text."""
    out = clean(markup + "<p>after</p>")
    assert "<p>kept</p>" in out
    assert "<p>after</p>" not in out


def test_an_unclosed_dropped_element_swallows_the_rest_rather_than_leaking_it():
    """Fail closed: a `<script>` nobody closed must not spill its body as text."""
    out = clean("<p>kept</p><script>window.pwned = true")
    assert "window.pwned" not in out
    assert "<p>kept</p>" in out


# ── Unknown elements: dropped, but their text is kept ────────────────────


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("<marquee>hello</marquee>", "hello"),
        ("<custom-element>hello</custom-element>", "hello"),
        ("<html><body><p>hi</p></body></html>", "<p>hi</p>"),
        ("<blink>a</blink><p>b</p>", "a<p>b</p>"),
        ("<font color=red>tinted</font>", "tinted"),
        ("<center><p>c</p></center>", "<p>c</p>"),
    ],
)
def test_unknown_elements_are_dropped_but_their_text_is_preserved(markup, expected):
    assert clean(markup) == expected


def test_a_documents_title_is_metadata_and_is_not_printed_into_the_body():
    """`<title>` is the tab's name, not the document's first line.

    Not in §5.2's drop-with-contents list, and it should be: every HTML
    artifact an agent writes has one, and preserving its text the way an
    unknown element's text is preserved prints the title as a stray sentence
    above the content it names.
    """
    out = clean("<html><head><title>Q3 Report</title></head><body><h1>Q3</h1></body></html>")
    assert "Q3 Report" not in out
    assert "<h1>Q3</h1>" in out


# ── Structure ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "markup",
    [
        "<div><p>unclosed",
        "<b><i>crossed</b></i>",
        "</p></div>stray end tags",
        "<ul><li>one<li>two</ul>",
        "<table><tr><td>cell",
        "<p>a<div>b</p>c</div>",
        "<div" * 200,
        "<div>" * 200,
    ],
)
def test_output_is_always_balanced(markup):
    clean(markup)  # the helper asserts well-formedness


def test_unclosed_elements_are_closed_by_the_sanitiser():
    assert clean("<div><p>text") == "<div><p>text</p></div>"


def test_stray_end_tags_are_ignored():
    assert clean("</p>text</div>") == "text"


def test_void_elements_are_emitted_self_closed_and_never_left_open():
    out = clean("<p>a<br>b<hr></p>")
    assert "<br />" in out or "<br/>" in out
    assert "</br>" not in out


def test_text_is_escaped_not_passed_through():
    out = clean("<p>1 < 2 && 3 > 2</p>")
    assert "&lt;" in out
    assert "&amp;" in out
    assert out.count("<") == out.count(">") == 2  # only <p> and </p>


def test_attribute_values_are_escaped():
    """A value that closes its own quote would grow a new attribute."""
    out = clean('<p title=\'a" onclick="steal()\'>t</p>')
    assert out == '<p title="a&quot; onclick=&quot;steal()">t</p>'


def test_comments_are_dropped():
    assert clean("<!-- secret --><p>a</p><!--[if IE]><script>x</script><![endif]-->") == "<p>a</p>"


def test_doctype_and_processing_instructions_are_dropped():
    out = clean("<!doctype html><?xml version='1.0'?><p>a</p>")
    assert out == "<p>a</p>"


def test_real_report_markup_survives_intact():
    """The shape agents actually write: headings, a table, links, code."""
    out = clean(
        "<html><head><title>Weekly</title></head><body>"
        "<h1>Weekly report</h1>"
        "<p>Throughput rose <strong>12%</strong>.</p>"
        '<table><thead><tr><th>Day</th><th colspan="2">Runs</th></tr></thead>'
        "<tbody><tr><td>Mon</td><td>4</td><td>1</td></tr></tbody></table>"
        '<p>See <a href="https://example.test/runs">the runs</a>.</p>'
        "<pre><code>uv run pytest</code></pre>"
        "</body></html>"
    )
    assert "<h1>Weekly report</h1>" in out
    assert "<strong>12%</strong>" in out
    assert '<th colspan="2">Runs</th>' in out
    assert '<a href="https://example.test/runs">the runs</a>' in out
    assert "<pre><code>uv run pytest</code></pre>" in out
    assert "Weekly report" in out


def test_sanitising_is_idempotent():
    source = (
        "<h1>t</h1><script>x</script><marquee>y</marquee>"
        '<a href="javascript:alert(1)">z</a><img src="https://a.example.test/i.png">'
    )
    once = clean(source)
    assert clean(once) == once


def test_empty_input_produces_empty_output():
    assert sanitize_html("") == ""
    assert sanitize_html("   \n  ").strip() == ""


def test_a_document_of_only_dropped_elements_produces_nothing():
    out = clean("<script>a</script><style>b</style><iframe src='https://x.invalid'></iframe>")
    assert out.strip() == ""


# ── The property, over inputs nobody thought to write ────────────────────

_FRAGMENTS = [
    "<script>", "</script>", "<p>", "</p>", "<div>", "</div>", "<a href=",
    '"javascript:alert(1)"', "<img src=x onerror=alert(1)>", "<!--", "-->",
    "<svg>", "</svg>", "<style>", "</style>", "<template>", "</template>",
    "<", ">", "&", '"', "'", "/", "=", "\x00", "\t", "\n", "&#x09;", "&lt;",
    "<br>", "<hr/>", "<title>", "</title>", "<marquee>", "</marquee>",
    "onclick", "data:", "text", "<b><i>", "</b></i>", "<form>", "<input>",
    # The void drops. Their end tags do not exist, so a fragment list without
    # them cannot discover an implementation waiting for one.
    "<link rel=stylesheet href=x>", "<base href=x>", "<embed src=x>",
    "<script/>", "<svg/>", "<style/>",
]

# One closer for every construct that swallows what follows it, repeated past
# the deepest nesting the strategy can build, so nothing is left pending.
#
# The prefix is not decoration. Each piece corresponds to a construct that
# legitimately consumes the rest of a document in a real browser too, so a
# sentinel behind one is correctly gone rather than wrongly dropped:
#   `'">'">`  terminates a half-written attribute and the tag holding it. Both
#             quote characters, twice: `href="x` needs a double quote CLOSED,
#             `href='x` a single one, and `href=` needs one OPENED first — and
#             the suffix cannot know which of the three it is looking at.
#   `-->`     closes an unterminated comment
# Every one of those was found by this property rather than reasoned about in
# advance, which is most of the argument for having it.
_CLOSE_EVERYTHING = '\'">\'">-->' + "".join(
    f"</{tag}>" for tag in sorted(DROPPED_WITH_CONTENTS)
) * 41


@settings(max_examples=1_500, deadline=None)
@given(st.lists(st.sampled_from(_FRAGMENTS), max_size=40))
def test_no_assembly_of_markup_fragments_escapes_the_allowlist(fragments):
    """Fuzz over the shapes an attacker builds from: the invariants hold for
    every arrangement, not just the ones a test author imagined."""
    assembled = "".join(fragments)
    out = sanitize_html(assembled)
    # `assert_well_formed` carries the safety invariants: every element and
    # attribute allowlisted, every URL permitted, every tag closed. String
    # assertions about the payload are deliberately absent — a payload that
    # survives as escaped TEXT is the sanitiser working, and asserting on the
    # text would only measure which fragments the fuzzer happened to join.
    assert_well_formed(out)
    assert "<script" not in out.lower()

    # The LIVENESS invariant, and it is the one that catches a sanitiser which
    # is safe by discarding too much: close every swallowing element, then a
    # sentence must still come through. Without this, "drop everything" passes
    # every other assertion in this module.
    live = sanitize_html(assembled + _CLOSE_EVERYTHING + "<p>SENTINEL</p>")
    assert "SENTINEL" in live, assembled


# ── Bounded work on hostile shapes ───────────────────────────────────────


def test_unmatched_end_tags_do_not_take_quadratic_time():
    """`</i>` never matches an open `<b>`, so every one of them used to scan
    the whole open-element stack. On a 2 MiB artifact — which the renderer
    admits — that is minutes of a BLOCKED event loop, reachable by anyone
    holding an anonymous share link.

    Timed as a ratio rather than an absolute so the test says "not quadratic"
    on any machine: 4x the input must not cost anything like 16x the time.
    """
    import time

    def elapsed(n: int) -> float:
        markup = "<b>" * n + "</i>" * n
        start = time.perf_counter()
        sanitize_html(markup)
        return time.perf_counter() - start

    small = max(elapsed(2_000), 0.001)
    large = elapsed(8_000)
    assert large < small * 8, f"{small=:.4f} {large=:.4f} — looks quadratic"
    assert large < 1.0, f"{large=:.4f}s for 48 KB of markup"


def test_a_two_mib_pathological_document_is_sanitised_promptly():
    """The real ceiling, not a scaled-down stand-in: `MAX_RENDER_BYTES` worth
    of the worst shape must finish, stay well-formed, and cost no more than
    its size.

    Anchored to a measurement taken on the SAME machine, like its quadratic
    sibling above, rather than to a fixed number of seconds. The absolute
    budget this used to carry was tuned on a developer laptop (~0.7s) and read
    as a latency SLO; the identical linear work takes 6-7s on a shared CI
    runner, so the test failed for the hardware it ran on rather than for
    anything about the sanitiser. The property worth defending is that 2 MiB
    costs roughly 8x what 256 KiB costs -- true of a linear sanitiser on any
    machine, and wildly false of a quadratic one, which would be nearer 64x.
    """
    import time

    from agentdrive.rendering.render import MAX_RENDER_BYTES

    unit = "<b></i>"

    def sanitised(byte_budget: int) -> tuple[float, str]:
        markup = unit * (byte_budget // len(unit))
        start = time.perf_counter()
        out = sanitize_html(markup)
        return time.perf_counter() - start, out

    eighth, _ = sanitised(MAX_RENDER_BYTES // 8)
    full, out = sanitised(MAX_RENDER_BYTES)

    # 8x is linear, 64x is quadratic; 24x leaves 3x for runner noise and is
    # still unreachable by a blow-up.
    assert full < max(eighth, 0.001) * 24, (
        f"{eighth=:.3f}s for an eighth, {full=:.3f}s for all of it "
        f"-- superlinear in the size of the document"
    )
    # Backstop only, for hardware too slow for the ratio to say anything
    # useful. Not a latency target.
    assert full < 60.0, f"{full:.2f}s to sanitise {MAX_RENDER_BYTES} bytes"
    assert_well_formed(out)


def test_nesting_is_capped_and_the_text_still_survives():
    """A million-deep document is a layout bomb for the reader's browser and
    an output amplifier for ours. Past the cap the ELEMENT is dropped the way
    an unknown one is — the tag goes, the words stay."""
    depth = MAX_OPEN_ELEMENTS + 50
    out = clean("<div>" * depth + "SENTINEL" + "</div>" * depth)
    assert "SENTINEL" in out
    assert out.count("<div>") == MAX_OPEN_ELEMENTS


def test_output_stays_within_a_small_multiple_of_its_input():
    """Every unclosed element costs a closing tag we emit ourselves."""
    markup = "<div>" * 5_000
    out = sanitize_html(markup)
    assert len(out) < len(markup) * 3
    assert_well_formed(out)


def test_duplicate_attributes_cannot_override_a_vetted_one():
    """Browsers take the first occurrence; so must we, or a second `href`
    reintroduces exactly what the first one was checked for."""
    out = clean('<a href="#ok" href="javascript:alert(1)">t</a>')
    assert out == f'<a href="#{CLASS_PREFIX}ok">t</a>'


# ── Artifact strings cannot collide with product strings ─────────────────

# A sample of the class names the product's own stylesheet styles. An artifact
# that keeps any of these verbatim renders as that piece of chrome.
_PRODUCT_CLASSES = [
    "untrusted-band", "untrusted-wrap", "doc-modes", "doc-mode", "doc-view",
    "btn", "doc-head", "doc-path", "doc-meta", "viewer-bar", "bar-btn",
    "kind", "download-card", "machine-strip", "blocked-remote", "highlight",
]


@pytest.mark.parametrize("name", _PRODUCT_CLASSES)
def test_an_artifact_cannot_wear_the_products_own_class(name):
    """The author's CSS is dropped (§5.3), so a verbatim `class` buys an
    artifact exactly one thing: OUR styling. A forged `untrusted-band` renders
    identical to the genuine one — and that band is the only mitigation §6.3
    names against the impersonation it warns about."""
    out = clean(f'<div class="{name}">Verified by AgentDrive</div>')
    assert f'class="{name}"' not in out
    assert f'class="{CLASS_PREFIX}{name}"' in out
    assert "Verified by AgentDrive" in out


def test_every_class_token_is_namespaced_not_just_the_first():
    out = clean('<div class="btn  doc-head\tuntrusted-band">x</div>')
    expected = " ".join(
        CLASS_PREFIX + token for token in ("btn", "doc-head", "untrusted-band")
    )
    assert out == f'<div class="{expected}">x</div>'


def test_an_artifact_cannot_wear_a_product_element_id():
    out = clean('<p id="shell-title">forged</p>')
    assert 'id="shell-title"' not in out
    assert f'id="{CLASS_PREFIX}shell-title"' in out


def test_namespacing_keeps_a_table_of_contents_working():
    """The id and the fragment that targets it move together, or the one link
    shape the fragment allowance exists for stops resolving."""
    out = clean('<p><a href="#notes">Jump</a></p><h2 id="notes">Notes</h2>')
    assert f'href="#{CLASS_PREFIX}notes"' in out
    assert f'id="{CLASS_PREFIX}notes"' in out
    assert out.count(CLASS_PREFIX) == 2


def test_a_bare_fragment_survives_namespacing():
    assert clean('<a href="#">top</a>') == '<a href="#">top</a>'


def test_an_empty_class_does_not_become_a_bare_prefix():
    out = clean('<div class="">x</div>')
    assert f'"{CLASS_PREFIX}"' not in out


def test_namespacing_deliberately_applies_again_on_re_sanitising():
    """The pass is NOT idempotent on these two attributes, and must not be: an
    artifact that pre-writes `ua-btn` would otherwise inherit the prefix as
    camouflage and land back on the product's own class."""
    once = clean('<div class="btn" id="notes">x</div>')
    assert once.count(CLASS_PREFIX) == 2
    assert clean(once).count(CLASS_PREFIX) == 4


def test_a_preemptive_prefix_does_not_reach_a_product_class():
    out = clean(f'<div class="{CLASS_PREFIX}btn">x</div>')
    assert f'class="{CLASS_PREFIX}{CLASS_PREFIX}btn"' in out


# ── Review findings: a normal document must survive ──────────────────────────
#
# Every case below was found by asking "does an ordinary document come out
# whole?" rather than "did the attack get through?". The suite above asks the
# second question thoroughly; these three bugs were all invisible to it.


@pytest.mark.parametrize("tag", ["form", "button", "object"])
def test_an_unclosed_non_raw_text_element_does_not_eat_the_document(tag):
    """`<form>` is not `<style>`: a browser renders straight past an unclosed one.

    These three sat in `DROPPED_WITH_CONTENTS`, so a missing end tag swallowed
    everything after them — a document ending at the first `<form>` an agent
    forgot to close, and a primitive for making the rendered view disagree
    with the source view and the bytes about what the artifact says.
    """
    out = sanitize_html(f"<p>BEFORE</p><{tag}><p>AFTER</p>")
    assert "BEFORE" in out
    assert "AFTER" in out, f"content after an unclosed <{tag}> was dropped: {out!r}"


@pytest.mark.parametrize("tag", ["script", "style", "title"])
def test_an_unclosed_raw_text_element_still_swallows(tag):
    """The counterpart: for raw text, swallowing is what a browser does too."""
    out = sanitize_html(f"<p>BEFORE</p><{tag}>x<p>AFTER</p>")
    assert "BEFORE" in out
    assert "AFTER" not in out


def test_a_form_keeps_nothing_that_could_collect_anything():
    """Loosening the drop must not loosen the safety it was there for."""
    out = sanitize_html(
        '<form action="https://evil.invalid" method="post">'
        '<input name="password" type="password"><button>Sign in</button>'
        "</form><p>AFTER</p>"
    )
    assert "<form" not in out and "evil.invalid" not in out
    assert "<input" not in out and "<button" not in out
    assert "AFTER" in out


def test_a_refused_image_is_stated_rather_than_silently_emptied():
    """An `<img>` with no src renders as nothing — indistinguishable from an
    image the author never wrote. Markdown answers this with a placeholder."""
    seen: list[tuple[str, str]] = []
    out = sanitize_html(
        '<img src="chart.png" alt="the chart">',
        image_placeholder=lambda alt, reason: seen.append((alt, reason)) or "[X]",
    )
    assert seen == [("the chart", "unresolvable")]
    assert out == "[X]"


def test_a_remote_image_is_refused_with_its_own_reason():
    """Remote is a refusal we chose; unresolvable is a file we cannot find.
    A reader is owed the difference, so the reason crosses the boundary."""
    seen: list[tuple[str, str]] = []
    out = sanitize_html(
        '<img src="https://beacon.invalid/px.gif?a=secret" alt="px">',
        image_placeholder=lambda alt, reason: seen.append((alt, reason)) or "[X]",
        allow_remote_images=False,
    )
    assert seen == [("px", "remote")]
    assert "beacon.invalid" not in out


def test_remote_images_are_kept_when_the_caller_allows_them():
    """The policy belongs to the renderer, not to the sanitiser."""
    out = sanitize_html('<img src="https://example.com/x.png" alt="x">')
    assert 'src="https://example.com/x.png"' in out


def test_a_remote_link_is_not_collateral_damage():
    """Only images beacon on open; a link needs a click and stays."""
    out = sanitize_html('<a href="https://example.com/x">link</a>', allow_remote_images=False)
    assert 'href="https://example.com/x"' in out
