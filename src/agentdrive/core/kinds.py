"""content_type → the design system's kind vocabulary.

ONE classifier, imported by every surface that renders a kind chip. Legacy
persisted an equivalent enum at upload; v0 derives it, so improving the
mapping improves every artifact at once with no backfill — the same reasoning
that keeps paths derived (§4.1).

The nine kinds are the design system's, because those are the values with
glyphs drawn for them. Anything unrecognized is `bundle`, never an exception:
a classifier that raises would take down a page over an unusual media type.
"""

from __future__ import annotations

import re

KINDS = (
    "md", "code", "image", "video", "dataset", "pdf", "skill", "bundle", "folder"
)

_MARKDOWN_SUFFIXES = (".md", ".markdown")
_MARKDOWN_TYPES = frozenset({"text/markdown", "text/x-markdown"})
# A workbook IS a dataset — it needs no new design-system glyph, and the
# chip in front of a reader stops saying "bundle" (which it did only
# because nothing else matched, not because a spreadsheet is an archive).
_DATASET_SUFFIXES = (
    ".csv", ".tsv", ".parquet", ".xlsx", ".xlsm", ".xlsb", ".xls", ".ods"
)
_DATASET_TYPES = frozenset({
    "text/csv",
    "text/tab-separated-values",
    "application/vnd.apache.parquet",
    # The pre-registration spelling many uploaders still send.
    "application/x-parquet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.ms-excel",
    # OpenDocument is a spreadsheet by the same argument as the OOXML ones
    # above. It is also a ZIP, so without this line it landed in `bundle`
    # while its Excel-shaped neighbours read `dataset` — the same file,
    # saved from a different application, described as a different thing.
    "application/vnd.oasis.opendocument.spreadsheet",
})
_CODE_TYPES = frozenset({
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/x-ndjson",
    "application/x-jsonl",
    "application/jsonl",
    "application/toml",
    "application/sql",
})

# Media types specific enough to settle the question on their own. Checked
# BEFORE any filename fallback: content_type is authoritative, so an
# `image/png` named `chart.csv` is an image, not a dataset.
_DECISIVE_PREFIXES = (("image/", "image"), ("video/", "video"))

# PDF has its own kind rather than falling through to `bundle`. It reached
# `bundle` because nothing else matched — not because a PDF is an archive of
# things — and the chip in front of the reader said so: a board deck listed as
# "bundle". Exact match, not a prefix: `application/pdf` is the type, and the
# neighbouring `application/*` types are genuinely not documents.
_PDF_TYPES = frozenset({"application/pdf", "application/x-pdf"})
_PDF_SUFFIX = ".pdf"


def kind_for(content_type: str, name: str = "") -> str:
    """Classify an artifact for display. Never raises.

    Order matters. A concrete media type wins outright; the filename is
    consulted only where the declared type is too generic to distinguish —
    `text/plain` named `notes.md` is markdown, because uploaders routinely
    send text/plain for everything.
    """
    base = (content_type or "").split(";", 1)[0].strip().lower()
    lower_name = (name or "").lower()

    for prefix, kind in _DECISIVE_PREFIXES:
        if base.startswith(prefix):
            return kind

    if base in _PDF_TYPES or lower_name.endswith(_PDF_SUFFIX):
        return "pdf"
    if base in _MARKDOWN_TYPES or lower_name.endswith(_MARKDOWN_SUFFIXES):
        return "md"
    if base in _DATASET_TYPES or lower_name.endswith(_DATASET_SUFFIXES):
        return "dataset"
    if base.startswith("text/") or base in _CODE_TYPES:
        return "code"
    # Any XML dialect (`application/atom+xml`, `application/rss+xml`, …) is
    # text a reader can open; SVG was already claimed by the image prefix.
    if base.endswith("+xml"):
        return "code"
    return "bundle"


# RFC 6838: type/subtype, each a token, optionally followed by ;param=value.
# Deliberately strict — this guards a response header, so anything it does not
# recognise is not worth the risk of emitting.
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+"
    r"(\s*;\s*[A-Za-z0-9!#$&^_.+-]+=(?:[A-Za-z0-9!#$&^_.+-]+|\"[^\"\\\r\n]*\"))*$"
)

SAFE_FALLBACK_CONTENT_TYPE = "application/octet-stream"


def safe_content_type(content_type: str | None) -> str:
    """A `content_type` fit to put in a response header.

    The stored value is uploader-controlled text: nothing validates it on the
    way in, so it can hold a CRLF. Interpolated into `Content-Type` that is a
    header-injection attempt, and while the server refuses the malformed
    header, it refuses by raising — which drops the connection and returns
    zero bytes. That turns one bad upload into a permanent, unfixable-by-the-
    reader break of that artifact's bytes for everyone.

    So this is the only thing that should reach a header. Unrecognised values
    become `application/octet-stream`, which is both safe and honest: we no
    longer know what the bytes are.
    """
    value = (content_type or "").strip()
    if not value or len(value) > 255 or not _MEDIA_TYPE_RE.match(value):
        return SAFE_FALLBACK_CONTENT_TYPE
    return value


# Finer-grained than `kind_for`, and deliberately so: the chip is display, not
# dispatch. Legacy showed `pdf`, `json`, `csv`, `png` where the coarse kind
# would say `bundle` or `code` — a reader recognises the format, and telling
# them "bundle" when they are looking at a PDF is worse than useless.
_CHIP_EXACT = {
    "text/markdown": "md",
    "text/x-markdown": "md",
    "application/json": "json",
    "application/x-ndjson": "jsonl",
    "application/x-jsonl": "jsonl",
    "application/jsonl": "jsonl",
    "text/csv": "csv",
    "text/tab-separated-values": "tsv",
    "application/pdf": "pdf",
    "application/vnd.apache.parquet": "parquet",
    "application/x-parquet": "parquet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsm",
    "application/vnd.ms-excel": "xls",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/yaml": "yaml",
    "application/x-yaml": "yaml",
    "text/yaml": "yaml",
    "text/x-yaml": "yaml",
    "text/xml": "xml",
    "application/xml": "xml",
    "text/html": "html",
}


def chip_label(content_type: str, name: str = "") -> str:
    """Short display label for the kind chip. Never raises.

    Falls back to the coarse `kind_for` rather than to a generic word, so an
    unrecognised type still says something true.
    """
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base in _CHIP_EXACT:
        return _CHIP_EXACT[base]
    for prefix in ("image/", "video/", "audio/", "text/"):
        if base.startswith(prefix):
            return _tidy(base[len(prefix) :]) or kind_for(content_type, name)
    if base.startswith("application/") and base.endswith("+xml"):
        # `atom`, `rss`, `xhtml`: the dialect's name, the way `svg` reads
        # for `image/svg+xml`.
        return _tidy(base[len("application/") :]) or "xml"
    return kind_for(content_type, name)


def _tidy(sub: str) -> str:
    """`x-python` -> `python`, `svg+xml` -> `svg`, `plain` -> `text`.

    The `x-` prefix and `+xml` suffix are media-type bookkeeping; a reader
    looking at a chip wants the format's name.
    """
    sub = sub.removeprefix("x-").split("+", 1)[0]
    return "text" if sub == "plain" else sub
