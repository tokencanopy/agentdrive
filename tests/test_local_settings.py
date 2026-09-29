"""AUTH_MODE=local at the Settings boundary.

Opaque API keys (§4.2 as amended 2026-09-21) removed everything local mode
used to derive — an issuer, two audiences, a key file, an explicit origin —
so what is left to test is mostly what local mode no longer demands, and the
two retired hub-mode settings it refuses by name so an upgrade from #723
stops rather than silently ignoring an operator's key file.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentdrive.config import Settings

ORIGIN = "http://drive.example.test"


def _local(tmp_path, **overrides):
    defaults = dict(
        database_url="postgresql://x:x@localhost:5432/x",
        session_secret="x" * 64,
        storage_backend="fs",
        storage_fs_root=str(tmp_path / "store"),
        auth_mode="local",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_local_mode_needs_no_origin_issuer_or_audience(tmp_path, monkeypatch):
    """#723 refused local mode without an explicit PUBLIC_BASE_URL because
    the issuer, both audiences and the sidecar's JWT tuple were derived from
    it. None of those exist now, so a self-hoster configures a database, a
    storage root and nothing else."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.delenv("AUTH_ISSUER", raising=False)
    monkeypatch.delenv("HUB_ISSUER", raising=False)
    monkeypatch.delenv("HUB_PRODUCT_AUDIENCE", raising=False)
    s = _local(tmp_path)
    assert s.auth_mode == "local"
    assert s.auth_local_signing_key_file == ""


def test_the_retired_signing_key_setting_is_refused_by_name(tmp_path):
    """An upgrade that leaves #723's key file configured must stop, not
    quietly ignore it: the operator would otherwise believe their tokens
    still verify."""
    with pytest.raises(ValidationError, match="AUTH_LOCAL_SIGNING_KEY_FILE"):
        _local(tmp_path, auth_local_signing_key_file=str(tmp_path / "key.pem"))


def test_auth_jwks_url_is_refused_in_local_mode(tmp_path):
    with pytest.raises(ValidationError, match="AUTH_JWKS_URL"):
        _local(tmp_path, auth_jwks_url="https://other.example.test/jwks")


def test_the_hub_issuer_and_audience_settings_are_refused_in_local_mode(tmp_path):
    """#723 REQUIRED `AUTH_ISSUER` to equal the install's origin and derived
    the audiences from it, so an upgrade from that release most likely carries
    them. Nothing reads them now, and silently dropping them is the same
    defect the key-file refusal exists for."""
    for setting, value in (
        ("hub_issuer", ORIGIN),
        ("hub_product_audience", ORIGIN),
        ("hub_mcp_audience", ORIGIN + "/mcp"),
    ):
        with pytest.raises(ValidationError, match="hub-mode settings"):
            _local(tmp_path, **{setting: value})


def test_an_explicit_origin_is_still_allowed(tmp_path):
    """Local mode ignores the origin for auth, but the renderer and the
    permalinks still use it, so setting it must not become an error."""
    s = _local(tmp_path, public_base_url=ORIGIN)
    assert s.public_base_url == ORIGIN


def test_auth_issuer_env_replaces_hub_issuer_and_the_alias_still_works(monkeypatch):
    base = dict(
        database_url="postgresql://x:x@localhost:5432/x",
        gcs_bucket="x",
        session_secret="x" * 64,
    )
    monkeypatch.delenv("AUTH_ISSUER", raising=False)
    monkeypatch.setenv("HUB_ISSUER", "https://hub.example.test/oidc")
    assert Settings(_env_file=None, **base).hub_issuer == "https://hub.example.test/oidc"
    monkeypatch.setenv("AUTH_ISSUER", "https://issuer.example.test/oidc")
    assert Settings(_env_file=None, **base).hub_issuer == "https://issuer.example.test/oidc"


def test_hub_mode_is_untouched_by_the_local_validator(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTH_ISSUER", raising=False)
    monkeypatch.delenv("HUB_ISSUER", raising=False)
    s = Settings(
        _env_file=None,
        database_url="postgresql://x:x@localhost:5432/x",
        gcs_bucket="x",
        session_secret="x" * 64,
        # Both refusals above are LOCAL-mode only: a hosted deployment that
        # carries either (none does) keeps booting exactly as before.
        auth_jwks_url="https://hub.example.test/jwks",
        auth_local_signing_key_file=str(tmp_path / "unread.pem"),
    )
    assert s.auth_mode == "hub"
    assert s.hub_issuer == "http://localhost:8080/oidc"
    assert s.hub_mcp_audience == s.hub_product_audience + "/mcp"
