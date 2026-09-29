"""Production and staging OpenAPI snapshot guards.

Pins the feature-off production document to `tests/openapi.golden.json` and
the feature-on staging document to `tests/openapi.staging.golden.json`.
Any change to a route decorator, response_model, request
body, or response field that mutates the public contract fails
this test until the snapshot is regenerated:

    uv run python -m agentdrive.scripts.dump_openapi

The diff between the old and new golden file is the API contract
change reviewers see in the PR. Intentional changes regen + commit;
accidental drift fails CI before it ships.

What's pinned:
  * `paths` — every route + operation, with summary, description,
    request body schema, response schema, parameters.
  * `components.schemas` — every Pydantic model that appears in a
    response_model, body, or query/header type.
  * `servers` — the environment's configured public API URL.
  * `info.title` — pinned literally.

What's NOT pinned (normalized to sentinels before comparison):
  * `info.version` — comes from the installed package version; a
    routine pyproject bump shouldn't churn this snapshot. The
    sentinel matches the dump script's `VERSION_SENTINEL`.
  * `servers` — comes from deployment configuration. Dedicated semantic
    tests verify environment safety; the snapshot pins a non-routable
    placeholder so both CI mount-prefix legs compare the same contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = REPO_ROOT / "tests" / "openapi.golden.json"
STAGING_GOLDEN_PATH = REPO_ROOT / "tests" / "openapi.staging.golden.json"


def _normalized_live_spec() -> dict:
    """Capture the live OpenAPI dict and apply the same normalization
    the dump script uses, so the comparison ignores volatile fields
    that don't represent contract changes."""
    from agentdrive.app import app
    from agentdrive.scripts.dump_openapi import normalize

    # Force a fresh rebuild from the CURRENT route table. FastAPI caches the
    # schema on `app.openapi_schema` after the first `app.openapi()` call; under
    # the parallel (pytest-xdist) suite another test in this worker may have
    # populated that cache while app/model state was transiently monkeypatched,
    # leaving a stale schema that this snapshot would then read and flag as a
    # false "drift". Clearing the cache makes this test reflect the real,
    # post-cleanup contract — exactly what the dump script captures in its own
    # fresh process. It can never MASK a real drift (a genuine contract change
    # still rebuilds and fails here).
    app.openapi_schema = None

    return normalize(app.openapi())


