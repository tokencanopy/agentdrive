"""Workbook write adapter (design §5.2/§5.8.1, plan Task 4).

`apply_edits` is what completion replays the edit log through. Its contract is
narrow and its failure modes are silent, which is why these are behavioural
tests against re-read bytes rather than assertions about openpyxl objects.
"""

from __future__ import annotations

import pytest

from agentdrive.sheets.a1 import parse_range
from agentdrive.sheets.workbook import Edit, SheetMissing, apply_edits, read_grid

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV = "text/csv"


def _edit(sheet: str, a1: str, values: list[list[object]]) -> Edit:
    return Edit(sheet=sheet, rect=parse_range(a1), values=values)


def test_writes_the_named_cells(values_workbook):
    out = apply_edits(
        values_workbook,
        [_edit("Q3", "C2:C3", [[1610], [1720]])],
        content_type=XLSX,
        name="q.xlsx",
    )
    grid = read_grid(out, content_type=XLSX, name="q.xlsx")
    assert grid["Q3"][1][2] == 1610
    assert grid["Q3"][2][2] == 1720


def test_edits_apply_in_sequence_order(values_workbook):
    """`seq` IS replay order. Two writes to one cell must land last-wins."""
    out = apply_edits(
        values_workbook,
        [_edit("Q3", "B2", [[1]]), _edit("Q3", "B2", [[2]])],
        content_type=XLSX,
        name="q.xlsx",
    )
    assert read_grid(out, content_type=XLSX, name="q.xlsx")["Q3"][1][1] == 2


def test_write_beyond_the_used_range_extends_it(values_workbook):
    """How an agent appends rows: there is no insert operation, and none is
    needed."""
    out = apply_edits(
        values_workbook,
        [_edit("Q3", "A10:B10", [["LATAM", 500]])],
        content_type=XLSX,
        name="q.xlsx",
    )
    grid = read_grid(out, content_type=XLSX, name="q.xlsx")
    assert grid["Q3"][9][0] == "LATAM"
    assert grid["Q3"][9][1] == 500


def test_clearing_a_cell_writes_none(values_workbook):
    out = apply_edits(
        values_workbook,
        [_edit("Q3", "B2", [[None]])],
        content_type=XLSX,
        name="q.xlsx",
    )
    assert read_grid(out, content_type=XLSX, name="q.xlsx")["Q3"][1][1] is None


def test_unknown_sheet_raises_sheet_missing(values_workbook):
    with pytest.raises(SheetMissing):
        apply_edits(
            values_workbook,
            [_edit("Nope", "A1", [[1]])],
            content_type=XLSX,
            name="q.xlsx",
        )


def test_a_failing_edit_writes_nothing(values_workbook):
    """All-or-nothing: a batch that cannot apply must not half-apply. The
    caller's transaction depends on this — completion parses, replays and
    serialises OUTSIDE the transaction precisely so a failure here costs
    nothing."""
    with pytest.raises(SheetMissing):
        apply_edits(
            values_workbook,
            [_edit("Q3", "B2", [[999]]), _edit("Nope", "A1", [[1]])],
            content_type=XLSX,
            name="q.xlsx",
        )
    # The source bytes are untouched by construction, but assert the obvious
    # thing anyway: nothing was mutated in place.
    assert read_grid(values_workbook, content_type=XLSX, name="q.xlsx")["Q3"][1][1] == 1200


def test_values_shape_must_match_the_rect(values_workbook):
    with pytest.raises(ValueError):
        apply_edits(
            values_workbook,
            [_edit("Q3", "A1:B2", [[1, 2]])],  # 1 row given, 2 required
            content_type=XLSX,
            name="q.xlsx",
        )


def test_an_rfc3339_string_into_a_date_cell_stays_a_date(dated_workbook):
    """O1's format-decides rule: the target cell's existing number format
    decides whether an RFC 3339 string is stored as a date or as text."""
    out = apply_edits(
        dated_workbook,
        [_edit("Dates", "A2", [["2026-12-25"]])],
        content_type=XLSX,
        name="d.xlsx",
    )
    value = read_grid(out, content_type=XLSX, name="d.xlsx")["Dates"][1][0]
    assert value.startswith("2026-12-25")


def test_an_rfc3339_string_into_a_text_cell_stays_text(values_workbook):
    out = apply_edits(
        values_workbook,
        [_edit("Q3", "A2", [["2026-12-25"]])],
        content_type=XLSX,
        name="q.xlsx",
    )
    assert read_grid(out, content_type=XLSX, name="q.xlsx")["Q3"][1][0] == "2026-12-25"


