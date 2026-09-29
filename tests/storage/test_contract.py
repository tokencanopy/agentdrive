"""The `ObjectStore` contract, run against every backend.

One suite, parametrized over the stores this process can build: the GCS
adapter against the fake-gcs emulator the rest of the suite already uses,
and the filesystem store over a temporary directory. A behavior the GC or
the commit seam depends on is asserted here once, and both backends have to
satisfy it — that is what makes the protocol a seam rather than a name.

Where the emulator cannot honour the contract (it does not enforce delete
preconditions, and it cannot sign), the case says so with a skip that names
the reason rather than asserting the weaker behavior.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest

from agentdrive.config import settings
from agentdrive.storage import base as base_mod
from agentdrive.storage.fs import FilesystemStore
from agentdrive.storage.gcs import GcsStore


@pytest.fixture(params=["gcs", "fs"])
def store(request, tmp_path):
    if request.param == "gcs":
        s = GcsStore()
        s.ensure_store()
        return s
    s = FilesystemStore(tmp_path / "store")
    s.ensure_store()
    return s


def _key() -> str:
    """A fresh CAS-shaped key per test, so runs never collide on the shared emulator."""
    return f"{base_mod.CAS_PREFIX}drv_{secrets.token_hex(8)}/{secrets.token_hex(32)}"


def _is_emulated(store) -> bool:
    return isinstance(store, GcsStore) and bool(settings.gcs_emulator_host)


async def test_store_id_is_what_the_commit_seam_persists(store):
    if isinstance(store, GcsStore):
        assert store.store_id == settings.gcs_bucket
    else:
        assert store.store_id.startswith("fs_")  # persisted in the root, not path-derived
    write = await store.put(_key(), b"x", "text/plain")
    assert write.bucket == store.store_id


async def test_put_creates_with_a_generation_and_stat_reports_it(store):
    key = _key()
    write = await store.put(key, b"hello", "text/plain")
    assert write.generation is not None and write.generation > 0
    assert write.adopted_existing is False
    st = await store.stat(key)
    assert st is not None
    assert st.size == 5
    assert st.generation == write.generation
    assert st.content_type == "text/plain"
    assert st.crc32c  # canonical padded base64, the GCS metadata form


async def test_put_of_identical_content_adopts_the_existing_generation(store):
    key = _key()
    first = await store.put(key, b"same", "text/plain")
    second = await store.put(key, b"same", "text/plain")
    assert second.adopted_existing is True
    assert second.generation == first.generation
    # The emulator's clock can run slightly ahead of the host's, so an
    # adoption a few milliseconds old may read as slightly negative there.
    assert second.adopted_age_seconds is not None and second.adopted_age_seconds > -5


async def test_put_of_different_content_under_a_cas_key_is_corruption(store):
    key = _key()
    await store.put(key, b"one", "text/plain")
    with pytest.raises(RuntimeError, match="does not match"):
        await store.put(key, b"two", "text/plain")


async def test_refresh_mints_a_new_generation_when_the_pin_matches(store):
    key = _key()
    first = await store.put(key, b"same", "text/plain")
    refreshed = await store.refresh_object_generation(
        key, b"same", "text/plain", if_generation_match=first.generation
    )
    assert refreshed.adopted_existing is False
    assert refreshed.generation is not None and refreshed.generation != first.generation
    st = await store.stat(key)
    assert st is not None and st.generation == refreshed.generation


async def test_refresh_with_a_stale_pin_adopts_the_current_object(store):
    if _is_emulated(store):
        pytest.skip("fake-gcs does not enforce upload preconditions")
    key = _key()
    first = await store.put(key, b"same", "text/plain")
    result = await store.refresh_object_generation(
        key, b"same", "text/plain", if_generation_match=first.generation + 1
    )
    assert result.adopted_existing is True
    assert result.generation == first.generation


async def test_delete_pinned_to_a_stale_generation_is_refused(store):
    if _is_emulated(store):
        pytest.skip("fake-gcs does not enforce delete preconditions (storage/gcs.py)")
    key = _key()
    write = await store.put(key, b"pinned", "text/plain")
    with pytest.raises(base_mod.PreconditionFailed):
        await store.delete(key, if_generation_match=write.generation + 1)
    assert await store.stat(key) is not None
    await store.delete(key, if_generation_match=write.generation)
    assert await store.stat(key) is None


async def test_delete_of_a_missing_object_raises_the_contract_not_found(store):
    with pytest.raises(base_mod.NotFound):
        await store.delete(_key())


async def test_get_and_ranges_and_stream_agree(store):
    key = _key()
    payload = bytes(range(256)) * 4
    write = await store.put(key, payload, "application/octet-stream")
    assert await store.get(key) == payload
    assert await store.get(key, bucket=store.store_id, generation=write.generation) == payload
    assert store.get_range(key, 10, 20) == payload[10:20]
    assert store.get_range(key, 20, 10) == b""
    assert b"".join(store.stream(key, 100)) == payload
    assert b"".join(store.stream(key, 7, start=5, end=300)) == payload[5:300]


async def test_get_of_a_missing_object_raises_the_contract_not_found(store):
    with pytest.raises(base_mod.NotFound):
        await store.get(_key())


async def test_filesystem_store_answers_only_for_its_own_id(tmp_path):
    store = FilesystemStore(tmp_path / "s")
    store.ensure_store()
    key = _key()
    await store.put(key, b"x", "text/plain")
    with pytest.raises(base_mod.NotFound):
        await store.get(key, bucket="some-other-store")
    with pytest.raises(base_mod.NotFound):
        await store.get(key, generation=1)  # versioning off: only the current generation exists


async def test_list_blobs_and_prefixes_enumerate_the_namespace(store):
    drive = f"drv_{secrets.token_hex(8)}"
    other = f"drv_{secrets.token_hex(8)}"
    keys = [f"{base_mod.CAS_PREFIX}{drive}/{secrets.token_hex(32)}" for _ in range(3)]
    for k in keys:
        await store.put(k, b"blob", "text/plain")
    await store.put(f"{base_mod.CAS_PREFIX}{other}/{secrets.token_hex(32)}", b"o", "text/plain")
    listed = [b async for b in store.list_blobs(f"{base_mod.CAS_PREFIX}{drive}/")]
    assert sorted(b.name for b in listed) == sorted(keys)
    for b in listed:
        assert b.size == 4
        assert b.generation is not None
        assert b.time_created.tzinfo is not None
        assert b.time_created <= datetime.now(UTC) + timedelta(seconds=5)  # emulator clock skew
    prefixes = await store.list_prefixes(base_mod.CAS_PREFIX)
    assert f"{base_mod.CAS_PREFIX}{drive}/" in prefixes
    assert f"{base_mod.CAS_PREFIX}{other}/" in prefixes
    assert all(p.endswith("/") for p in prefixes)


async def test_signed_download_is_none_when_the_store_cannot_sign(store):
    if isinstance(store, GcsStore) and not settings.gcs_emulator_host:
        pytest.skip("a real GCS credential may sign")
    key = _key()
    await store.put(key, b"x", "text/plain")
    assert (
        await store.signed_download_url(
            key, content_type="text/plain", filename="x.txt", ttl_s=60
        )
        is None
    )
    assert store.capabilities.signed_download is False


def test_filesystem_store_refuses_names_that_could_leave_the_root(tmp_path):
    store = FilesystemStore(tmp_path / "s")
    store.ensure_store()
    for bad in ("/abs", "a/../b", "a//b", "a/./b", "", "x.meta.json", "a/b/"):
        with pytest.raises(ValueError):
            store._path(bad)


def test_filesystem_store_probe_leaves_only_the_stores_own_files(tmp_path):
    """`ensure_store` writes a probe, proves the pin and the lock, and leaves
    behind only the identity marker and the lock directory."""
    store = FilesystemStore(tmp_path / "s")
    store.ensure_store()
    assert sorted(p.name for p in (tmp_path / "s").iterdir()) == [".agentdrive-store", ".locks"]


async def test_filesystem_generations_never_reuse_after_delete_and_recreate(tmp_path):
    store = FilesystemStore(tmp_path / "s")
    store.ensure_store()
    key = _key()
    first = await store.put(key, b"a", "text/plain")
    await store.delete(key)
    second = await store.put(key, b"a", "text/plain")
    assert second.generation > first.generation
