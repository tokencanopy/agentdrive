"""Range validation for FastAPI datetime query params.

A parseable-but-absurd datetime (year >= 9999) overflows Postgres
timestamptz at query time → an unhandled 500. The `BoundedDatetime` param
type rejects it during request validation (422), consistent with how
FastAPI already rejects a malformed datetime param. Covers the
`events?since/before` and `search`/`find` `updated_after/before` params.
"""

from __future__ import annotations

from datetime import UTC

OVERFLOW = "9999-12-31T23:59:59"
# Year-1 local + positive tz offset UTC-shifts below timestamptz min → 500
# unless the lower bound is guarded too.
UNDERFLOW = "0001-01-01T00:00:00+14:00"












# ── the two fail-open ISO parsers: overflow date is dropped, not crashed ────


def test_ts_out_of_range_guards_both_ends():
    from datetime import datetime, timedelta, timezone

    from agentdrive.core.timestamps import ts_out_of_range

    assert ts_out_of_range(datetime(9999, 1, 1)) is True                    # upper
    assert ts_out_of_range(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14)))) is True  # lower
    assert ts_out_of_range(datetime(2026, 6, 21, tzinfo=UTC)) is False             # normal
