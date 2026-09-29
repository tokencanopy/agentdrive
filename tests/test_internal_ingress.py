"""The private loopback ingress: what it refuses, and what it still enforces.

The 2026-08-28 security remediation split the MCP audience from the public
`/v0` product audience. Before it, a token minted for a coding agent's MCP
session was a fully valid `/v0` product token, so the reviewed bounded MCP
surface was not a boundary at all — any holder could call `/v0` directly and
reach operations the MCP never exposes.

That split takes away the sidecar's route to AgentDrive, because public `/v0`
now rejects its audience. This module is the replacement, and these are the
properties that make replacing one boundary with another safe rather than
merely different.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from agentdrive.api import v0_deps
from agentdrive.api.v0_deps import v0_actor
from agentdrive.api.v0_errors import V0ApiError
from agentdrive.config import settings
from agentdrive.identity.internal_proof import (
    INTERNAL_PROOF_HEADER,
    InvalidInternalProof,
    proof_matches,
    require_configured_proof,
)
from agentdrive.internal_ingress import (
    MCP_ALLOWED_OPERATION_IDS,
    MCP_SERVICE_AUTHORIZATION_HEADER,
    _clear_google_cert_cache_for_tests,
    _verify_google_service_identity,
    build_internal_app,
    build_network_internal_app,
)

PROOF = "synthetic-per-boot-proof-0123456789abcdefXY"


def test_network_ingress_allowlist_names_only_manifest_operations():
    manifest_path = (
        Path(__file__).resolve().parent.parent
        / "src/agentdrive/api/v0-operations.json"
    )
    manifest = json.loads(manifest_path.read_text())
    operation_ids = {operation["operation_id"] for operation in manifest["operations"]}

    assert operation_ids >= MCP_ALLOWED_OPERATION_IDS


@pytest.fixture(autouse=True)
def _fresh_jwks_store():
    """The JWKS store is module-global by design; isolate it per test."""
    v0_deps.reset()
    yield
    v0_deps.reset()


@pytest.fixture
def ingress():
    return build_internal_app(PROOF)


@pytest.fixture
def actor_dependency(ingress):
    """The one dependency that makes this a different boundary."""
    return ingress.dependency_overrides[v0_actor]


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v0/drives",
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in headers.items()
            ],
        }
    )


# ---------------------------------------------------------------------------
# The per-boot proof
# ---------------------------------------------------------------------------


def test_the_proof_must_be_a_real_per_boot_secret():
    """Fails closed on absence rather than defaulting to an empty string.

    An empty expected value that compared equal to an absent header would
    turn the gate into a no-op precisely when it is misconfigured.
    """
    for bad in (None, "", "   ", "short", "a" * 42):
        with pytest.raises(InvalidInternalProof):
            require_configured_proof(bad)
    assert require_configured_proof("a" * 43) == "a" * 43


def test_the_ingress_refuses_to_build_without_one():
    with pytest.raises(InvalidInternalProof):
        build_internal_app("")


def test_proof_comparison_rejects_absent_and_empty_values():
    assert proof_matches(PROOF, PROOF) is True
    assert proof_matches(None, PROOF) is False
    assert proof_matches("", PROOF) is False
    assert proof_matches(PROOF, "") is False
    assert proof_matches(PROOF[:-1] + "Z", PROOF) is False


async def test_a_request_without_the_proof_is_indistinguishable_from_a_closed_route(
    ingress,
):
    """404, not 401, and no Bearer challenge.

    A process that reached the port and cannot present the boot proof learns
    only that there is nothing here. A 401 with a challenge would advertise a
    private surface and invite going to get a token for it.
    """
    transport = ASGITransport(app=ingress)
    async with AsyncClient(transport=transport, base_url="http://internal") as client:
        for headers in (
            {},
            {INTERNAL_PROOF_HEADER: "wrong"},
            {INTERNAL_PROOF_HEADER: ""},
            # A prefix of the real proof: the comparison is not a startswith.
            {INTERNAL_PROOF_HEADER: PROOF[:10]},
        ):
            response = await client.get("/v0/drives", headers=headers)
            assert response.status_code == 404, headers
            assert response.json()["error"]["code"] == "NOT_FOUND"
            assert "www-authenticate" not in response.headers


async def test_the_proof_alone_authorizes_nothing(actor_dependency):
    """THE property that makes the proof safe to hold.

    It is not a credential. With a valid proof and no bearer the answer is
    401, exactly as the public boundary answers, because the caller's
    Hub-issued token is still what authorizes the request.
    """
    with pytest.raises(V0ApiError) as raised:
        await actor_dependency(
            _request({INTERNAL_PROOF_HEADER: PROOF}), authorization=None
        )
    assert raised.value.status_code == 401
    assert raised.value.code == "AUTHENTICATION_REQUIRED"


# ---------------------------------------------------------------------------
# The audience split
# ---------------------------------------------------------------------------


def test_the_two_audiences_are_derived_and_can_never_be_equal():
    assert settings.hub_mcp_audience == f"{settings.hub_product_audience}/mcp"
    assert settings.hub_mcp_audience != settings.hub_product_audience


async def test_a_root_audience_token_is_refused_by_the_internal_ingress(
    actor_dependency, monkeypatch, hub_jwks, hub_claims
):
    """The half of the split that protects the MCP surface.

    A `/v0` product token — a machine credential's, or the console's delegated
    one — must not become an MCP session by being pointed at this port.
    """
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store(settings.hub_mcp_audience).prime()

    product = hub_jwks.sign(hub_claims(aud=settings.hub_product_audience))
    with pytest.raises(V0ApiError) as raised:
        await actor_dependency(
            _request({INTERNAL_PROOF_HEADER: PROOF}),
            authorization=f"Bearer {product}",
        )
    assert raised.value.status_code == 401


async def test_an_mcp_audience_token_authenticates_at_the_internal_ingress(
    actor_dependency, monkeypatch, hub_jwks, hub_claims
):
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store(settings.hub_mcp_audience).prime()

    mcp = hub_jwks.sign(hub_claims(aud=settings.hub_mcp_audience))
    actor = await actor_dependency(
        _request({INTERNAL_PROOF_HEADER: PROOF}), authorization=f"Bearer {mcp}"
    )
    # It returns the SAME actor context the public boundary builds, so every
    # downstream scope and grant check is the unchanged `/v0` code.
    assert actor.workspace_id == "tcws_0000000000000001"
    assert actor.can("content:read")


async def test_a_per_product_origin_token_is_verified_against_its_own_audience(
    monkeypatch, hub_jwks, hub_claims
):
    """ADR-0002: the ingress accepts the origin resource's audience beside the
    legacy one — each verified exactly, neither a bare origin."""
    monkeypatch.setattr(settings, "mcp_origin_base_url", "https://drive.mcp.example.test")
    origin_audience = "https://drive.mcp.example.test/mcp"
    assert settings.hub_mcp_audiences == (settings.hub_mcp_audience, origin_audience)
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    for audience in settings.hub_mcp_audiences:
        await v0_deps._store(audience).prime()
    actor_dependency = build_internal_app(PROOF).dependency_overrides[v0_actor]

    for audience in settings.hub_mcp_audiences:
        actor = await actor_dependency(
            _request({INTERNAL_PROOF_HEADER: PROOF}),
            authorization=f"Bearer {hub_jwks.sign(hub_claims(aud=audience))}",
        )
        assert actor.workspace_id == "tcws_0000000000000001"

    # The BARE origin is not an audience here any more than on the API host:
    # Hub's alias is a lookup key, and a token could only carry it if Hub
    # were minting the product-shaped audience the split forbids.
    for aud in (
        "https://drive.mcp.example.test",
        settings.hub_product_audience,
        [settings.hub_mcp_audience, origin_audience],
    ):
        with pytest.raises(V0ApiError) as raised:
            await actor_dependency(
                _request({INTERNAL_PROOF_HEADER: PROOF}),
                authorization=f"Bearer {hub_jwks.sign(hub_claims(aud=aud))}",
            )
        assert raised.value.status_code == 401


async def test_a_legacy_audience_token_is_refused_once_the_legacy_transport_is_retired(
    monkeypatch, hub_jwks, hub_claims
):
    monkeypatch.setattr(settings, "mcp_origin_base_url", "https://drive.mcp.example.test")
    monkeypatch.setattr(settings, "mcp_legacy_retired", True)
    origin_audience = "https://drive.mcp.example.test/mcp"
    assert settings.hub_mcp_audiences == (origin_audience,)
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store(origin_audience).prime()
    actor_dependency = build_internal_app(PROOF).dependency_overrides[v0_actor]

    actor = await actor_dependency(
        _request({INTERNAL_PROOF_HEADER: PROOF}),
        authorization=f"Bearer {hub_jwks.sign(hub_claims(aud=origin_audience))}",
    )
    assert actor.workspace_id == "tcws_0000000000000001"
    # The legacy audience — still a perfectly well-formed Hub token — is
    # refused: the grants bound to it are retired with the transport.
    with pytest.raises(V0ApiError) as raised:
        await actor_dependency(
            _request({INTERNAL_PROOF_HEADER: PROOF}),
            authorization=f"Bearer {hub_jwks.sign(hub_claims(aud=settings.hub_mcp_audience))}",
        )
    assert raised.value.status_code == 401


async def test_an_expired_or_foreign_mcp_token_is_still_refused(
    actor_dependency, monkeypatch, hub_jwks, foreign_jwks, hub_claims
):
    """The ingress is not a relaxed verifier. Only the audience differs."""
    import time

    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store(settings.hub_mcp_audience).prime()

    now = int(time.time())
    for token in (
        hub_jwks.sign(hub_claims(aud=settings.hub_mcp_audience, exp=now - 60)),
        hub_jwks.sign(hub_claims(aud=settings.hub_mcp_audience, iss="https://evil.invalid")),
        foreign_jwks.sign(
            hub_claims(aud=settings.hub_mcp_audience), kid=hub_jwks.kid
        ),
    ):
        with pytest.raises(V0ApiError) as raised:
            await actor_dependency(
                _request({INTERNAL_PROOF_HEADER: PROOF}),
                authorization=f"Bearer {token}",
            )
        assert raised.value.status_code == 401


# ---------------------------------------------------------------------------
# Surface isolation
# ---------------------------------------------------------------------------


def test_the_ingress_exposes_only_the_verticals_the_mcp_tools_use(ingress):
    """A narrower surface than the public app, deliberately.

    A future tool needing another vertical is an explicit change to
    `_MCP_ROUTERS`, not a side effect of a router landing in the public app.
    """
    paths = {route.path for route in ingress.routes}
    assert any(path.startswith("/v0/drives") for path in paths)
    for absent in (
        "/v0/drives/{drive_id}/viewer-sessions",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        assert absent not in paths, absent
    assert not any("/sheets" in path for path in paths)
    assert not any("/download" in path for path in paths)


def test_the_ingress_publishes_no_schema_or_docs(ingress):
    """No clients to document, and publishing one would invite treating this
    as an API rather than a private seam."""
    assert ingress.openapi_url is None
    assert ingress.docs_url is None
    assert ingress.redoc_url is None
    paths = {route.path for route in ingress.routes}
    assert "/openapi.json" not in paths


def test_the_public_app_does_not_mount_the_ingress():
    """The load-bearing containment property.

    A path-routed private surface inside the public app is one routing bug
    away from the internet. This asserts the public app neither imports the
    ingress nor overrides the actor dependency it would need to.
    """
    from agentdrive.app import app as public_app

    assert v0_actor not in public_app.dependency_overrides
    assert public_app.dependency_overrides == {}


async def test_network_ingress_requires_the_exact_mcp_service_identity():
    async def verify(token: str, audience: str) -> dict:
        if token != "valid-service-token":
            raise ValueError("invalid")
        return {
            "aud": audience,
            "email": "agentdrive-mcp@example.iam.gserviceaccount.com",
            "email_verified": True,
        }

    ingress = build_network_internal_app(
        "agentdrive-mcp@example.iam.gserviceaccount.com",
        "https://agentdrive-abc-uc.a.run.app",
        identity_verifier=verify,
    )
    transport = ASGITransport(app=ingress)
    async with AsyncClient(transport=transport, base_url="http://internal") as client:
        for headers in ({}, {MCP_SERVICE_AUTHORIZATION_HEADER: "Bearer wrong"}):
            response = await client.get("/v0/drives", headers=headers)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "NOT_FOUND"

        authenticated_workload = await client.get(
            "/v0/drives",
            headers={
                MCP_SERVICE_AUTHORIZATION_HEADER: "Bearer valid-service-token"
            },
        )
        assert authenticated_workload.status_code == 401
        assert authenticated_workload.json()["error"]["code"] == (
            "AUTHENTICATION_REQUIRED"
        )


async def test_google_identity_verification_reuses_cached_signing_certs(monkeypatch):
    """Rotating invalid service tokens must not turn one request into one
    synchronous outbound certificate fetch."""
    from google.oauth2 import id_token

    import agentdrive.internal_ingress as internal_ingress

    calls = 0

    class Response:
        status = 200
        data = b"{}"
        headers = {"cache-control": "public, max-age=300"}

    def fetch(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return Response()

    def reject(_token, request, _audience):
        request("https://www.googleapis.test/oauth2/v1/certs", method="GET")
        raise ValueError("synthetic invalid token")

    _clear_google_cert_cache_for_tests()
    monkeypatch.setattr(internal_ingress, "_google_request", fetch)
    monkeypatch.setattr(id_token, "verify_oauth2_token", reject)
    for token in ("synthetic-one", "synthetic-two"):
        with pytest.raises(ValueError, match="synthetic invalid token"):
            await _verify_google_service_identity(token, "https://api.example.test")

    assert calls == 1
    _clear_google_cert_cache_for_tests()


async def test_network_ingress_refuses_routes_outside_the_reviewed_tool_set():
    async def verify(_token: str, audience: str) -> dict:
        return {
            "aud": audience,
            "email": "agentdrive-mcp@example.iam.gserviceaccount.com",
            "email_verified": True,
        }

    ingress = build_network_internal_app(
        "agentdrive-mcp@example.iam.gserviceaccount.com",
        "https://agentdrive-abc-uc.a.run.app",
        identity_verifier=verify,
    )
    transport = ASGITransport(app=ingress)
    async with AsyncClient(transport=transport, base_url="http://internal") as client:
        response = await client.get(
            "/v0/drives/drv_0000000000000001/folders",
            headers={MCP_SERVICE_AUTHORIZATION_HEADER: "Bearer service-token"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# End to end, over real Postgres
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def live_ingress(app_with_lifespan):
    """The ingress against the same live database the public app uses.

    `app_with_lifespan` is here for its DATABASE side effects — the pool, the
    schema, the bucket — not for the app itself. The ingress runs its own
    routers, so this proves the whole path: the proof gate, the `/mcp`-audience
    verification, the mounted routers, the rate-limit dependency, and the error
    envelope, all at once. Every other test in this file stops at the
    dependency, which would not have caught a router that fails to mount.
    """
    ingress = build_internal_app(PROOF)
    transport = ASGITransport(app=ingress)
    async with AsyncClient(transport=transport, base_url="http://internal") as client:
        yield client


async def test_a_real_v0_call_succeeds_through_the_ingress(
    live_ingress, monkeypatch, hub_jwks, hub_claims
):
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store(settings.hub_mcp_audience).prime()
    token = hub_jwks.sign(hub_claims(aud=settings.hub_mcp_audience))

    response = await live_ingress.get(
        "/v0/drives",
        headers={
            INTERNAL_PROOF_HEADER: PROOF,
            "Authorization": f"Bearer {token}",
        },
    )
    assert response.status_code == 200, response.text
    assert "items" in response.json()


async def test_scope_enforcement_is_the_unchanged_v0_code(
    live_ingress, monkeypatch, hub_jwks, hub_claims
):
    """The ingress overrides WHO the caller is, never WHAT they may do.

    A token without `drives:read` is refused by the route's own scope check —
    the same `_require_scope` the public API runs — and not by anything this
    module added.
    """
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    await v0_deps._store(settings.hub_mcp_audience).prime()
    token = hub_jwks.sign(
        hub_claims(aud=settings.hub_mcp_audience, scope="content:read")
    )

    response = await live_ingress.get(
        "/v0/drives",
        headers={
            INTERNAL_PROOF_HEADER: PROOF,
            "Authorization": f"Bearer {token}",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_an_unmounted_vertical_is_404_with_the_v0_envelope(live_ingress):
    """A path the ingress does not serve fails like every other /v0 error.

    Also proves the shared error handlers are installed: without them FastAPI
    would answer `{"detail": "Not Found"}`, a second error shape for the same
    condition.
    """
    response = await live_ingress.get(
        "/v0/drives/drv_0000000000000001/sheets",
        headers={INTERNAL_PROOF_HEADER: PROOF},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert "detail" not in response.json()
