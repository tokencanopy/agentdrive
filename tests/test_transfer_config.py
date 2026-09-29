"""B3 packet 2 — the complete, fail-closed §9 direct-transfer configuration
surface (2026-08-14 direct-transfer design §9).

Packet 1 shipped only the two hard logical ceilings and refused enablement
outright. Packet 2 lands the REST of the §9 configuration interface —
origin, endpoints, bucket/prefixes, TTLs, session/rate limits — so the
completeness validator can be real: an enabled configuration must be
COMPLETE and bounded or the process never becomes ready. Defaults stay
disabled; B8 owns every launch value. All fixture values are synthetic.
"""

import pytest

from agentdrive.config import Settings

_BASE = {
    "_env_file": None,
    "database_url": "postgresql://t:t@localhost:5432/x",
    # Pinned: direct transfer is GCS-only, and these cases are about the §9
    # completeness rules, not about which backend the environment selected.
    "storage_backend": "gcs",
    "gcs_bucket": "artifact-bucket-demo",
    "session_secret": "s" * 48,
}

# A complete, bounded, synthetic §9 surface. Every value here is a test
# fixture, not a recommendation — B8 chooses real values from staging
# evidence.
_COMPLETE = {
    "direct_transfer_enabled": True,
    "direct_transfer_min_bytes": 1,
    "direct_transfer_max_bytes": 10_000_000,
    "direct_transfer_allow_zero_bytes": False,
    "direct_transfer_session_ttl_seconds": 3600,
    "direct_transfer_terminal_retention_seconds": 86_400,
    "direct_transfer_gc_grace_seconds": 3600,
    "direct_transfer_max_active_sessions_principal": 4,
    "direct_transfer_max_active_sessions_workspace": 16,
    "direct_transfer_max_active_sessions_drive": 8,
    "direct_transfer_rate_principal": 60,
    "direct_transfer_rate_workspace": 600,
    "direct_transfer_rate_drive": 300,
    "direct_transfer_hard_logical_version_bytes_workspace": 10**9,
    "direct_transfer_hard_logical_version_bytes_drive": 10**8,
    "direct_transfer_canonical_browser_origin": "https://console.example.test",
    "direct_transfer_upload_endpoint": "https://storage.example.test",
    "direct_transfer_download_endpoint": "https://storage.example.test",
    "direct_transfer_bucket": "transfer-bucket-demo",
    "direct_transfer_scratch_prefix": "transfer-scratch/",
    "direct_transfer_immutable_prefix": "transfer-immutable/",
    "direct_download_capability_ttl_seconds": 300,
}


def _settings(**overrides):
    merged = {**_BASE, **_COMPLETE, **overrides}
    return Settings(**merged)


def test_defaults_are_disabled_and_empty():
    """The shipped default is OFF with no permissive value anywhere."""
    s = Settings(**_BASE)
    assert s.direct_transfer_enabled is False
    assert s.direct_transfer_canonical_browser_origin == ""
    assert s.direct_transfer_upload_endpoint == ""
    assert s.direct_transfer_download_endpoint == ""
    assert s.direct_transfer_bucket == ""
    assert s.direct_transfer_scratch_prefix == ""
    assert s.direct_transfer_immutable_prefix == ""
    assert s.direct_transfer_min_bytes is None
    assert s.direct_transfer_max_bytes is None
    assert s.direct_transfer_allow_zero_bytes is None
    assert s.direct_transfer_session_ttl_seconds is None
    assert s.direct_transfer_terminal_retention_seconds is None
    assert s.direct_transfer_gc_grace_seconds is None
    assert s.direct_transfer_max_active_sessions_principal is None
    assert s.direct_transfer_max_active_sessions_workspace is None
    assert s.direct_transfer_max_active_sessions_drive is None
    assert s.direct_transfer_rate_principal is None
    assert s.direct_transfer_rate_workspace is None
    assert s.direct_transfer_rate_drive is None
    assert s.direct_download_capability_ttl_seconds is None


def test_complete_enabled_configuration_boots():
    """The full bounded surface is accepted — the §9 interface exists.

    Nothing mounts a transfer route in packet 2, so this proves only that
    the completeness validator can be satisfied, not that any endpoint
    serves."""
    s = _settings()
    assert s.direct_transfer_enabled is True


