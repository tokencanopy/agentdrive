"""A PowerPoint deck → the slides as a document, in our typography.

The same decision docx made with mammoth, made here by hand because no
converter does it: a `.pptx` is rendered as its CONTENT — each slide a
section with its title, its text as paragraphs and nested lists, its tables
as tables, its pictures inline, its speaker notes behind a disclosure — and
not as its layout. Agents write text-heavy decks, and a reader following a
share link wants to read them; the slide's geometry is what the download is
for. Charts, SmartArt, media and embedded objects have no faithful text form
and are named in a note rather than dropped silently.

Everything here is markup THIS module writes. Every author string reaches
the output through `html.escape`, every hyperlink through a scheme
allowlist, every picture through a media-type allowlist and a byte budget.
Nothing an author wrote is ever emitted as markup, so the result does not
go through the sanitiser (which would namespace our classes) — the tests
prove the escaping instead.

Bounds, all stated in the note when they cut: `MAX_SLIDES` rendered,
`IMAGE_MAX_BYTES` per picture and `IMAGE_BUDGET` for the whole deck (a
2 MB photo inlined as a data URL is 2.7 MB of page), and text runs capped
at `MAX_TEXT_CHARS` per slide so one pathological text box is not the page.
"""

from __future__ import annotations

import base64
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from html import escape

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

MAX_SLIDES = 200
IMAGE_MAX_BYTES = 512 * 1024
IMAGE_BUDGET = 4 * 1024 * 1024
MAX_TEXT_CHARS = 20_000

# Raster types a browser draws from a data: URL. SVG is excluded on purpose —
# the sanitiser refuses `data:image/svg+xml` for the same reason (a second
# markup language wearing a URL) — and EMF/WMF are not web images at all.
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"})
_LINK_SCHEMES = ("http://", "https://", "mailto:")
_URL_NOISE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class SlidesDoc:
    html: str
    title: str | None
    omitted: list[str]


@dataclass
class _Budget:
    image_bytes: int = 0
    omitted: list[str] = field(default_factory=list)
    images_skipped: int = 0

    def note(self, what: str) -> None:
        if what not in self.omitted:
            self.omitted.append(what)


def render_deck(data: bytes) -> SlidesDoc | None:
    """The deck as a document, or None when python-pptx cannot open it.

    Raises nothing: a bad upload is the caller's download card, never a 500.
    """
    try:
        prs = Presentation(io.BytesIO(data))
        slides = list(prs.slides)
    except Exception:  # noqa: BLE001 - untrusted bytes: any failure is "cannot open"
        return None
    budget = _Budget()
    parts: list[str] = []
    title: str | None = None
    for number, slide in enumerate(slides[:MAX_SLIDES], 1):
        try:
            section, slide_title = _slide(slide, number, budget)
        except Exception:  # noqa: BLE001 - one malformed slide must not lose the deck
            section = (
                f'<section class="slide" id="slide-{number}">'
                f'<h2><span class="slide-number">{number}</span> Slide {number}</h2>'
                '<p class="doc-note">This slide could not be read.</p></section>'
            )
            slide_title = None
        if title is None and slide_title:
            title = slide_title
        parts.append(section)
    if len(slides) > MAX_SLIDES:
        budget.note(f"the first {MAX_SLIDES} of {len(slides)} slides")
    if budget.images_skipped:
        budget.note(
            f"{budget.images_skipped} picture{'s' if budget.images_skipped != 1 else ''} "
            "too large to show inline"
        )
    return SlidesDoc(html="".join(parts), title=title, omitted=budget.omitted)


def _slide(slide, number: int, budget: _Budget) -> tuple[str, str | None]:  # noqa: ANN001
    title_shape = None
    try:
        title_shape = slide.shapes.title
    except Exception:  # noqa: BLE001 - a layout without a title placeholder
        title_shape = None
    title = _text_of(title_shape).strip() if title_shape is not None else ""
    heading = escape(title) if title else f"Slide {number}"
    body: list[str] = []
    chars = 0
    for shape in _reading_order(slide.shapes):
        if title_shape is not None and shape.shape_id == title_shape.shape_id:
            continue
        rendered, used = _shape(shape, budget, chars)
        chars += used
        if rendered:
            body.append(rendered)
        if chars > MAX_TEXT_CHARS:
            budget.note(f"the first {MAX_TEXT_CHARS:,} characters of text on long slides")
            break
    notes = ""
    try:
        if slide.has_notes_slide:
            text = slide.notes_slide.notes_text_frame.text.strip()
            if text:
                notes = (
                    '<details class="slide-notes"><summary>Speaker notes</summary>'
                    + "".join(
                        f"<p>{escape(line)}</p>" for line in text.splitlines() if line.strip()
                    )
                    + "</details>"
                )
    except Exception:  # noqa: BLE001 - notes are a nicety, never a failure
        notes = ""
    return (
        f'<section class="slide" id="slide-{number}">'
        f'<h2><span class="slide-number">{number}</span> {heading}</h2>'
        + "".join(body)
        + notes
        + "</section>",
        title or None,
    )


def _reading_order(shapes) -> list:  # noqa: ANN001
    """Top-to-bottom, left-to-right — how a reader scans a slide. The file's
    z-order is how the author stacked things, which is not the same."""
    return sorted(shapes, key=lambda s: ((s.top or 0), (s.left or 0)))


