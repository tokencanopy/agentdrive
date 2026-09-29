"""Settings validators that gate dangerous configuration."""

import pytest
from pydantic import ValidationError

from agentdrive.config import Settings


def _settings_with(**overrides):
    """Build a Settings() with the required fields populated so we can
    isolate the validator under test from the others.

    Passes `_env_file=None` to disable pydantic-settings' `.env` file
    loading — otherwise a developer's local `.env` (which sets
    suites cover the on-state) would override the code default we're
    trying to verify. `monkeypatch.delenv` alone isn't enough; it
    only clears the process env var, not the dotenv file."""
    defaults = dict(
        database_url="postgresql://x:x@localhost:5432/x",
        gcs_bucket="x",
        session_secret="x" * 64,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


# ---------------------------------------------------------------------------
# Hosted MCP sidecar boundary
# ---------------------------------------------------------------------------


def test_mcp_proxy_url_is_disabled_by_default():
    assert _settings_with().mcp_proxy_url == ""


@pytest.mark.parametrize(
    "url, expected",
    (
        ("http://127.0.0.1:8081/", "http://127.0.0.1:8081"),
        ("http://localhost:8081", "http://localhost:8081"),
        ("http://[::1]:8081", "http://[::1]:8081"),
    ),
)
def test_mcp_proxy_url_accepts_only_loopback_http_origins(url, expected):
    assert _settings_with(mcp_proxy_url=url).mcp_proxy_url == expected


@pytest.mark.parametrize(
    "url",
    (
        "https://127.0.0.1:8081",
        "http://127.0.0.1",
        "http://127.0.0.1:8081/mcp",
        "http://127.0.0.1:8081?x=1",
        "http://user:pass@127.0.0.1:8081",
        "http://198.51.100.10:8081",
    ),
)
def test_mcp_proxy_url_rejects_non_loopback_or_ambiguous_targets(url):
    with pytest.raises(ValidationError):
        _settings_with(mcp_proxy_url=url)


# ---------------------------------------------------------------------------
# Isolated content origins (AgentDrive beta B1)
# ---------------------------------------------------------------------------


def test_content_origins_default_to_empty_for_local_dev_and_rollback():
    """No configured origins keeps the local single-origin mode and is the
    explicit rollback state for restoring the direct share renderer."""
    s = _settings_with()
    assert s.share_base_url == ""
    assert s.public_content_base_url == ""
    assert s.viewer_base_url == ""


def test_content_origin_normalizes_a_trailing_slash():
    s = _settings_with(share_base_url="https://share.example.test/")
    assert s.share_base_url == "https://share.example.test"


@pytest.mark.parametrize(
    "origin, expected",
    (
        ("https://SHARE.EXAMPLE.TEST./", "https://share.example.test"),
        ("https://bücher.example.test/", "https://xn--bcher-kva.example.test"),
        ("http://LOCALHOST.:8000/", "http://localhost:8000"),
        ("http://BÜCHER.localhost:8766/", "http://xn--bcher-kva.localhost:8766"),
    ),
)
def test_content_origin_canonicalizes_registered_and_loopback_names(origin, expected):
    s = _settings_with(share_base_url=origin)
    assert s.share_base_url == expected


def test_content_origin_rejects_multiple_trailing_root_dots():
    with pytest.raises(ValidationError):
        _settings_with(share_base_url="https://share.example.test..")


@pytest.mark.parametrize(
    "origin",
    (
        "http://.localhost:8766",
        "http://evil..localhost:8766",
        "http://-evil.localhost:8766",
        "http://evil-.localhost:8766",
        "http://evil_.localhost:8766",
    ),
)
def test_content_origin_rejects_malformed_localhost_subdomains(origin):
    with pytest.raises(ValidationError):
        _settings_with(viewer_base_url=origin)


def test_content_origins_accept_distinct_configured_hosts():
    s = _settings_with(
        share_base_url="https://share.example.test",
        public_content_base_url="https://public.example.test",
        viewer_base_url="https://viewer.example.test",
    )
    assert s.share_base_url == "https://share.example.test"
    assert s.public_content_base_url == "https://public.example.test"
    assert s.viewer_base_url == "https://viewer.example.test"


@pytest.mark.parametrize(
    "field, origin, expected",
    [
        ("share_base_url", "http://localhost:8000/", "http://localhost:8000"),
        ("public_content_base_url", "http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("viewer_base_url", "http://[::1]:8000/", "http://[::1]:8000"),
        (
            "viewer_base_url",
            "http://viewer.localhost:8766/",
            "http://viewer.localhost:8766",
        ),
    ],
)
def test_content_origins_allow_http_only_on_loopback(field, origin, expected):
    overrides = {field: origin}
    if field == "public_content_base_url":
        overrides["share_base_url"] = "https://share.example.test"
    s = _settings_with(**overrides)
    assert getattr(s, field) == expected


@pytest.mark.parametrize(
    "field",
    ("share_base_url", "public_content_base_url", "viewer_base_url"),
)
def test_content_origins_require_https_off_loopback(field):
    overrides = {field: "http://content.example.test"}
    if field == "public_content_base_url":
        overrides["share_base_url"] = "https://share.example.test"
    with pytest.raises(ValidationError):
        _settings_with(**overrides)


@pytest.mark.parametrize(
    "bad_origin",
    (
        "https://user:password@share.example.test",
        "https://share.example.test/path",
        "https://share.example.test?query=value",
        "https://share.example.test#fragment",
        "https://",
        "https://share.example.test:8443",
        "https://*.example.test",
    ),
)
def test_content_origin_rejects_unsafe_or_non_exact_values(bad_origin):
    with pytest.raises(ValidationError):
        _settings_with(share_base_url=bad_origin)


@pytest.mark.parametrize(
    "bad_origin",
    (
        "https://share example.test",
        "https://-share.example.test",
        "https://share-.example.test",
        "https://share..example.test",
        "https://share_.example.test",
    ),
)
def test_content_origin_rejects_malformed_registered_host_labels(bad_origin):
    with pytest.raises(ValidationError):
        _settings_with(share_base_url=bad_origin)


@pytest.mark.parametrize(
    "overrides",
    (
        {
            "share_base_url": "https://same.example.test",
            "public_content_base_url": "https://same.example.test",
        },
        {
            "share_base_url": "https://same.example.test",
            "viewer_base_url": "https://same.example.test",
        },
        {
            "share_base_url": "https://share.example.test",
            "public_content_base_url": "https://same.example.test",
            "viewer_base_url": "https://same.example.test",
        },
    ),
)
def test_configured_content_origins_require_distinct_hosts(overrides):
    with pytest.raises(ValidationError):
        _settings_with(**overrides)


@pytest.mark.parametrize(
    "share_origin, public_origin",
    (
        ("https://same.example.test", "https://SAME.EXAMPLE.TEST."),
        ("https://BÜCHER.example.test.", "https://xn--bcher-kva.example.test"),
    ),
)
def test_equivalent_content_hosts_cannot_bypass_isolation(share_origin, public_origin):
    with pytest.raises(ValidationError):
        _settings_with(
            share_base_url=share_origin,
            public_content_base_url=public_origin,
        )


def test_public_content_origin_requires_a_trusted_share_shell():
    with pytest.raises(ValidationError):
        _settings_with(public_content_base_url="https://public.example.test")


def test_hub_product_audience_defaults_to_contract_origin():
    """Contract §3 pins the product-token audience to the agent-facing origin;
    the default is that URL, not the archived sign-in client id."""
    s = _settings_with()
    assert s.hub_product_audience == "https://drive.tokencanopy.com"


def test_hub_product_audience_rejects_non_urls():
    """A bare product name (e.g. the sign-in client id "agentdrive") or any
    non-absolute value is the exact conflation the setting exists to undo."""
    for bad in ("agentdrive", "not-a-url", "api.tokencanopy.com/drive", "https://"):
        with pytest.raises(ValidationError, match="HUB_PRODUCT_AUDIENCE"):
            _settings_with(hub_product_audience=bad)


def test_hub_product_audience_accepts_http_and_https_urls():
    for good in (
        "https://drive.tokencanopy.com",
        "https://api.staging.agentdrive.run",
        "http://localhost:8000",
    ):
        s = _settings_with(hub_product_audience=good)
        assert s.hub_product_audience == good


# ---------------------------------------------------------------------------
# WorkOS configuration (S3) — env-var shape + replay-window guards
# ---------------------------------------------------------------------------


def test_mcp_origin_is_unbound_by_default_and_adds_no_audience():
    settings = _settings_with()
    assert settings.mcp_origin_base_url == ""
    assert settings.hub_mcp_audiences == (settings.hub_mcp_audience,)


def test_mcp_origin_adds_its_own_mcp_audience_beside_the_legacy_one():
    settings = _settings_with(
        api_base_url="https://drive.example.test",
        hub_product_audience="https://drive.example.test",
        mcp_origin_base_url="https://drive.mcp.example.test/",
    )
    # Normalized like every other origin setting, and never a bare audience:
    # both entries carry the exact /mcp path.
    assert settings.mcp_origin_base_url == "https://drive.mcp.example.test"
    assert settings.hub_mcp_audiences == (
        "https://drive.example.test/mcp",
        "https://drive.mcp.example.test/mcp",
    )


@pytest.mark.parametrize(
    "origin",
    (
        "https://drive.mcp.example.test/mcp",
        "https://drive.mcp.example.test?x=1",
        "https://user:pw@drive.mcp.example.test",
        "http://drive.mcp.example.test",
        "https://*.mcp.example.test",
    ),
)
def test_mcp_origin_must_be_an_exact_https_origin(origin):
    with pytest.raises(ValidationError):
        _settings_with(mcp_origin_base_url=origin)


def test_mcp_origin_must_not_be_the_api_host():
    # The bare origin there IS the /v0 product resource; Hub refuses the alias
    # on it and this side refuses to bind it, so the two cannot drift apart.
    with pytest.raises(ValidationError, match="must not be the API host"):
        _settings_with(
            api_base_url="https://drive.example.test",
            mcp_origin_base_url="https://drive.example.test",
        )
    with pytest.raises(ValidationError, match="must not be the API host"):
        _settings_with(
            public_base_url="http://localhost:8000",
            mcp_origin_base_url="http://localhost:8000",
        )


def test_mcp_origin_must_not_collide_with_a_content_host():
    with pytest.raises(ValidationError, match="distinct hosts"):
        _settings_with(
            share_base_url="https://share.example.test",
            mcp_origin_base_url="https://share.example.test",
        )


def test_retiring_the_legacy_transport_requires_an_origin_and_narrows_the_audiences():
    with pytest.raises(ValidationError, match="MCP_LEGACY_RETIRED requires MCP_ORIGIN_BASE_URL"):
        _settings_with(mcp_legacy_retired=True)
    settings = _settings_with(
        api_base_url="https://drive.example.test",
        hub_product_audience="https://drive.example.test",
        mcp_origin_base_url="https://drive.mcp.example.test",
        mcp_legacy_retired=True,
    )
    # The legacy audience is gone from the ingress the moment the flag is on.
    assert settings.hub_mcp_audiences == ("https://drive.mcp.example.test/mcp",)


def test_auth_mode_defaults_to_hub():
    """Hub is the only issuer the hosted product knows; the default says so."""
    assert _settings_with().auth_mode == "hub"


def test_auth_mode_accepts_the_one_release_alias():
    """Every hosted environment still carries AUTH_MODE=hub-oidc from the
    archived sign-in plane; it must keep booting, normalized to "hub"."""
    assert _settings_with(auth_mode="hub-oidc").auth_mode == "hub"
    assert _settings_with(auth_mode=" Hub-OIDC ").auth_mode == "hub"


@pytest.mark.parametrize("mode", ["workos", "bogus", ""])
def test_auth_mode_rejects_retired_and_unknown_values(mode):
    """The archived WorkOS plane is gone, so its mode is a misconfiguration
    that fails at Settings construction — the boot-time check it replaced
    lived in app.py and only ran once a server was already being built."""
    with pytest.raises(ValidationError, match="AUTH_MODE"):
        _settings_with(auth_mode=mode)


def test_legacy_hosts_default_to_no_redirect():
    """A standalone install has no alias domains: nothing configured means
    the redirect middleware is inert for every Host."""
    s = _settings_with()
    assert s.legacy_hosts == ""
    assert s.legacy_redirect_host == ""
    assert s.legacy_host_set == frozenset()


def test_legacy_hosts_parse_to_a_lowercase_set_and_target():
    s = _settings_with(
        legacy_hosts=" ADRV.AI, adrive.run ,Agentdrive.Mnexa.AI,",
        legacy_redirect_host="AgentDrive.run",
    )
    assert s.legacy_host_set == frozenset({"adrv.ai", "adrive.run", "agentdrive.mnexa.ai"})
    assert s.legacy_redirect_host == "agentdrive.run"


@pytest.mark.parametrize(
    "overrides",
    [
        {"legacy_hosts": "adrv.ai"},
        {"legacy_redirect_host": "agentdrive.run"},
    ],
)
def test_legacy_hosts_and_redirect_target_are_configured_together(overrides):
    """Half a redirect is a misconfiguration: hosts with nowhere to send them,
    or a target nothing is sent to. Refuse at boot rather than silently
    installing a no-op."""
    with pytest.raises(ValidationError, match="LEGACY_"):
        _settings_with(**overrides)


@pytest.mark.parametrize(
    "bad",
    ["https://adrv.ai", "adrv.ai/path", "adrv.ai:443", "adrv ai", "*.adrv.ai"],
)
def test_legacy_hosts_must_be_bare_hostnames(bad):
    """The value is compared against the Host header, so a scheme, path,
    port or wildcard would never match and would leave the alias unbound."""
    with pytest.raises(ValidationError, match="LEGACY_HOSTS"):
        _settings_with(legacy_hosts=bad, legacy_redirect_host="agentdrive.run")
    with pytest.raises(ValidationError, match="LEGACY_REDIRECT_HOST"):
        _settings_with(legacy_hosts="adrv.ai", legacy_redirect_host=bad)


def test_legacy_redirect_target_cannot_be_one_of_the_aliases():
    with pytest.raises(ValidationError, match="LEGACY_REDIRECT_HOST"):
        _settings_with(legacy_hosts="adrv.ai,agentdrive.run", legacy_redirect_host="agentdrive.run")


def test_auth_mode_alias_is_accepted_from_the_environment(monkeypatch):
    """Every hosted environment delivers AUTH_MODE through the process
    environment, not kwargs; prove the alias normalizes on that path."""
    monkeypatch.setenv("AUTH_MODE", "hub-oidc")
    assert _settings_with().auth_mode == "hub"
    monkeypatch.setenv("AUTH_MODE", "workos")
    with pytest.raises(ValidationError, match="AUTH_MODE"):
        _settings_with()


def test_legacy_hosts_are_deduplicated():
    s = _settings_with(legacy_hosts="a.test,A.TEST, a.test", legacy_redirect_host="b.test")
    assert s.legacy_hosts == "a.test"
    assert s.legacy_host_set == frozenset({"a.test"})


def test_legacy_hosts_are_stored_in_canonical_form():
    """A trailing DNS root dot and a Unicode label are honest spellings of a
    host, so they normalize (the Host header side canonicalizes the same
    way) rather than being refused with a message about schemes and ports."""
    s = _settings_with(legacy_hosts="ADRV.AI., bücher.example", legacy_redirect_host="Target.Test.")
    assert s.legacy_host_set == frozenset({"adrv.ai", "xn--bcher-kva.example"})
    assert s.legacy_redirect_host == "target.test"


def test_console_base_url_may_be_empty_and_then_no_console_link_is_built():
    """A self-hosted install has no console; empty means no `Open in console`
    button on the share page rather than a button to a dead localhost:3000."""
    from agentdrive.core import urls

    s = _settings_with(console_base_url="")
    assert s.console_base_url == ""
    assert _settings_with(console_base_url=" ").console_base_url == ""
    with pytest.raises(ValidationError, match="CONSOLE_BASE_URL"):
        _settings_with(console_base_url="not-an-origin")
    from agentdrive.config import settings as live

    original = live.console_base_url
    try:
        object.__setattr__(live, "console_base_url", "")
        assert urls.console_artifact_url("drv_0123456789abcdef", "art_0123456789abcdef") == ""
        object.__setattr__(live, "console_base_url", "https://app.example.test")
        assert urls.console_artifact_url("drv_0123456789abcdef", "art_0123456789abcdef") == (
            "https://app.example.test/drive/drv_0123456789abcdef/a/art_0123456789abcdef/"
        )
    finally:
        object.__setattr__(live, "console_base_url", original)

