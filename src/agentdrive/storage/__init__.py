"""Object storage: the facade every caller imports.

`from .. import storage` and then the module-level operations, exceptions
and types keep working exactly as they did when this was one GCS module.
Each function delegates to the store the factory built from settings —
`STORAGE_BACKEND=gcs` (the hosted product) or `fs` (a self-hosted install)
— so no caller knows, or branches on, the backend.

What is public API here, in three groups:

- the contract: `ObjectStore`, `Capabilities`, `NotFound`,
  `PreconditionFailed`, `ObjectWriteResult`, `ObjectStat`, `Blob`, and the
  namespace constants;
- the operations, as module-level functions;
- `RangedSource`, a lazy `rendering.source.ByteSource` over one pinned
  object, constructed by the routes without a store in hand.

Tests monkeypatch the functions on this module (`storage.get`,
`storage.stream`, `storage.signed_download_url`), which keeps working
because callers look them up through the module at call time. The GCS-only
direct-transfer signers import `agentdrive.storage.gcs` directly for its
credential and client helpers; nothing GCS-specific is re-exported here, so
a filesystem install never imports the GCS SDK through this package.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

from .base import (
    CAS_PREFIX,
    CAS_REFRESH_AGE,
    OBJ_PREFIX,
    SCRATCH_PREFIX,
    Blob,
    Capabilities,
    NotFound,
    ObjectStat,
    ObjectStore,
    ObjectWriteResult,
    PreconditionFailed,
)
from .factory import build_store, current_store, reset_store

__all__ = [
    "CAS_PREFIX",
    "CAS_REFRESH_AGE",
    "OBJ_PREFIX",
    "SCRATCH_PREFIX",
    "Blob",
    "Capabilities",
    "NotFound",
    "ObjectStat",
    "ObjectStore",
    "ObjectWriteResult",
    "PreconditionFailed",
    "RangedSource",
    "build_store",
    "current_store",
    "reset_store",
    "store_id",
    "capabilities",
    "ensure_store",
    "ensure_bucket",
    "put",
    "refresh_object_generation",
    "get",
    "delete",
    "stat",
    "list_blobs",
    "list_prefixes",
    "get_range",
    "stream",
    "signed_download_url",
]


def store_id() -> str:
    """The value the commit seam persists as `storage_bucket` for objects
    written by this process's store, and the only `bucket=` it answers for."""
    return current_store().store_id


def capabilities() -> Capabilities:
    return current_store().capabilities


def ensure_store() -> None:
    """Create the store if missing and prove it honours its capabilities.
    Called once from the lifespan."""
    current_store().ensure_store()


# The name every caller used when the only store was a bucket.
ensure_bucket = ensure_store


async def put(object_name: str, data: bytes, content_type: str) -> ObjectWriteResult:
    return await current_store().put(object_name, data, content_type)


async def refresh_object_generation(
    object_name: str,
    data: bytes,
    content_type: str,
    *,
    if_generation_match: int,
) -> ObjectWriteResult:
    return await current_store().refresh_object_generation(
        object_name, data, content_type, if_generation_match=if_generation_match
    )


async def get(
    object_name: str,
    *,
    bucket: str | None = None,
    generation: int | None = None,
) -> bytes:
    return await current_store().get(object_name, bucket=bucket, generation=generation)


async def delete(object_name: str, *, if_generation_match: int | None = None) -> None:
    await current_store().delete(object_name, if_generation_match=if_generation_match)


async def stat(object_name: str) -> ObjectStat | None:
    return await current_store().stat(object_name)


def list_blobs(prefix: str) -> AsyncIterator[Blob]:
    return current_store().list_blobs(prefix)


async def list_prefixes(prefix: str, delimiter: str = "/") -> list[str]:
    return await current_store().list_prefixes(prefix, delimiter)


def get_range(
    object_name: str,
    start: int,
    end: int,
    *,
    bucket: str | None = None,
    generation: int | None = None,
) -> bytes:
    return current_store().get_range(
        object_name, start, end, bucket=bucket, generation=generation
    )


def stream(
    object_name: str,
    chunk_size: int = 64 * 1024,
    *,
    bucket: str | None = None,
    generation: int | None = None,
    start: int = 0,
    end: int | None = None,
) -> Iterator[bytes]:
    return current_store().stream(
        object_name,
        chunk_size,
        bucket=bucket,
        generation=generation,
        start=start,
        end=end,
    )


async def signed_download_url(
    object_name: str,
    *,
    content_type: str,
    filename: str,
    ttl_s: int,
    bucket: str | None = None,
    generation: int | None = None,
) -> str | None:
    return await current_store().signed_download_url(
        object_name,
        content_type=content_type,
        filename=filename,
        ttl_s=ttl_s,
        bucket=bucket,
        generation=generation,
    )


@dataclass
class RangedSource:
    """A `rendering.source.ByteSource` over one pinned object.

    Lazy: constructing it fetches nothing. The renderer asks for the ranges a
    format's preview needs — a parquet footer and one row group, a csv's
    first megabyte — and each `read_range` is one read of exactly that."""

    object_name: str
    size: int
    bucket: str | None = None
    generation: int | None = None

    def read_range(self, start: int, end: int) -> bytes:
        return get_range(
            self.object_name, start, end, bucket=self.bucket, generation=self.generation
        )