def _golden_spec(path: Path = GOLDEN_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _isolated_profile_spec(*, enabled: bool, output: Path) -> dict:
    env = os.environ.copy()
    env["SHEET_SESSIONS_ENABLED"] = "true" if enabled else "false"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "agentdrive.scripts.dump_openapi",
            "--current-process-output",
            str(output),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def _string_schema(property_schema: dict) -> dict:
    """Return the string branch from a required or nullable property."""
    if property_schema.get("type") == "string":
        return property_schema
    return next(
        branch for branch in property_schema["anyOf"] if branch.get("type") == "string"
    )


def _operation(spec: dict, operation_id: str) -> dict:
    return next(
        operation
        for path in spec["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and operation.get("operationId") == operation_id
    )


def test_sheet_session_contract_declares_real_headers_and_success_models():
    spec = _normalized_live_spec()
    create = _operation(spec, "sheet_sessions_create")
    create_headers = {
        parameter["name"]: parameter
        for parameter in create["parameters"]
        if parameter["in"] == "header"
    }
    assert create_headers["If-Match"]["required"] is True
    assert {"200", "201"} <= set(create["responses"])
    assert (
        create["responses"]["200"]["content"]["application/json"]["schema"]
        == create["responses"]["201"]["content"]["application/json"]["schema"]
    )

    for operation_id in (
        "sheet_sessions_create",
        "sheet_sessions_list",
        "sheet_sessions_read",
        "sheet_sessions_delete",
        "sheet_sessions_write_cells",
        "sheet_sessions_read_cells",
        "sheet_sessions_list_edits",
        "sheet_sessions_complete",
    ):
        operation = _operation(spec, operation_id)
        for status in operation["responses"]:
            if status not in {"200", "201"}:
                continue
            schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert schema, f"{operation_id} {status} has no response schema"

    read = _operation(spec, "sheet_sessions_read")
    assert "ETag" in read["responses"]["200"]["headers"]
    assert "ETag" in read["responses"]["304"]["headers"]

    schemas = spec["components"]["schemas"]
    for schema_name in (
        "SheetSessionActorOut",
        "SheetSessionSheetOut",
        "SheetSessionTouchedOut",
        "SheetSessionOut",
        "SheetSessionCreateOut",
        "SheetSessionListOut",
        "SheetSessionWriteOut",
        "SheetSessionEditOut",
        "CellRangeOut",
        "SheetSessionEditListOut",
        "SheetSessionCompleteOut",
    ):
        assert schemas[schema_name]["additionalProperties"] is False


@pytest.mark.parametrize(
    ("schema_name", "property_name"),
    [
        ("FolderCreateIn", "name"),
        ("FolderUpdateIn", "name"),
        ("FolderCopyIn", "destination_name"),
        ("ArtifactUpdateIn", "name"),
        ("ArtifactCopyIn", "destination_name"),
    ],
)
def test_item_name_request_schemas_advertise_1_to_255_code_points(
    schema_name: str, property_name: str
) -> None:
    """Generated clients retain the public item-name size contract."""
    schemas = _normalized_live_spec()["components"]["schemas"]
    name_schema = _string_schema(schemas[schema_name]["properties"][property_name])

    assert name_schema["minLength"] == 1
    assert name_schema["maxLength"] == 255


def test_staging_openapi_snapshot_matches_golden_file():
    """The feature-on test process must equal the staging snapshot.

    Failure means: the live `/openapi.json` response will change
    shape for any consumer (SDKs, Swagger UI, OpenAPI-driven
    tooling) once this code ships. If that's intentional:

        uv run python -m agentdrive.scripts.dump_openapi
        git add tests/openapi.golden.json tests/openapi.staging.golden.json
        git commit  # the diff IS the contract change

    If it's NOT intentional, fix the route/schema change that
    caused the drift before merging."""
    live = _normalized_live_spec()
    golden = _golden_spec(STAGING_GOLDEN_PATH)
    if live == golden:
        return
    # Build a focused diff so the failure message points at the
    # exact piece that drifted, rather than dumping the whole 150KB
    # spec into the test output.
    live_paths = set(live.get("paths", {}).keys())
    golden_paths = set(golden.get("paths", {}).keys())
    added_paths = sorted(live_paths - golden_paths)
    removed_paths = sorted(golden_paths - live_paths)

    live_schemas = set((live.get("components") or {}).get("schemas", {}).keys())
    golden_schemas = set((golden.get("components") or {}).get("schemas", {}).keys())
    added_schemas = sorted(live_schemas - golden_schemas)
    removed_schemas = sorted(golden_schemas - live_schemas)

    msg = [
        "OpenAPI spec drifted from tests/openapi.staging.golden.json.",
        "Regenerate with:  uv run python -m agentdrive.scripts.dump_openapi",
        "",
    ]
    if added_paths:
        msg.append(f"Added paths ({len(added_paths)}):")
        for p in added_paths[:10]:
            msg.append(f"  + {p}")
        if len(added_paths) > 10:
            msg.append(f"  + ... and {len(added_paths) - 10} more")
    if removed_paths:
        msg.append(f"Removed paths ({len(removed_paths)}):")
        for p in removed_paths[:10]:
            msg.append(f"  - {p}")
        if len(removed_paths) > 10:
            msg.append(f"  - ... and {len(removed_paths) - 10} more")
    if added_schemas:
        msg.append(f"Added schemas: {added_schemas}")
    if removed_schemas:
        msg.append(f"Removed schemas: {removed_schemas}")
    # When the path + schema sets match, the drift is in operation
    # details (param types, response models, descriptions). The user
    # still needs to regen — the diff lives in the file itself.
    if not (added_paths or removed_paths or added_schemas or removed_schemas):
        msg.append(
            "Operation-level drift (params, responses, or descriptions changed). "
            "Run the dump script and inspect the golden-file diff "
            "to see exactly what mutated."
        )
    pytest.fail("\n".join(msg))


def test_production_openapi_snapshot_matches_golden_file(tmp_path):
    """The fail-closed process must exactly equal the canonical snapshot."""
    live = _isolated_profile_spec(
        enabled=False, output=tmp_path / "openapi.production.json"
    )
    assert live == _golden_spec()

    methods = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
    v0 = {
        (path, method)
        for path, path_item in live["paths"].items()
        if path == "/v0" or path.startswith("/v0/")
        for method in path_item
        if method in methods
    }
    assert len(v0) == 51
    assert not any("/sheet-sessions" in path for path, _method in v0)


# ---------------------------------------------------------------------------
# Sanity guards on the snapshot itself — make sure the golden file
# doesn't rot into something that no longer matches our contract
# expectations (e.g., someone hand-edits it and breaks the wire format).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [GOLDEN_PATH, STAGING_GOLDEN_PATH])
def test_golden_file_advertises_openapi_3_1(path):
    """The committed snapshot declares OpenAPI 3.1.0 (FastAPI ≥0.100
    default; tracks JSON Schema draft 2020-12). Catches a downgrade
    or a hand-edit that breaks the version line."""
    golden = _golden_spec(path)
    assert golden.get("openapi", "").startswith("3.1"), (
        f"unexpected openapi version in golden file: {golden.get('openapi')!r}"
    )


@pytest.mark.parametrize("path", [GOLDEN_PATH, STAGING_GOLDEN_PATH])
def test_golden_file_normalizes_the_servers_block(path):
    """`servers` is deployment-derived, so the snapshot pins only the
    sentinel. The live derivation is asserted by
    `test_servers_follow_the_deployment_origin` below."""
    from agentdrive.scripts.dump_openapi import SERVERS_SENTINEL

    golden = _golden_spec(path)
    assert golden.get("servers") == SERVERS_SENTINEL, (
        "openapi.golden.json should carry the normalized `servers` "
        "sentinel — regenerate with `uv run python -m "
        "agentdrive.scripts.dump_openapi`"
    )


def test_every_manifested_v0_operation_is_beta_and_only_v0_is_beta():
    """The private-beta decision applies to the whole authenticated v0 API."""
    from agentdrive.api.stability import BETA_OPERATIONS
    from agentdrive.api.v0_manifest import operations

    spec = _normalized_live_spec()
    expected = {(operation["path"], operation["method"].lower()) for operation in operations}
    emitted = {
        (path, method)
        for path, path_item in spec["paths"].items()
        for method, operation in path_item.items()
        if isinstance(operation, dict)
        and operation.get("x-stability-level") == "beta"
    }

    assert len(expected) == 59
    assert all(path.startswith("/v0/") for path, _method in expected)
    assert expected == BETA_OPERATIONS
    assert emitted == expected


@pytest.mark.parametrize(
    ("api_base_url", "public_base_url", "expected"),
    [
        # Production: API_BASE_URL is set, so servers[0] is the prod API
        # host — byte-identical to the constant this derivation replaced.
        ("https://api.agentdrive.run", "https://agentdrive.run",
         "https://api.agentdrive.run"),
        # Staging must advertise ITSELF. A staging spec that names prod
        # would generate SDKs that send staging credentials to prod.
        ("https://api.staging.agentdrive.run",
         "https://app.staging.tokencanopy.com/drive",
         "https://api.staging.agentdrive.run"),
        # No API_BASE_URL → fall back to the browser origin, so local dev
        # and single-host deployments work without a second env var.
        ("", "https://app.staging.tokencanopy.com/drive",
         "https://app.staging.tokencanopy.com/drive"),
    ],
)
def test_servers_follow_the_deployment_origin(
    monkeypatch, api_base_url, public_base_url, expected
):
    """`servers[0]` is THIS deployment's agent-facing origin — the same
    derivation that mints JWT `iss`/`aud` and the RFC 8414 discovery
    document. SDK generators default to the first entry, so a spec that
    named a different environment than the one issuing the tokens would
    point generated clients at the wrong host."""
    from agentdrive.app import _openapi_servers
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "api_base_url", api_base_url)
    monkeypatch.setattr(settings, "public_base_url", public_base_url)
    servers = _openapi_servers()
    assert servers[0]["url"] == expected


def test_servers_omits_duplicate_local_entry(monkeypatch):
    """On localhost the derivation already resolves to the dev host, so
    the secondary "Local dev" entry would be a duplicate."""
    from agentdrive.app import _openapi_servers
    from agentdrive.config import settings

    monkeypatch.setattr(settings, "api_base_url", "http://127.0.0.1:8000")
    servers = _openapi_servers()
    assert [s["url"] for s in servers] == ["http://127.0.0.1:8000"]


def test_golden_file_keeps_v1_prefix_off_the_wire():
    """Wire-protocol invariant per CLAUDE.md: the public API namespace
    is `/v0/...`. Bumping to `/v1/` would be a coordinated breaking
    change (SDK release, deprecation window, etc.), not something a
    refactor accidentally introduces. Cheap guard: any `/v1/` path
    in the snapshot fails CI."""
    golden = _golden_spec()
    paths = list(golden.get("paths", {}).keys())
    leaked = [p for p in paths if p.startswith("/v1/")]
    assert not leaked, (
        f"/v1/ paths in OpenAPI snapshot — coordinate a versioning change "
        f"before this lands: {leaked}"
    )


def test_enrichment_failure_does_not_poison_the_schema_cache(monkeypatch):
    """`FastAPI.openapi()` caches the RAW schema on `app.openapi_schema` as a
    side effect before `_contract_openapi` enriches it. If enrichment raises,
    that leftover cache must not survive — otherwise the first request 500s
    and every later request silently serves the raw, unenriched spec with a
    200 (exactly how the missing-manifest image masked itself in staging)."""
    import agentdrive.app as app_module
    from agentdrive.app import app

    app.openapi_schema = None

    def _boom(_spec):
        raise FileNotFoundError("manifest missing")

    monkeypatch.setattr(app_module, "add_documented_response_headers", _boom)
    with pytest.raises(FileNotFoundError):
        app.openapi()
    assert app.openapi_schema is None, (
        "enrichment failure left FastAPI's raw schema cached — later "
        "requests would serve an unenriched spec with a 200"
    )

    monkeypatch.undo()
    spec = app.openapi()
    assert "/v0/drives" in spec["paths"], "recovery after failure must serve the enriched spec"
