"""AgentDrive-specific OpenAPI compatibility checks.

oasdiff owns wire-level compatibility. This module protects generated-client
and authentication surfaces that need exact comparison and provides the
one-time transition to the private beta's all-v0 classification.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from agentdrive.api.stability import (
    HTTP_METHODS,
    POLICY_VERSION,
)

ALL_V0_BETA_BOOTSTRAP_POLICY_VERSION = 2


class CompatibilityError(ValueError):
    """The candidate changes AgentDrive's stable public contract."""


def _operations(spec: dict[str, Any]):
    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                yield path, method, operation


def _operation_map(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (path, method): operation
        for path, method, operation in _operations(spec)
    }


def _is_beta(operation: dict[str, Any]) -> bool:
    return operation.get("x-stability-level") == "beta"


def _bootstrap_v2_policy(
    base: dict[str, Any], revision: dict[str, Any]
) -> dict[str, Any]:
    """Align a pre-v2 base with the ratified all-v0 beta classification.

    Policy v2 is a one-time, owner-approved pre-launch classification change.
    Once v2 is on the base branch, the ordinary stable-to-beta prohibition
    remains in force for every non-v0 operation.
    """
    base_version = base.get("x-agentdrive-compatibility-policy")
    revision_version = revision.get("x-agentdrive-compatibility-policy")
    if (
        revision_version != ALL_V0_BETA_BOOTSTRAP_POLICY_VERSION
        or base_version not in {None, 1}
    ):
        return base

    bootstrapped = deepcopy(base)
    for path, _method, operation in _operations(bootstrapped):
        if path == "/v0" or path.startswith("/v0/"):
            operation["x-stability-level"] = "beta"
    bootstrapped["x-agentdrive-compatibility-policy"] = POLICY_VERSION
    return bootstrapped


def _validate_revision_stability(
    revision: dict[str, Any], failures: list[str]
) -> None:
    version = revision.get("x-agentdrive-compatibility-policy")
    if version != POLICY_VERSION:
        failures.append(
            "candidate compatibility policy version is "
            f"{version!r}; expected {POLICY_VERSION}"
        )

    operations = _operation_map(revision)
    expected_beta = {
        key for key in operations if key[0] == "/v0" or key[0].startswith("/v0/")
    }
    emitted_beta = {key for key, operation in operations.items() if _is_beta(operation)}
    undeclared = emitted_beta - expected_beta
    missing_markers = expected_beta - emitted_beta
    for path, method in sorted(undeclared):
        failures.append(f"undeclared beta operation: {method.upper()} {path}")
    for path, method in sorted(missing_markers):
        failures.append(f"declared beta operation lacks marker: {method.upper()} {path}")


def _validate_policy_version_transition(
    base: dict[str, Any], revision: dict[str, Any], failures: list[str]
) -> None:
    base_version = base.get("x-agentdrive-compatibility-policy")
    revision_version = revision.get("x-agentdrive-compatibility-policy")
    if (
        isinstance(base_version, int)
        and isinstance(revision_version, int)
        and revision_version < base_version
    ):
        failures.append(
            "compatibility policy version decreased: "
            f"{base_version} -> {revision_version}"
        )


def _schema_names_reachable_from_stable_operations(
    spec: dict[str, Any],
) -> set[str]:
    schemas = spec.get("components", {}).get("schemas", {})
    reachable: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            prefix = "#/components/schemas/"
            if isinstance(ref, str) and ref.startswith(prefix):
                name = ref.removeprefix(prefix)
                if name not in reachable:
                    reachable.add(name)
                    target = schemas.get(name)
                    if target is not None:
                        visit(target)
            for key, child in value.items():
                if key != "$ref":
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for _, _, operation in _operations(spec):
        if not _is_beta(operation):
            visit(operation)
    return reachable


