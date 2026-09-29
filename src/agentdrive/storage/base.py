"""The object-store contract every backend satisfies.

One protocol, two implementations (`gcs.py`, the hosted product's store, and
`fs.py`, a self-hosted install's), and a facade (`__init__.py`) that keeps the
module-level names every caller already imports. Nothing here imports a
provider SDK: the exceptions, the result types and the capability flags are
AgentDrive's own, so a caller never learns which backend it is talking to.

Generations are part of the contract, not a GCS detail. Every object carries
an integer generation that changes on every successful write, INCLUDING a
byte-identical overwrite, and a delete pinned to a generation fails with
`PreconditionFailed` when the object has moved since. The GC's mark-sweep
(`core/gc.py`) and the reconcile job depend on exactly that property, and
`specs/tla/ArtifactGC.tla` models it; a backend that cannot honour it must
say so through `Capabilities.generation_pinned_delete` and is refused for
sweeps rather than trusted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Cross-pipeline storage prefix constants (PR 0 cross-PR contract).
#
# `CAS_PREFIX` is the namespace for content-addressed artifact blobs; laid
# out as `cas/{drive_id}/{sha256}` so a drive's mark-sweep can enumerate just
# that drive's prefix. `SCRATCH_PREFIX` is reserved for pipeline scratch that
# the GC scratch-sweep reclaims independently of the artifact CAS lifecycle.
# `OBJ_PREFIX` is the namespace for large-upload blobs: opaque keys
# `obj/{drive_id}/{upload_id}`, laid out per drive like CAS.
# ---------------------------------------------------------------------------
CAS_PREFIX = "cas/"
SCRATCH_PREFIX = "embed-scratch/"
OBJ_PREFIX = "obj/"

# An existing CAS object at least this old is refreshed (conditional
# same-content re-upload → new generation) before an UNREFERENCED adoption,
# so a mark-sweep whose listing predates the adopting commit can never delete
# it with a matching generation pin (adversarial review I-1). Must stay
# strictly below `GCSweeper.MARK_SWEEP_AGE` (24 h) —
# `test_cas_refresh_age_sits_inside_the_mark_sweep_window` pins the relation.
CAS_REFRESH_AGE = timedelta(hours=23)


class NotFound(Exception):
    """The object (or, for a pinned read, that generation of it) is gone."""


class PreconditionFailed(Exception):
    """A generation pin did not match: the object moved since it was observed."""


@dataclass(frozen=True)
class Capabilities:
    """What a backend can honour. `generation_pinned_delete` is checked by
    the GC before a mark-sweep, which refuses to run without it; the other
    two are informational (signing fails soft at call time, and direct
    transfer is refused at configuration time on a store without it)."""

    generation_pinned_delete: bool
    signed_download: bool
    direct_transfer: bool


@dataclass(frozen=True)
class ObjectWriteResult:
    """The coordinates of a just-written object: its store id and the exact
    generation the write landed as. Inline writes persist both on the version
    row (B3 §7); a `None` generation (a backend that omitted it) makes the
    commit seam fail closed rather than persist a fabricated or half-paired
    value.

    `adopted_existing`/`adopted_age_seconds` report that the create-only
    write found the object already present (duplicate content) and reused its
    generation — the caller decides whether an AGED, UNREFERENCED adoption
    needs `refresh_object_generation` (see CAS_REFRESH_AGE)."""

    bucket: str
    generation: int | None
    adopted_existing: bool = False
    adopted_age_seconds: float | None = None


@dataclass(frozen=True)
class ObjectStat:
    """What `stat` returns: the store-recorded size, checksums, generation and
    content type of an object. `crc32c` is always present; `md5` may be
    absent (GCS composite objects)."""

    size: int
    crc32c: str | None
    md5: str | None
    generation: int | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class Blob:
    """Lightweight descriptor returned by `list_blobs`: only the fields the
    GC sweeps and observability need. The sweeps pass `generation` back to
    `delete(if_generation_match=...)` so the delete is a no-op if the object
    was re-put since the listing (ArtifactGC.tla finding 2)."""

    name: str
    size: int
    time_created: datetime
    generation: int | None = None


@runtime_checkable
class ObjectStore(Protocol):
    """One object store. Async methods run provider I/O off the event loop;
    `get_range` and `stream` are sync on purpose — the renderer and
    StreamingResponse call them from worker threads."""

    @property
    def store_id(self) -> str:
        """The value persisted as `artifact_versions.storage_bucket` for
        objects written here, and the only `bucket=` this store answers for.
        GCS: the bucket name. Filesystem: `fs:<root basename>`."""
        ...

    @property
    def capabilities(self) -> Capabilities: ...

    def ensure_store(self) -> None:
        """Create the store if missing and verify it can honour its declared
        capabilities. Called once from the lifespan; a store that fails its
        own probe raises rather than serving."""
        ...

    async def put(self, object_name: str, data: bytes, content_type: str) -> ObjectWriteResult:
        """Create-only write. On an existing object under the same key the
        EXISTING object's coordinates are validated (same size, same crc32c)
        and reused — a different object under a content-addressed key is
        corruption, never something to adopt."""
        ...

    async def refresh_object_generation(
        self,
        object_name: str,
        data: bytes,
        content_type: str,
        *,
        if_generation_match: int,
    ) -> ObjectWriteResult:
        """Re-write the same content conditionally on the observed generation,
        minting a fresh one. On a lost race the CURRENT object is validated
        and adopted."""
        ...

    async def get(
        self,
        object_name: str,
        *,
        bucket: str | None = None,
        generation: int | None = None,
    ) -> bytes: ...

    async def delete(self, object_name: str, *, if_generation_match: int | None = None) -> None:
        """Delete; raises `NotFound` when already gone and `PreconditionFailed`
        when a pin no longer matches."""
        ...

    async def stat(self, object_name: str) -> ObjectStat | None: ...

    def list_blobs(self, prefix: str) -> AsyncIterator[Blob]: ...

    async def list_prefixes(self, prefix: str, delimiter: str = "/") -> list[str]: ...

    def get_range(
        self,
        object_name: str,
        start: int,
        end: int,
        *,
        bucket: str | None = None,
        generation: int | None = None,
    ) -> bytes:
        """Bytes `[start, end)` in one read. Sync (worker thread)."""
        ...

    def stream(
        self,
        object_name: str,
        chunk_size: int = 64 * 1024,
        *,
        bucket: str | None = None,
        generation: int | None = None,
        start: int = 0,
        end: int | None = None,
    ) -> Iterator[bytes]: ...

    async def signed_download_url(
        self,
        object_name: str,
        *,
        content_type: str,
        filename: str,
        ttl_s: int,
        bucket: str | None = None,
        generation: int | None = None,
    ) -> str | None:
        """A short-lived direct-download URL, or `None` when the store cannot
        sign (the caller proxy-streams instead)."""
        ...
