"""Mount-prefix foundation: u() helper, root_path wiring, startup self-check.
Pure unit tests — no fixtures."""

import pytest

# Imported at module top — NOT inside the test bodies. Importing
# agentdrive.app constructs `FastAPI(root_path=settings.mount_prefix…)`
# PROCESS-WIDE; doing it inside a test while `mount_prefix` is
# monkeypatched would bake the patched value into the app object every
# later test (e.g. test_static_cache via the `client` fixture) then
# uses — an ordering-dependent red. At collection time settings are
# unpatched, so the app object is built with the real configured prefix.
from agentdrive.app import check_mount_config


def test_self_check_rejects_mismatched_base_url(monkeypatch):
    from agentdrive.config import settings
    monkeypatch.setattr(settings, "mount_prefix", "/drive")
    monkeypatch.setattr(settings, "public_base_url", "https://agentdrive.run")
    with pytest.raises(RuntimeError, match="MOUNT_PREFIX"):
        check_mount_config()


def test_self_check_accepts_matching_config(monkeypatch):
    from agentdrive.config import settings
    monkeypatch.setattr(settings, "mount_prefix", "/drive")
    monkeypatch.setattr(settings, "public_base_url", "https://app.tokencanopy.com/drive")
    check_mount_config()  # no raise
