"""Settings → the one `ObjectStore` this process uses.

Built once, on first use (the lifespan's `ensure_store` is the first), so
importing the storage package costs nothing and a test can swap the store
by resetting it. `STORAGE_BACKEND` chooses the implementation; the settings
validators have already guaranteed that the backend's own configuration
(`GCS_BUCKET`, or `STORAGE_FS_ROOT`) is present.
"""

from __future__ import annotations

from ..config import settings
from .base import ObjectStore

_store: ObjectStore | None = None


def build_store() -> ObjectStore:
    if settings.storage_backend == "fs":
        from .fs import FilesystemStore

        return FilesystemStore(settings.storage_fs_root)
    from .gcs import GcsStore

    return GcsStore()


def current_store() -> ObjectStore:
    global _store
    if _store is None:
        _store = build_store()
    return _store


def reset_store() -> None:
    """Forget the cached store so the next call rebuilds it from settings.
    Test-only: production settings do not change while a process runs."""
    global _store
    _store = None
