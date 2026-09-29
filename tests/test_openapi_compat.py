"""Focused tests for AgentDrive-specific OpenAPI compatibility policy."""

from __future__ import annotations

from copy import deepcopy

import pytest

import agentdrive.scripts.openapi_compat as openapi_compat
from agentdrive.api.stability import POLICY_VERSION
from agentdrive.scripts.openapi_compat import (
    CompatibilityError,
    check_agentdrive_compatibility,
    normalize_base_for_comparison,
    normalize_revision_for_comparison,
)

STABLE_PATH = "/stable/widgets/{widget_id}"


def _schema(
    *,
    beta: bool = False,
    path: str = STABLE_PATH,
    policy_version: int = POLICY_VERSION,
) -> dict:
    operation = {
        "operationId": "getWidget",
        "tags": ["widgets"],
        "responses": {
            "200": {
                "description": "ok",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/Widget"}
                    }
                },
            }
        },
    }
    if beta:
        operation["x-stability-level"] = "beta"
    return {
        "openapi": "3.1.0",
        "x-agentdrive-compatibility-policy": policy_version,
        "paths": {path: {"get": operation}},
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "AgentDrive API key or JWT",
                }
            },
            "schemas": {
                "Widget": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                }
            },
        },
    }


def _assert_rejected(base: dict, revision: dict, message: str) -> None:
    with pytest.raises(CompatibilityError, match=message):
        check_agentdrive_compatibility(base, revision)


def test_additive_response_field_passes_custom_policy():
    base = _schema()
    revision = deepcopy(base)
    revision["components"]["schemas"]["Widget"]["properties"]["label"] = {
        "type": "string"
    }

    check_agentdrive_compatibility(base, revision)


def test_security_scheme_change_is_rejected():
    base = _schema()
    revision = deepcopy(base)
    revision["components"]["securitySchemes"]["BearerAuth"]["scheme"] = "basic"

    _assert_rejected(base, revision, "securitySchemes")


def test_stable_operation_id_change_is_rejected():
    base = _schema()
    revision = deepcopy(base)
    revision["paths"][STABLE_PATH]["get"]["operationId"] = "fetchWidget"

    _assert_rejected(base, revision, "operationId")


def test_stable_operation_tag_change_is_rejected():
    base = _schema()
    revision = deepcopy(base)
    revision["paths"][STABLE_PATH]["get"]["tags"] = ["objects"]

    _assert_rejected(base, revision, "ordered tags")


def test_stable_public_schema_name_removal_is_rejected():
    base = _schema()
    revision = deepcopy(base)
    widget = revision["components"]["schemas"].pop("Widget")
    revision["components"]["schemas"]["RenamedWidget"] = widget
    revision["paths"][STABLE_PATH]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"] = "#/components/schemas/RenamedWidget"

    _assert_rejected(base, revision, "public schema")


def test_stable_schema_ref_retarget_with_old_alias_retained_is_rejected():
    base = _schema()
    revision = deepcopy(base)
    revision["components"]["schemas"]["RenamedWidget"] = deepcopy(
        revision["components"]["schemas"]["Widget"]
    )
    revision["paths"][STABLE_PATH]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"] = (
        "#/components/schemas/RenamedWidget"
    )

    _assert_rejected(base, revision, r"stable \$ref binding changed")


def test_stable_operation_cannot_be_demoted_to_beta():
    base = _schema()
    revision = deepcopy(base)
    revision["paths"][STABLE_PATH]["get"]["x-stability-level"] = "beta"

    _assert_rejected(base, revision, "stability decrease")


def test_ratified_policy_v2_transition_reclassifies_the_v0_base_as_beta():
    """The launch ratification is the one allowed all-v0 beta transition."""
    assert POLICY_VERSION == 2
    path = "/v0/widgets/{widget_id}"
    base = _schema(path=path, policy_version=1)
    revision = deepcopy(base)
    revision["x-agentdrive-compatibility-policy"] = 2
    revision["paths"][path]["get"]["x-stability-level"] = "beta"

    check_agentdrive_compatibility(base, revision)

    normalized_base = normalize_base_for_comparison(base, revision)
    assert normalized_base["x-agentdrive-compatibility-policy"] == 2
    assert (
        normalized_base["paths"][path]["get"]["x-stability-level"]
        == "beta"
    )


def test_v2_bootstrap_never_follows_a_future_policy_version(monkeypatch):
    path = "/v0/widgets/{widget_id}"
    base = _schema(path=path, policy_version=1)
    revision = _schema(beta=True, path=path, policy_version=3)
    monkeypatch.setattr(openapi_compat, "POLICY_VERSION", 3)

    normalized_base = normalize_base_for_comparison(base, revision)

    assert normalized_base["x-agentdrive-compatibility-policy"] == 1
    assert "x-stability-level" not in normalized_base["paths"][path]["get"]


def test_compatibility_policy_version_cannot_decrease():
    base = _schema(policy_version=2)
    revision = _schema(policy_version=1)

    _assert_rejected(base, revision, "compatibility policy version decreased")


def test_beta_operation_can_be_removed():
    base = _schema(beta=True, path="/v0/widgets/{widget_id}")
    revision = deepcopy(base)
    revision["paths"] = {}

    check_agentdrive_compatibility(base, revision)


def test_v2_bootstrap_does_not_exempt_a_non_v0_stable_operation():
    base = _schema()
    base.pop("x-agentdrive-compatibility-policy")
    revision = deepcopy(base)
    revision["x-agentdrive-compatibility-policy"] = POLICY_VERSION
    revision["paths"] = {}

    _assert_rejected(base, revision, "stable operation removed")


def test_oasdiff_normalization_disambiguates_route_converters():
    spec = _schema()
    operation = spec["paths"].pop(STABLE_PATH)
    spec["paths"] = {
        "/v0/artifacts/{art_id}/download": operation,
        "/v0/artifacts/{path}/download": deepcopy(operation),
    }

    normalized = normalize_revision_for_comparison(spec)

    assert set(normalized["paths"]) == {
        "/v0/artifacts/__art_id__/{art_id}/download",
        "/v0/artifacts/{path}/download",
    }
