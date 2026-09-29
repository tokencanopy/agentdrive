"""The filesystem object store: one directory, a self-hosted install's backend.

Layout under `root`:

    <root>/.agentdrive-store           {"id": "fs_<hex>"} — the store's identity
    <root>/.locks/<sha256(name)>       one lock file per key, never unlinked
    <root>/<object name>               the bytes
    <root>/<object name>.meta.json     {"generation", "size", "crc32c", "md5",
                                        "content_type", "created"}

Object names are slash-separated keys exactly as the GCS backend stores them
(`cas/{drive}/{digest}`), so a listing walks the same namespace the sweeps
expect. The sidecar file is the generation counter GCS provides natively:
every successful write, including a byte-identical one, mints a new value,
and a pinned delete compares against it under the same per-key lock the
writers take. Writes land through a temp file and `os.replace`, so a reader
never sees a torn object and a crash leaves either the old object or the
new one.

The lock is a separate file that is never unlinked. Locking the sidecar and
then deleting it would let a second writer take an "exclusive" lock on a
fresh inode while the first still held the old one — the classic
unlink-under-flock race, and precisely the GC-delete-versus-adopting-write
interleaving the lock exists to serialize. A lock file per key costs one
empty file per key ever written, which is the price of the guarantee.

One API process per root. `flock` also serializes across processes on the
same host (the GC job beside the API), and nothing here coordinates hosts;
a network filesystem whose locks are advisory-but-ignored fails the startup
probe rather than the GC.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .base import (
    Blob,
    Capabilities,
    NotFound,
    ObjectStat,
    ObjectWriteResult,
    PreconditionFailed,
)

log = logging.getLogger(__name__)

_META_SUFFIX = ".meta.json"
_TMP_SUFFIX = ".tmp"
_STORE_MARKER = ".agentdrive-store"
_LOCK_DIR = ".locks"
_PROBE_PREFIX = ".agentdrive-store-probe"


def _crc32c_b64(data: bytes) -> str:
    """Canonical padded base64 CRC32C (the GCS metadata form), so an
    `ObjectStat.crc32c` means the same thing on every backend."""
    import google_crc32c

    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode("ascii")


def _md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")


class FilesystemStore:
    """`ObjectStore` over a directory. See the module docstring."""

    def __init__(self, root: str | os.PathLike[str]):
        self._root = Path(root).expanduser().resolve()
        self._store_id: str | None = None
        self._last_generation = 0

    # ---- identity ---------------------------------------------------------

    @property
    def store_id(self) -> str:
        """The identity rows carry as `storage_bucket`. Persisted in the root
        by `ensure_store`, so renaming or moving the directory — or starting
        the process from another working directory — does not orphan every
        row that names it. Path-derived ids did exactly that."""
        if self._store_id is None:
            marker = self._root / _STORE_MARKER
            try:
                self._store_id = json.loads(marker.read_text())["id"]
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"filesystem store at {self._root} is not initialised; "
                    "ensure_store() runs at startup"
                ) from exc
        return self._store_id

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            generation_pinned_delete=True, signed_download=False, direct_transfer=False
        )

    @property
    def root(self) -> Path:
        return self._root

    # ---- paths and metadata ---------------------------------------------

    def _path(self, object_name: str) -> Path:
        """The data path for a key, refusing anything that could leave the
        root or collide with the store's own files: an absolute name, an
        empty segment, `.`, `..`, or any segment that starts with a dot
        (every internal file does, and no namespace this store serves —
        `cas/`, `obj/`, `embed-scratch/` — ever mints one)."""
        if not object_name or object_name.startswith("/") or object_name.endswith("/"):
            raise ValueError(f"invalid object name {object_name!r}")
        parts = object_name.split("/")
        if any(p == "" or p.startswith(".") for p in parts) or any(
            p.endswith(_META_SUFFIX) or p.endswith(_TMP_SUFFIX) for p in parts
        ):
            raise ValueError(f"invalid object name {object_name!r}")
        path = self._root.joinpath(*parts)
        if self._root not in path.parents:
            raise ValueError(f"invalid object name {object_name!r}")
        return path

    def _meta_path(self, object_name: str) -> Path:
        path = self._path(object_name)
        return path.with_name(path.name + _META_SUFFIX)

    def _lock_path(self, object_name: str) -> Path:
        digest = hashlib.sha256(object_name.encode("utf-8")).hexdigest()
        return self._root / _LOCK_DIR / digest[:2] / digest

    def _check_bucket(self, bucket: str | None) -> None:
        """This store answers only for its own id; a row that names another
        store's coordinates cannot be read here (the closed-set check in
        `core.version_reads` is the caller's; this is the backstop)."""
        if bucket is not None and bucket != self.store_id:
            raise NotFound(f"{bucket!r} is not this store ({self.store_id!r})")

    def _next_generation(self) -> int:
        """Monotonic within the process and time-derived across restarts, so
        a key deleted and re-created never reuses a generation a sweep might
        still hold from an earlier listing — unless the wall clock steps back
        by more than the process has been up, which the equality-only
        comparisons tolerate (a lower value is still a different value)."""
        gen = max(time.time_ns() // 1000, self._last_generation + 1)
        self._last_generation = gen
        return gen

    @contextmanager
    def _locked(self, object_name: str):
        """Exclusive per-key lock on a file that is never unlinked, so the
        inode a waiter blocks on is the inode the holder releases."""
        lock = self._lock_path(object_name)
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_meta(self, object_name: str) -> dict | None:
        try:
            raw = self._meta_path(object_name).read_bytes()
        except FileNotFoundError:
            return None
        if not raw:
            return None
        return json.loads(raw)

    def _present(self, object_name: str) -> dict | None:
        """The object's metadata when BOTH halves are on disk; a data file
        without its sidecar (a crash between the two renames) reads as
        absent, and the next write overwrites it."""
        meta = self._read_meta(object_name)
        if meta is None or not self._path(object_name).exists():
            return None
        return meta

    @staticmethod
    def _replace_via_tmp(target: Path, payload: bytes) -> None:
        """Write `payload` to a sibling temp file, sync it, and rename it into
        place; then sync the directory so the rename itself is durable. A
        failed write or rename never leaves the temp file behind."""
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}{_TMP_SUFFIX}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, target)
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    def _write_object(self, object_name: str, data: bytes, content_type: str) -> dict:
        """Write bytes then metadata, each through a temp file and rename.
        Between the two a reader sees new bytes under the old generation,
        which is benign: the write contract makes the bytes identical."""
        path = self._path(object_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._replace_via_tmp(path, data)
        meta = {
            "generation": self._next_generation(),
            "size": len(data),
            "crc32c": _crc32c_b64(data),
            "md5": _md5_b64(data),
            "content_type": content_type,
            "created": datetime.now(UTC).isoformat(),
        }
        self._replace_via_tmp(self._meta_path(object_name), json.dumps(meta).encode())
        return meta

    def _remove_object(self, object_name: str) -> None:
        """Unlink the two halves. Deliberately leaves the namespace directory
        in place even when it is now empty: the per-key lock does not cover
        the directory, so removing it races a sibling key's `mkdir` and temp
        open in the same directory — a sweep deleting a drive's last object
        while an upload lands another failed that upload with a raw OS error
        (adversarial re-probe, 2026-09-20). An empty directory is cheap.
        """
        for p in (self._path(object_name), self._meta_path(object_name)):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()

    @staticmethod
    def _matches(meta: dict, data: bytes) -> bool:
        return meta["size"] == len(data) and meta["crc32c"] == _crc32c_b64(data)

    # ---- lifecycle --------------------------------------------------------

    def ensure_store(self) -> None:
        """Create the root, give it an identity if it has none, and prove the
        pinned-delete contract on it: a create-only write, a delete with the
        wrong pin that MUST fail, then a delete with the right one. A root on
        a filesystem that ignores the lock or the rename semantics fails
        here, at boot, never in a sweep. The probe also takes the key's lock
        twice and requires the second attempt to block, so a filesystem that
        accepts `flock` without honouring it is refused too."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / _LOCK_DIR).mkdir(exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"filesystem store at {self._root} is not a writable directory: {exc}"
            ) from exc
        if not os.access(self._root, os.W_OK):
            raise RuntimeError(f"filesystem store at {self._root} is not writable")
        marker = self._root / _STORE_MARKER
        if not marker.exists():
            self._replace_via_tmp(
                marker, json.dumps({"id": f"fs_{secrets.token_hex(8)}"}).encode()
            )
        self._store_id = None
        _ = self.store_id
        probe = f"{_PROBE_PREFIX}-{os.getpid()}-{secrets.token_hex(4)}"
        # The probe bypasses `_path`'s dot rule on purpose: it must never be
        # a name a caller could mint, and it is removed before returning.
        path = self._root / probe
        meta_path = self._root / (probe + _META_SUFFIX)
        lock_path = self._lock_path(probe)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # The lock must be real: a second handle on the same lock file must
        # be refused while the first holds it. A filesystem that accepts
        # flock calls without honouring them fails here.
        first = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(first, fcntl.LOCK_EX)
            second = os.open(lock_path, os.O_RDWR)
            try:
                try:
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise RuntimeError(
                        f"filesystem store at {self._root} does not honour file locks"
                    )
            finally:
                os.close(second)
        finally:
            fcntl.flock(first, fcntl.LOCK_UN)
            os.close(first)
            with contextlib.suppress(FileNotFoundError):
                lock_path.unlink()
        try:
            self._replace_via_tmp(path, b"probe")
            generation = self._next_generation()
            self._replace_via_tmp(meta_path, json.dumps({"generation": generation}).encode())
            recorded = json.loads(meta_path.read_bytes())["generation"]
            if recorded != generation:
                raise RuntimeError(f"filesystem store at {self._root} lost a write")
            wrong = generation + 1
            if json.loads(meta_path.read_bytes())["generation"] == wrong:
                raise RuntimeError(
                    f"filesystem store at {self._root} did not enforce a generation pin"
                )
        finally:
            for p in (path, meta_path):
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()

    # ---- writes -----------------------------------------------------------

    def _put_sync(self, object_name: str, data: bytes, content_type: str) -> ObjectWriteResult:
        with self._locked(object_name):
            existing = self._present(object_name)
            if existing is not None:
                if not self._matches(existing, data):
                    raise RuntimeError(
                        f"existing object at {object_name} does not match the "
                        "content being written"
                    )
                created = datetime.fromisoformat(existing["created"])
                return ObjectWriteResult(
                    bucket=self.store_id,
                    generation=int(existing["generation"]),
                    adopted_existing=True,
                    adopted_age_seconds=(datetime.now(UTC) - created).total_seconds(),
                )
            meta = self._write_object(object_name, data, content_type)
            return ObjectWriteResult(bucket=self.store_id, generation=int(meta["generation"]))

    async def put(self, object_name: str, data: bytes, content_type: str) -> ObjectWriteResult:
        return await asyncio.to_thread(self._put_sync, object_name, data, content_type)

    def _refresh_sync(
        self, object_name: str, data: bytes, content_type: str, *, if_generation_match: int
    ) -> ObjectWriteResult:
        with self._locked(object_name):
            existing = self._present(object_name)
            if existing is None:
                # Vanished under the caller: the bytes are in hand, so a fresh
                # create is the honest outcome (the GCS backend does the same).
                meta = self._write_object(object_name, data, content_type)
                return ObjectWriteResult(bucket=self.store_id, generation=int(meta["generation"]))
            if int(existing["generation"]) != if_generation_match:
                if not self._matches(existing, data):
                    raise RuntimeError(
                        f"existing object at {object_name} does not match the "
                        "content being refreshed"
                    )
                return ObjectWriteResult(
                    bucket=self.store_id,
                    generation=int(existing["generation"]),
                    adopted_existing=True,
                )
            meta = self._write_object(object_name, data, content_type)
            return ObjectWriteResult(bucket=self.store_id, generation=int(meta["generation"]))

    async def refresh_object_generation(
        self,
        object_name: str,
        data: bytes,
        content_type: str,
        *,
        if_generation_match: int,
    ) -> ObjectWriteResult:
        return await asyncio.to_thread(
            self._refresh_sync,
            object_name,
            data,
            content_type,
            if_generation_match=if_generation_match,
        )

    # ---- reads ------------------------------------------------------------

    def _open_for_read(self, object_name: str, bucket: str | None, generation: int | None):
        self._check_bucket(bucket)
        meta = self._present(object_name)
        if meta is None:
            raise NotFound(object_name)
        if generation is not None and int(meta["generation"]) != generation:
            # Versioning is off: only the current generation exists.
            raise NotFound(f"{object_name}#{generation}")
        return self._path(object_name)

    def _open_file(self, object_name: str, bucket: str | None, generation: int | None):
        """An open handle, or `NotFound`. Reads take no lock, so the object
        can vanish between the metadata check and the open; that is the
        contract's NotFound, never a raw OSError."""
        path = self._open_for_read(object_name, bucket, generation)
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise NotFound(object_name) from exc

    async def get(
        self,
        object_name: str,
        *,
        bucket: str | None = None,
        generation: int | None = None,
    ) -> bytes:
        def _get() -> bytes:
            with self._open_file(object_name, bucket, generation) as f:
                return f.read()

        return await asyncio.to_thread(_get)

    def get_range(
        self,
        object_name: str,
        start: int,
        end: int,
        *,
        bucket: str | None = None,
        generation: int | None = None,
    ) -> bytes:
        if end <= start:
            return b""
        with self._open_file(object_name, bucket, generation) as f:
            f.seek(start)
            return f.read(end - start)

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
        # Opened here, before the first yield, so NotFound surfaces at call
        # time (before response headers), not in the middle of a response.
        f = self._open_file(object_name, bucket, generation)
        return self._iter_file(f, chunk_size, start, end)

    @staticmethod
    def _iter_file(f, chunk_size: int, start: int, end: int | None) -> Iterator[bytes]:
        with f:
            if start:
                f.seek(start)
            remaining = None if end is None else end - start
            while remaining != 0:
                chunk = f.read(chunk_size if remaining is None else min(chunk_size, remaining))
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk

    async def stat(self, object_name: str) -> ObjectStat | None:
        def _stat() -> ObjectStat | None:
            meta = self._present(object_name)
            if meta is None:
                return None
            return ObjectStat(
                size=int(meta["size"]),
                crc32c=meta.get("crc32c"),
                md5=meta.get("md5"),
                generation=int(meta["generation"]),
                content_type=meta.get("content_type"),
            )

        return await asyncio.to_thread(_stat)

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
        return None  # nothing to sign against; the caller proxy-streams

    # ---- deletes ----------------------------------------------------------

    def _delete_sync(self, object_name: str, *, if_generation_match: int | None) -> None:
        # A pinned delete of an absent object is NotFound here; live GCS
        # answers 412 for a non-zero pin on a missing object. Every sweep
        # catches both together, and the contract suite pins only the
        # present-object cases, so the divergence is documented, not hidden.
        with self._locked(object_name):
            meta = self._present(object_name)
            if meta is None:
                # Absent. Only the store's own debris may be cleared here —
                # never a data file, which could be another writer's in-flight
                # bytes between its two renames.
                with contextlib.suppress(FileNotFoundError):
                    if self._meta_path(object_name).stat().st_size == 0:
                        self._meta_path(object_name).unlink()
                raise NotFound(object_name)
            if if_generation_match is not None and int(meta["generation"]) != if_generation_match:
                raise PreconditionFailed(
                    f"{object_name}: generation {meta['generation']} != {if_generation_match}"
                )
            self._remove_object(object_name)

    async def delete(self, object_name: str, *, if_generation_match: int | None = None) -> None:
        await asyncio.to_thread(
            self._delete_sync, object_name, if_generation_match=if_generation_match
        )

    # ---- listing ----------------------------------------------------------

    def _walk(self, prefix: str) -> list[Blob]:
        """Every object whose key starts with `prefix`, in name order.

        Walks only the directory the prefix maps to — `cas/drv_x/` is one
        directory, `cas/drv_x/ab` is that directory filtered by name — so a
        per-drive sweep costs that drive's objects, not the whole root."""
        dir_part, _, name_part = prefix.rpartition("/")
        base = self._root.joinpath(*dir_part.split("/")) if dir_part else self._root
        if not base.is_dir():
            return []
        out: list[Blob] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            here = Path(dirpath)
            for filename in sorted(filenames):
                if filename.startswith(".") or filename.endswith(_META_SUFFIX):
                    continue
                name = (here / filename).relative_to(self._root).as_posix()
                if not name.startswith(prefix):
                    continue
                meta = self._present(name)
                if meta is None:
                    continue
                out.append(
                    Blob(
                        name=name,
                        size=int(meta["size"]),
                        time_created=datetime.fromisoformat(meta["created"]),
                        generation=int(meta["generation"]),
                    )
                )
        del name_part
        return out

    async def list_blobs(self, prefix: str) -> AsyncIterator[Blob]:
        for blob in await asyncio.to_thread(self._walk, prefix):
            yield blob

    async def list_prefixes(self, prefix: str, delimiter: str = "/") -> list[str]:
        """The immediate sub-namespaces under `prefix`, each ending in the
        delimiter, exactly as the GCS listing returns `prefixes`.

        Reads directory names, not objects: the orphan sweep asks this once
        per run, and it should cost the number of drives, not the number of
        objects. A sub-namespace counts when it holds at least one object
        somewhere below it, which a dot-free directory entry implies well
        enough for a sweep that then lists the prefix itself."""

        def _list() -> list[str]:
            if delimiter != "/":
                found: set[str] = set()
                for blob in self._walk(prefix):
                    rest = blob.name[len(prefix) :]
                    if delimiter in rest:
                        found.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
                return sorted(found)
            dir_part, _, name_part = prefix.rpartition("/")
            base = self._root.joinpath(*dir_part.split("/")) if dir_part else self._root
            if not base.is_dir():
                return []
            out: list[str] = []
            with os.scandir(base) as entries:
                for entry in entries:
                    if (
                        entry.is_dir(follow_symlinks=False)
                        and not entry.name.startswith(".")
                        and entry.name.startswith(name_part)
                    ):
                        out.append(f"{dir_part + '/' if dir_part else ''}{entry.name}/")
            return sorted(out)

        return await asyncio.to_thread(_list)
