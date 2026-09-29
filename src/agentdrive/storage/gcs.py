"""The GCS object store: the hosted product's backend.

The module-level functions are the original implementation, kept as they
were (async via asyncio.to_thread over the sync google-cloud-storage client
so route handlers don't block the event loop); `GcsStore` at the bottom is
the thin `ObjectStore` adapter over them, and the only place a provider
exception becomes one of AgentDrive's own. Callers import the facade
(`agentdrive.storage`), never this module — except the direct-transfer
signers, which are GCS-only by design and reach `_get_signing_creds` /
`_client_singleton` here.

The STORAGE_EMULATOR_HOST mutation happens inside ensure_bucket() so it
runs once at app startup, never at import — easier to reason about, and
test environments that don't use GCS can still import this module."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

from google.api_core import exceptions as _gcs_exc
from google.cloud import storage as gcs

from ..config import settings
from ..content_disposition import build_content_disposition
from .base import (
    CAS_PREFIX,
    CAS_REFRESH_AGE,
    OBJ_PREFIX,
    SCRATCH_PREFIX,
    Blob,
    Capabilities,
    NotFound,
    ObjectStat,
    ObjectWriteResult,
    PreconditionFailed,
)

__all__ = [
    "CAS_PREFIX",
    "CAS_REFRESH_AGE",
    "OBJ_PREFIX",
    "SCRATCH_PREFIX",
    "GcsStore",
]

log = logging.getLogger(__name__)

_client: gcs.Client | None = None


def _setup_emulator() -> None:
    if settings.gcs_emulator_host:
        os.environ.setdefault("STORAGE_EMULATOR_HOST", settings.gcs_emulator_host)


def _client_singleton() -> gcs.Client:
    global _client
    if _client is None:
        _setup_emulator()
        _client = gcs.Client(project=settings.gcs_project)
    return _client


def _bucket():
    return _client_singleton().bucket(settings.gcs_bucket)


def ensure_bucket() -> None:
    """Create the bucket if missing. Called once from the FastAPI lifespan.

    Failures are logged loudly — a missing bucket in prod manifests as
    every upload silently 500ing, and the startup signal is our last
    chance to surface the configuration problem."""
    _setup_emulator()
    b = _bucket()
    try:
        if not b.exists():
            _client_singleton().create_bucket(b.name)
    except Exception as e:
        # Don't crash startup (emulator races, race against concurrent
        # creators, etc.) but make the failure visible.
        log.warning(
            "ensure_bucket(%s) failed at startup: %s — uploads may fail",
            settings.gcs_bucket, e,
        )


def _crc32c_b64(data: bytes) -> str:
    """Canonical padded base64 CRC32C of `data` (GCS metadata form)."""
    import base64

    import google_crc32c

    checksum = google_crc32c.Checksum(data)
    return base64.b64encode(checksum.digest()).decode("ascii")


async def put(object_name: str, data: bytes, content_type: str) -> ObjectWriteResult:
    """Create-only object write returning the landed coordinates.

    CAS keys are shared by every version row with the same content, and the
    artifact bucket has object versioning OFF — an unconditional upload of
    identical bytes would REPLACE generation G1 with G2 while earlier
    immutable version rows still point at G1 (B3 packet-1 correction,
    blocker 1). `ifGenerationMatch=0` makes creation exclusive; on the 412
    (the object already exists — a duplicate-content write or the loser of
    a concurrent race) the EXISTING object's coordinates are validated and
    reused, so every row referencing one CAS key carries one stable
    generation.
    """
    def _put() -> ObjectWriteResult:
        bucket = _bucket()
        blob = bucket.blob(object_name)
        try:
            blob.upload_from_string(
                data, content_type=content_type, if_generation_match=0,
            )
        except _gcs_exc.PreconditionFailed:
            existing = bucket.blob(object_name)
            fresh_create = False
            try:
                existing.reload()
            except _gcs_exc.NotFound:
                # Deleted between our 412 and the stat (GC race): one retry
                # of the exclusive create; a second 412 means an active
                # writer owns it — reload once more and reuse.
                blob = bucket.blob(object_name)
                try:
                    blob.upload_from_string(
                        data, content_type=content_type, if_generation_match=0,
                    )
                except _gcs_exc.PreconditionFailed:
                    existing = bucket.blob(object_name)
                    existing.reload()
                else:
                    existing = blob
                    fresh_create = True
            if (existing.size or 0) != len(data) or (
                existing.crc32c is not None
                and not fresh_create
                and existing.crc32c != _crc32c_b64(data)
            ):
                # A different object under a content-addressed key is
                # corruption, never something to silently adopt.
                raise RuntimeError(
                    f"existing object at {object_name} does not match the "
                    f"content being written"
                ) from None
            age_seconds = None
            if not fresh_create and existing.time_created is not None:
                age_seconds = (
                    datetime.now(UTC) - existing.time_created
                ).total_seconds()
            return ObjectWriteResult(
                bucket=bucket.name,
                generation=int(existing.generation) if existing.generation else None,
                adopted_existing=not fresh_create,
                adopted_age_seconds=age_seconds,
            )
        if blob.generation is None:
            # The upload response normally carries the generation; reload is
            # the fallback so a caller never persists a fabricated value.
            try:
                blob.reload()
            except _gcs_exc.NotFound:  # pragma: no cover — race with a delete
                return ObjectWriteResult(bucket=bucket.name, generation=None)
        return ObjectWriteResult(
            bucket=bucket.name,
            generation=int(blob.generation) if blob.generation else None,
        )

    return await asyncio.to_thread(_put)


async def refresh_object_generation(
    object_name: str,
    data: bytes,
    content_type: str,
    *,
    if_generation_match: int,
) -> ObjectWriteResult:
    """Bump an UNREFERENCED CAS object to a fresh generation by re-uploading
    the same content conditionally on the observed generation.

    Used only when adopting an object older than `CAS_REFRESH_AGE` that no
    version row references (adversarial review I-1): the fresh generation
    makes any in-flight mark-sweep's pinned delete miss, and each GCS
    generation carries its own timeCreated, so the object also leaves the
    sweep's age window. Safe precisely because nothing references the old
    generation. On a lost race (412 — another writer refreshed first, or
    the object vanished and was recreated) the CURRENT object is validated
    and adopted; it is young by construction."""
    def _refresh() -> ObjectWriteResult:
        bucket = _bucket()
        blob = bucket.blob(object_name)
        try:
            blob.upload_from_string(
                data, content_type=content_type,
                if_generation_match=if_generation_match,
            )
            return ObjectWriteResult(
                bucket=bucket.name,
                generation=int(blob.generation) if blob.generation else None,
            )
        except _gcs_exc.PreconditionFailed:
            existing = bucket.blob(object_name)
            try:
                existing.reload()
            except _gcs_exc.NotFound:
                # Vanished under the caller (a sweep won the race): the bytes
                # are in hand, so a fresh create-only write is the honest
                # outcome — the same contract the filesystem store keeps.
                fresh = bucket.blob(object_name)
                fresh.upload_from_string(
                    data, content_type=content_type, if_generation_match=0,
                )
                return ObjectWriteResult(
                    bucket=bucket.name,
                    generation=int(fresh.generation) if fresh.generation else None,
                )
            if (existing.size or 0) != len(data) or (
                existing.crc32c is not None
                and existing.crc32c != _crc32c_b64(data)
            ):
                raise RuntimeError(
                    f"existing object at {object_name} does not match the "
                    f"content being refreshed"
                ) from None
            return ObjectWriteResult(
                bucket=bucket.name,
                generation=int(existing.generation) if existing.generation else None,
                adopted_existing=True,
            )

    return await asyncio.to_thread(_refresh)


def _bucket_for(name: str | None):
    """The artifact CAS bucket by default; a caller-supplied bucket for
    version rows that record B3 direct-transfer coordinates. Callers are
    responsible for the closed-set check (`core.version_reads`) — this
    helper never decides which buckets are legitimate."""
    return _client_singleton().bucket(name) if name else _bucket()


async def get(
    object_name: str,
    *,
    bucket: str | None = None,
    generation: int | None = None,
) -> bytes:
    return await asyncio.to_thread(
        lambda: _bucket_for(bucket)
        .blob(object_name, generation=generation)
        .download_as_bytes()
    )


async def delete(object_name: str, *, if_generation_match: int | None = None) -> None:
    """Delete a blob. `if_generation_match` pins the delete to a specific
    generation (from a listing): if the object was overwritten since, GCS
    rejects with 412 → `PreconditionFailed`. The GC sweeps use this so a
    CAS re-upload landing between LIST and DELETE survives; unconditional
    callers (drive teardown, feedback attachments) omit it.

    CAUTION: the fake-gcs emulator does NOT enforce delete preconditions
    (verified 2026-08-15), so tests must never rely on the pin alone —
    which is also why mark-sweep re-checks DB membership immediately
    before each delete."""
    await asyncio.to_thread(
        lambda: _bucket().blob(object_name).delete(
            if_generation_match=if_generation_match
        )
    )


# `create_resumable_upload_session` (JSON API via the google client, origin
# passed per call) was REMOVED in B3 packet 2, not wrapped: GCS does not
# enforce bucket CORS on JSON API endpoints, so its browser-upload claim was
# unsound, and its `X-Upload-Content-Length` size claim is not part of the
# B3 contract. The XML adapter in `storage_transfers.py` is the one
# resumable-initiation path; its only live callers arrive with packet 3.


# Cached signing credentials. On Cloud Run these are the runtime SA's compute
# credentials (no private key in-process); we sign V4 URLs via the IAM
# Credentials API by passing the SA email + a fresh access token to
# generate_signed_url. Cached so we don't hit the metadata server every call —
# refreshed only when the token has expired. A benign double-refresh under
# concurrency is harmless (idempotent token fetch).
_signing_creds = None


def _get_signing_creds():
    """Return refreshed ADC, or None if no signing identity is available
    (e.g. local user creds with no service_account_email)."""
    global _signing_creds
    import google.auth
    from google.auth.transport import requests as ga_requests

    if _signing_creds is None:
        _signing_creds, _ = google.auth.default()
    if not _signing_creds.valid:
        try:
            _signing_creds.refresh(ga_requests.Request())
        except Exception:
            # Don't leave a broken/expired creds object cached — the next call
            # re-runs google.auth.default(). The signer's outer except turns
            # this into a None (proxy fallback), so a transient metadata-server
            # hiccup degrades gracefully and self-heals.
            _signing_creds = None
            raise
    return _signing_creds


async def signed_download_url(
    object_name: str,
    *,
    content_type: str,
    filename: str,
    ttl_s: int,
    bucket: str | None = None,
    generation: int | None = None,
) -> str | None:
    """Mint a short-lived V4 signed GCS GET URL for a direct client download,
    or return ``None`` when signing is unavailable (caller falls back to the
    proxy stream). See large-download-design.md §3 / §5.

    Unavailable when (a) the GCS emulator is in use — fake-gcs has no real V4
    verification, or (b) the runtime credentials can't sign (no private key AND
    no IAM signBlob identity). On Cloud Run we sign WITHOUT a local key via the
    IAM Credentials API: pass the runtime SA email + a fresh access token, which
    delegates to ``iamcredentials.signBlob`` — this needs the runtime SA to hold
    ``roles/iam.serviceAccountTokenCreator`` on itself.

    ``content_type`` sets Response-Content-Type and ``filename`` sets a
    Content-Disposition (ASCII fallback + RFC 5987 ``filename*``) so the direct
    GCS download presents with the right type + name. Any failure fails soft
    (logs + returns ``None``) — a download must never 500 because signing broke.
    """
    if settings.gcs_emulator_host:
        return None  # fake-gcs doesn't verify real V4 signatures

    def _sign() -> str | None:
        try:
            creds = _get_signing_creds()
            sa_email = getattr(creds, "service_account_email", None)
            token = getattr(creds, "token", None)
            if not sa_email or not token:
                return None  # ADC without a signing identity (e.g. user creds)
            disposition = build_content_disposition("attachment", filename)
            return _bucket_for(bucket).blob(
                object_name, generation=generation
            ).generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=ttl_s),
                method="GET",
                response_type=content_type,
                response_disposition=disposition,
                service_account_email=sa_email,
                access_token=token,
            )
        except Exception as e:  # fail soft — never break a download on signing
            log.warning("signed_download_url failed for %s: %s", object_name, e)
            return None

    return await asyncio.to_thread(_sign)


async def stat(object_name: str) -> ObjectStat | None:
    """Fetch a blob's metadata (size + checksums + generation), or None if it
    doesn't exist. Does NOT download the body — a single metadata GET."""
    def _stat() -> ObjectStat | None:
        blob = _bucket().blob(object_name)
        try:
            blob.reload()  # metadata GET; raises NotFound if the object is gone
        except _gcs_exc.NotFound:
            return None
        return ObjectStat(
            size=blob.size or 0, crc32c=blob.crc32c, md5=blob.md5_hash,
            generation=int(blob.generation) if blob.generation else None,
            content_type=blob.content_type,
        )

    return await asyncio.to_thread(_stat)


