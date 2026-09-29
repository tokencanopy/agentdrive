"""The sheet-session surface is staging-only and fails closed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentdrive.config import Settings
from agentdrive.feature_settings import FeatureSettings

SRC = Path(__file__).parents[1] / "src"


def test_sheet_sessions_default_off():
    assert Settings.model_fields["sheet_sessions_enabled"].default is False
    assert FeatureSettings.model_fields["sheet_sessions_enabled"].default is False


def _dependency_free_feature_setting(*, name: str, value: str) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() != "sheet_sessions_enabled"
    }
    env["PYTHONPATH"] = str(SRC)
    env[name] = value
    return subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "from agentdrive.feature_settings import feature_settings; "
                "print(feature_settings.sheet_sessions_enabled)"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("0", False), ("YES", True), ("off", False)],
)
def test_feature_settings_load_without_application_dependencies(value, expected):
    """The stdlib parser matches Pydantic's accepted boolean grammar."""
    assert FeatureSettings(sheet_sessions_enabled=value).sheet_sessions_enabled is expected
    result = _dependency_free_feature_setting(
        name="sheet_sessions_enabled", value=value
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


@pytest.mark.parametrize("value", [" true ", "enabled", ""])
def test_dependency_free_feature_settings_reject_invalid_values_like_pydantic(value):
    with pytest.raises(ValueError):
        FeatureSettings(sheet_sessions_enabled=value)
    result = _dependency_free_feature_setting(name="SHEET_SESSIONS_ENABLED", value=value)
    assert result.returncode != 0
    assert "SHEET_SESSIONS_ENABLED must be a boolean" in result.stderr


def _probe(enabled: bool) -> str:
    env = os.environ.copy()
    env["SHEET_SESSIONS_ENABLED"] = "true" if enabled else "false"
    script = r'''
import json
from fastapi.testclient import TestClient
from agentdrive.app import app
from agentdrive.api.v0_manifest import OPERATION_COUNT, operations
from agentdrive.config import settings

spec = app.openapi()
methods = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
served_v0 = {
    (method.upper(), path, operation["operationId"])
    for path, path_item in spec["paths"].items()
    if path == "/v0" or path.startswith("/v0/")
    for method, operation in path_item.items()
    if method in methods
}
manifest_v0 = {
    (operation["method"], operation["path"], operation["operation_id"])
    for operation in operations
}
session_paths = sorted(p for p in spec["paths"] if "/sheet-sessions" in p)
sheet_read_paths = sorted(
    p for p in spec["paths"]
    if p.endswith("/sheets") or p.endswith("/cells") and "/sheet-sessions" not in p
)
client = TestClient(app, raise_server_exceptions=False)
response = client.get(
    "/v0/drives/drv_0000000000000000/artifacts/art_0000000000000000/sheet-sessions"
)
disabled_leak_rejected = None
if not settings.sheet_sessions_enabled:
    from agentdrive.api.v0_sheets import sheet_sessions_router

    app.include_router(sheet_sessions_router)
    app.openapi_schema = None
    try:
        app.openapi()
    except RuntimeError as exc:
        disabled_leak_rejected = "inventory mismatch" in str(exc)
    else:
        disabled_leak_rejected = False
print(json.dumps({
    "operation_count": OPERATION_COUNT,
    "manifest_matches_settings": len(operations) == (
        59 if settings.sheet_sessions_enabled else 51
    ),
    "manifest_matches_openapi": manifest_v0 == served_v0,
    "session_paths": session_paths,
    "sheet_read_paths": sheet_read_paths,
    "status": response.status_code,
    "disabled_leak_rejected": disabled_leak_rejected,
}))
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().splitlines()[-1]


def test_sheet_sessions_are_absent_when_disabled():
    import json

    result = json.loads(_probe(False))
    assert result["operation_count"] == 51
    assert result["manifest_matches_settings"] is True
    assert result["manifest_matches_openapi"] is True
    assert result["session_paths"] == []
    assert len(result["sheet_read_paths"]) == 4
    assert result["status"] == 404
    assert result["disabled_leak_rejected"] is True


def test_sheet_sessions_are_mounted_only_when_enabled():
    import json

    result = json.loads(_probe(True))
    assert result["operation_count"] == 59
    assert result["manifest_matches_settings"] is True
    assert result["manifest_matches_openapi"] is True
    assert len(result["session_paths"]) == 5
    assert len(result["sheet_read_paths"]) == 4
    # The route exists; without a bearer it fails auth rather than hiding.
    assert result["status"] == 401
    assert result["disabled_leak_rejected"] is None
