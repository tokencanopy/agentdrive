"""The filesystem store's own guarantees: the ones the contract suite
cannot express because they are about concurrency, crashes and the root.

Each test here pins a finding from the two review passes on the first cut of
the store, so the failure mode it names cannot come back quietly.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from agentdrive.storage import base as base_mod
from agentdrive.storage import fs as fs_mod
from agentdrive.storage.fs import FilesystemStore

KEY = "cas/drv_00000000000000aa/" + "ab" * 32


@pytest.fixture
def store(tmp_path):
    s = FilesystemStore(tmp_path / "store")
    s.ensure_store()
    return s


# ---- the lock ----------------------------------------------------------------


def test_the_per_key_lock_excludes_concurrent_writers_and_deleters(store):
    """Review finding (both passes): the first cut locked the sidecar file,
    which the delete unlinked, so a second caller could take an "exclusive"
    lock on a fresh inode while the first still held the old one — tens of
    thousands of violations in seconds. The lock now lives on a file that is
    never unlinked; peak concurrency inside the critical section must be 1."""
    inside = 0
    peak = 0
    guard = threading.Lock()
    original = store._locked

    class Counted:
        def __init__(self, name):
            self._cm = original(name)

        def __enter__(self):
            nonlocal inside, peak
            self._cm.__enter__()
            with guard:
                inside += 1
                peak = max(peak, inside)
            return self

        def __exit__(self, *exc):
            nonlocal inside
            with guard:
                inside -= 1
            return self._cm.__exit__(*exc)

    store._locked = Counted  # type: ignore[method-assign]
    stop = time.monotonic() + 1.0
    errors: list[BaseException] = []

    def writer():
        while time.monotonic() < stop:
            try:
                store._put_sync(KEY, b"same bytes", "text/plain")
            except BaseException as exc:  # noqa: BLE001 — collected below
                errors.append(exc)

    def refresher():
        while time.monotonic() < stop:
            try:
                meta = store._present(KEY)
                if meta is not None:
                    store._refresh_sync(
                        KEY, b"same bytes", "text/plain",
                        if_generation_match=int(meta["generation"]),
                    )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    def deleter():
        while time.monotonic() < stop:
            meta = store._present(KEY)
            if meta is None:
                continue
            try:
                store._delete_sync(KEY, if_generation_match=int(meta["generation"]))
            except (base_mod.NotFound, base_mod.PreconditionFailed):
                pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=t) for t in (writer, writer, refresher, refresher, deleter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert peak == 1
    # Whatever the final state, it is a whole object or nothing.
    data_present = store._path(KEY).exists()
    meta_present = store._read_meta(KEY) is not None
    assert data_present == meta_present


def test_a_delete_cannot_remove_the_bytes_of_an_in_flight_write(store, monkeypatch):
    """The adversarial reproduction: writer B has renamed its bytes into place
    and is about to rename its sidecar; deleter C, having found the key
    absent, must not remove B's data file. With a real lock C cannot even
    run until B is done; the NotFound branch is additionally forbidden from
    touching a data file, so the guarantee holds twice over."""
    paused = threading.Event()
    release = threading.Event()
    real_replace = os.replace

    def pausing_replace(src, dst):
        real_replace(src, dst)
        if str(dst).endswith(fs_mod._META_SUFFIX) is False and not paused.is_set():
            # After the DATA rename of the first write, hold the writer.
            paused.set()
            release.wait(timeout=5)

    monkeypatch.setattr(fs_mod.os, "replace", pausing_replace)
    result: dict = {}

    def writer():
        result["write"] = store._put_sync(KEY, b"payload", "text/plain")

    b = threading.Thread(target=writer)
    b.start()
    assert paused.wait(timeout=5)
    # C runs while B sits between its two renames.
    outcomes: dict = {}

    def deleter():
        try:
            store._delete_sync(KEY, if_generation_match=None)
            outcomes["delete"] = "deleted"
        except base_mod.NotFound:
            outcomes["delete"] = "not-found"

    c = threading.Thread(target=deleter)
    c.start()
    time.sleep(0.2)
    release.set()
    b.join(timeout=5)
    c.join(timeout=5)
    # Either C waited for B and then deleted a whole object, or C never saw
    # it; in no case does B's generation refer to bytes that are gone.
    if outcomes["delete"] == "deleted":
        assert store._present(KEY) is None
        assert not store._path(KEY).exists()
    else:
        assert store._present(KEY) is not None
        assert store._path(KEY).read_bytes() == b"payload"
    assert result["write"].generation is not None


# ---- reads and writes under churn ------------------------------------------


def test_a_read_of_an_object_deleted_after_its_metadata_check_is_not_found(store, monkeypatch):
    """Reads take no lock, so the data file can vanish between the metadata
    check and the open; that is the contract's NotFound, never a raw OSError."""
    store._put_sync(KEY, b"bytes", "text/plain")
    real = store._open_for_read

    def check_then_delete(*args, **kwargs):
        path = real(*args, **kwargs)
        store._path(KEY).unlink()
        return path

    monkeypatch.setattr(store, "_open_for_read", check_then_delete)
    with pytest.raises(base_mod.NotFound):
        store.get_range(KEY, 0, 3)
    store._put_sync(KEY, b"bytes", "text/plain")
    with pytest.raises(base_mod.NotFound):
        list(store.stream(KEY))


