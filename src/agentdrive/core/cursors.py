"""Sealed pagination cursors.

A cursor carries the client's position and nothing on the server records it
(D14). That is what makes the change feed at-least-once rather than
at-most-once: re-presenting a cursor re-delivers the same page, because there
is no server-side position that could have advanced past a response the client
never received. Blocker B2 is not fixed here so much as made unrepresentable
-- there is no table to write a delivered-vs-acknowledged position into.

The cost of putting the position in the client's hands is that the client can
edit it. So every cursor is sealed with an HMAC over four things:

    kind | drive_id | bound context | position

and `unseal` re-derives that MAC from the caller's *expected* kind, drive, and
context. A mismatch in any of them fails closed. Concretely that stops:

  * editing the position to page into rows a filter excluded;
  * replaying an `entries` cursor against `changes`, resuming a different
    sequence at a meaningless offset;
  * carrying a cursor from one drive to another, which would turn pagination
    into the cross-drive enumeration oracle §7.1's 404 rule exists to prevent;
  * changing `mode` or the query between pages, which §6.6 forbids directly.

The token is *signed, not encrypted*. Contents are readable by design -- a
keyset position is not a secret, and readability makes support and debugging
possible. What a caller cannot do is change it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from ..config import settings

_PREFIX = "cur_"

# Truncated to 128 bits. Full SHA-256 doubles the token for no attacker-facing
# benefit: forging one needs a preimage under an unknown key, and 128 bits is
# far past the point where that is the weakest link.
_MAC_BYTES = 16


class BadCursor(ValueError):
    """A cursor that is malformed, tampered with, or bound to something else.

    Deliberately one exception for all of those. Telling a caller *which* of
    kind, drive, or context mismatched would confirm that some other drive or
    collection exists, which is the leak §7.1 closes with a uniform 404.
    """


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(value: Any) -> str:
    """Order-insensitive rendering, so an equivalent dict seals identically.

    Two handlers building the same filter set in a different order must
    produce the same MAC, or pagination would break on dict iteration order.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _signing_key() -> bytes:
    return settings.session_secret.encode("utf-8")


def _mac(kind: str, drive_id: str, bound: dict[str, Any] | None, position: Any) -> str:
    # Length-prefixed rather than delimiter-joined: with a plain separator a
    # crafted kind or drive id could shift a byte across the boundary and
    # collide with a different tuple.
    parts = [kind, drive_id, _canonical(bound or {}), _canonical(position)]
    payload = b"".join(f"{len(p)}:".encode() + p.encode("utf-8") for p in parts)
    return _b64e(hmac.new(_signing_key(), payload, hashlib.sha256).digest()[:_MAC_BYTES])


def seal(
    kind: str,
    drive_id: str,
    position: dict[str, Any],
    *,
    bound: dict[str, Any] | None = None,
) -> str:
    """Mint an opaque cursor for `position`.

    `kind` names the collection (`entries`, `changes`, `search`, ...).
    `bound` is the context the cursor must not outlive -- the normalized
    query, filters and mode for search (§6.6). Anything a later page must not
    be allowed to change belongs in `bound`, not in `position`.
    """
    body = {"k": kind, "d": drive_id, "p": position, "m": _mac(kind, drive_id, bound, position)}
    return _PREFIX + _b64e(_canonical(body).encode("utf-8"))


def unseal(
    kind: str,
    drive_id: str,
    token: str,
    *,
    bound: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recover the position from `token`, or raise `BadCursor`.

    Pure: the same token unseals to the same position every time. That is
    §6.7's "presenting the same cursor twice returns the same page" holding by
    construction rather than by a handler remembering not to advance anything.
    """
    if not isinstance(token, str) or not token.startswith(_PREFIX):
        raise BadCursor("cursor is malformed")
    try:
        body = json.loads(_b64d(token[len(_PREFIX) :]))
    except Exception as e:
        raise BadCursor("cursor is malformed") from e
    if not isinstance(body, dict) or not {"k", "d", "p", "m"} <= body.keys():
        raise BadCursor("cursor is malformed")

    expected = _mac(kind, drive_id, bound, body["p"])
    # `compare_digest`, not `==`: the comparison is over attacker-supplied
    # input, and an early-exit compare leaks the MAC a byte at a time.
    # `compare_digest` also refuses non-ASCII `str` operands outright, and a
    # non-string `m` would be mixed-type — either of which would otherwise
    # escape the uniform `BadCursor` contract as an unhandled `TypeError`
    # (a 500) on an unauthenticated-shaped request. `seal` only ever writes
    # an ASCII MAC, so rejecting those inputs is never a false negative.
    try:
        valid = isinstance(body["m"], str) and hmac.compare_digest(expected, body["m"])
    except TypeError:
        valid = False
    if not valid:
        raise BadCursor("cursor is not valid for this request")
    # Belt and braces. The MAC already covers kind and drive, so a mismatch
    # here is unreachable -- but it means a future change to the MAC input
    # cannot silently drop the binding without this failing too.
    if body["k"] != kind or body["d"] != drive_id:
        raise BadCursor("cursor is not valid for this request")

    position = body["p"]
    if not isinstance(position, dict):
        raise BadCursor("cursor is malformed")
    return position