@pytest.mark.parametrize(
    "missing",
    [
        "direct_transfer_min_bytes",
        "direct_transfer_max_bytes",
        "direct_transfer_allow_zero_bytes",
        "direct_transfer_session_ttl_seconds",
        "direct_transfer_terminal_retention_seconds",
        "direct_transfer_gc_grace_seconds",
        "direct_transfer_max_active_sessions_principal",
        "direct_transfer_max_active_sessions_workspace",
        "direct_transfer_max_active_sessions_drive",
        "direct_transfer_rate_principal",
        "direct_transfer_rate_workspace",
        "direct_transfer_rate_drive",
        "direct_transfer_hard_logical_version_bytes_workspace",
        "direct_transfer_hard_logical_version_bytes_drive",
        "direct_download_capability_ttl_seconds",
    ],
)
def test_enabled_with_any_missing_numeric_field_fails_closed(missing):
    overrides = {missing: None}
    with pytest.raises(ValueError, match="incomplete"):
        _settings(**overrides)


@pytest.mark.parametrize(
    "missing",
    [
        "direct_transfer_canonical_browser_origin",
        "direct_transfer_upload_endpoint",
        "direct_transfer_download_endpoint",
        "direct_transfer_bucket",
        "direct_transfer_scratch_prefix",
        "direct_transfer_immutable_prefix",
    ],
)
def test_enabled_with_any_missing_string_field_fails_closed(missing):
    with pytest.raises(ValueError, match="incomplete"):
        _settings(**{missing: ""})


def test_disabled_tolerates_a_partial_surface():
    """While the flag is false the §9 fields may be absent — disabled is
    the one honest partial state."""
    s = Settings(**_BASE, direct_transfer_bucket="transfer-bucket-demo")
    assert s.direct_transfer_enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # wildcards and non-origins can never be the canonical browser origin
        ("direct_transfer_canonical_browser_origin", "*"),
        ("direct_transfer_canonical_browser_origin", "https://*.example.test"),
        ("direct_transfer_canonical_browser_origin", "http://console.example.test"),
        ("direct_transfer_canonical_browser_origin", "https://console.example.test/app"),
        ("direct_transfer_canonical_browser_origin", "https://user:pw@console.example.test"),
        # endpoints must be bare https origins
        ("direct_transfer_upload_endpoint", "http://storage.example.test"),
        ("direct_transfer_upload_endpoint", "https://storage.example.test/upload"),
        ("direct_transfer_upload_endpoint", "https://storage.example.test?x=1"),
        ("direct_transfer_download_endpoint", "ftp://storage.example.test"),
    ],
)
def test_enabled_rejects_non_exact_origins_and_endpoints(field, value):
    with pytest.raises(ValueError):
        _settings(**{field: value})


def test_loopback_http_is_allowed_only_for_the_upload_emulator_surface():
    """Local development runs fake-gcs over plain http on loopback; that
    stays possible for the UPLOAD endpoint and browser origin without
    weakening the non-loopback https rule. The DOWNLOAD endpoint is the
    exception to the exception (packet-4 review blocker): it hosts
    disclosed bearer targets and is HTTPS-only, loopback included."""
    s = _settings(
        direct_transfer_upload_endpoint="http://localhost:4443",
        direct_transfer_canonical_browser_origin="http://localhost:3000",
    )
    assert s.direct_transfer_enabled is True


def test_transfer_bucket_must_differ_from_the_artifact_bucket():
    """B3 §8: a DEDICATED transfer bucket — scratch/adopted objects never
    share the artifact CAS bucket."""
    with pytest.raises(ValueError, match="artifact"):
        _settings(direct_transfer_bucket="artifact-bucket-demo")


