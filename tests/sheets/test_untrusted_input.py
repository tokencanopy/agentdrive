"""Hostile and malformed workbooks (design §12.5, plan Task 11).

These are correctness tests, not hardening extras. Every case must produce a
BOUNDED REFUSAL — never a hang, never an OOM, never a bare library exception
reaching a request handler as a 500. Two real bugs of exactly that shape have
already been caught here (`zipfile.BadZipFile` escaping the catch, and
`IllegalCharacterError` on save), which is why the surface is enumerated
rather than sampled.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from agentdrive.sheets.workbook import (
    MAX_COMPRESSION_RATIO,
    MAX_UNCOMPRESSED_BYTES,
    Unparseable,
    extract_text,
    read_grid,
    read_index,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _zip_bomb() -> bytes:
    """A small archive declaring an enormous payload.

    Highly compressible zeroes: the central directory states the real
    uncompressed size, so the guard sees it without inflating anything.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * (64 * 1024 * 1024))
    return buf.getvalue()


def _billion_laughs() -> bytes:
    """An entity-expansion attempt inside an otherwise well-formed container."""
    entities = "".join(
        f'<!ENTITY e{i} "&e{i - 1};&e{i - 1};">' for i in range(1, 12)
    )
    payload = (
        '<?xml version="1.0"?>'
        f'<!DOCTYPE r [<!ENTITY e0 "boom">{entities}]>'
        "<worksheet><sheetData>&e11;</sheetData></worksheet>"
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", b"<Types/>")
        zf.writestr("xl/worksheets/sheet1.xml", payload)
    return buf.getvalue()


@pytest.mark.parametrize(
    "data,name",
    [
        (b"not a workbook", "x.xlsx"),
        (b"", "empty.xlsx"),
        (b"id,qty\n1,2\n", "lie.xlsx"),  # declared type disagrees with bytes
        (b"PK\x03\x04truncated", "cut.xlsx"),
    ],
)
def test_malformed_containers_are_bounded_refusals(data, name):
    with pytest.raises(Unparseable):
        read_index(data, content_type=XLSX, name=name)
    with pytest.raises(Unparseable):
        read_grid(data, content_type=XLSX, name=name)


def test_a_truncated_real_workbook_is_refused(values_workbook):
    with pytest.raises(Unparseable):
        read_index(values_workbook[: len(values_workbook) // 2], content_type=XLSX, name="c.xlsx")


def test_a_zip_bomb_is_refused_without_inflating_it():
    """The refusal reads only the central directory, so the payload is never
    expanded — which is the only safe moment to make this decision."""
    with pytest.raises(Unparseable, match="uncompressed|ratio"):
        read_index(_zip_bomb(), content_type=XLSX, name="bomb.xlsx")


def test_an_entity_expansion_attempt_does_not_hang():
    """Whatever the parser does with it, the caller gets a refusal rather
    than an unbounded expansion or a bare library error."""
    with pytest.raises(Unparseable):
        read_index(_billion_laughs(), content_type=XLSX, name="xxe.xlsx")


def test_the_guards_are_both_present_and_finite():
    """Pinned so neither is quietly raised to infinity: an absolute ceiling
    alone is evadable by a small file, and a ratio alone by a large one."""
    assert 0 < MAX_UNCOMPRESSED_BYTES <= 2 * 1024 * 1024 * 1024
    assert 0 < MAX_COMPRESSION_RATIO <= 1_000


def test_extraction_never_raises_on_hostile_input():
    """Search extraction runs on every upload. An unsearchable artifact is
    far better than a failed upload, so this path refuses by returning None."""
    for data in (b"not a workbook", b"", _zip_bomb(), _billion_laughs()):
        assert extract_text(data, content_type=XLSX, name="x.xlsx", limit=1024) is None


def test_extraction_pulls_sheet_names_and_values(values_workbook):
    text = extract_text(values_workbook, content_type=XLSX, name="q.xlsx", limit=16384)
    assert "Q3" in text and "Notes" in text, "sheet names are often the best hit"
    assert "EMEA" in text
    assert "1200" in text


def test_extraction_is_bounded_by_the_limit(make_xlsx):
    big = make_xlsx({"S": [[f"value{i}" for i in range(50)] for _ in range(400)]})
    text = extract_text(big, content_type=XLSX, name="big.xlsx", limit=2048)
    assert text is not None
    assert len(text) <= 2048


def test_extraction_strips_nul():
    """NUL is valid UTF-8 and ILLEGAL in a Postgres text value, so leaving it
    in would take down the whole WRITE — not merely empty the preview. That
    is the failure `_derive_preview` records having hit.

    Exercised through csv rather than xlsx because openpyxl refuses to write
    a NUL cell at all, so an xlsx fixture cannot be built; a csv carrying one
    is the reachable path anyway.
    """
    text = extract_text(
        b"id,note\nx,before\x00after\n",
        content_type="text/csv",
        name="n.csv",
        limit=1024,
    )
    assert "\x00" not in text
    assert "before" in text


def test_a_macro_container_is_never_opened(values_workbook):
    index = read_index(values_workbook, content_type=XLSX, name="m.xlsm")
    assert index.editability.status == "blocked"
    assert index.editability.reason == "UNSUPPORTED_FEATURES"
    assert index.sheets == (), "a macro workbook is refused, not inspected"
