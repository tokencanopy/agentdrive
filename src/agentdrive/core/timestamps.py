"""Timestamp bounds and wire serialization shared across layers.

Postgres `timestamptz` binding goes through a Python `datetime` (range
year 1..9999); asyncpg normalizes the value to UTC at bind time, and a
near-boundary value can shift PAST those limits — e.g. ``9999-...-05:00``
→ year 10000, or ``0001-...+14:00`` → year 0 — raising an unhandled
`DataError` (500). Both ends must be rejected BEFORE the query.

The shift is at most ±24h, so only the BOUNDARY years (1 and 9999) can be
carried out of range; years 2..9998 are always safe. We therefore reject
the two boundary years outright — pagination/filter timestamps are always
near-present, so this over-rejection is moot in practice.

Lives in `core` (a DB-level concern) so `api`, `mcp_server`, `web`, and
`core` callers share ONE definition instead of re-deriving the bound.

This module also owns `to_rfc3339()` — the single canonical serializer
for hand-built response timestamps (RFC3339 UTC with the "Z"
designator), matching what Pydantic response models already emit.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Boundary years a ±24h tz shift can carry out of timestamptz range.
MIN_TS_YEAR = 1
MAX_TS_YEAR = 9999


def ts_out_of_range(dt: datetime) -> bool:
    """True if `dt` could overflow Postgres timestamptz binding (either end)."""
    return dt.year <= MIN_TS_YEAR or dt.year >= MAX_TS_YEAR


def to_rfc3339(dt: datetime) -> str:
    """Serialize an aware datetime as RFC3339 UTC with the Z designator
    and fixed 6-digit fractional seconds — the one wire format for every
    hand-built timestamp field (Pydantic response models already emit Z;
    this keeps manual dict-building endpoints identical)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
