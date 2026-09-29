"""Real-app conformance: the cutover composition itself (§11.4).

Gates:

  1. **Surface equality** — the mounted app's OpenAPI `/v0` operations are
     EXACTLY the manifest's 47 operations as (method, path, operationId)
     triples: no more, no less, no drift.
  2. **Envelope** — a real /v0 request renders the §6.3 top-level
     `{"error": {code, message}}` envelope, never FastAPI's `{"detail": ...}`
     wrapper.
  3. **Served-spec truthfulness** — every /v0 operation documents the generic
     failure set (401/403/429/400), the 401 carries the WWW-Authenticate
     challenge, and the contractually-mandatory headers are marked required —
     Idempotency-Key driven by the manifest's explicit ``idempotency_class``
     (B3 §1), If-Match by its ``precondition_class``.
"""

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from agentdrive.api.v0_manifest import operations
from agentdrive.app import app

pytestmark = pytest.mark.asyncio


_HTTP_METHODS = {
    "GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE",
}


async def test_real_app_v0_openapi_equals_target_manifest():
    app.openapi_schema = None
    spec = app.openapi()
    v0_ops = {
        (method.upper(), path, operation["operationId"])
        for path, methods in spec["paths"].items()
        if path.startswith("/v0")
        for method, operation in methods.items()
        if method.upper() in _HTTP_METHODS
    }
    expected = {(o["method"], o["path"], o["operation_id"]) for o in operations}
    assert v0_ops == expected, (
        f"mounted /v0 surface differs from the manifest: "
        f"missing={expected - v0_ops}, extra={v0_ops - expected}"
    )


async def test_validation_errors_use_v0_envelope():
    app.openapi_schema = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v0/drives")  # no bearer
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert "detail" not in body


async def test_discovery_endpoint_is_served():
    """The ROOT document describes public `/v0`, and only that.

    The path-scoped `/mcp` document used to be the identical document. Since
    the 2026-08-28 audience split it describes a DIFFERENT resource with a
    DIFFERENT audience, and the MCP sidecar is its authority — so a
    deployment with no sidecar configured (every test run, and staging)
    answers 404 rather than handing back the root document. Serving the root
    document there would tell a client the `/mcp` resource is the product
    origin, and a client that believed it would request a product-audience
    token the MCP transport refuses.
    """
    app.openapi_schema = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        root_resp = await client.get("/.well-known/oauth-protected-resource")
        mcp_resp = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert root_resp.status_code == 200
    assert mcp_resp.status_code == 404
    assert mcp_resp.json()["error"]["code"] == "NOT_FOUND"
    body = root_resp.json()
    assert body["resource"]
    assert body["authorization_servers"]
    assert "content:read" in body["scopes_supported"]
    # The root document is the `/v0` API's, so it advertises the full product
    # scope list — including the `drives:write` the MCP surface never uses.
    assert "drives:write" in body["scopes_supported"]


def _v0_operations(spec: dict) -> list[tuple[str, str, dict]]:
    return [
        (method.upper(), path, operation)
        for path, methods in spec["paths"].items()
        if path.startswith("/v0")
        for method, operation in methods.items()
        if method.upper() in _HTTP_METHODS
    ]


def _manifest_by_op_id() -> dict[str, dict]:
    return {o["operation_id"]: o for o in operations}


async def test_manifest_success_statuses_match_handler_route_declarations():
    """The manifest must agree with handler-owned route metadata.

    This intentionally inspects ``APIRoute`` before the manifest-driven
    OpenAPI enrichment runs, so the check cannot pass tautologically because
    the served document copied ``expected_statuses`` from the manifest.
    Alternate 304/307/replay outcomes therefore belong on the route decorator
    as explicit ``responses`` metadata.
    """
    declared: dict[str, set[int]] = {}
    for route in app.routes:
        if (
            not isinstance(route, APIRoute)
            or not route.path.startswith("/v0")
            or not route.include_in_schema
        ):
            continue
        statuses = {route.status_code or 200}
        statuses.update(
            int(status)
            for status in route.responses
            if 200 <= int(status) < 400
        )
        declared[route.operation_id] = statuses

    manifest = _manifest_by_op_id()
    assert set(declared) == set(manifest)
    for operation_id, route_statuses in declared.items():
        assert set(manifest[operation_id]["expected_statuses"]) == route_statuses, (
            operation_id,
            manifest[operation_id]["expected_statuses"],
            sorted(route_statuses),
        )


async def test_served_openapi_success_statuses_match_manifest_exactly():
    """No impossible 2xx/3xx result may leak into generated clients."""
    app.openapi_schema = None
    spec = app.openapi()
    manifest = _manifest_by_op_id()
    for _method, _path, operation in _v0_operations(spec):
        operation_id = operation["operationId"]
        documented = {
            int(status)
            for status in operation["responses"]
            if 200 <= int(status) < 400
        }
        assert documented == set(manifest[operation_id]["expected_statuses"]), (
            operation_id,
            sorted(documented),
            manifest[operation_id]["expected_statuses"],
        )
        assert 202 not in documented