def _refs_by_location(
    value: Any, location: tuple[str | int, ...] = ()
) -> dict[tuple[str | int, ...], str]:
    refs: dict[tuple[str | int, ...], str] = {}
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            refs[location] = ref
        for key, child in value.items():
            if key != "$ref":
                refs.update(_refs_by_location(child, (*location, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            refs.update(_refs_by_location(child, (*location, index)))
    return refs


def _stable_ref_bindings(
    spec: dict[str, Any],
) -> dict[tuple[str | int, ...], str]:
    """Return SDK-visible `$ref` targets rooted in stable public surfaces."""
    bindings: dict[tuple[str | int, ...], str] = {}
    for path, method, operation in _operations(spec):
        if _is_beta(operation):
            continue
        for location, ref in _refs_by_location(operation).items():
            bindings[("operation", path, method, *location)] = ref

    schemas = spec.get("components", {}).get("schemas", {})
    for name in _schema_names_reachable_from_stable_operations(spec):
        schema = schemas.get(name)
        if schema is None:
            continue
        for location, ref in _refs_by_location(schema).items():
            bindings[("schema", name, *location)] = ref
    return bindings


def check_agentdrive_compatibility(
    base: dict[str, Any], revision: dict[str, Any]
) -> None:
    """Raise `CompatibilityError` for AgentDrive-specific stable regressions."""
    failures: list[str] = []
    _validate_policy_version_transition(base, revision, failures)
    base = _bootstrap_v2_policy(base, revision)
    _validate_revision_stability(revision, failures)

    base_security = base.get("components", {}).get("securitySchemes", {})
    revision_security = revision.get("components", {}).get("securitySchemes", {})
    if base_security != revision_security:
        failures.append("components.securitySchemes changed")

    base_operations = _operation_map(base)
    revision_operations = _operation_map(revision)
    for key, base_operation in sorted(base_operations.items()):
        if _is_beta(base_operation):
            continue
        path, method = key
        label = f"{method.upper()} {path}"
        revision_operation = revision_operations.get(key)
        if revision_operation is None:
            failures.append(f"stable operation removed: {label}")
            continue
        if _is_beta(revision_operation):
            failures.append(f"stable operation stability decrease: {label}")
            continue
        if base_operation.get("operationId") != revision_operation.get("operationId"):
            failures.append(
                f"stable operationId changed: {label} "
                f"{base_operation.get('operationId')!r} -> "
                f"{revision_operation.get('operationId')!r}"
            )
        if base_operation.get("tags", []) != revision_operation.get("tags", []):
            failures.append(
                f"stable operation ordered tags changed: {label} "
                f"{base_operation.get('tags', [])!r} -> "
                f"{revision_operation.get('tags', [])!r}"
            )

    revision_schemas = set(
        revision.get("components", {}).get("schemas", {})
    )
    removed_public_schemas = (
        _schema_names_reachable_from_stable_operations(base) - revision_schemas
    )
    if removed_public_schemas:
        failures.append(
            "stable public schema names removed: "
            + ", ".join(sorted(removed_public_schemas))
        )

    revision_ref_bindings = _stable_ref_bindings(revision)
    for location, base_ref in sorted(
        _stable_ref_bindings(base).items(), key=lambda item: repr(item[0])
    ):
        revision_ref = revision_ref_bindings.get(location)
        if revision_ref != base_ref:
            location_label = "/".join(str(part) for part in location)
            failures.append(
                f"stable $ref binding changed: {location_label} "
                f"{base_ref!r} -> {revision_ref!r}"
            )

    if failures:
        raise CompatibilityError("\n".join(failures))


def normalize_base_for_comparison(
    base: dict[str, Any], revision: dict[str, Any]
) -> dict[str, Any]:
    """Return the base document with the one-time v2 bootstrap applied."""
    return _normalize_volatile_fields(_bootstrap_v2_policy(base, revision))


def normalize_revision_for_comparison(
    revision: dict[str, Any],
) -> dict[str, Any]:
    """Make snapshot-only sentinels valid inputs for semantic tooling."""
    return _normalize_volatile_fields(revision)


def _normalize_volatile_fields(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(spec)
    if normalized.get("servers") == ["<DEPLOYMENT-DERIVED>"]:
        normalized["servers"] = [{"url": "https://deployment-derived.invalid"}]
    # OpenAPI has no route-converter vocabulary, so oasdiff considers
    # AgentDrive's `/artifacts/{art_id}` and catch-all `/artifacts/{path}`
    # operations duplicates even though Starlette's ID regex disambiguates
    # them. Give ID-converted parameters a comparison-only literal segment.
    # Exact path removal is already protected before this normalization.
    paths = normalized.get("paths", {})
    normalized_paths = {}
    for path, path_item in paths.items():
        comparison_path = path
        for parameter in ("art_id", "fld_id", "fbk_id", "job_id"):
            comparison_path = comparison_path.replace(
                f"/{{{parameter}}}", f"/__{parameter}__/{{{parameter}}}"
            )
        normalized_paths[comparison_path] = path_item
    normalized["paths"] = normalized_paths
    return normalized


def _load_source(source: str, repo_root: Path) -> dict[str, Any]:
    path = Path(source)
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
    else:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show", source],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise CompatibilityError(
                f"OpenAPI input is not a file or Git object: {source}"
            )
        raw = result.stdout
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CompatibilityError(f"OpenAPI input is not valid JSON: {source}") from exc
    if not isinstance(document, dict):
        raise CompatibilityError(f"OpenAPI input is not an object: {source}")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check AgentDrive-specific stable OpenAPI compatibility."
    )
    parser.add_argument("base", help="base JSON file or Git object")
    parser.add_argument("revision", help="candidate JSON file or Git object")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[4],
    )
    parser.add_argument(
        "--normalized-base-out",
        type=Path,
        help="write the base with the one-time v2 bootstrap applied",
    )
    parser.add_argument(
        "--normalized-revision-out",
        type=Path,
        help="write the candidate with snapshot-only sentinels normalized",
    )
    args = parser.parse_args(argv)
    try:
        base = _load_source(args.base, args.repo_root)
        revision = _load_source(args.revision, args.repo_root)
        check_agentdrive_compatibility(base, revision)
        if args.normalized_base_out is not None:
            args.normalized_base_out.write_text(
                json.dumps(
                    normalize_base_for_comparison(base, revision),
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        if args.normalized_revision_out is not None:
            args.normalized_revision_out.write_text(
                json.dumps(
                    normalize_revision_for_comparison(revision),
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
    except CompatibilityError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
