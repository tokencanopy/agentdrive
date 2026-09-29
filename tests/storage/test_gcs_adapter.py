"""The GCS adapter's boundary: provider exceptions never leave it, and a
refresh of an object that vanished under the caller recreates it (the same
answer the filesystem store gives), pinned with a fake bucket because the
emulator enforces no upload preconditions and so cannot reach these paths.
"""

from __future__ import annotations

import pytest
from google.api_core import exceptions as gcs_exc

from agentdrive.storage import base as base_mod
from agentdrive.storage import gcs as gcs_mod
from agentdrive.storage.gcs import GcsStore


class _Blob:
    """A blob whose upload always 412s and whose reload behaves as scripted."""

    def __init__(self, bucket, name, *, reload_outcomes: list):
        self._bucket = bucket
        self.name = name
        self._reloads = reload_outcomes
        self.generation = None
        self.size = None
        self.crc32c = None
        self.time_created = None
        self.uploads: list[int | None] = self._bucket.uploads

    def upload_from_string(self, data, content_type, if_generation_match=None):
        self.uploads.append(if_generation_match)
        if self._bucket.create_succeeds and if_generation_match == 0:
            self.generation = 777
            return
        raise gcs_exc.PreconditionFailed("412")

    def reload(self):
        outcome = self._reloads.pop(0) if self._reloads else "present"
        if outcome == "missing":
            raise gcs_exc.NotFound("404")
        self.generation = 5
        self.size = 5
        self.crc32c = None


class _Bucket:
    name = "fake-bucket"

    def __init__(self, *, reload_outcomes, create_succeeds=False):
        self._outcomes = reload_outcomes
        self.create_succeeds = create_succeeds
        self.uploads: list[int | None] = []

    def blob(self, name, generation=None):
        return _Blob(self, name, reload_outcomes=self._outcomes)


async def test_refresh_of_a_vanished_object_recreates_it_with_a_fresh_generation(monkeypatch):
    """412 on the pinned re-upload, then 404 on the reload: the object was
    swept between the adoption and the refresh. The bytes are in hand, so the
    adapter creates it anew rather than surfacing the provider's NotFound."""
    bucket = _Bucket(reload_outcomes=["missing"], create_succeeds=True)
    monkeypatch.setattr(gcs_mod, "_bucket", lambda: bucket)
    result = await GcsStore().refresh_object_generation(
        "cas/drv_x/digest", b"bytes", "text/plain", if_generation_match=5
    )
    assert result.adopted_existing is False
    assert result.generation == 777
    assert bucket.uploads == [5, 0]  # the pinned attempt, then create-only


async def test_put_translates_a_provider_not_found_into_the_contracts(monkeypatch):
    """A create that 412s, whose object then vanishes, and whose retry 412s
    again with the object gone once more: the adapter's caller sees
    storage.NotFound, never google.api_core's class."""
    bucket = _Bucket(reload_outcomes=["missing", "missing"], create_succeeds=False)
    monkeypatch.setattr(gcs_mod, "_bucket", lambda: bucket)
    with pytest.raises(base_mod.NotFound):
        await GcsStore().put("cas/drv_x/digest", b"bytes", "text/plain")


async def test_refresh_translates_a_provider_precondition_failure(monkeypatch):
    class _PreconditionBucket(_Bucket):
        def blob(self, name, generation=None):
            blob = super().blob(name, generation)

            def upload(*a, **k):
                raise gcs_exc.PreconditionFailed("412")

            def reload():
                raise gcs_exc.PreconditionFailed("412 on reload, oddly")

            blob.upload_from_string = upload
            blob.reload = reload
            return blob

    monkeypatch.setattr(gcs_mod, "_bucket", lambda: _PreconditionBucket(reload_outcomes=[]))
    with pytest.raises(base_mod.PreconditionFailed):
        await GcsStore().refresh_object_generation(
            "cas/drv_x/digest", b"bytes", "text/plain", if_generation_match=5
        )