async def test_every_v0_operation_documents_the_generic_failure_set():
    """Every served /v0 op must document 401/403/429/400 (auth, scope,
    rate-limit, malformed request) — the wire outcomes every generated SDK
    needs to model."""
    app.openapi_schema = None
    spec = app.openapi()
    for method, path, operation in _v0_operations(spec):
        responses = operation["responses"]
        missing = {"400", "401", "403", "429"} - set(responses)
        assert not missing, (
            f"{method} {path} ({operation.get('operationId')}) is missing "
            f"generic failure responses: {sorted(missing)}"
        )
        assert "WWW-Authenticate" in responses["401"].get("headers", {}), (
            f"{method} {path} 401 must document the WWW-Authenticate header"
        )


async def test_mutation_headers_required_follow_manifest_idempotency_class():
    """Idempotency-Key is mandatory exactly where the manifest's explicit
    ``idempotency_class`` says ``required`` (B3 §1 — never inferred from the
    HTTP method), and If-Match follows its own explicit class."""
    manifest = _manifest_by_op_id()
    idempotency_expected = {
        op_id for op_id, m in manifest.items()
        if m["idempotency_class"] == "required"
    }
    if_match_expected = {
        op_id for op_id, m in manifest.items()
        if m["if_match_class"] == "required"
    }

    app.openapi_schema = None
    spec = app.openapi()
    idempotency_required: set[str] = set()
    if_match_required: set[str] = set()
    seen: set[str] = set()
    for _method, _path, operation in _v0_operations(spec):
        op_id = operation.get("operationId", "")
        seen.add(op_id)
        for parameter in operation.get("parameters", []):
            if parameter.get("in") != "header":
                continue
            name = parameter.get("name")
            if name == "Idempotency-Key" and parameter.get("required"):
                idempotency_required.add(op_id)
            elif name == "If-Match" and parameter.get("required"):
                if_match_required.add(op_id)

    assert seen == set(manifest), "served op ids must match the manifest"
    assert idempotency_required == idempotency_expected, (
        "Idempotency-Key required set drifted from the manifest's "
        "precondition_class: "
        f"missing={idempotency_expected - idempotency_required}, "
        f"extra={idempotency_required - idempotency_expected}"
    )
    assert if_match_required == if_match_expected, (
        "If-Match required set drifted from the manifest's if_match_class: "
        f"missing={if_match_expected - if_match_required}, "
        f"extra={if_match_required - if_match_expected}"
    )


async def test_201_create_operations_document_location_header():
    """Every creation op advertises Location on its 201 — matching the
    runtime emission (including the version append/restore 201s)."""
    manifest = _manifest_by_op_id()
    create_op_ids = {
        op_id for op_id, m in manifest.items() if 201 in m["expected_statuses"]
    }
    app.openapi_schema = None
    spec = app.openapi()
    for _method, _path, operation in _v0_operations(spec):
        op_id = operation.get("operationId", "")
        if op_id not in create_op_ids:
            continue
        assert "Location" in operation["responses"]["201"].get("headers", {}), (
            f"{op_id} 201 must document the Location header"
        )


