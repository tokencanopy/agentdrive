"""Parsed-grid memo (design §6, plan Task 5).

The property that matters: the key is an immutable version id, so a hit can
never be stale and a miss can never be wrong. These pin both halves — that it
caches, and that losing an entry degrades to a re-parse rather than an error.
"""

from __future__ import annotations

import threading

import pytest

from agentdrive.sheets import cache
from agentdrive.sheets.workbook import read_grid

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _clear():
    cache.clear()
    yield
    cache.clear()


def _grid(data: bytes) -> dict:
    return read_grid(data, content_type=XLSX, name="q.xlsx")


def test_a_miss_is_none_and_a_stored_grid_comes_back(values_workbook):
    assert cache.get("ver_0000000000000001") is None
    grid = cache.store("ver_0000000000000001", _grid(values_workbook))
    assert cache.get("ver_0000000000000001") is grid
    assert grid["Q3"][0][0] == "Region"


def test_distinct_versions_do_not_collide(values_workbook):
    cache.store("ver_0000000000000001", _grid(values_workbook))
    assert cache.get("ver_0000000000000002") is None


def test_eviction_degrades_to_a_miss_never_an_error(values_workbook):
    """Losing an entry is a latency event, not a correctness one — the
    property that lets this be in-process with no operational story."""
    grid = _grid(values_workbook)
    for i in range(cache.MAX_ENTRIES + 3):
        cache.store(f"ver_{i:016d}", grid)

    assert cache.get("ver_0000000000000000") is None  # evicted, not corrupt
    assert cache.get(f"ver_{cache.MAX_ENTRIES + 2:016d}") is grid


def test_the_memo_is_bounded(values_workbook):
    grid = _grid(values_workbook)
    for i in range(cache.MAX_ENTRIES * 3):
        cache.store(f"ver_{i:016d}", grid)
    assert len(cache._entries) == cache.MAX_ENTRIES


def test_reading_refreshes_recency(values_workbook):
    grid = _grid(values_workbook)
    for i in range(cache.MAX_ENTRIES):
        cache.store(f"ver_{i:016d}", grid)

    cache.get("ver_0000000000000000")  # touch the oldest
    cache.store("ver_9999999999999999", grid)  # force one eviction

    assert cache.get("ver_0000000000000000") is not None, "touched entry was evicted"
    assert cache.get("ver_0000000000000001") is None, "the true LRU should have gone"


def test_concurrent_use_does_not_corrupt_the_map(values_workbook):
    grid = _grid(values_workbook)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            for n in range(20):
                cache.store(f"ver_{(i * 20 + n) % 6:016d}", grid)
                cache.get(f"ver_{n % 6:016d}")
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(cache._entries) <= cache.MAX_ENTRIES
