"""A parsed-grid memo keyed by immutable version id.

The base grid behind an edit session is **not stored** (design §6): it is
derivable from a version, and a version's bytes cannot change. That makes this
the rare cache whose key can never be invalidated — no staleness to reason
about, no invalidation path to get wrong, and a miss costs one object read
plus one parse.

Losing an entry is a latency event and never a correctness one, which is what
makes an in-process LRU the right shape: no shared store, no network, nothing
to operate. If cross-instance cold parses ever show up in traces, this is the
seam a shared cache slots behind.

Deliberately a plain memo — `get` / `store` — rather than a `get_or_parse`
that owns a loader. Callers here are async (the bytes come from object
storage) and a cache that takes a coroutine would have to hold its lock across
an await, which is how a cache becomes a bottleneck. Parsing stays outside.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from .workbook import Value

# Small on purpose. A session reads a handful of ranges and then completes;
# holding many workbooks resident buys nothing and costs memory on an
# instance that is also serving everything else.
MAX_ENTRIES = 8

Grid = dict[str, list[list[Value]]]

_lock = threading.Lock()
_entries: OrderedDict[str, Grid] = OrderedDict()


def get(version_id: str) -> Grid | None:
    """The parsed grid for `version_id`, or None. Never stale — the key is
    an immutable version."""
    with _lock:
        hit = _entries.get(version_id)
        if hit is not None:
            _entries.move_to_end(version_id)
        return hit


def store(version_id: str, grid: Grid) -> Grid:
    """Memoize `grid` and return it, evicting the least recently used."""
    with _lock:
        _entries[version_id] = grid
        _entries.move_to_end(version_id)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)
    return grid


def clear() -> None:
    """Drop everything. For tests and for a deliberate operational reset."""
    with _lock:
        _entries.clear()