async def list_blobs(prefix: str) -> AsyncIterator[Blob]:
    """Paginate GCS blobs under `prefix`. Each item exposes `.name`,
    `.size`, `.time_created` — the fields the GC sweep needs (gc-sweeper-
    design.md §10.3).

    Pagination is handled by the underlying client; we materialize one
    page at a time on the worker thread, then yield each blob across
    the event loop. Trade-off: a single very long list call holds the
    worker thread until the page is materialized — at GCS's 1000 blobs/
    page default, that's tens of ms. Cheap relative to per-blob processing
    (DB reads, audit inserts) downstream."""
    # Materialize the iterator inside the worker thread; pagination
    # state lives in the google-cloud-storage client. We `list()` each
    # page to a Python list because the client's lazy iterator isn't
    # async-safe — yielding it across `to_thread` boundaries can land
    # on a different thread mid-iteration.
    def _list_all() -> list[Blob]:
        return [
            Blob(
                name=b.name, size=b.size or 0, time_created=b.time_created,
                generation=b.generation,
            )
            for b in _bucket().list_blobs(prefix=prefix)
        ]

    blobs = await asyncio.to_thread(_list_all)
    for b in blobs:
        yield b


async def list_prefixes(prefix: str, delimiter: str = "/") -> list[str]:
    """List top-level pseudo-directories under `prefix`. With
    `delimiter='/'`, returns entries like `['cas/drv_A/', 'cas/drv_B/']`.
    Used by orphan-sweep + scratch-sweep to enumerate per-drive
    namespaces (gc-sweeper-design.md §10.6 / §10.7)."""
    def _list() -> list[str]:
        # The google-cloud-storage client exposes `prefixes` on the
        # iterator after exhaustion. We must drain the blob results to
        # populate `prefixes`; we don't care about the blob results
        # here.
        it = _bucket().list_blobs(prefix=prefix, delimiter=delimiter)
        for _ in it:
            pass
        return list(it.prefixes)

    return await asyncio.to_thread(_list)


