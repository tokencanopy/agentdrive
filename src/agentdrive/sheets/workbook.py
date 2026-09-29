"""Workbook bytes -> a typed grid, and an editability verdict.

Two formats behind one interface: xlsx and delimited text. They are NOT the
same underneath, and the difference that matters is typing — a csv cell stays
a string, always, because inferring types from csv text is how a 20-digit
order id becomes `1.2346E+19`, silently and unrecoverably.

**Editability is whole-workbook and it is the point of this module.** openpyxl
holds either formulas or their cached results, never both, and saving discards
every cached value in the workbook — not just in the sheets that were touched.
A one-cell edit to a forty-tab model would therefore blank every formula's
result for every downstream reader, while still opening correctly in Excel,
which recalculates. There is no per-cell escape, so a workbook containing any
formula is refused before a session opens.

Everything here is pure: bytes in, values out. No HTTP, no database, no
network. `Unparseable` is the only exception that escapes — a bare library
error must never reach a request handler.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from dataclasses import dataclass
from typing import Literal

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles.numbers import is_date_format
from openpyxl.utils.exceptions import InvalidFileException

from .a1 import Rect, cell_count, column_label

XLSX_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.ms-excel",
    }
)
_XLSX_SUFFIXES = (".xlsx", ".xlsm", ".xlsb", ".xls")
_MACRO_SUFFIXES = (".xlsm", ".xlsb")
_TSV_TYPES = frozenset({"text/tab-separated-values"})
_TSV_SUFFIXES = (".tsv",)
_CSV_TYPES = frozenset({"text/csv"})
_CSV_SUFFIXES = (".csv",)

CSV_SHEET_NAME = "Sheet1"

# Refuse a container whose DECLARED uncompressed size is absurd, before
# extracting a byte of it. An xlsx is a zip, and a zip's central directory
# states each entry's uncompressed size — so the bomb is detectable
# without decompressing anything, which is the only safe moment to look.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
# A ratio guard as well as an absolute one: a small, highly compressible
# file can sit under the absolute cap and still be a deliberate bomb.
MAX_COMPRESSION_RATIO = 200

Format = Literal["xlsx", "csv", "tsv"]
Value = str | int | float | bool | None


class Unparseable(ValueError):
    """The bytes are not a readable workbook of the declared type.

    Surfaces as `422 WORKBOOK_UNPARSEABLE`. Every parser failure funnels
    here so no library exception reaches a request handler.
    """


class SheetMissing(ValueError):
    """An edit names a sheet the workbook does not have.

    Surfaces as `404 SHEET_NOT_FOUND` — never `ARTIFACT_NOT_FOUND`, which
    would claim the artifact itself is gone.
    """


@dataclass(frozen=True)
class Editability:
    """Discriminated, not a boolean plus an optional reason: a caller
    branches on `status` and never reasons about a sometimes-absent field."""

    status: Literal["ok", "blocked"]
    reason: str | None = None


@dataclass(frozen=True)
class SheetInfo:
    name: str
    index: int
    rows: int
    columns: int
    has_formulas: bool


@dataclass(frozen=True)
class Edit:
    """One range write, replayed in `seq` order at completion."""

    sheet: str
    rect: Rect
    values: list[list[Value]]


@dataclass(frozen=True)
class WorkbookIndex:
    format: Format
    sheets: tuple[SheetInfo, ...]
    cell_count: int
    editability: Editability


def detect_format(content_type: str, name: str) -> Format:
    """Declared media type first, filename second.

    The same ordering `core/kinds.py` uses, and for the same reason: the
    declared type is authoritative because uploaders routinely send the wrong
    extension, and an `image/png` named `chart.csv` is a png.
    """
    base = (content_type or "").split(";", 1)[0].strip().lower()
    lower = (name or "").lower()
    if base in XLSX_TYPES:
        return "xlsx"
    if base in _TSV_TYPES:
        return "tsv"
    if base in _CSV_TYPES:
        return "csv"
    if lower.endswith(_XLSX_SUFFIXES):
        return "xlsx"
    if lower.endswith(_TSV_SUFFIXES):
        return "tsv"
    if lower.endswith(_CSV_SUFFIXES):
        return "csv"
    raise Unparseable(f"not a spreadsheet: {content_type!r} / {name!r}")


def _is_macro_container(content_type: str, name: str) -> bool:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return (name or "").lower().endswith(_MACRO_SUFFIXES) or base.endswith(
        "macroenabled.12"
    )


def _cell_value(value: object) -> Value:
    """openpyxl's Python value -> our wire value.

    Dates become RFC 3339 strings so a client never has to know the
    workbook's date system. Everything else passes through as the scalar it
    already is; an empty cell is `None`, which is not `""`.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return value
    return str(value)


