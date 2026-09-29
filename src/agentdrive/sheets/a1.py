"""A1 notation <-> zero-based rectangles.

A1 text is ONE-based; everything inside this package is ZERO-based. This
module is the single place that boundary is crossed, so no caller does the
arithmetic itself and no caller gets it wrong in its own way.

Deliberately strict. Anything it cannot parse raises rather than guessing: a
silently misparsed range writes cells the caller never named, and nothing
downstream would notice. `InvalidRange` surfaces as `400 INVALID_ARGUMENT`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Three letters max, and the value ceiling below rejects the rest of that
# space. Rows are one-based, so a leading zero is not a row.
_CELL_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")
_COLUMN_RE = re.compile(r"^[A-Z]{1,3}$")

# The xlsx grid: XFD (16383 zero-based) by 1,048,576.
MAX_COLUMN = 16_383
MAX_ROW = 1_048_575


class InvalidRange(ValueError):
    """Malformed A1 notation. Surfaces as 400 INVALID_ARGUMENT."""


@dataclass(frozen=True)
class Rect:
    """An inclusive, normalized rectangle. Zero-based on both axes."""

    row0: int
    col0: int
    row1: int
    col1: int


def column_index(letters: str) -> int:
    """`"A"` -> 0, `"AA"` -> 26. Bijective base-26, not plain base-26."""
    if not _COLUMN_RE.match(letters or ""):
        raise InvalidRange(f"not a column reference: {letters!r}")
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - ord("A") + 1)
    index = value - 1
    if index > MAX_COLUMN:
        raise InvalidRange(f"column out of range: {letters!r}")
    return index


def column_label(index: int) -> str:
    """`0` -> `"A"`, `26` -> `"AA"`. Inverse of `column_index`."""
    if index < 0 or index > MAX_COLUMN:
        raise InvalidRange(f"column index out of range: {index!r}")
    out = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _cell(text: str) -> tuple[int, int]:
    match = _CELL_RE.match(text)
    if not match:
        raise InvalidRange(f"not a cell reference: {text!r}")
    col = column_index(match.group(1))
    row = int(match.group(2)) - 1
    if row > MAX_ROW:
        raise InvalidRange(f"row out of range: {text!r}")
    return row, col


def parse_range(text: str) -> Rect:
    """`"A1"`, `"B7"`, `"A1:D20"` -> a normalized inclusive rectangle.

    Reversed corners are normalized, so `D20:A1` and `A1:D20` are the same
    rectangle and no caller has to handle the reversed pair.
    """
    raw = (text or "").strip().upper()
    if not raw:
        raise InvalidRange("empty range")
    parts = raw.split(":")
    if len(parts) == 1:
        row, col = _cell(parts[0])
        return Rect(row, col, row, col)
    if len(parts) != 2:
        raise InvalidRange(f"not a range: {text!r}")
    r0, c0 = _cell(parts[0])
    r1, c1 = _cell(parts[1])
    return Rect(min(r0, r1), min(c0, c1), max(r0, r1), max(c0, c1))


def format_range(rect: Rect) -> str:
    """Inverse of `parse_range`. A single cell renders as `B2`, never
    `B2:B2` -- the degenerate form reads as a bug, and single-cell
    rectangles are the common case in a grouped diff."""
    start = f"{column_label(rect.col0)}{rect.row0 + 1}"
    end = f"{column_label(rect.col1)}{rect.row1 + 1}"
    return start if start == end else f"{start}:{end}"


def cell_count(rect: Rect) -> int:
    """Cells in the rectangle. The unit every cap in §8 is expressed in."""
    return (rect.row1 - rect.row0 + 1) * (rect.col1 - rect.col0 + 1)
