"""Lifecycle metadata for the public OpenAPI operation surface.

The private beta classifies the entire authenticated ``/v0`` API as beta.
Non-v0 operations stay stable by default. Deriving the exact current beta set
from the operation manifest keeps the lifecycle marker and the public API
inventory under one ownership boundary. The active manifest is exact: every
active operation must be mounted and no disabled or unmanifested v0 operation
may appear.
"""

from __future__ import annotations

from typing import Any

from agentdrive.api.v0_manifest import operations as v0_operations

HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
)
POLICY_VERSION = 2
BETA_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    (operation["path"], operation["method"].lower()) for operation in v0_operations
)
REQUIRED_BETA_OPERATIONS = BETA_OPERATIONS


def add_stability_metadata(spec: dict[str, Any]) -> dict[str, Any]:
    """Add the compatibility-policy version and exact beta markers."""
    spec["x-agentdrive-compatibility-policy"] = POLICY_VERSION
    found: set[tuple[str, str]] = set()
    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            key = (path, method)
            if path == "/v0" or path.startswith("/v0/"):
                operation["x-stability-level"] = "beta"
                found.add(key)
            else:
                operation.pop("x-stability-level", None)

    missing = REQUIRED_BETA_OPERATIONS - found
    unexpected = found - BETA_OPERATIONS
    if missing or unexpected:
        problems = []
        if missing:
            problems.append(
                "missing from OpenAPI: "
                + ", ".join(
                    f"{method.upper()} {path}" for path, method in sorted(missing)
                )
            )
        if unexpected:
            problems.append(
                "missing from v0 manifest: "
                + ", ".join(
                    f"{method.upper()} {path}" for path, method in sorted(unexpected)
                )
            )
        raise RuntimeError("v0 beta operation inventory mismatch: " + "; ".join(problems))
    return spec
