"""`_derive_preview` — what counts as text, and what can actually be stored.

Pure unit tests, deliberately in their own module: `test_v0_artifacts.py`
carries a file-level `pytest.mark.asyncio`, which warns on every sync test.
The API-level proofs for the same defect live there, next to the create and
append paths they exercise.
"""

from __future__ import annotations

from agentdrive.core.v0_artifacts import _derive_preview


def test_drops_nul_but_keeps_surrounding_text():
    """NUL decodes as UTF-8 but cannot be bound to a Postgres text column, so
    it is stripped rather than allowed to fail the enclosing write."""
    assert _derive_preview(b"alpha\x00beta") == "alphabeta"


def test_none_when_only_nul():
    """All-NUL is not text. The emptiness check has to run AFTER the strip, or
    this stores an empty preview instead of none."""
    assert _derive_preview(b"\x00\x00\x00\x00") is None


def test_none_for_undecodable_binary():
    """The pre-existing binary path is unchanged: 0xFF is not valid UTF-8."""
    assert _derive_preview(b"\x89PNG\r\n\x1a\n\xff\xd8") is None


def test_plain_text_is_unchanged():
    assert _derive_preview(b"hello world") == "hello world"


def test_whitespace_only_is_none():
    assert _derive_preview(b"  \n\t ") is None


def test_ascii_range_utf16le_reads_as_its_text():
    """A concrete case the strip turns from a hard 400 into a useful preview:
    UTF-16LE over ASCII is a NUL after every character."""
    assert _derive_preview("hi".encode("utf-16-le")) == "hi"
