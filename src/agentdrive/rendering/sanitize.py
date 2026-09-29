"""Agent-authored HTML → inert, allowlisted markup.

The one job: take a `text/html` artifact — written by an agent, opened by a
person on our own origin — and return markup that renders as a document and
does nothing else. It is deliberately NOT a general-purpose HTML sanitiser,
takes no dependency, and has exactly one failure mode: dropping too much.

Three properties, in the order they matter:

1. **Allowlist, never denylist.** Only the elements, attributes and URL
   schemes named below are emitted; everything else is dropped by
   construction. That is what makes an evasion shape uninteresting — there is
   no pattern to slip past, because nothing is matched *against*. Every `on*`
   handler is covered by the attribute allowlist without naming one.

2. **The output is ours, not the author's.** Tags are re-emitted from the
   parsed token stream and every unclosed element is closed at the end, so the
   result is balanced whatever the input was. Nothing here ever passes an
   author byte through as markup; text is escaped, attribute values are
   escaped, and the author's byte stream is not echoed.

3. **Text survives, structure need not.** An element we do not recognise is
   dropped and its children are kept — a `<marquee>` should cost the reader
   its styling, not its sentence. The exception is `DROPPED_WITH_CONTENTS`:
   elements whose contents are code, styling, or metadata rather than prose.

Why sanitise at all when the page CSP already refuses scripts, inline
handlers, frames and styles: because the CSP is one edit away from not
refusing them, and a design where a single directive change turns inert
markup into live markup is a trap for a future maintainer. Two independent
layers means neither one being wrong is sufficient.

(There is a third on the private surface only — the shell stages the markup
in a `<template>` and `innerHTML` never runs a script — but the public
renderer interpolates server-side through Jinja, where that property does not
apply. The count differs by surface; the two layers above do not.)
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from html import escape
from html.parser import HTMLParser

#: Given an image's alt text and WHY it will not appear (`"remote"` or
#: `"unresolvable"`), return the markup that stands in for it. The reason
#: crosses the boundary because the two are not the same statement to a
#: reader: one is a refusal we chose, the other is a file we cannot find.
type ImagePlaceholder = Callable[[str, str], str]

# Structural and textual elements only. No element that loads a subresource
# other than an image, none that submits, none that scripts, none that styles.
ALLOWED_ELEMENTS = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tr", "th", "td", "caption",
    "a", "img", "code", "pre", "blockquote",
    "strong", "em", "b", "i", "s", "del", "ins", "sub", "sup", "small",
    "span", "div", "figure", "figcaption",
    "section", "article", "header", "footer", "main", "nav", "aside",
})

# Everything else is dropped, which covers every `on*` handler and every
# `data-*` hook by construction rather than by pattern-matching. `style` is
# absent on purpose: the author's CSS is not applied (design §5.3), and an
# attribute the CSP refuses is not an attribute worth emitting.
ALLOWED_ATTRIBUTES = frozenset({
    "href", "src", "alt", "title", "colspan", "rowspan",
    "id", "class", "width", "height", "lang", "dir",
})

# Dropped WITH their contents, because their contents are not prose:
# executable (`script`), styling (`style`), a subresource or navigation
# instruction (`iframe`/`object`), input collection (`form`/`button`),
# inert-until-adopted markup (`template`), or a second markup language with
# its own script surface (`svg`/`math`).
#
# `title` is the one addition to the design's §5.2 list, and it earns its
# place: every HTML document an agent writes has one, and treating it like an
# unknown element — dropping the tag, keeping the text — prints the document's
# title as a stray sentence above the content it names. It is metadata, and
# the surrounding page already draws it.
#
# `head` is deliberately NOT here: its end tag is OPTIONAL in HTML, so
# dropping-with-contents on an unclosed `<head>` would swallow the entire
# document. Its children (`title`, `link`, `style`, `meta`) are handled
# individually, which is the same outcome without the cliff.
# Every member is a RAW-TEXT or foreign-content element, or one a browser
# genuinely does not render — which is what makes swallowing to the end tag
# safe. `<style>x` with no `</style>` eats the rest of a real browser's
# document too, so matching that is correct rather than a cliff.
#
# `form`, `button` and `object` were here and had to leave. None of the three
# is raw text: a browser renders straight past an unclosed one, so swallowing
# meant a document ending at the first `<form>` an agent forgot to close —
# the same failure `DROPPED_VOID` below was written for, and the same
# selective-truncation primitive, where the rendered view silently disagrees
# with the source view and the downloaded bytes about what the artifact says.
# They are treated as unknown elements instead: the tag goes, the children
# stay. Nothing is weakened by that, because what made them dangerous is
# dropped on its own — `input` is void-dropped, a `<form>`'s `action` dies
# with its tag, and a `<button>`'s only remaining trace is its label as prose.
DROPPED_WITH_CONTENTS = frozenset({
    "script", "style", "iframe", "template", "svg", "math", "title",
})

# The same §5.2 drop list, minus the contents — because these four have no
# contents to take. `link`, `base`, `input` and `embed` are VOID: their end tag
# is not optional, it is forbidden, so a `</link>` never arrives. Waiting for
# one discards the rest of the document, and a stylesheet `<link>` in `<head>`
# is in nearly every complete HTML document a model writes — which made the
# modal case render as an empty card claiming the file had no content. Worse,
# it let an artifact choose where its own rendering stopped, so the bytes and
# the source view could carry text the rendered view silently dropped.
DROPPED_VOID = frozenset({"link", "base", "input", "embed"})

# Foreign content is the one place HTML honours a self-closing slash, so
# `<svg/>` really is an empty element while `<script/>` is not: a browser
# reads on to `</script>`. Anything outside this set that arrives self-closed
# is treated as OPEN, which is what stops script text from being re-emitted as
# rendered markup.
_SELF_CLOSING_HONOURED = frozenset({"svg", "math"})

# A ceiling on how deep the emitted document nests. No real document
# approaches it; a hostile one nests until the reader's browser gives up, and
# every open element also costs a closing tag we emit ourselves. Past the cap
# an element is dropped the way an unknown one is: the tag goes, the text
# stays.
MAX_OPEN_ELEMENTS = 512

# The void elements within `ALLOWED_ELEMENTS`. They are emitted self-closed
# and never pushed onto the open-element stack, so a `<br>` cannot leave the
# document one element deep for the rest of its length.
VOID_ELEMENTS = frozenset({"br", "hr", "img"})

ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})

# An absolute http(s) image source — a request to a third party made the
# instant the document opens, which is a read receipt on the private surface
# and, on a script-free page, still a beacon on the public one. Markdown has
# refused these on both surfaces for exactly this reason; `page` mode refusing
# them too is what stops one drive answering the same question two ways.
_REMOTE_IMAGE = re.compile(r"^https?://", re.IGNORECASE)

# Every `class` token and every `id` an artifact keeps is rewritten with this
# prefix, and same-document fragment targets move with it.
#
# The author's own CSS is dropped (§5.3), so a verbatim class buys an artifact
# exactly one thing: OUR styling. `<p class="untrusted-band">This document has
# been verified</p>` renders pixel-identical to the genuine untrusted-content
# band — the only mitigation §6.3 names against the impersonation it warns
# about — and `<a class="btn" href="https://…">Verify your account</a>` renders
# as the product's own primary button. Namespacing removes the whole class of
# collision at the source, rather than scoping each chrome selector and hoping
# the next stylesheet edit remembers.
#
# Deliberately applied again on re-sanitising rather than detected and skipped:
# an artifact that pre-writes `ua-btn` must land on `ua-ua-btn`, not inherit
# the prefix as camouflage.
CLASS_PREFIX = "ua-"


def _namespaced(value: str | None) -> str:
    """`"btn doc-head"` -> `"ua-btn ua-doc-head"`. Empty stays empty."""
    return " ".join(CLASS_PREFIX + token for token in (value or "").split())

# A scheme is `letter *( letter / digit / "+" / "-" / "." ) ":"` at the very
# start of the value. Anchored, so `/a:b` and `#a:b` are correctly read as a
# path and a fragment rather than as schemes.
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")

# C0 controls and DEL. Browsers strip tab, CR and LF from a URL *before*
# resolving its scheme, which is the whole trick behind `jav&#x09;ascript:` —
# `html.parser` hands us the decoded tab, the browser then removes it, and a
# scheme check on the raw value sees something that is not `javascript:`. So
# the same removal happens here, and the stripped value is what gets emitted:
# deciding on one string and emitting another is how this class of bug works.
_URL_NOISE = re.compile(r"[\x00-\x1f\x7f]")

# Only `<img>` may carry a `data:` URL, and only an image one. `svg+xml` is
# excluded: script inside an `<img>`-loaded SVG does not run, but SVG is a
# second markup language with its own surface, the element form is already in
# `DROPPED_WITH_CONTENTS`, and the same capability wearing a URL is still the
# same capability.
# Matched against the LOWERCASED value: a media type is case-insensitive to
# the browser that resolves it, so `data:image/SVG+XML` is the same capability
# as `data:image/svg+xml`, and `DATA:image/png` is the same legitimate image.
_DATA_IMAGE = re.compile(r"^data:image/(?!svg\+xml)[a-z0-9.+-]+[;,]")


def _safe_url(value: str | None, *, attribute: str, tag: str) -> str | None:
    """The URL to emit, or None to drop the attribute and keep the text.

    Dropping the attribute rather than the element is deliberate: a link whose
    target we refuse is still a sentence the reader should see.
    """
    if value is None:
        return None
    cleaned = _URL_NOISE.sub("", value).strip()
    if not cleaned:
        return None

    match = _SCHEME.match(cleaned)
    if match is None:
        # No scheme: a fragment stays (an in-document table of contents is the
        # single most common link an agent report contains, and it can neither
        # issue a request nor leave the document). Every other relative form —
        # `/path`, `path`, `//host/path` — is dropped: it would resolve against
        # OUR origin or silently inherit our scheme, which is never what the
        # author of a stored artifact can have meant.
        #
        # The target moves with the id it points at, or namespacing would
        # break the one link shape this allowance exists for.
        if cleaned == "#":
            return cleaned
        return "#" + CLASS_PREFIX + cleaned[1:] if cleaned.startswith("#") else None

    scheme = match.group(1).lower()
    if scheme in ALLOWED_URL_SCHEMES:
        # `mailto:` addresses a person, so it belongs on a link and nowhere
        # else; as a subresource source it is meaningless.
        if scheme == "mailto" and attribute != "href":
            return None
        return cleaned
    if scheme == "data" and attribute == "src" and tag == "img":
        # Decide on the lowercased form, emit the original: deciding on one
        # string and emitting another is only a trap when the emitted one can
        # mean something the decision did not see, and case cannot.
        return cleaned if _DATA_IMAGE.match(cleaned.lower()) else None
    return None


class _Sanitizer(HTMLParser):
    """Emits its own tags from the parsed token stream. Never echoes input."""

    def __init__(
        self,
        image_placeholder: ImagePlaceholder | None = None,
        *,
        allow_remote_images: bool = True,
    ) -> None:
        # convert_charrefs decodes entities in text before we see it, so what
        # we escape on the way out is the real character rather than a second
        # encoding of it. `&amp;lt;script&amp;gt;` cannot survive as markup.
        super().__init__(convert_charrefs=True)
        # How to say "this image is not going to appear". Injected rather than
        # built here: the markup belongs to the renderer, which already states
        # exactly this for markdown, and a sanitiser that hardcodes product
        # markup is a sanitiser that drifts from it.
        self._image_placeholder = image_placeholder
        self._allow_remote_images = allow_remote_images
        self._out: list[str] = []
        self._open: list[str] = []
        # How many of each tag are open, so "is this end tag matched by
        # anything?" is O(1). Scanning `self._open` instead made an artifact of
        # `<b>`s followed by `</i>`s quadratic: at the 2 MiB render ceiling
        # that is minutes of a blocked event loop, from one anonymous GET.
        self._open_counts: Counter[str] = Counter()
        # The element whose contents are being swallowed, plus how deep the
        # same element is nested inside itself. Tracked by name so a nested
        # `<div>` inside a dropped `<template>` cannot end the suppression.
        self._skip_tag: str | None = None
        self._skip_depth = 0

    # ── output ───────────────────────────────────────────────────────────

    def _emit_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        rendered = []
        seen: set[str] = set()
        for name, value in attrs:
            lowered = name.lower()
            # First occurrence wins, matching what a browser does with a
            # duplicated attribute — so a second `href` cannot override a
            # first one that we already vetted.
            if lowered in seen or lowered not in ALLOWED_ATTRIBUTES:
                continue
            seen.add(lowered)
            if lowered in ("href", "src"):
                url = _safe_url(value, attribute=lowered, tag=tag)
                if (
                    url is not None
                    and tag == "img"
                    and lowered == "src"
                    and not self._allow_remote_images
                    and _REMOTE_IMAGE.match(url)
                ):
                    url = None
                if url is None:
                    continue
                rendered.append(f'{lowered}="{escape(url, quote=True)}"')
                continue
            if lowered in ("class", "id"):
                rendered.append(
                    f'{lowered}="{escape(_namespaced(value), quote=True)}"'
                )
                continue
            rendered.append(f'{lowered}="{escape(value or "", quote=True)}"')
        if tag == "img" and not any(r.startswith('src="') for r in rendered):
            raw = next((v or "" for n, v in attrs if n.lower() == "src"), "")
            reason = "remote" if _REMOTE_IMAGE.match(raw.strip()) else "unresolvable"
            # An `<img>` whose src we refused renders as nothing at all — no
            # broken glyph, no alt text in most browsers — so the reader
            # cannot tell a dropped image from an image the author never
            # wrote. Markdown answers the identical situation with a labelled
            # placeholder; a document store should not answer it two ways.
            if self._image_placeholder is not None:
                alt = next(
                    (v or "" for n, v in attrs if n.lower() == "alt"), ""
                )
                self._out.append(self._image_placeholder(alt, reason))
                return
            # No placeholder supplied: emit the image stripped of its source,
            # which is this function's older and narrower contract. Callers
            # that care what a reader sees pass one.
        joined = ("" if not rendered else " " + " ".join(rendered))
        if tag in VOID_ELEMENTS:
            self._out.append(f"<{tag}{joined} />")
            return
        if len(self._open) >= MAX_OPEN_ELEMENTS:
            return  # too deep: drop the tag, keep the children
        self._out.append(f"<{tag}{joined}>")
        self._open.append(tag)
        self._open_counts[tag] += 1

    # ── parser callbacks ─────────────────────────────────────────────────

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in DROPPED_VOID:
            # No end tag exists, so there is nothing to skip TO.
            return
        if tag in DROPPED_WITH_CONTENTS:
            self._skip_tag = tag
            self._skip_depth = 1
            return
        if tag not in ALLOWED_ELEMENTS:
            # Unknown element: the tag goes, its children stay.
            return
        self._emit_start(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                # A self-closed occurrence of the element being skipped opens
                # and closes nothing; the skip continues either way.
                pass
            return
        if tag in DROPPED_VOID:
            return
        if tag in DROPPED_WITH_CONTENTS:
            if tag in _SELF_CLOSING_HONOURED:
                return
            # `<script/>` does NOT close a script — a browser reads on to
            # `</script>`. Honouring the slash here would re-emit script text
            # as rendered markup: nothing executes, but markup a browser would
            # have swallowed becomes document structure, which is a smuggling
            # channel and defeats anyone reading the artifact's source.
            self._skip_tag = tag
            self._skip_depth = 1
            return
        if tag not in ALLOWED_ELEMENTS:
            return
        if tag in VOID_ELEMENTS:
            self._emit_start(tag, attrs)
            return
        # `<div/>` is not self-closing in HTML either; a browser would treat it
        # as an open tag. Emit the pair so the output says what it means.
        self._emit_start(tag, attrs)
        self._close_through(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = None
            return
        if tag in VOID_ELEMENTS or tag not in ALLOWED_ELEMENTS:
            return
        self._close_through(tag)

    def _close_through(self, tag: str) -> None:
        """Close `tag`, and anything the author left open inside it.

        An end tag with nothing matching it is ignored rather than emitted:
        emitting it is precisely how a sanitiser produces markup that reparents
        the rest of the document into somebody else's element.
        """
        if not self._open_counts[tag]:
            return
        while self._open:
            current = self._open.pop()
            self._open_counts[current] -= 1
            self._out.append(f"</{current}>")
            if current == tag:
                return

    def handle_data(self, data: str) -> None:
        if self._skip_tag is not None:
            return
        if data:
            self._out.append(escape(data, quote=False))

    # Comments, doctypes, processing instructions and unknown declarations
    # carry no prose and are dropped. Conditional comments are the reason this
    # is not "render the comment's text": their contents are markup.
    def handle_comment(self, data: str) -> None:
        return

    def handle_decl(self, decl: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    def result(self) -> str:
        while self._open:
            tag = self._open.pop()
            self._open_counts[tag] -= 1
            self._out.append(f"</{tag}>")
        return "".join(self._out)


def sanitize_html(
    markup: str,
    *,
    image_placeholder: ImagePlaceholder | None = None,
    allow_remote_images: bool = True,
) -> str:
    """Allowlisted, balanced markup for `markup`.

    Total on its input: malformed, truncated and hostile documents all return
    a string. Callers still treat a raised exception as "fall back to source",
    because emitting unsanitised author markup is the one outcome that must be
    impossible — but nothing in here is expected to raise.
    """
    parser = _Sanitizer(image_placeholder, allow_remote_images=allow_remote_images)
    parser.feed(markup)
    parser.close()
    return parser.result()
