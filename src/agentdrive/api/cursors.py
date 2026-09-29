"""Opaque cursor helpers for paginated v0 endpoints.

Cursors are base64-URL-encoded JSON. Each paginated endpoint defines
its own cursor shape (the fields it needs to resume — e.g., `after_path`
for the path-sorted list, `after_score + after_id` for search results).

Why opaque rather than raw timestamps / IDs:
  * Server can change pagination strategy without breaking clients.
  * Multi-field keyset cursors fit naturally in JSON.
  * Discourages clients from parsing — they treat it as a sentinel.

Why keyset rather than OFFSET:
  * Breaks under insert/delete mid-traversal (page 2 can skip or
    duplicate rows).
  * OFFSET is O(N) on Postgres for deep pages — full scan + skip.
  * Doesn't compose with filters reliably.

Bad-cursor handling: callers see HTTP 400 BAD_CURSOR rather than a
silent fall-through to "start over." Surfacing the error makes
client-side bugs visible early.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from ..core.timestamps import ts_out_of_range


class BadCursor(ValueError):
    """Caller-supplied cursor failed validation. REST callers see this
    as 400 BAD_CURSOR; MCP callers see it as a tool-error ValueError.
    Translation lives at the respective entrypoint layer."""


def encode_cursor(payload: dict[str, Any]) -> str:
    """Serialize a cursor payload to an opaque URL-safe string.

    Caller chooses the field set per endpoint — the encoder treats
    the dict as opaque blob. Returns a base64-URL-encoded JSON
    string; never URL-quoted (callers can use it verbatim as a query
    param value)."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # utf-8 (not ascii): a cursor field may legitimately carry a non-ASCII
    # value (e.g. a path/title) — base64 transports the bytes either way,
    # and ascii would raise UnicodeEncodeError. Decode mirrors this.
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(s: str | None) -> dict[str, Any] | None:
    """Inverse of `encode_cursor`. Returns None when no cursor was
    provided.

    Raises `BadCursor` for any malformed input — base64 decode failure,
    JSON parse failure, non-object payload. Falling through to "start
    at the top" silently would hide a real bug."""
    if not s:
        return None
    try:
        # Re-pad — encode strips '=' (URL-safe); urlsafe_b64decode wants
        # the canonical length. Pad with '=' to a multiple of 4.
        padded = s + "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise BadCursor(f"malformed cursor: {e}") from e
    if not isinstance(payload, dict):
        raise BadCursor("cursor must encode a JSON object")
    return payload


def clamp_limit(limit: int | None, max_limit: int = 100, default: int = 50) -> int:
    """Normalize a caller-supplied page size to `[1, max_limit]`.

    Clamp, don't reject (contract-audit C-2 / list-pagination design §2):
    an over-limit request returns the max page rather than a 422/INVALID_LIMIT,
    keeping REST and MCP surfaces aligned. `None` (param omitted) → `default`;
    `< 1` → 1; `> max_limit` → `max_limit`."""
    if limit is None:
        return default
    return max(1, min(limit, max_limit))


# ---------------------------------------------------------------------------
# Typed field accessors. Each paginated endpoint reads its own keyset fields
# out of the decoded (opaque) payload. These validate the field's TYPE/format
# and raise `BadCursor` on a mismatch, so a forged or corrupt cursor surfaces
# as 400 BAD_CURSOR (REST) / a tool-error (MCP) — never as a 500 (a bad value
# reaching `datetime.fromisoformat` or asyncpg) or a silent wrong-page read.
# Absent key ⇒ None (the endpoint then starts from the top, as before).
# ---------------------------------------------------------------------------


def cursor_str(payload: dict[str, Any], key: str) -> str | None:
    v = payload.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise BadCursor(f"cursor field {key!r} must be a string")
    return v


# Every int cursor field maps to a Postgres `int4` column (e.g. version_number);
# an out-of-int32 value passes isinstance(int) but raises an unhandled asyncpg
# DataError at the query → guard the RANGE here, not just the type.
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1

# Timestamp range bound lives in core.timestamps (shared with the REST query
# params + MCP date args); imported above as `ts_out_of_range`.


def cursor_int(payload: dict[str, Any], key: str) -> int | None:
    v = payload.get(key)
    if v is None:
        return None
    # bool is an int subclass in Python — reject JSON true/false explicitly.
    if isinstance(v, bool) or not isinstance(v, int):
        raise BadCursor(f"cursor field {key!r} must be an integer")
    if not (_INT32_MIN <= v <= _INT32_MAX):
        raise BadCursor(f"cursor field {key!r} is out of range")
    return v


def cursor_ts(payload: dict[str, Any], key: str) -> datetime | None:
    v = payload.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise BadCursor(f"cursor field {key!r} must be an ISO-8601 timestamp string")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError as e:
        raise BadCursor(
            f"cursor field {key!r} is not a valid ISO-8601 timestamp"
        ) from e
    if dt.utcoffset() is None:
        raise BadCursor(f"cursor field {key!r} must include a UTC offset")
    if ts_out_of_range(dt):
        raise BadCursor(f"cursor field {key!r} timestamp is out of range")
    return dt