def test_csv_round_trips_string_cells_exactly():
    """The 20-digit id and the leading-zero id must survive a write to a
    NEIGHBOURING cell. This is the corruption that is silent and permanent."""
    data = b"id,qty\n00123,7\n12345678901234567890,8\n"
    out = apply_edits(
        data, [_edit("Sheet1", "B2", [["9"]])], content_type=CSV, name="d.csv"
    )
    grid = read_grid(out, content_type=CSV, name="d.csv")
    assert grid["Sheet1"][1] == ["00123", "9"]
    assert grid["Sheet1"][2][0] == "12345678901234567890"


def test_csv_extends_with_empty_cells_not_ragged_rows():
    out = apply_edits(
        b"a,b\n1,2\n", [_edit("Sheet1", "D4", [["x"]])], content_type=CSV, name="d.csv"
    )
    grid = read_grid(out, content_type=CSV, name="d.csv")
    assert grid["Sheet1"][3][3] == "x"
    assert all(len(row) == 4 for row in grid["Sheet1"])


@pytest.mark.parametrize("bad", ["\x00", "a\x01b", "\x1f"])
def test_control_characters_are_refused_not_stripped(values_workbook, bad):
    """Legal in JSON and UTF-8, illegal in an xlsx cell. openpyxl raises on
    save, which would be a 500 for what is really a bad value — so it is
    caught in validation and REFUSED.

    Refused rather than stripped on purpose: the value is the agent's
    payload, and silently mangling it breaks write-then-read-back with no
    way for the caller to notice. (`_derive_preview` strips NUL for the
    opposite reason — a preview is a search aid, not the data.)

    Found by the hypothesis property in test_fidelity_corpus.
    """
    with pytest.raises(ValueError, match="control characters"):
        apply_edits(
            values_workbook,
            [_edit("Q3", "A1", [[bad]])],
            content_type=XLSX,
            name="q.xlsx",
        )


def test_tab_and_newline_are_storable(values_workbook):
    """The refusal must be narrow: tab, newline and CR are legal in a cell
    and a blanket control-character ban would reject ordinary multi-line
    text."""
    out = apply_edits(
        values_workbook,
        [_edit("Q3", "A1", [["line one\nline two\tcol"]])],
        content_type=XLSX,
        name="q.xlsx",
    )
    assert "line two" in read_grid(out, content_type=XLSX, name="q.xlsx")["Q3"][0][0]


@pytest.mark.parametrize(
    "text",
    ["=0", "=SUM(A1:A2)", "=> see notes", "=", "==", "=/=", "=x+1"],
)
def test_a_value_edit_never_becomes_a_formula(values_workbook, text):
    """A VALUE write must stay a value, whatever character it starts with.

    openpyxl infers a formula from a leading `=` on assignment, so writing the
    string `=0` stored a formula. Two things followed, both silent:

    1. It read back as `None`. A formula written programmatically has no
       cached result — only Excel computes one — and `read_grid` asks for
       cached results. The write returned success and the value was gone.

    2. The workbook was left containing a formula, so the next `read_index`
       answered `blocked / FORMULAS_PRESENT`. One `=0` permanently locked the
       file out of the very API that wrote it.

    This module has no formula-writing feature to protect: a workbook with a
    formula is refused before a session opens (see the module docstring), so
    a value edit manufacturing one is never what the caller asked for.
    """
    out = apply_edits(
        values_workbook, [_edit("Q3", "A2", [[text]])], content_type=XLSX, name="q.xlsx"
    )
    assert read_grid(out, content_type=XLSX, name="q.xlsx")["Q3"][1][0] == text


def test_writing_equals_text_leaves_the_workbook_editable(values_workbook):
    """The second consequence, asserted on its own.

    Round-tripping the value is not enough — the file must still be editable
    afterwards, or the next session is refused on damage this one caused.
    """
    from agentdrive.sheets.workbook import read_index

    out = apply_edits(
        values_workbook, [_edit("Q3", "A2", [["=0"]])], content_type=XLSX, name="q.xlsx"
    )
    index = read_index(out, content_type=XLSX, name="q.xlsx")
    assert index.editability.status == "ok"
    assert all(sheet.has_formulas is False for sheet in index.sheets)


def test_equals_text_survives_a_second_edit_round_trip(values_workbook):
    """Replay is the real usage: completion applies the whole log at once, and
    a later session edits the result again."""
    once = apply_edits(
        values_workbook, [_edit("Q3", "A2", [["=0"]])], content_type=XLSX, name="q.xlsx"
    )
    twice = apply_edits(
        once, [_edit("Q3", "B2", [["=SUM(A1:A2)"]])], content_type=XLSX, name="q.xlsx"
    )
    grid = read_grid(twice, content_type=XLSX, name="q.xlsx")["Q3"]
    assert grid[1][0] == "=0"
    assert grid[1][1] == "=SUM(A1:A2)"