def guard_archive(data: bytes) -> None:
    """Bound decompression before openpyxl touches the container.

    Reads only the zip central directory, which is metadata: nothing is
    inflated here, so a bomb is refused rather than expanded. Both an
    absolute ceiling and a ratio, because either alone is evadable.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            declared = sum(info.file_size for info in zf.infolist())
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise Unparseable(f"unreadable container: {exc}") from exc
    if declared > MAX_UNCOMPRESSED_BYTES:
        raise Unparseable(
            f"container declares {declared} uncompressed bytes; "
            f"the limit is {MAX_UNCOMPRESSED_BYTES}"
        )
    if data and declared / max(len(data), 1) > MAX_COMPRESSION_RATIO:
        raise Unparseable("container compression ratio is implausible")


def _load(data: bytes, *, data_only: bool):
    guard_archive(data)
    try:
        return openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=data_only
        )
    except (
        InvalidFileException,
        zipfile.BadZipFile,
        KeyError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        # openpyxl raises a wide and undocumented set on malformed input: a
        # non-zip surfaces as zipfile.BadZipFile (which subclasses Exception
        # directly, so it is NOT covered by OSError/ValueError and has to be
        # named), a truncated container as KeyError from the zip directory,
        # a wrong-format file as InvalidFileException. All of it is one
        # refusal here, because a bare library error reaching a request
        # handler is a 500 for what is really a bad upload.
        raise Unparseable(f"unreadable workbook: {exc}") from exc


def _delimiter(fmt: Format) -> str:
    return "\t" if fmt == "tsv" else ","


def _read_delimited(data: bytes, fmt: Format) -> list[list[str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Unparseable(f"not utf-8 text: {exc}") from exc
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=_delimiter(fmt)))
    except csv.Error as exc:
        raise Unparseable(f"unreadable delimited text: {exc}") from exc
    return [row for row in rows if row]


def read_index(data: bytes, *, content_type: str, name: str) -> WorkbookIndex:
    """Sheet structure plus the editability verdict, without reading values.

    Never raises except `Unparseable`.
    """
    fmt = detect_format(content_type, name)

    if fmt in ("csv", "tsv"):
        rows = _read_delimited(data, fmt)
        columns = max((len(r) for r in rows), default=0)
        return WorkbookIndex(
            format=fmt,
            sheets=(
                SheetInfo(
                    name=CSV_SHEET_NAME,
                    index=0,
                    rows=len(rows),
                    columns=columns,
                    has_formulas=False,
                ),
            ),
            cell_count=len(rows) * columns,
            # Delimited text has no formulas to lose, so it is always editable.
            editability=Editability("ok"),
        )

    if _is_macro_container(content_type, name):
        # Refused on the container, before opening it. We do not read macro
        # workbooks at all, so there is nothing to inspect.
        return WorkbookIndex(
            format="xlsx",
            sheets=(),
            cell_count=0,
            editability=Editability("blocked", "UNSUPPORTED_FEATURES"),
        )

    # Formulas are visible only with data_only=False; values only with True.
    # The index needs the formula verdict, so it reads the formula view.
    wb = _load(data, data_only=False)
    try:
        sheets: list[SheetInfo] = []
        total = 0
        any_formula = False
        for i, ws in enumerate(wb.worksheets):
            rows = ws.max_row or 0
            columns = ws.max_column or 0
            has_formula = any(_is_formula(cell) for row in ws.iter_rows() for cell in row)
            any_formula = any_formula or has_formula
            total += rows * columns
            sheets.append(
                SheetInfo(
                    name=ws.title,
                    index=i,
                    rows=rows,
                    columns=columns,
                    has_formulas=has_formula,
                )
            )
    finally:
        wb.close()

    return WorkbookIndex(
        format="xlsx",
        sheets=tuple(sheets),
        cell_count=total,
        editability=(
            Editability("blocked", "FORMULAS_PRESENT")
            if any_formula
            else Editability("ok")
        ),
    )


def read_grid(data: bytes, *, content_type: str, name: str) -> dict[str, list[list[Value]]]:
    """Every sheet's values, keyed by sheet name, row-major.

    Rectangular per sheet: short rows are padded with `None` so no caller
    handles a ragged grid. Never raises except `Unparseable`.
    """
    fmt = detect_format(content_type, name)

    if fmt in ("csv", "tsv"):
        rows = _read_delimited(data, fmt)
        width = max((len(r) for r in rows), default=0)
        # Strings, always. No inference — see the module docstring.
        return {CSV_SHEET_NAME: [r + [""] * (width - len(r)) for r in rows]}

    wb = _load(data, data_only=True)
    try:
        grid: dict[str, list[list[Value]]] = {}
        for ws in wb.worksheets:
            width = ws.max_column or 0
            out: list[list[Value]] = []
            for row in ws.iter_rows(values_only=True):
                values = [_cell_value(v) for v in row]
                values += [None] * (width - len(values))
                out.append(values)
            grid[ws.title] = out
    finally:
        wb.close()
    return grid


def _load_writable(data: bytes):
    """A workbook opened for mutation.

    NOT `read_only` (writing needs random access) and NOT `data_only`, which
    would replace every formula with its cached value and silently destroy the
    model. Formula workbooks never reach here — they are refused at session
    open — but the flag is stated so nobody "optimises" it later.
    """
    guard_archive(data)
    try:
        return openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    except (
        InvalidFileException,
        zipfile.BadZipFile,
        KeyError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        raise Unparseable(f"unreadable workbook: {exc}") from exc


def _parse_rfc3339(text: str) -> dt.datetime | dt.date | None:
    for parse in (dt.datetime.fromisoformat, dt.date.fromisoformat):
        try:
            return parse(text)
        except (ValueError, TypeError):
            continue
    return None


def _validate(edits: list[Edit], sheet_names: set[str]) -> None:
    """Every edit checked BEFORE any is applied.

    All-or-nothing is a contract the caller depends on: completion parses,
    replays and serialises outside its transaction precisely so a failure here
    costs nothing and publishes nothing.
    """
    for edit in edits:
        if edit.sheet not in sheet_names:
            raise SheetMissing(f"no such sheet: {edit.sheet!r}")
        rows = edit.rect.row1 - edit.rect.row0 + 1
        cols = edit.rect.col1 - edit.rect.col0 + 1
        if len(edit.values) != rows or any(len(r) != cols for r in edit.values):
            raise ValueError(
                f"values shape does not match range: expected {rows}x{cols}"
            )
        for r, row in enumerate(edit.values):
            for c, value in enumerate(row):
                if isinstance(value, str) and ILLEGAL_CHARACTERS_RE.search(value):
                    # Control characters are legal in JSON and in UTF-8 but
                    # ILLEGAL in an xlsx cell, so openpyxl raises on save -- a
                    # bare library error, i.e. a 500, for what is really a bad
                    # value. Refused here instead, and REFUSED rather than
                    # stripped: the value is the agent's payload, and silently
                    # mangling it breaks write-then-read-back with no way for
                    # the caller to notice. (`_derive_preview` strips NUL for
                    # the opposite reason -- a preview is a search aid, not the
                    # data itself.)
                    raise ValueError(
                        "control characters are not storable in a spreadsheet "
                        f"cell: {edit.sheet}!"
                        f"{column_label(edit.rect.col0 + c)}"
                        f"{edit.rect.row0 + r + 1}"
                    )


def _is_formula(cell: object) -> bool:
    """Is this cell a formula? Ask the cell, not its text.

    This was `isinstance(cell.value, str) and cell.value.startswith("=")`,
    which is wrong in BOTH directions because a leading `=` is a property of
    the rendered text, not of the cell:

    * FALSE POSITIVE. `=> see notes` is prose a user typed. A spreadsheet
      stores it as an inline string, and refusing the workbook for
      FORMULAS_PRESENT is both wrong and unexplainable — nothing in the file
      is a formula.
    * FALSE NEGATIVE, and this is the one that loses data. An array formula's
      `cell.value` is an `ArrayFormula` object, not a string, so the test
      never matched it. The workbook was reported editable, and saving it
      blanks every cached value in the file — the exact loss the
      whole-workbook refusal exists to prevent.

    `data_type == "f"` is what openpyxl actually records, for plain formulas,
    array formulas and data-table formulas alike, in both the read-only and
    read-write loaders.
    """
    return getattr(cell, "data_type", None) == "f"


def _coerce(value: Value, number_format: str | None) -> object:
    """O1's format-decides rule.

    An RFC 3339 string written into a cell whose existing number format is a
    date format is stored as a date; anywhere else it stays text. The cell's
    own format is the only signal available — the wire type is `str` either
    way — and honouring it is what makes "write back what you read" preserve
    a date column.
    """
    if isinstance(value, str) and number_format and is_date_format(number_format):
        parsed = _parse_rfc3339(value)
        if parsed is not None:
            return parsed
    return value


def _apply_xlsx(data: bytes, edits: list[Edit]) -> bytes:
    wb = _load_writable(data)
    try:
        _validate(edits, {ws.title for ws in wb.worksheets})
        for edit in edits:
            ws = wb[edit.sheet]
            for r, row in enumerate(edit.values):
                for c, value in enumerate(row):
                    cell = ws.cell(row=edit.rect.row0 + r + 1, column=edit.rect.col0 + c + 1)
                    coerced = _coerce(value, cell.number_format)
                    cell.value = coerced
                    # openpyxl infers a FORMULA from a leading `=` on
                    # assignment. This module has no formula-writing feature
                    # to serve -- a workbook containing one is refused before
                    # a session opens -- so a value edit that manufactured one
                    # was never what the caller asked for, and it failed
                    # silently twice over: the cell read back as `None`
                    # (a programmatically written formula has no cached
                    # result), and the workbook was left FORMULAS_PRESENT,
                    # locked out of the API that had just written it.
                    #
                    # Setting the type back is the whole fix: the string is
                    # stored inline, exactly as a spreadsheet stores text a
                    # user typed beginning with `=`.
                    if isinstance(coerced, str) and cell.data_type == "f":
                        cell.data_type = "s"
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    finally:
        wb.close()


def _apply_delimited(data: bytes, edits: list[Edit], fmt: Format) -> bytes:
    rows = _read_delimited(data, fmt)
    _validate(edits, {CSV_SHEET_NAME})

    needed_rows = max(
        [len(rows)] + [e.rect.row1 + 1 for e in edits],
    )
    needed_cols = max(
        [max((len(r) for r in rows), default=0)] + [e.rect.col1 + 1 for e in edits],
    )
    # Rectangular by construction: a ragged csv renders as a staircase and
    # reads as a rendering bug, so the grid is padded once, here.
    grid = [
        list(rows[r]) + [""] * (needed_cols - len(rows[r]))
        if r < len(rows)
        else [""] * needed_cols
        for r in range(needed_rows)
    ]

    for edit in edits:
        for r, row in enumerate(edit.values):
            for c, value in enumerate(row):
                # Strings only, always — see the module docstring.
                grid[edit.rect.row0 + r][edit.rect.col0 + c] = (
                    "" if value is None else str(value)
                )

    out = io.StringIO()
    writer = csv.writer(out, delimiter=_delimiter(fmt), lineterminator="\n")
    writer.writerows(grid)
    return out.getvalue().encode("utf-8")


def apply_edits(
    data: bytes, edits: list[Edit], *, content_type: str, name: str
) -> bytes:
    """Replay `edits` in order onto `data` and return new bytes.

    Deliberately NOT byte-deterministic, and nothing may depend on it being
    so: zip entries carry timestamps and `docProps/core.xml` carries
    `dcterms:modified`, so two replays of one log can hash differently.
    Exactly-once comes from the session fence and the idempotency key, never
    from serializer stability (design §5.8.1).
    """
    fmt = detect_format(content_type, name)
    if fmt in ("csv", "tsv"):
        return _apply_delimited(data, list(edits), fmt)
    return _apply_xlsx(data, list(edits))


def total_cells(edits: list[Edit]) -> int:
    """Cells an edit batch writes. The unit every §8 cap counts in."""
    return sum(cell_count(e.rect) for e in edits)


# Search extraction is bounded independently of the render caps: this feeds
# `content_preview`, which Postgres truncates anyway, so walking a whole
# 200k-cell workbook to build a 16 KiB string is pure waste.
_PREVIEW_MAX_CELLS = 20_000


def extract_text(data: bytes, *, content_type: str, name: str, limit: int) -> str | None:
    """Searchable text from a workbook, or None when there is none.

    Sheet names are included because they are frequently the most meaningful
    words in the file — "Q4 Forecast" is a better search hit than any cell in
    it. Values are flattened with tabs and newlines so `websearch_to_tsquery`
    sees ordinary word boundaries.

    Never raises: a workbook that cannot be parsed simply has no preview, and
    an unsearchable artifact is far better than a failed upload.
    """
    try:
        grid = read_grid(data, content_type=content_type, name=name)
    except Unparseable:
        return None

    parts: list[str] = []
    size = 0
    cells = 0
    for sheet, rows in grid.items():
        parts.append(sheet)
        size += len(sheet) + 1
        for row in rows:
            if size >= limit or cells >= _PREVIEW_MAX_CELLS:
                break
            line = "\t".join("" if v is None else str(v) for v in row).strip()
            cells += len(row)
            if not line:
                continue
            parts.append(line)
            size += len(line) + 1
        if size >= limit or cells >= _PREVIEW_MAX_CELLS:
            break

    text = "\n".join(parts).strip()
    if not text:
        return None
    # NUL is valid UTF-8 but ILLEGAL in a Postgres text value, and a cell can
    # carry one. Stripped rather than refused: this is a search aid, not the
    # data — the same reasoning `_derive_preview` records for its own strip.
    return text.replace("\x00", "")[:limit]
