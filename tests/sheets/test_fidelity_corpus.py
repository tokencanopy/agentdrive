"""Round-trip fidelity (design §12.4, plan Task 4).

The dangerous failure in this feature is not a wrong status code. It is
**silent corruption of parts of the workbook nobody edited** — an adapter that
drops a sheet, reorders tabs, or blanks a column still passes every
behavioural test in `test_workbook_write.py`, because those only assert the
cells the test touched.

So: representative workbook shapes, one edit each, assert everything ELSE is
identical. Plus a hypothesis property over random edit sequences, which
explores orderings and overlaps nobody writes by hand.
"""

from __future__ import annotations

import io

import openpyxl
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agentdrive.sheets.a1 import Rect, column_label, parse_range
from agentdrive.sheets.workbook import Edit, apply_edits, read_grid, read_index

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(sheets: dict[str, list[list[object]]]) -> bytes:
    """Local builder rather than the `make_xlsx` fixture: hypothesis refuses
    function-scoped fixtures inside `@given`, and rightly — they are not reset
    between generated inputs. This is pure, so calling it directly is both
    correct and simpler than suppressing the health check."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CORPUS: dict[str, dict[str, list[list[object]]]] = {
    "multi_sheet": {"A": [[1, 2], [3, 4]], "B": [["x"]], "C": [["only"]]},
    "wide": {
        "W": [[f"c{i}" for i in range(120)], list(range(120))],
    },
    "deep": {"D": [[i, i * 2] for i in range(1, 801)]},
    "unicode": {"予算 — Q3 plan": [["名前", "値"], ["東京", 42]]},
    "single_cell": {"S": [["only"]]},
    "sparse": {"P": [["a", None, "c"], [None, None, None], ["d", None, None]]},
    "mixed_types": {"M": [["s", 1, 1.5, True], ["t", -2, 0.0, False]]},
}


@pytest.mark.parametrize("case", sorted(CORPUS))
def test_one_edit_preserves_everything_else(case, make_xlsx):
    data = make_xlsx(CORPUS[case])
    before_index = read_index(data, content_type=XLSX, name=f"{case}.xlsx")
    before = read_grid(data, content_type=XLSX, name=f"{case}.xlsx")
    target = before_index.sheets[0].name

    out = apply_edits(
        data,
        [Edit(sheet=target, rect=parse_range("A1"), values=[["EDITED"]])],
        content_type=XLSX,
        name=f"{case}.xlsx",
    )

    after_index = read_index(out, content_type=XLSX, name=f"{case}.xlsx")
    after = read_grid(out, content_type=XLSX, name=f"{case}.xlsx")

    # Sheet identity: count, ORDER, and names. A reordered tab strip is a
    # corruption users notice immediately and no cell assertion catches.
    assert [s.name for s in after_index.sheets] == [s.name for s in before_index.sheets]
    assert after[target][0][0] == "EDITED"

    for sheet, rows in before.items():
        assert len(after[sheet]) == len(rows), f"{case}: {sheet} row count changed"
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                if (sheet, r, c) == (target, 0, 0):
                    continue
                assert after[sheet][r][c] == value, f"{case}: {sheet}!r{r}c{c} changed"


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    edits=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=11),  # row
            st.integers(min_value=0, max_value=5),  # column
            st.one_of(
                # Control characters are excluded because they are NOT
                # storable in a spreadsheet cell and are refused upstream;
                # test_workbook_write covers that refusal explicitly. This
                # property is about values that CAN round-trip.
                st.text(
                    alphabet=st.characters(blacklist_categories=("Cc", "Cs")),
                    max_size=8,
                ),
                st.integers(-999, 999),
                st.none(),
            ),
        ),
        min_size=1,
        max_size=25,
    )
)
def test_random_edit_sequences_match_an_independent_expectation(edits):
    """Replay is deterministic given the log, so the expectation can be
    computed independently and compared. Hypothesis explores overlapping
    writes, repeated cells and clears in orders nobody would enumerate."""
    base = {"S": [[f"r{r}c{c}" for c in range(6)] for r in range(12)]}
    data = _xlsx(base)

    expected = [row[:] for row in base["S"]]
    log: list[Edit] = []
    for row, col, value in edits:
        expected[row][col] = value
        log.append(
            Edit(sheet="S", rect=Rect(row, col, row, col), values=[[value]])
        )

    out = apply_edits(data, log, content_type=XLSX, name="p.xlsx")
    got = read_grid(out, content_type=XLSX, name="p.xlsx")["S"]

    # openpyxl stores "" as an empty cell, which reads back as None. That is
    # the format's behaviour, not the adapter's, so normalize both sides.
    def norm(v: object) -> object:
        return None if v == "" else v

    for r, row in enumerate(expected):
        for c, value in enumerate(row):
            assert norm(got[r][c]) == norm(value), f"{column_label(c)}{r + 1}"
