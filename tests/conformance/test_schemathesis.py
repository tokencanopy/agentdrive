"""Spec-driven response validation with Schemathesis (§11.4 conformance).

FastAPI's ``response_model`` guards the code paths the hand-written suite
exercises. Schemathesis adds the spec-driven half: it generates requests from
the served OpenAPI and validates every response against the declared response
schema — including shapes the behavior suite may never hit with a 2xx.

The v0 surface is heavily constrained (Idempotency-Key, If-Match, multipart,
cross-resource FK references), so unconstrained auto-generation would mostly
produce 4xx with no schema-validatable body. This test therefore:

  * scopes generation to the read-only ops whose request shapes are fully
    covered by path/query defaults (drives_list, drives_usage, changes_list),
  * authenticates as a workspace manager via ``app.dependency_overrides``,
  * seeds one real drive and pins its ``drive_id`` through a
    ``before_generate_path_parameters`` hook so path-parameterized ops hit a
    live resource,
  * runs Schemathesis' ``response_schema_conformance`` + ``status_code``
    checks and treats any failure as a hard error (``call_and_validate``
    raises on a violated check).

This is intentionally narrow — it guards the schema contract for the ops most
likely to regress a response shape, complementing the router-level
``response_model`` guard rather than replacing the behavior suite.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentdrive.api.v0_deps import v0_actor
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

# The full v0 scope set (drives:*, content:*, sharing:*, usage:read,
# changes:read) so every generated op passes its scope check.
_SCOPES = frozenset(
    {
        "drives:read",
        "drives:write",
        "content:read",
        "content:write",
        "sharing:read",
        "sharing:write",
        "usage:read",
        "changes:read",
    }
)


def _make_manager() -> V0ActorContext:
    return V0ActorContext(
        subject="tcagt_0000000000000001",
        subject_type="agent",
        workspace_id="tcws_0000000000000001",
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=_SCOPES,
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id="tcusr_0000000000000001",
    )


async def _seed_drive(http) -> str:
    resp = await http.post(
        "/v0/drives",
        json={"name": "schemathesis", "metadata": {"purpose": "conformance"}},
        headers={"Idempotency-Key": "schemathesis-seed-1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest_asyncio.fixture
async def _schemathesis_setup(app_with_lifespan):
    app = app_with_lifespan
    app.dependency_overrides[v0_actor] = _make_manager
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        drive_id = await _seed_drive(client)
    yield app, drive_id
    app.dependency_overrides.clear()


async def test_schemathesis_response_schema_conformance(_schemathesis_setup):
    from schemathesis import openapi
    from schemathesis.specs.openapi.checks import (
        response_schema_conformance,
        status_code_conformance,
    )

    app, drive_id = _schemathesis_setup
    schema = openapi.from_asgi("/openapi.json", app)

    targets = {"drives_list", "drives_usage", "changes_list"}

    # A real change-feed cursor: changes requires exactly one of start|cursor,
    # and a fabricated cursor fails closed. Capture one from the seeded drive.
    import httpx

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        cap = await client.get(f"/v0/drives/{drive_id}/changes", params={"start": "now"})
        assert cap.status_code == 200, cap.text
        change_cursor = cap.json()["next_cursor"]

    failures = []
    ran = 0
    for path, methods in schema.app.openapi()["paths"].items():
        if not path.startswith("/v0"):
            continue
        for method, _op in methods.items():
            if method.upper() == "PARAMETERS":
                continue
            api_op = schema[path][method.upper()]
            operation_id = api_op.definition.raw.get("operationId")
            if operation_id not in targets:
                continue
            strategy = api_op.as_strategy()
            for _ in range(8):
                case = strategy.example()
                if "drive_id" in (case.path_parameters or {}):
                    case.path_parameters["drive_id"] = drive_id
                # Pin the full query to values our API accepts (its validation is
                # stricter than the schema's enums, so raw generation produces
                # 400s the schema doesn't document — irrelevant to the goal of
                # validating 2xx response shapes).
                if operation_id == "changes_list":
                    # Exactly one of start|cursor; use a real captured cursor.
                    case.query = {"cursor": change_cursor}
                elif operation_id == "drives_list":
                    case.query = {"state": "active", "limit": 5}
                elif operation_id == "drives_usage":
                    case.query = {}
                ran += 1
                try:
                    case.call_and_validate(
                        checks=[response_schema_conformance, status_code_conformance]
                    )
                except Exception as exc:  # a violated check raises
                    failures.append(
                        f"{operation_id}: {case.method} {case.path} -> {exc}"
                    )
    assert ran > 0, "Schemathesis generated no cases"
    assert not failures, (
        "Schemathesis response-schema violations:\n" + "\n".join(failures)
    )
    # Schemathesis' TestClient-based ASGI transport enters/exits the app
    # lifespan per case, which close_pool()s the shared DB pool the outer
    # `app_with_lifespan` fixture owns. Restore it so the fixture teardown
    # (and every later test) still has a live pool.
    from agentdrive.db import init_pool

    await init_pool()
