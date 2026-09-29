"""The GC's mark-sweep refuses a store that cannot honour a generation pin.

`ArtifactGC.tla` proves the sweep safe on the assumption that a delete pinned
to a listed generation misses when the object was re-put in between. A
backend that reports it cannot enforce that pin must not be swept blind;
the capability flag is what the sweeper checks, and this pins that it does.
"""

from __future__ import annotations

import pytest

from agentdrive import storage
from agentdrive.core.gc import GCSweeper, SweepResult
from agentdrive.storage.base import Capabilities


async def test_mark_sweep_refuses_a_store_without_pinned_deletes(monkeypatch):
    monkeypatch.setattr(
        storage,
        "capabilities",
        lambda: Capabilities(
            generation_pinned_delete=False, signed_download=False, direct_transfer=False
        ),
    )
    with pytest.raises(RuntimeError, match="generation-pinned"):
        await GCSweeper()._mark_sweep(None, SweepResult())


def test_both_shipped_backends_declare_pinned_deletes():
    """Neither backend trips the guard: GCS enforces the pin natively and the
    filesystem store proves its own at boot."""
    from agentdrive.storage.fs import FilesystemStore
    from agentdrive.storage.gcs import GcsStore

    assert GcsStore().capabilities.generation_pinned_delete is True
    assert FilesystemStore("/tmp/unused").capabilities.generation_pinned_delete is True