def get_range(
    object_name: str,
    start: int,
    end: int,
    *,
    bucket: str | None = None,
    generation: int | None = None,
) -> bytes:
    """Bytes `[start, end)` of a blob, in one ranged GET. Sync — this is
    called from a worker thread by the renderer's large-file preview path,
    never on the event loop."""
    if end <= start:
        return b""
    blob = _bucket_for(bucket).blob(object_name, generation=generation)
    # GCS ranges are inclusive on both ends.
    return blob.download_as_bytes(start=start, end=end - 1)


def stream(
    object_name: str,
    chunk_size: int = 64 * 1024,
    *,
    bucket: str | None = None,
    generation: int | None = None,
    start: int = 0,
    end: int | None = None,
) -> Iterator[bytes]:
    """Sync chunked iterator over a blob. Starlette's StreamingResponse
    drains sync generators across the event loop; each `read()` call is
    serialised on the worker thread for the duration of that chunk."""
    blob = _bucket_for(bucket).blob(object_name, generation=generation)
    with blob.open("rb") as f:
        if start:
            f.seek(start)
        remaining = None if end is None else end - start
        while True:
            if remaining == 0:
                break
            chunk = f.read(chunk_size if remaining is None else min(chunk_size, remaining))
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk


def _translate(exc: Exception) -> Exception:
    """Map the provider's exceptions onto the contract's."""
    if isinstance(exc, _gcs_exc.NotFound):
        return NotFound(str(exc))
    if isinstance(exc, _gcs_exc.PreconditionFailed):
        return PreconditionFailed(str(exc))
    return exc


