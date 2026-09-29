"""The Python SDK page renders one canonical, contract-aligned design source."""

import re
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agentdrive.api.v0_manifest import op_ids
from agentdrive.api.v0_manifest import operations as v0_operations
from agentdrive.config import settings
from agentdrive.middleware import PUBLIC_PREFIXES, HostSurfaceMiddleware
from agentdrive.rendering.render import render_body
from agentdrive.site.routes import SDK_DESIGN_PATH, router, sdk_design_html
from agentdrive.site.templates import u

DESIGN = Path("docs/agentdriveSDK.md")
TEMPLATE = Path("src/agentdrive/site/templates/python_sdk.html")
DOCKERFILE = Path("Dockerfile")
GITIGNORE = Path(".gitignore")

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})
PUBLIC_OPERATIONS = {
    "health": ("GET", "/health"),
    "oauth_protected_resource": ("GET", "/.well-known/oauth-protected-resource"),
    "oauth_protected_resource_mcp": (
        "GET",
        "/.well-known/oauth-protected-resource/mcp",
    ),
    "shares_redeem": ("GET", "/s/{share_key}"),
}


def _page_app():
    page_app = FastAPI()
    page_app.include_router(router)
    return page_app


async def _get_page(app=None, *, host: str | None = None):
    target = app or _page_app()
    transport = ASGITransport(app=target)
    headers = {"host": host} if host else None
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/sdk/python", headers=headers)


def _openapi_operations() -> dict[str, dict]:
    from agentdrive.app import app

    app.openapi_schema = None
    return {
        operation["operationId"]: {
            "method": method.upper(),
            "path": path,
            "security": operation.get("security"),
        }
        for path, path_item in app.openapi()["paths"].items()
        for method, operation in path_item.items()
        if method.lower() in HTTP_METHODS and "operationId" in operation
    }


async def test_python_sdk_page_is_anonymous_html_and_explicitly_a_rebuild():
    response = await _get_page()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["cache-control"] == "public, max-age=300"

    body = response.text
    assert "agentdrive-sdk" in body
    assert "AgentDrive Python SDK Design" in body
    assert "This is a rebuild, not a greenfield package" in body
    assert "Phase 1: generated SDK core" in body
    assert "Phase 2: ergonomic Python facade" in body
    assert "Documentation strategy" in body
    assert "Acceptance criteria" in body
    assert "Canonical source" in body


async def test_python_sdk_canonical_uses_the_supported_origin_not_the_legacy_base(
    monkeypatch,
):
    """The canonical must name where this deployment actually answers.

    `public_base_url` is compatibility configuration for archived helpers and
    the mount-prefix startup invariant, and production deliberately keeps it
    on a retired brand domain so nobody mistakes it for a supported origin
    (tests/test_deploy_contract.py pins exactly that). Building the canonical
    from it published that legacy label as the page's authoritative URL, on a
    domain that now redirects away and discards the path.
    """
    monkeypatch.setattr(settings, "api_base_url", "https://drive.example.test")
    monkeypatch.setattr(settings, "public_base_url", "https://retired.example.invalid/drive")

    response = await _get_page()

    assert 'href="https://drive.example.test/sdk/python"' in response.text
    assert "retired.example.invalid" not in response.text


async def test_python_sdk_canonical_falls_back_when_no_api_base_is_configured(
    monkeypatch,
):
    """Local and test deployments set no api_base_url; the page still resolves."""
    monkeypatch.setattr(settings, "api_base_url", "")
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")

    response = await _get_page()

    assert 'href="http://localhost:8000/sdk/python"' in response.text


async def test_python_sdk_page_renders_the_canonical_markdown_source():
    response = await _get_page()
    expected = render_body(DESIGN.read_bytes(), "text/markdown", DESIGN.name)

    assert DESIGN.resolve() == SDK_DESIGN_PATH
    assert expected.mode == "markdown"
    assert expected.html == sdk_design_html()
    assert expected.html in response.text


def test_python_sdk_scope_is_exactly_59_bearer_and_four_public_operations():
    operations = _openapi_operations()
    authenticated = {
        operation["operation_id"]: (operation["method"], operation["path"])
        for operation in v0_operations
    }
    authenticated_ids = set(authenticated)
    public_ids = set(PUBLIC_OPERATIONS)

    assert len(authenticated_ids) == 59
    assert len(operations) == 63
    assert set(operations) == authenticated_ids | public_ids

    for operation_id, (method, path) in authenticated.items():
        operation = operations[operation_id]
        assert (operation["method"], operation["path"]) == (method, path)
        assert operation["security"] == [{"bearerAuth": []}]

    for operation_id, (method, path) in PUBLIC_OPERATIONS.items():
        operation = operations[operation_id]
        assert (operation["method"], operation["path"]) == (method, path)
        assert operation["security"] is None