def test_a_failed_write_leaves_no_temp_file_and_no_object(store, monkeypatch):
    real_write = os.write

    def enospc(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fs_mod.os, "write", enospc)
    with pytest.raises(OSError):
        store._put_sync(KEY, b"bytes", "text/plain")
    monkeypatch.setattr(fs_mod.os, "write", real_write)
    leftovers = [p for p in store.root.rglob("*") if p.name.endswith(fs_mod._TMP_SUFFIX)]
    assert leftovers == []
    assert store._present(KEY) is None


def test_refresh_of_a_vanished_object_recreates_it(store):
    first = store._put_sync(KEY, b"bytes", "text/plain")
    store._delete_sync(KEY, if_generation_match=None)
    again = store._refresh_sync(
        KEY, b"bytes", "text/plain", if_generation_match=first.generation
    )
    assert again.adopted_existing is False
    assert store._present(KEY) is not None
    assert again.generation != first.generation


def test_pinned_delete_of_a_missing_object_is_not_found(store):
    with pytest.raises(base_mod.NotFound):
        store._delete_sync(KEY, if_generation_match=12345)


# ---- identity and the root -------------------------------------------------


def test_store_id_survives_a_rename_of_the_root(tmp_path):
    """The id is persisted in the root, so moving the directory (or a
    different working directory for a relative root) keeps every row that
    names it readable. A path-derived id orphaned them all."""
    root = tmp_path / "before"
    store = FilesystemStore(root)
    store.ensure_store()
    original = store.store_id
    assert original.startswith("fs_")
    root.rename(tmp_path / "after")
    moved = FilesystemStore(tmp_path / "after")
    moved.ensure_store()
    assert moved.store_id == original
    other = FilesystemStore(tmp_path / "elsewhere" / "after")
    other.ensure_store()
    assert other.store_id != original  # a same-named root is a different store


def test_ensure_store_names_the_root_when_it_is_not_a_writable_directory(tmp_path):
    as_file = tmp_path / "not-a-dir"
    as_file.write_bytes(b"x")
    with pytest.raises(RuntimeError, match=str(as_file)):
        FilesystemStore(as_file).ensure_store()
    read_only = tmp_path / "ro"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        if os.access(read_only, os.W_OK):
            pytest.skip("running as a user the read-only bit does not bind")
        with pytest.raises(RuntimeError, match=str(read_only)):
            FilesystemStore(read_only).ensure_store()
    finally:
        read_only.chmod(0o700)


def test_ensure_store_leaves_nothing_but_the_marker_and_lock_dir(tmp_path):
    store = FilesystemStore(tmp_path / "s")
    store.ensure_store()
    names = sorted(p.name for p in (tmp_path / "s").iterdir())
    assert names == sorted([fs_mod._LOCK_DIR, fs_mod._STORE_MARKER])
    assert json.loads((tmp_path / "s" / fs_mod._STORE_MARKER).read_text())["id"] == store.store_id


# ---- names and listing -----------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "/abs", "a/../b", "a//b", "a/./b", "", "a/b/",
        "x.meta.json", "cas/drv_a/.hidden.tmp", ".agentdrive-store-probe-1",
        "cas/.locks/x", "cas/drv_a/x.tmp",
    ],
)
def test_names_that_could_leave_the_root_or_collide_with_store_files_are_refused(store, bad):
    with pytest.raises(ValueError):
        store._path(bad)


def test_listing_walks_only_the_prefix_and_skips_store_files(store):
    keys = [f"cas/drv_a/{i:02d}" + "0" * 62 for i in range(3)]
    for k in keys:
        store._put_sync(k, b"x", "text/plain")
    store._put_sync("cas/drv_b/" + "1" * 64, b"x", "text/plain")
    store._put_sync("obj/drv_a/upload-1", b"x", "text/plain")
    # Debris a crash or another process could leave: invisible to listings.
    (store.root / "cas" / "drv_a" / ".zz.tmp").write_bytes(b"")
    (store.root / "cas" / "drv_a" / ".stray").write_bytes(b"")
    listed = [b.name for b in store._walk("cas/drv_a/")]
    assert listed == sorted(keys)
    # A prefix that ends mid-name filters by name within its directory.
    assert [b.name for b in store._walk("cas/drv_a/01")] == [keys[1]]
    assert store._walk("cas/drv_missing/") == []
    prefixes = [
        p for p in __import__("asyncio").run(store.list_prefixes("cas/"))
    ]
    assert prefixes == ["cas/drv_a/", "cas/drv_b/"]


def test_deletes_and_sibling_writes_in_one_directory_never_fail_each_other(store):
    """Adversarial re-probe: removing a namespace directory after its last
    delete raced a sibling key's mkdir and temp open (249 raw OS errors in
    3 s). The directory now stays; three threads putting and deleting
    sibling keys must see no error at all."""
    keys = [f"cas/drv_00000000000000bb/{i:02d}" + "0" * 62 for i in range(3)]
    stop = time.monotonic() + 1.0
    errors: list[BaseException] = []

    def churn(key):
        while time.monotonic() < stop:
            try:
                store._put_sync(key, b"x", "text/plain")
                store._delete_sync(key, if_generation_match=None)
            except base_mod.NotFound:
                pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=churn, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert (store.root / fs_mod._LOCK_DIR).exists()  # locks are never removed


def test_root_is_resolved_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = FilesystemStore("./relative-store")
    assert store.root == (tmp_path / "relative-store").resolve()
    assert Path(store.root).is_absolute()
