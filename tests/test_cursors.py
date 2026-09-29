"""Unit tests for the opaque-cursor helpers (`api.cursors`).

Pure functions — no DB. Covers the round-trip (incl. non-ASCII payloads),
malformed-container rejection, and the typed field accessors that turn a
forged/corrupt cursor field into `BadCursor` (→ 400 INVALID_CURSOR) instead of
a downstream 500.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentdrive.api.cursors import (
    BadCursor,
    cursor_int,
    cursor_str,
    cursor_ts,
    decode_cursor,
    encode_cursor,
)

# ── round-trip + container validation ───────────────────────────────────────


def test_roundtrip_basic():
    payload = {"after_path": "reports/q3.md", "after_version_number": 7}
    assert decode_cursor(encode_cursor(payload)) == payload


def test_roundtrip_non_ascii():
    # utf-8 fix: a cursor field with non-ASCII content must not crash encode.
    payload = {"after_path": "rapports/q3-café-señor-日本.md"}
    assert decode_cursor(encode_cursor(payload)) == payload


def test_decode_none_is_none():
    assert decode_cursor(None) is None
    assert decode_cursor("") is None


@pytest.mark.parametrize("bad", ["!!!notbase64!!!", "Zm9v", "W10="])  # garbage / "foo" / "[]"
def test_decode_malformed_raises_badcursor(bad):
    with pytest.raises(BadCursor):
        decode_cursor(bad)


# ── typed accessors: absent → None, wrong type/format → BadCursor ───────────


def test_cursor_str():
    assert cursor_str({"k": "v"}, "k") == "v"
    assert cursor_str({}, "k") is None
    with pytest.raises(BadCursor):
        cursor_str({"k": 5}, "k")


def test_cursor_int():
    assert cursor_int({"k": 7}, "k") == 7
    assert cursor_int({}, "k") is None
    with pytest.raises(BadCursor):
        cursor_int({"k": "7"}, "k")        # string, not int
    with pytest.raises(BadCursor):
        cursor_int({"k": True}, "k")        # bool is not a valid int cursor


def test_cursor_int_rejects_out_of_int32_range():
    # int4 column — an arbitrary-precision int passes isinstance but would
    # raise asyncpg DataError; reject it here as BadCursor (→ 400).
    assert cursor_int({"k": 2**31 - 1}, "k") == 2**31 - 1   # max int32 ok
    with pytest.raises(BadCursor):
        cursor_int({"k": 2**31}, "k")        # one past int32
    with pytest.raises(BadCursor):
        cursor_int({"k": 10**20}, "k")


def test_cursor_ts():
    iso = "2026-06-21T12:00:00+00:00"
    assert cursor_ts({"k": iso}, "k") == datetime(2026, 6, 21, 12, tzinfo=UTC)
    assert cursor_ts({}, "k") is None
    with pytest.raises(BadCursor):
        cursor_ts({"k": "last tuesday"}, "k")   # unparseable
    with pytest.raises(BadCursor):
        cursor_ts({"k": 123}, "k")              # not a string
    with pytest.raises(BadCursor):
        cursor_ts({"k": "2026-06-21T12:00:00"}, "k")  # missing RFC 3339 offset
    with pytest.raises(BadCursor):
        cursor_ts({"k": "9999-12-31T23:59:59"}, "k")  # overflows timestamptz (upper)
    with pytest.raises(BadCursor):
        cursor_ts({"k": "0001-01-01T00:00:00+14:00"}, "k")  # underflows (lower)


def test_accessors_reject_forged_cursor_field_end_to_end():
    # A well-formed (decodable) cursor carrying a wrong-typed field is the
    # exact crash class this closes — must be BadCursor, not a raw ValueError
    # from fromisoformat / asyncpg downstream.
    forged = encode_cursor({"before": "garbage", "after_version_number": "nope"})
    payload = decode_cursor(forged)
    with pytest.raises(BadCursor):
        cursor_ts(payload, "before")
    with pytest.raises(BadCursor):
        cursor_int(payload, "after_version_number")