class GcsStore:
    """`ObjectStore` over the module-level functions above. Its `store_id` is
    the artifact bucket, which is also what the commit seam persists as
    `artifact_versions.storage_bucket` for inline writes."""

    @property
    def store_id(self) -> str:
        return settings.gcs_bucket

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            generation_pinned_delete=True,
            # Best effort: fake-gcs cannot verify V4 signatures, and a real
            # credential without a signing identity also yields None at call
            # time. Callers fall back to proxy streaming either way.
            signed_download=not settings.gcs_emulator_host,
            direct_transfer=True,
        )

    def ensure_store(self) -> None:
        ensure_bucket()

    async def put(self, object_name: str, data: bytes, content_type: str) -> ObjectWriteResult:
        try:
            return await put(object_name, data, content_type)
        except _gcs_exc.GoogleAPICallError as exc:
            raise _translate(exc) from exc

    async def refresh_object_generation(
        self,
        object_name: str,
        data: bytes,
        content_type: str,
        *,
        if_generation_match: int,
    ) -> ObjectWriteResult:
        try:
            return await refresh_object_generation(
                object_name, data, content_type, if_generation_match=if_generation_match
            )
        except _gcs_exc.GoogleAPICallError as exc:
            raise _translate(exc) from exc

    async def get(
        self,
        object_name: str,
        *,
        bucket: str | None = None,
        generation: int | None = None,
    ) -> bytes:
        try:
            return await get(object_name, bucket=bucket, generation=generation)
        except _gcs_exc.GoogleAPICallError as exc:
            raise _translate(exc) from exc

    async def delete(self, object_name: str, *, if_generation_match: int | None = None) -> None:
        try:
            await delete(object_name, if_generation_match=if_generation_match)
        except _gcs_exc.GoogleAPICallError as exc:
            raise _translate(exc) from exc

    async def stat(self, object_name: str) -> ObjectStat | None:
        return await stat(object_name)

    def list_blobs(self, prefix: str) -> AsyncIterator[Blob]:
        return list_blobs(prefix)

    async def list_prefixes(self, prefix: str, delimiter: str = "/") -> list[str]:
        return await list_prefixes(prefix, delimiter)

    def get_range(
        self,
        object_name: str,
        start: int,
        end: int,
        *,
        bucket: str | None = None,
        generation: int | None = None,
    ) -> bytes:
        try:
            return get_range(object_name, start, end, bucket=bucket, generation=generation)
        except _gcs_exc.GoogleAPICallError as exc:
            raise _translate(exc) from exc

    def stream(
        self,
        object_name: str,
        chunk_size: int = 64 * 1024,
        *,
        bucket: str | None = None,
        generation: int | None = None,
        start: int = 0,
        end: int | None = None,
    ) -> Iterator[bytes]:
        try:
            yield from stream(
                object_name,
                chunk_size,
                bucket=bucket,
                generation=generation,
                start=start,
                end=end,
            )
        except _gcs_exc.GoogleAPICallError as exc:
            raise _translate(exc) from exc

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
        return await signed_download_url(
            object_name,
            content_type=content_type,
            filename=filename,
            ttl_s=ttl_s,
            bucket=bucket,
            generation=generation,
        )