async def test_uploads_create_serves_the_closed_begin_request_body():
    """B3 round 3 blocker: the SERVED spec (not just a snapshot) must model
    begin's strict artifact/version target union and content declaration —
    generated clients discover the primary operation's input from here."""
    app.openapi_schema = None
    spec = app.openapi()
    operation = spec["paths"]["/v0/drives/{drive_id}/uploads"]["post"]
    body = operation.get("requestBody")
    assert body, "uploads_create must document its request body"
    assert body["required"] is True
    media = body["content"]["application/json"]
    schema = media["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"target", "content"}
    target = schema["properties"]["target"]
    assert len(target["oneOf"]) == 2
    kinds = set()
    for member in target["oneOf"]:
        assert member["additionalProperties"] is False
        kinds.add(member["properties"]["kind"]["enum"][0])
    assert kinds == {"artifact", "version"}
    content_schema = schema["properties"]["content"]
    assert content_schema["additionalProperties"] is False
    assert set(content_schema["required"]) == {
        "size_bytes", "media_type", "checksum"
    }
    checksum = content_schema["properties"]["checksum"]
    assert checksum["additionalProperties"] is False
    assert checksum["properties"]["algorithm"]["enum"] == ["crc32c"]
    # The conditional If-Match semantics ride on the operation description.
    assert "If-Match" in (operation.get("description") or "")


async def test_upload_operations_serve_the_negotiation_statuses():
    app.openapi_schema = None
    spec = app.openapi()
    begin = spec["paths"]["/v0/drives/{drive_id}/uploads"]["post"]
    for status in ("200", "201", "406", "409", "412", "413", "415", "422",
                   "428", "503"):
        assert status in begin["responses"], status
    for path, method in (
        ("/v0/drives/{drive_id}/uploads/{upload_id}", "get"),
        ("/v0/drives/{drive_id}/uploads/{upload_id}", "delete"),
        ("/v0/drives/{drive_id}/uploads/{upload_id}/complete", "post"),
    ):
        operation = spec["paths"][path][method]
        assert "406" in operation["responses"], (path, method)
    read = spec["paths"]["/v0/drives/{drive_id}/uploads/{upload_id}"]["get"]
    # The not_required contract is stated where SDK generators read it.
    assert "Idempotency-Key" in (read.get("description") or "")


async def test_download_mint_serves_the_closed_request_union():
    """B3 packet 4: the SERVED spec must model the mint's strict
    artifact/version target union — generated clients discover the input
    from here, and the route parses the raw body so FastAPI cannot infer
    it."""
    app.openapi_schema = None
    spec = app.openapi()
    operation = spec["paths"]["/v0/drives/{drive_id}/download-capabilities"]["post"]
    assert operation["operationId"] == "download_capabilities_create"
    body = operation.get("requestBody")
    assert body, "download_capabilities_create must document its request body"
    assert body["required"] is True
    schema = body["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["target"]
    target = schema["properties"]["target"]
    assert len(target["oneOf"]) == 2
    kinds = {}
    for member in target["oneOf"]:
        assert member["additionalProperties"] is False
        kinds[member["properties"]["kind"]["enum"][0]] = set(member["required"])
    assert kinds == {
        "artifact": {"kind", "artifact_id"},
        "version": {"kind", "artifact_id", "version_id"},
    }


async def test_download_mint_serves_exactly_its_reachable_statuses():
    """Task-4 review blocker: the served response map must be EXACTLY the
    statuses this endpoint can produce — equality, not containment. The
    mint has no If-Match, no idempotency claim, no publication semantics,
    and no typed FastAPI body, so 409/412/422/428 are unreachable and must
    not be advertised."""
    app.openapi_schema = None
    spec = app.openapi()
    operation = spec["paths"]["/v0/drives/{drive_id}/download-capabilities"]["post"]
    assert set(operation["responses"]) == {
        "200", "400", "401", "403", "404", "406", "415", "429", "503",
    }
    # Forbidden idempotency is stated where SDK generators read it, and the
    # header is NOT served as an accepted parameter.
    description = operation.get("description") or ""
    assert "Idempotency-Key" in description
    assert "forbidden" in description.lower() or "reject" in description.lower()
    parameter_names = {
        p.get("name") for p in operation.get("parameters", [])
        if p.get("in") == "header"
    }
    assert "Idempotency-Key" not in parameter_names
    assert "If-Match" not in parameter_names


async def test_download_mint_serves_exact_response_schema_and_headers():
    """The 200 contract is closed: exact header set, closed response
    models, and required_headers modeled as EXACTLY the empty object so a
    generated client can never infer arbitrary required capability
    headers."""
    app.openapi_schema = None
    spec = app.openapi()
    operation = spec["paths"]["/v0/drives/{drive_id}/download-capabilities"]["post"]
    success = operation["responses"]["200"]
    assert set(success["headers"]) == {
        "Cache-Control", "Referrer-Policy", "X-Content-Type-Options",
        "X-Request-Id",
    }
    ref = success["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/DownloadCapabilityOut"
    schemas = spec["components"]["schemas"]
    for name in ("DownloadCapabilityOut", "DownloadOut", "DownloadTargetOut"):
        assert schemas[name].get("additionalProperties") is False, name
    target = schemas["DownloadTargetOut"]["properties"]
    assert target["required_headers"]["maxProperties"] == 0
    assert target["required_headers"].get("additionalProperties") is False
    assert target["method"]["const"] == "GET" or target["method"].get("enum") == ["GET"]
    # The signing-unavailable/disabled 503 carries no invented Retry-After
    # promise of its own (B8 owns retry policy); the only 503 retry hint
    # documented is the generic auth-unavailability one.
    assert "503" in operation["responses"]


async def test_download_mint_does_not_change_the_content_get_contract():
    """§12 compatibility: the existing conditional content GETs keep their
    stream-or-307 surface; the mint is a separate operation."""
    app.openapi_schema = None
    spec = app.openapi()
    for path in (
        "/v0/drives/{drive_id}/artifacts/{artifact_id}/content",
        "/v0/drives/{drive_id}/artifacts/{artifact_id}/versions/{version_id}/content",
    ):
        operation = spec["paths"][path]["get"]
        assert "307" in operation["responses"], path
        assert "200" in operation["responses"], path


async def test_uploads_delete_documents_the_strict_if_match_rules():
    """Round-4 nit: '*' and multi-member If-Match are 400 on this operation
    (they cannot pin a session revision) — a generated client must learn
    that from the served spec, not from a surprise."""
    app.openapi_schema = None
    spec = app.openapi()
    operation = spec["paths"]["/v0/drives/{drive_id}/uploads/{upload_id}"]["delete"]
    description = operation.get("description") or ""
    assert "If-Match" in description
    assert "*" in description
    assert "replay" in description.lower()
