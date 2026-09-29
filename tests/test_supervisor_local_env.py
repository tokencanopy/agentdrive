"""Under AUTH_MODE=local the supervisor puts the MCP sidecar into its
`api-key` mode — two variables, nothing derived from an origin — and
nothing at all under Hub, where the sidecar's presets and its Hub JWT path
apply."""

from __future__ import annotations

from agentdrive.process_supervisor import local_api_key_sidecar_env


def test_hub_mode_derives_nothing():
    assert local_api_key_sidecar_env({"AUTH_MODE": "hub", "PUBLIC_BASE_URL": "http://x"}) == {}
    assert local_api_key_sidecar_env({}) == {}


def test_local_mode_selects_the_api_key_authenticator():
    """These two variables are the whole contract with the sidecar, and both
    are load-bearing: `MCP_AUTH_MODE=api-key` selects the introspection path,
    and `MCP_ENVIRONMENT` is required beside it — the sidecar refuses to start
    without one ("MCP_ENVIRONMENT is required with MCP_AUTH_MODE=api-key") and
    the supervisor treats that exit as a failed revision. That the sidecar
    accepts exactly this pair WITHOUT the retired issuer tuple is pinned
    across the language seam by `test_mcp_deployment_contract.py`."""
    assert local_api_key_sidecar_env({"AUTH_MODE": "local"}) == {
        "MCP_AUTH_MODE": "api-key",
        "MCP_ENVIRONMENT": "local",
    }


def test_local_mode_needs_no_origin():
    """#723 raised here without PUBLIC_BASE_URL because the sidecar's JWT
    tuple was derived from the origin. An opaque key needs no issuer, no
    audience and no JWKS, so there is nothing left to derive and nothing to
    refuse."""
    assert "MCP_AUTH_ISSUER" not in local_api_key_sidecar_env({"AUTH_MODE": "local"})
    assert "MCP_AUTH_JWKS_URL" not in local_api_key_sidecar_env({"AUTH_MODE": "local"})


def test_an_operators_explicit_sidecar_value_is_never_overwritten():
    env = local_api_key_sidecar_env(
        {"AUTH_MODE": "local", "MCP_ENVIRONMENT": "staging"}
    )
    assert env == {"MCP_AUTH_MODE": "api-key"}
    assert local_api_key_sidecar_env({"AUTH_MODE": "local", "MCP_AUTH_MODE": "jwt"}) == {
        "MCP_ENVIRONMENT": "local"
    }


def test_the_scheduler_starts_only_when_asked():
    """Self-hosted compose sets SCHEDULER_ENABLED; the hosted deployment never
    does (its Cloud Scheduler runs the same jobs, and two schedulers would
    double every run)."""
    from agentdrive.process_supervisor import scheduler_enabled

    assert scheduler_enabled({"SCHEDULER_ENABLED": "true"})
    assert scheduler_enabled({"SCHEDULER_ENABLED": " TRUE "})
    assert scheduler_enabled({"SCHEDULER_ENABLED": "1"})
    assert not scheduler_enabled({})
    assert not scheduler_enabled({"SCHEDULER_ENABLED": "false"})
    assert not scheduler_enabled({"SCHEDULER_ENABLED": ""})
