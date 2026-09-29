"""Sealed cursors: the guarantees that make paginated reads honest.

The legacy cursor was base64 JSON — readable, editable, and replayable
anywhere. Three contract clauses close that:

  * §6.6 the cursor binds the normalized query, filters, and mode "so filters
    cannot be changed between pages";
  * §6.7 + D14 the change cursor is "a sealed token binding drive id and
    cursor kind";
  * §7.1 a resource outside the token's reach is a 404, never a leak — which
    a cursor naming another drive would defeat.

None of those survive an unauthenticated blob. Every test here is a thing a
caller could otherwise do by editing base64.
"""

from __future__ import annotations

import base64
import json

import pytest

from agentdrive.core import cursors

DRIVE = "drv_00000000000000a1"
OTHER = "drv_00000000000000b1"


def test_roundtrip_returns_the_position():
    token = cursors.seal("entries", DRIVE, {"after_id": "art_0000000000000a01"})
    assert cursors.unseal("entries", DRIVE, token) == {
        "after_id": "art_0000000000000a01"
    }


def test_cursor_carries_its_prefix():
    """§4.1: cursors use a distinct opaque prefix, like every other handle."""
    assert cursors.seal("entries", DRIVE, {"after_id": "x"}).startswith("cur_")


def test_tampering_is_rejected():
    """The whole point of sealing: an edited payload must not verify."""
    token = cursors.seal("entries", DRIVE, {"after_id": "art_0000000000000a01"})
    body = token.removeprefix("cur_")
    padded = body + "=" * (-len(body) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    payload["p"]["after_id"] = "art_ffffffffffffffff"
    forged = "cur_" + base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()

    with pytest.raises(cursors.BadCursor):
        cursors.unseal("entries", DRIVE, forged)


def test_a_cursor_is_bound_to_its_kind():
    """An `entries` position replayed against `changes` would resume a
    completely different sequence at a meaningless offset."""
    token = cursors.seal("entries", DRIVE, {"after_id": "art_0000000000000a01"})
    with pytest.raises(cursors.BadCursor):
        cursors.unseal("changes", DRIVE, token)


def test_a_cursor_is_bound_to_its_drive():
    """§7.1's anti-enumeration rule: a cursor from one drive must not read
    another, or pagination becomes a cross-drive oracle."""
    token = cursors.seal("entries", DRIVE, {"after_id": "art_0000000000000a01"})
    with pytest.raises(cursors.BadCursor):
        cursors.unseal("entries", OTHER, token)


def test_filters_cannot_change_between_pages():
    """§6.6 states this directly. The filter set is sealed alongside the
    position, so page 2 cannot quietly widen what page 1 was allowed to see.
    """
    token = cursors.seal(
        "search", DRIVE, {"after_score": 0.5}, bound={"mode": "lexical", "q": "budget"}
    )
    assert cursors.unseal(
        "search", DRIVE, token, bound={"mode": "lexical", "q": "budget"}
    ) == {"after_score": 0.5}

    with pytest.raises(cursors.BadCursor):
        cursors.unseal(
            "search", DRIVE, token, bound={"mode": "semantic", "q": "budget"}
        )
    with pytest.raises(cursors.BadCursor):
        cursors.unseal("search", DRIVE, token, bound={"mode": "lexical", "q": "salary"})


def test_bound_context_is_order_insensitive():
    """Two callers building the same filter dict in a different order must
    produce the same seal, or pagination breaks on dict iteration order."""
    a = cursors.seal("search", DRIVE, {"n": 1}, bound={"mode": "lexical", "q": "x"})
    b = cursors.seal("search", DRIVE, {"n": 1}, bound={"q": "x", "mode": "lexical"})
    assert a == b


def test_garbage_is_rejected_not_ignored():
    """A malformed cursor is a client bug worth surfacing; silently starting
    from the beginning would hide it and re-deliver the whole collection."""
    for junk in ("", "cur_", "cur_!!!!", "not-a-cursor", "cur_" + "A" * 40):
        with pytest.raises(cursors.BadCursor):
            cursors.unseal("entries", DRIVE, junk)


def test_the_same_cursor_reads_the_same_page_twice():
    """§6.7's observable form of at-least-once: "presenting the same cursor
    twice returns the same page". Unsealing must be pure — no server-side
    position to advance, which is what makes B2 unrepresentable."""
    token = cursors.seal("changes", DRIVE, {"after_sequence": 41})
    first = cursors.unseal("changes", DRIVE, token)
    second = cursors.unseal("changes", DRIVE, token)
    assert first == second == {"after_sequence": 41}


def _raw_token(body: dict) -> str:
    """Build a cursor whose payload is `body` verbatim, without a valid MAC.

    `seal` only ever writes an ASCII MAC, so reaching the non-ASCII `m` field
    the review found requires forging the token by hand.
    """
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return "cur_" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_every_unverifiable_mac_field_is_a_badcursor_not_a_typeerror():
    """§7.1: *every* unverifiable cursor is `BadCursor` (→ 400 INVALID_CURSOR),
    never a 500. `hmac.compare_digest` refuses non-ASCII `str` operands with a
    `TypeError`, and the guard must contain that — an unauthenticated-shaped
    client must not be able to pick a payload that escapes as an unhandled
    exception. Non-string `m` values pass the body-shape check, so they must
    fail the MAC check like any wrong signature, not blow up inside it."""
    for m in ("é", 123, [], {}, None, True):
        with pytest.raises(cursors.BadCursor):
            cursors.unseal(
                "entries",
                DRIVE,
                _raw_token({"k": "entries", "d": DRIVE, "p": {"after_id": "x"}, "m": m}),
            )
