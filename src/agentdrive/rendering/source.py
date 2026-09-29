"""Byte sources for previews that must not read the whole file.

The renderer's contract is "bytes in, markup out", and up to `MAX_RENDER_BYTES`
it stays that way: the caller fetches the object and hands over `bytes`. Above
that ceiling a whole-object fetch is the wrong tool — a 200 MB parquet needs
its footer and one row group, a 50 MB csv needs its first megabyte — so the
caller hands over a `ByteSource` instead and the renderer asks for exactly
the ranges it needs.

`RangedFile` adapts a `ByteSource` to the seekable file protocol pyarrow
reads through, and it is the ONE place a budget is enforced: however a
format's reader walks the file, the total it may pull is bounded, and past
the bound it raises rather than fetching on. A hostile footer that points
every column chunk at the far end of a huge object costs at most the budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# The most a large-file preview may fetch in total, whatever the format's
# reader asks for. Generous for a parquet footer plus one row group (the
# row-group guard in render.py bounds that separately) and far below what a
# whole-object fetch of the same file would cost.
RANGED_READ_BUDGET = 64 * 1024 * 1024


class ByteSource(Protocol):
    """A readable object whose ranges can be fetched independently."""

    @property
    def size(self) -> int: ...

    def read_range(self, start: int, end: int) -> bytes:
        """Bytes `[start, end)`, clamped to the object. Blocking."""
        ...


class PreviewBudgetExceeded(RuntimeError):
    """A preview tried to read more than `RANGED_READ_BUDGET` bytes."""


@dataclass
class BytesSource:
    """A `ByteSource` over bytes already in memory — tests, and the small-file
    path when a caller wants one code path. Counts what was asked for, so a
    test can prove a preview read the footer and one row group, not the file."""

    data: bytes
    fetched: int = 0
    calls: int = 0

    @property
    def size(self) -> int:
        return len(self.data)

    def read_range(self, start: int, end: int) -> bytes:
        start = max(0, start)
        end = min(len(self.data), end)
        chunk = self.data[start:end] if end > start else b""
        self.fetched += len(chunk)
        self.calls += 1
        return chunk


class RangedFile:
    """The seekable file protocol over a `ByteSource`, with a read budget.

    pyarrow wraps any Python object exposing `read`, `seek`, `tell`, `size`
    and `closed`; this is that object. Every `read` is one ranged fetch, so
    a reader that seeks to the footer and then to one row group makes exactly
    those fetches and no others — which is the whole point of the class.
    """

    mode = "rb"
    closed = False

    def __init__(self, source: ByteSource, *, budget: int = RANGED_READ_BUDGET) -> None:
        self._source = source
        self._pos = 0
        self._budget = budget
        self.fetched = 0

    def size(self) -> int:
        return self._source.size

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._source.size + offset
        else:
            raise ValueError(f"bad whence {whence}")
        self._pos = max(0, self._pos)
        return self._pos

    def read(self, n: int = -1) -> bytes:
        end = self._source.size if n is None or n < 0 else min(self._source.size, self._pos + n)
        if end <= self._pos:
            return b""
        if self.fetched + (end - self._pos) > self._budget:
            raise PreviewBudgetExceeded(
                f"preview would read more than {self._budget} bytes"
            )
        chunk = self._source.read_range(self._pos, end)
        self.fetched += len(chunk)
        self._pos += len(chunk)
        return chunk

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None
