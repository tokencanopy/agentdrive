"""A1 notation (design §5.1, plan Task 2).

The one boundary this module owns: A1 text is ONE-based, everything inside is
ZERO-based. Isolated and tested hard because a silent off-by-one here writes
cells the caller did not name, and nothing downstream would notice.
"""

from __future__ import annotations

import pytest

from agentdrive.sheets.a1 import (
    InvalidRange,
    Rect,
    cell_count,
    column_index,
    column_label,
    format_range,
    parse_range,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("A1", Rect(0, 0, 0, 0)),
        ("B7", Rect(6, 1, 6, 1)),
        ("A1:D20", Rect(0, 0, 19, 3)),
        ("Z1:AA2", Rect(0, 25, 1, 26)),
        ("C3:C4", Rect(2, 2, 3, 2)),
        ("XFD1", Rect(0, 16383, 0, 16383)),  # the xlsx column ceiling
    ],
)
def test_parse_range(text, expected):
    assert parse_range(text) == expected


def test_parse_range_is_case_insensitive_and_trims():
    assert parse_range("  b7  ") == parse_range("B7")


def test_parse_range_normalizes_reversed_corners():
    """`D20:A1` names the same rectangle as `A1:D20`. Normalizing here means
    no caller ever has to handle a reversed pair."""
    assert parse_range("D20:A1") == Rect(0, 0, 19, 3)
    assert parse_range("D1:A20") == Rect(0, 0, 19, 3)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "A",
        "1",
        "A0",  # rows are one-based; there is no row zero
        "A1:",
        ":B2",
        "A1:B2:C3",
        "-A1",
        "A 1",
        "AAAA1",  # beyond three letters
        "XFE1",  # one past the xlsx column ceiling
        "A99999999",
    ],
)
def test_parse_range_rejects_malformed(text):
    with pytest.raises(InvalidRange):
        parse_range(text)


def test_format_range_round_trips():
    for text in ["A1", "B7", "A1:D20", "Z1:AA2", "XFD1"]:
        assert format_range(parse_range(text)) == text


def test_format_range_collapses_a_single_cell():
    """`B2`, never `B2:B2`. The degenerate range reads as a bug, and
    single-cell rectangles are the common case."""
    assert format_range(Rect(1, 1, 1, 1)) == "B2"


def test_cell_count():
    assert cell_count(parse_range("A1:D20")) == 80
    assert cell_count(parse_range("A1")) == 1
    assert cell_count(parse_range("A1:B3")) == 6


@pytest.mark.parametrize(
    "index,label",
    [(0, "A"), (25, "Z"), (26, "AA"), (27, "AB"), (51, "AZ"), (701, "ZZ"), (702, "AAA")],
)
def test_column_label_and_index_are_inverses(index, label):
    assert column_label(index) == label
    assert column_index(label) == index


def test_column_index_rejects_junk():
    for bad in ["", "1", "A1", "a b"]:
        with pytest.raises(InvalidRange):
            column_index(bad)