def _shape(shape, budget: _Budget, chars: int) -> tuple[str, int]:  # noqa: ANN001
    kind = shape.shape_type
    if kind == MSO_SHAPE_TYPE.GROUP:
        out: list[str] = []
        used = 0
        for child in _reading_order(shape.shapes):
            rendered, n = _shape(child, budget, chars + used)
            used += n
            out.append(rendered)
        return "".join(out), used
    if kind == MSO_SHAPE_TYPE.PICTURE:
        return _picture(shape, budget), 0
    if getattr(shape, "has_chart", False) and shape.has_chart:
        budget.note("charts")
        return "", 0
    if getattr(shape, "has_table", False) and shape.has_table:
        return _table(shape.table)
    if kind == MSO_SHAPE_TYPE.MEDIA:
        budget.note("audio and video")
        return "", 0
    if kind == MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT or kind == MSO_SHAPE_TYPE.LINKED_OLE_OBJECT:
        budget.note("embedded objects")
        return "", 0
    if shape.has_text_frame:
        return _text_frame(shape, chars)
    if kind == MSO_SHAPE_TYPE.PLACEHOLDER or _is_graphic_frame(shape):
        # A graphic frame that is not a table or chart is SmartArt or an
        # embedded diagram — no text form worth inventing.
        if _is_graphic_frame(shape):
            budget.note("diagrams (SmartArt)")
        return "", 0
    return "", 0


def _is_graphic_frame(shape) -> bool:  # noqa: ANN001
    return shape._element.tag.endswith("}graphicFrame")


def _text_of(shape) -> str:  # noqa: ANN001
    if shape is None or not shape.has_text_frame:
        return ""
    return shape.text_frame.text


def _text_frame(shape, chars: int) -> tuple[str, int]:  # noqa: ANN001
    paragraphs = [p for p in shape.text_frame.paragraphs if p.text.strip() or p.runs]
    paragraphs = [p for p in paragraphs if p.text.strip()]
    if not paragraphs:
        return "", 0
    used = sum(len(p.text) for p in paragraphs)
    remaining = max(0, MAX_TEXT_CHARS - chars)
    is_list = len(paragraphs) > 1 or any(p.level > 0 for p in paragraphs)
    if not is_list:
        return f"<p>{_runs(paragraphs[0], remaining)}</p>", used
    # Nested lists from paragraph levels. A level jump of more than one is
    # treated as one: the author's indentation, not a claim about structure.
    out: list[str] = []
    depth = 0
    for para in paragraphs:
        level = max(0, min(int(para.level or 0), 8))
        while depth <= level:
            out.append("<ul>")
            depth += 1
        while depth > level + 1:
            out.append("</ul>")
            depth -= 1
        out.append(f"<li>{_runs(para, remaining)}</li>")
    out.append("</ul>" * depth)
    return "".join(out), used


def _runs(paragraph, remaining: int) -> str:  # noqa: ANN001
    out: list[str] = []
    budget = remaining
    for run in paragraph.runs:
        text = run.text
        if budget <= 0:
            break
        if len(text) > budget:
            text = text[:budget] + "…"
        budget -= len(text)
        html = escape(text)
        try:
            if run.font.bold:
                html = f"<strong>{html}</strong>"
            if run.font.italic:
                html = f"<em>{html}</em>"
        except Exception:  # noqa: BLE001 - font inheritance can be malformed
            pass
        href = _safe_link(getattr(getattr(run, "hyperlink", None), "address", None))
        if href:
            html = f'<a href="{escape(href)}" rel="noopener">{html}</a>'
        out.append(html)
    if not out:
        return escape(paragraph.text[:remaining])
    return "".join(out)


def _safe_link(address: str | None) -> str | None:
    if not address:
        return None
    cleaned = _URL_NOISE.sub("", address).strip()
    return cleaned if cleaned.lower().startswith(_LINK_SCHEMES) else None


def _table(table) -> tuple[str, int]:  # noqa: ANN001
    rows = list(table.rows)
    if not rows:
        return "", 0
    used = 0
    parts = ['<div class="table-scroll"><table class="data-table">']
    header = bool(getattr(table, "first_row", False))
    for index, row in enumerate(rows):
        tag = "th" if header and index == 0 else "td"
        cells = []
        for cell in row.cells:
            text = cell.text.strip()
            used += len(text)
            cells.append(f"<{tag}>{escape(text)}</{tag}>")
        wrap = ("<thead>", "</thead>") if tag == "th" else ("", "")
        parts.append(f"{wrap[0]}<tr>{''.join(cells)}</tr>{wrap[1]}")
    parts.append("</table></div>")
    return "".join(parts), used


def _picture(shape, budget: _Budget) -> str:  # noqa: ANN001
    try:
        image = shape.image
        blob = image.blob
        media_type = (image.content_type or "").lower()
    except Exception:  # noqa: BLE001 - a picture whose part is missing
        return _placeholder("Image unavailable")
    if media_type not in _IMAGE_TYPES:
        return _placeholder("Image format not shown")
    if len(blob) > IMAGE_MAX_BYTES or budget.image_bytes + len(blob) > IMAGE_BUDGET:
        budget.images_skipped += 1
        return _placeholder("Image too large to show inline")
    budget.image_bytes += len(blob)
    alt = escape((getattr(shape, "name", "") or "Picture").strip())
    encoded = base64.b64encode(blob).decode("ascii")
    return (
        f'<figure class="slide-picture"><img alt="{alt}" '
        f'src="data:{media_type};base64,{encoded}" /></figure>'
    )


def _placeholder(reason: str) -> str:
    return (
        f'<span class="blocked-remote image-unavailable" role="img" aria-label="{escape(reason)}">'
        f"<span>image</span><span>{escape(reason.lower())}</span></span>"
    )


def iter_omitted(doc: SlidesDoc) -> Iterable[str]:
    return doc.omitted
