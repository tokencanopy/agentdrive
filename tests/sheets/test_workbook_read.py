"""Workbook read adapter (design §5.2/§5.3, plan Task 3).

The load-bearing behaviour here is the EDITABILITY verdict. openpyxl holds
formulas or their cached results, never both, and saving discards every cached
value workbook-wide — so a workbook containing any formula must be refused
before a session opens, not after the agent has done its work. These tests are
what keep that refusal honest.
"""

from __future__ import annotations

import pytest

from agentdrive.sheets.workbook import Unparseable, read_grid, read_index

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_index_reports_sheets_in_workbook_order(values_workbook):
    index = read_index(values_workbook, content_type=XLSX, name="q.xlsx")
    assert index.format == "xlsx"
    assert [s.name for s in index.sheets] == ["Q3", "Notes"]
    assert [s.index for s in index.sheets] == [0, 1]
    assert index.sheets[0].rows == 3
    assert index.sheets[0].columns == 3
    assert index.editability.status == "ok"
    assert index.editability.reason is None


def test_index_counts_cells_across_every_sheet(values_workbook):
    """The session cap is whole-workbook, not per-sheet — a forty-tab model
    reaches it at 5,000 cells a tab."""
    index = read_index(values_workbook, content_type=XLSX, name="q.xlsx")
    assert index.cell_count == (3 * 3) + (2 * 1)


def test_a_formula_anywhere_blocks_the_whole_workbook(formula_workbook):
    index = read_index(formula_workbook, content_type=XLSX, name="m.xlsx")
    assert index.editability.status == "blocked"
    assert index.editability.reason == "FORMULAS_PRESENT"
    assert index.sheets[0].has_formulas is True


def test_text_that_merely_starts_with_equals_is_not_a_formula(
    literal_equals_workbook,
):
    """`=> see notes` is prose, and prose must not block a workbook.

    The verdict used to be a leading-`=` string test, which cannot tell a
    formula from text that begins with the same character. A user typing
    `=> see notes`, `=/=`, or a column of comparison operators had their
    workbook refused for editing with FORMULAS_PRESENT, which is both wrong
    and unexplainable to them: nothing in the file is a formula.
    """
    index = read_index(literal_equals_workbook, content_type=XLSX, name="n.xlsx")
    assert index.sheets[0].has_formulas is False
    assert index.editability.status == "ok"
    assert index.editability.reason is None

    # And it is still just text on the way out.
    grid = read_grid(literal_equals_workbook, content_type=XLSX, name="n.xlsx")
    assert grid["Q3"][1][0] == "=> see notes"


def test_an_array_formula_blocks_the_workbook(array_formula_workbook):
    """The dangerous direction: a formula the old test could not see.

    An array formula's `cell.value` is an `ArrayFormula` object, not a string,
    so `isinstance(value, str) and value.startswith("=")` never matched it.
    The workbook was reported editable, and saving it would blank every cached
    value in the file — the exact loss the whole-workbook refusal exists to
    prevent. A false NEGATIVE here destroys data; a false positive only
    annoys.
    """
    index = read_index(array_formula_workbook, content_type=XLSX, name="m.xlsx")
    assert index.sheets[0].has_formulas is True
    assert index.editability.status == "blocked"
    assert index.editability.reason == "FORMULAS_PRESENT"


def test_macro_enabled_workbooks_are_blocked(values_workbook):
    """Refused on the filename alone: we never open a macro container."""
    index = read_index(values_workbook, content_type=XLSX, name="m.xlsm")
    assert index.editability.status == "blocked"
    assert index.editability.reason == "UNSUPPORTED_FEATURES"


def test_grid_uses_native_types(values_workbook):
    grid = read_grid(values_workbook, content_type=XLSX, name="q.xlsx")
    assert grid["Q3"][0] == ["Region", "Q3", "Q4"]
    assert grid["Q3"][1] == ["EMEA", 1200, 1450]


def test_empty_cell_is_none_never_empty_string(values_workbook):
    grid = read_grid(values_workbook, content_type=XLSX, name="q.xlsx")
    assert grid["Q3"][2][2] is None


def test_dates_render_as_rfc3339_strings(dated_workbook):
    grid = read_grid(dated_workbook, content_type=XLSX, name="d.xlsx")
    assert grid["Dates"][1][0].startswith("2026-08-22")
    assert grid["Dates"][2][0].startswith("2026-08-22T14:30")


def test_csv_cells_are_always_strings():
    """No type inference on csv, ever. It is how a 20-digit order id becomes
    1.2346E+19, and the failure is silent and unrecoverable."""
    data = b"id,qty\n00123,7\n12345678901234567890,8\n"
    grid = read_grid(data, content_type="text/csv", name="d.csv")
    assert grid["Sheet1"][1] == ["00123", "7"]
    assert grid["Sheet1"][2][0] == "12345678901234567890"


def test_tsv_splits_on_tabs():
    grid = read_grid(b"a\tb\n1\t2\n", content_type="text/tab-separated-values", name="d.tsv")
    assert grid["Sheet1"][1] == ["1", "2"]


def test_csv_is_always_editable():
    index = read_index(b"a,b\n1,2\n", content_type="text/csv", name="d.csv")
    assert index.format == "csv"
    assert index.editability.status == "ok"
    assert [s.name for s in index.sheets] == ["Sheet1"]


def test_unicode_and_spaced_sheet_names_survive(unicode_sheet_workbook):
    """Excel forbids `\\ / ? * : [ ]` in sheet titles, so the encoding hazard
    is spaces and non-ASCII, not slashes."""
    index = read_index(unicode_sheet_workbook, content_type=XLSX, name="u.xlsx")
    assert index.sheets[0].name == "予算 — Q3 plan"


@pytest.mark.parametrize(
    "data,content_type,name",
    [
        (b"not a workbook", XLSX, "x.xlsx"),
        (b"", XLSX, "empty.xlsx"),
        (b"id,qty\n1,2\n", XLSX, "lie.xlsx"),  # declared type disagrees with bytes
    ],
)
def test_unreadable_bytes_raise_unparseable(data, content_type, name):
    """Never a bare library exception into a request handler."""
    with pytest.raises(Unparseable):
        read_index(data, content_type=content_type, name=name)


def test_content_type_wins_over_the_filename(values_workbook):
    """`kind_for`'s rule: the declared media type is authoritative, because
    uploaders routinely send the wrong extension."""
    index = read_index(values_workbook, content_type=XLSX, name="mislabelled.csv")
    assert index.format == "xlsx"