@pytest.mark.parametrize(
    ("scratch", "immutable"),
    [
        ("transfer-scratch/", "transfer-scratch/"),  # identical
        ("transfer-scratch/", "transfer-scratch/adopted/"),  # nested
        ("transfer-scratch/x/", "transfer-scratch/"),  # nested the other way
        ("transfer-scratch", "transfer-immutable/"),  # missing slash
        ("transfer-scratch/", "transfer-immutable"),  # missing slash
        ("/transfer-scratch/", "transfer-immutable/"),  # absolute-looking
    ],
)
def test_prefixes_must_be_disjoint_slash_terminated_namespaces(scratch, immutable):
    with pytest.raises(ValueError):
        _settings(
            direct_transfer_scratch_prefix=scratch,
            direct_transfer_immutable_prefix=immutable,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("direct_transfer_max_bytes", 0),
        ("direct_transfer_min_bytes", -1),
        ("direct_transfer_session_ttl_seconds", 0),
        ("direct_transfer_terminal_retention_seconds", -5),
        ("direct_transfer_gc_grace_seconds", 0),
        ("direct_transfer_max_active_sessions_principal", 0),
        ("direct_transfer_rate_workspace", 0),
        ("direct_transfer_hard_logical_version_bytes_drive", 0),
        ("direct_download_capability_ttl_seconds", 0),
    ],
)
def test_enabled_rejects_non_positive_bounds(field, value):
    with pytest.raises(ValueError):
        _settings(**{field: value})


def test_size_window_must_be_ordered():
    with pytest.raises(ValueError):
        _settings(direct_transfer_min_bytes=100, direct_transfer_max_bytes=50)


def test_zero_byte_policy_is_explicit_and_consistent():
    """§5.2: zero is neither implicitly allowed nor rejected — the enabled
    configuration decides, and it must not contradict the size window."""
    # allow_zero=False with min 0 silently permits zero: contradiction
    with pytest.raises(ValueError):
        _settings(
            direct_transfer_allow_zero_bytes=False, direct_transfer_min_bytes=0
        )
    # allow_zero=True with min > 0 can never accept zero: contradiction
    with pytest.raises(ValueError):
        _settings(
            direct_transfer_allow_zero_bytes=True, direct_transfer_min_bytes=1
        )
    s = _settings(direct_transfer_allow_zero_bytes=True, direct_transfer_min_bytes=0)
    assert s.direct_transfer_allow_zero_bytes is True


def test_session_ttl_is_bounded_by_the_provider_residual_lifetime():
    """A product session deadline beyond the provider's ~one-week resumable
    lifetime would promise recoverability the bearer cannot deliver."""
    with pytest.raises(ValueError):
        _settings(direct_transfer_session_ttl_seconds=8 * 24 * 3600)


def test_download_capability_ttl_is_short():
    """§5.7: a signed GET target is short-lived by design."""
    with pytest.raises(ValueError):
        _settings(direct_download_capability_ttl_seconds=24 * 3600)


def test_download_endpoint_is_https_only_even_on_loopback():
    """Task-4 review blocker 2: the download bearer contract is HTTPS-only.
    The loopback-HTTP exception exists for the upload/emulator surface and
    must not extend to the endpoint that hosts disclosed bearer targets."""
    from pydantic import ValidationError

    for endpoint in (
        "http://localhost:4443",
        "http://127.0.0.1:4443",
        "http://[::1]:4443",
    ):
        with pytest.raises(ValidationError):
            _settings(direct_transfer_download_endpoint=endpoint)
    # The upload endpoint deliberately KEEPS the loopback exception (the
    # fake-GCS emulator needs it); only the download boundary is stricter.
    s = _settings(direct_transfer_upload_endpoint="http://localhost:4443")
    assert s.direct_transfer_upload_endpoint == "http://localhost:4443"


def test_a_complete_configuration_is_still_refused_on_the_filesystem_backend(tmp_path):
    """The resumable protocol signs GCS URLs and pins GCS generations; a
    filesystem store cannot honour any of it, so enabling transfer there is a
    boot error — after the completeness checks, so an operator fixing a
    missing field is not told about the backend first."""
    with pytest.raises(ValueError, match="requires STORAGE_BACKEND=gcs"):
        Settings(
            **{
                **_BASE,
                **_COMPLETE,
                "storage_backend": "fs",
                "storage_fs_root": str(tmp_path),
                "gcs_bucket": "",
            }
        )