def test_python_sdk_markdown_records_complete_generated_first_architecture_and_gates():
    source = DESIGN.read_text()

    for phrase in (
        # rebuild framing: the package and repo already exist
        "tokencanopy/agentdrive-sdk",
        "This is a rebuild, not a greenfield package",
        # single generated core, e2a's pin and transport model
        "openapitools/openapi-generator-cli:v7.16.0",
        "`library=httpx`",
        "exactly one",
        "not a second generated transport",
        # deterministic spec preparation ahead of the generator
        "Spec preparation",
        "normalize the 3.1 snapshot",
        "`--skip-validate-spec` is not",
        # phase 1 gates
        "Snapshot drift",
        "Deterministic regeneration",
        "Full contract shape",
        "Compatibility",
        "Model evolution",
        "Live conformance",
        # facade discipline
        "generated operations and models",
        "parity",
        "Generated documentation is the only exact API reference",
    ):
        assert phrase in source


def test_python_sdk_markdown_does_not_handwrite_the_exact_api_reference():
    source = DESIGN.read_text()

    for operation_id in op_ids():
        assert operation_id not in source

    for forbidden in (
        "REST operation ID",
        "Resource interface",
        "Response models",
        "Common types",
        "Error interface",
        "class AgentDrive",
    ):
        assert forbidden not in source

    assert re.search(r"(?m)^\s*(?:async\s+)?def\s+\w+\(", source) is None
    assert re.search(r"\bHTTP\s+[1-5][0-9]{2}\b", source) is None


def test_python_sdk_template_is_presentation_only():
    source = TEMPLATE.read_text()

    assert "{{ design_html|safe }}" in source
    for duplicated_design_detail in (
        "50-operation",
        "47 authenticated",
        "OpenAPI Generator",
        "agentdrive_sdk.generated",
        "Full contract shape",
        "Phase 1 quality gates",
        "Implementation sequence",
    ):
        assert duplicated_design_detail not in source


def test_python_sdk_has_no_pdf_latex_or_parse_cache_duplicate():
    assert not Path("docs/agentdriveSDK.pdf").exists()
    assert not Path("docs/agentdriveSDK.tex").exists()
    assert not tuple(DESIGN.parent.glob("_markdown_*"))

    ignore = GITIGNORE.read_text()
    assert "!docs/agentdriveSDK.pdf" not in ignore
    assert "docs/_markdown_*/" not in ignore


def test_python_sdk_canonical_markdown_is_available_in_the_runtime_image():
    """The COPY is the whole contract, and the ignore file must not undo it.

    `apps/drive/.dockerignore` has been through three states. It once carried
    a blanket `*.md` exclusion with a `!docs/agentdriveSDK.md` negation to
    re-include this one file; it was deleted when the image moved to a
    repo-root build context, because Docker reads only the ignore file at the
    CONTEXT root and an inert file that looks load-bearing is worse than none;
    and it came back when the context moved to `apps/drive` so the image can
    build from a checkout of this app alone.

    It came back WITHOUT the `*.md` exclusion, so there is no negation to
    assert — only that nothing in it excludes this file, which is checked
    directly rather than by pinning a rule that may be spelled differently.
    """
    dockerfile = DOCKERFILE.read_text()
    assert (
        "COPY --chown=app:app docs/agentdriveSDK.md /app/docs/agentdriveSDK.md"
        in dockerfile
    )
    # Resolved from this file, not the CWD. `DOCKERFILE = Path("Dockerfile")`
    # above is the module's existing convention and it only works because
    # pytest is run from `apps/drive`; a CWD-relative read here would find the
    # REPOSITORY root's `.dockerignore`, which also has no `*.md` rule — so
    # the assertion would pass against the wrong file.
    app = Path(__file__).resolve().parent.parent
    ignore = (app / ".dockerignore").read_text().splitlines()
    rules = [line.strip() for line in ignore if line.strip() and not line.startswith("#")]
    assert "*.md" not in rules and "**/*.md" not in rules
    assert not any(rule.rstrip("/") == "docs" for rule in rules)


def test_python_sdk_route_is_mounted_but_not_added_to_openapi():
    from agentdrive.app import app

    assert any(route.path == "/sdk/python" for route in app.routes)
    assert "/sdk/python" not in app.openapi()["paths"]


def test_site_url_helper_preserves_mount_prefix(monkeypatch):
    monkeypatch.setattr(settings, "mount_prefix", "")
    assert u("/sdk/python") == "/sdk/python"

    monkeypatch.setattr(settings, "mount_prefix", "/drive")
    assert u("/sdk/python") == "/drive/sdk/python"
    assert u("/docs") == "/drive/docs"


async def test_sdk_page_is_refused_on_the_share_host():
    share_host = "share.example.test"
    wrapped = HostSurfaceMiddleware(
        _page_app(),
        surfaces=[(share_host, PUBLIC_PREFIXES, False)],
    )

    refused = await _get_page(wrapped, host=share_host)
    allowed = await _get_page(wrapped, host="app.example.test")

    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "NOT_FOUND"
    assert allowed.status_code == 200


def test_python_sdk_template_has_no_inline_css():
    source = TEMPLATE.read_text()
    assert "style=" not in source
    assert "<style" not in source
