"""The private data plane for the hosted MCP transport.

WHY THIS EXISTS. Before the 2026-08-28 security remediation the MCP transport
and the public `/v0` API shared one RFC 8707 resource and one JWT audience, so
a token minted for a coding agent's MCP session was a fully valid `/v0`
product token. The reviewed bounded MCP surface was not a boundary at all:
any holder could call `/v0` directly and reach operations the MCP deliberately
does not expose.

Splitting the audiences closes that, and takes the transport's route to
AgentDrive with it — public `/v0` now rejects an MCP-audience token, and
handing the transport a product-audience token instead would put the confusion
straight back. It therefore calls this narrower reuse of the same `/v0`
routers. Production mounts it under `/_internal/mcp` and admits only the
dedicated MCP workload identity; local transition mode binds it to loopback.

WHAT MAKES IT SAFE. Three independent things, in this order:

  1. A dedicated Google service identity gates the network mount; local
     transition mode instead uses a per-boot 256-bit proof on a numeric
     loopback socket. A missing or wrong identity gets `404`.
  2. **The workload identity is not user authorization.** Every request still carries the
     caller's Hub-issued MCP JWT, this app still verifies it — against the
     `/mcp` audience, so a root-audience token is refused here too — and the
     unchanged `/v0` route code still intersects its scopes with live local
     grants. Remove the workload credential and nothing is readable; it only
     decides whether the internal surface answers at all.
  3. The network mount admits only the exact operation IDs used by the 24
     reviewed tools. Including a router for code reuse cannot widen authority.

WHAT IT DOES NOT DO. It does not reimplement authorization, add an operation,
widen a scope, or serve anything the public app does not. It mounts a NARROWER
router set — exactly the verticals the MCP tools use — so a future tool that
needs another one is an explicit change here rather than an accident.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import jwt
from fastapi import FastAPI, Header, Request

from .api.v0_artifacts import router as v0_artifacts_router
from .api.v0_changes import router as v0_changes_router
from .api.v0_deps import (
    check_local_credentials,
    prime_jwks,
    resolve_actor,
    v0_actor,
)
from .api.v0_drives import router as v0_drives_router
from .api.v0_error_handlers import install_v0_error_handlers
from .api.v0_errors import V0ApiError
from .api.v0_folders import router as v0_folders_router
from .api.v0_grants import router as v0_grants_router
from .api.v0_navigation import router as v0_navigation_router
from .api.v0_search import router as v0_search_router
from .api.v0_shares import router as v0_shares_router
from .api.v0_uploads import router as v0_uploads_router
from .api.v0_versions import router as v0_versions_router
from .config import settings
from .db import close_pool, init_pool
from .identity.actor import V0ActorContext
from .identity.internal_proof import (
    INTERNAL_PROOF_HEADER,
    proof_matches,
    require_configured_proof,
)
from .observability import RequestContextMiddleware, setup_logging
from .ratelimit import limiter
from .storage import ensure_store

log = logging.getLogger(__name__)

# google-auth's ID-token helper fetches Google's signing certificates for
# every verification unless its Request transport supplies caching. This
# mount is reachable through public Cloud Run ingress, so a fresh outbound
# fetch per arbitrary header would be an amplification seam. One bounded,
# single-flight cache is shared by all verifier threads.
_GOOGLE_CERT_CACHE_MAX_AGE_SECONDS = 3600
_GOOGLE_CERT_CACHE_DEFAULT_AGE_SECONDS = 300
_google_cert_cache: dict[str, tuple[float, Any]] = {}
_google_cert_cache_lock = threading.Lock()
_google_request: Any = None


def _cache_age(headers: Any) -> int:
    value = str((headers or {}).get("cache-control", ""))
    for directive in value.split(","):
        name, separator, raw = directive.strip().partition("=")
        if separator and name.lower() == "max-age":
            try:
                return max(1, min(int(raw), _GOOGLE_CERT_CACHE_MAX_AGE_SECONDS))
            except ValueError:
                break
    return _GOOGLE_CERT_CACHE_DEFAULT_AGE_SECONDS


def _cached_google_request(
    url: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: Any = None,
    timeout: int | None = None,
    **kwargs: Any,
) -> Any:
    """google-auth transport with a bounded, single-flight cert cache."""
    global _google_request
    cacheable = method.upper() == "GET" and body is None
    with _google_cert_cache_lock:
        now = time.monotonic()
        cached = _google_cert_cache.get(url) if cacheable else None
        if cached and cached[0] > now:
            return cached[1]
        if _google_request is None:
            from google.auth.transport.requests import Request as GoogleRequest

            _google_request = GoogleRequest()
        # Bound network occupancy; holding the lock supplies single-flight.
        effective_timeout = min(timeout or 5, 5)
        response = _google_request(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=effective_timeout,
            **kwargs,
        )
        if cacheable and response.status == 200:
            _google_cert_cache[url] = (
                now + _cache_age(response.headers),
                response,
            )
        return response


def _clear_google_cert_cache_for_tests() -> None:
    with _google_cert_cache_lock:
        _google_cert_cache.clear()

MCP_SERVICE_AUTHORIZATION_HEADER = "x-agentdrive-mcp-service-authorization"

# Exact operations used by the 24 reviewed MCP tools. The routers below are a
# convenient code-reuse boundary, not an authority boundary: the networked
# service identity gate checks this set after FastAPI has resolved the route.
MCP_ALLOWED_OPERATION_IDS = frozenset(
    {
        "drives_list",
        "drives_usage",
        "drives_create",
        "drives_delete",
        "drives_restore",
        "entries_list",
        "lookup",
        "drive_search",
        "folders_read",
        "folders_create",
        "folders_update",
        "folders_delete",
        "folders_restore",
        "artifacts_read",
        "artifacts_create",
        "artifacts_update",
        "artifacts_delete",
        "artifacts_restore",
        "artifacts_content",
        "versions_list",
        "versions_append",
        "changes_list",
        "grants_list",
        "grants_create",
        "grants_revoke",
        "shares_create",
        "uploads_create",
        "uploads_read",
        "uploads_complete",
        "uploads_delete",
    }
)

#: Exactly the verticals the 24 MCP tools reach. Adding one is a
#: deliberate widening of this private surface, not a side effect of a new
#: router landing in the public app.
_MCP_ROUTERS = (
    v0_drives_router,
    v0_navigation_router,
    v0_search_router,
    v0_folders_router,
    v0_artifacts_router,
    v0_versions_router,
    v0_uploads_router,
    v0_changes_router,
    v0_grants_router,
    v0_shares_router,
)


def _not_found() -> V0ApiError:
    """The answer to a request without a valid proof.

    `404`, not `401`: a process that reached this port and cannot present the
    boot proof learns only that there is nothing here, which is what a closed
    route would have told it. It carries no `WWW-Authenticate` challenge for
    the same reason — this surface is not something to go get a token for.
    """
    return V0ApiError(404, "NOT_FOUND", "not found")


def _audience_for(authorization: str | None, audiences: tuple[str, ...]) -> str:
    """Pick WHICH configured `/mcp` audience a bearer is verified against.

    Read off the token's own unverified `aud`, which then has to survive the
    full verification for that audience — selecting is not trusting. A token
    naming anything outside the configured set, a list, or nothing at all is
    verified against the primary audience and refused exactly as before; the
    only thing this adds is that a token for the per-product origin's
    resource (ADR-0002) is checked against ITS audience rather than the
    legacy one.
    """
    if len(audiences) == 1 or not authorization:
        return audiences[0]
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return audiences[0]
    try:
        claims = jwt.decode(token.strip(), options={"verify_signature": False})
    except jwt.PyJWTError:
        return audiences[0]
    aud = claims.get("aud")
    return aud if isinstance(aud, str) and aud in audiences else audiences[0]


def build_internal_app(proof: str) -> FastAPI:
    """Build the internal ingress bound to one boot proof."""
    expected_proof = require_configured_proof(proof)
    mcp_audiences = settings.hub_mcp_audiences

    async def internal_actor(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> V0ActorContext:
        if not proof_matches(
            request.headers.get(INTERNAL_PROOF_HEADER), expected_proof
        ):
            raise _not_found()
        # The SAME verification the public boundary runs, against an `/mcp`
        # audience instead of the product one. Not a relaxed variant: a
        # malformed, expired, wrong-issuer, or ROOT-AUDIENCE token is refused
        # here exactly as it would be there.
        return await resolve_actor(
            authorization, audience=_audience_for(authorization, mcp_audiences)
        )

    #: One local client, one worker. A pool the size of the public API's
    #: would double this instance's Cloud SQL connections for a surface that
    #: serves only the sidecar beside it.
    INGRESS_POOL_MAX = 4

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await init_pool(max_size=INGRESS_POOL_MAX)
        await check_local_credentials()
        ensure_store()
        # Warm the `/mcp` verifiers specifically. Priming the product audience
        # here would leave the first real request doing network I/O for the
        # verifier it actually needs.
        for mcp_audience in mcp_audiences:
            await prime_jwks(mcp_audience)
        log.info("agentdrive internal ingress started")
        yield
        await close_pool()
        log.info("agentdrive internal ingress stopped")

    app = FastAPI(
        title="AgentDrive internal ingress",
        lifespan=lifespan,
        # No interactive docs and no schema: this surface has no clients to
        # document, and publishing one would invite treating it as an API.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.limiter = limiter
    app.add_middleware(RequestContextMiddleware)
    install_v0_error_handlers(app)

    # The ONE substitution that makes this a different boundary. Overriding
    # the dependency rather than editing the routes means scope enforcement,
    # local grant resolution, revision preconditions, idempotency, and the
    # error envelope are the same code the public API runs — there is no
    # second implementation to drift.
    app.dependency_overrides[v0_actor] = internal_actor

    for router in _MCP_ROUTERS:
        app.include_router(router)

    if settings.auth_mode == "local":
        # Credential introspection for the MCP sidecar's `api-key` mode
        # (§4.2 as amended). LOCAL MODE ONLY, and not mounted at all under
        # `hub` — the hosted sidecar verifies a Hub JWT itself and has no use
        # for this, so the route should not exist there to be reached.
        #
        # It is not a second authorization path: it runs the SAME
        # `resolve_actor` the ingress's own dependency runs, behind the SAME
        # per-boot proof, and it authorizes nothing by itself. Every
        # `tools/call` still arrives at a `/v0` route below with the key,
        # where it is resolved again and intersected with live local grants.
        # The sidecar caches a positive answer for 30s; that shortens
        # `tools/list`, never authorization.
        @app.post("/_internal/introspect", include_in_schema=False)
        async def introspect(
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> dict:
            if not proof_matches(
                request.headers.get(INTERNAL_PROOF_HEADER), expected_proof
            ):
                raise _not_found()
            actor = await resolve_actor(authorization)
            return {
                "subject": actor.subject,
                "principal_type": actor.subject_type,
                "workspace_id": actor.workspace_id,
                "scopes": sorted(actor.scopes),
                "workspace_role": actor.workspace_role,
                "sponsor_id": actor.sponsor_id,
                "key_id": actor.token_id,
            }

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok"}

    return app


async def _verify_google_service_identity(token: str, audience: str) -> dict[str, Any]:
    """Verify a Google-signed Cloud Run caller token off the event loop."""
    from google.oauth2 import id_token

    return await asyncio.to_thread(
        id_token.verify_oauth2_token,
        token,
        _cached_google_request,
        audience,
    )


def build_network_internal_app(
    expected_service_account: str,
    expected_audience: str,
    *,
    identity_verifier: Callable[[str, str], Awaitable[dict[str, Any]]] = (
        _verify_google_service_identity
    ),
) -> FastAPI:
    """Data-plane seam for the separately privileged hosted-MCP service.

    The Google identity establishes WHICH workload is calling; the forwarded
    MCP bearer still establishes the user/agent and is verified against the
    MCP resource audience. Both are required, then the ordinary v0 scope and
    local-grant checks run unchanged.
    """
    account = expected_service_account.strip()
    audience = expected_audience.rstrip("/")
    if not account or not audience:
        raise ValueError("network MCP ingress requires service account and audience")
    mcp_audiences = settings.hub_mcp_audiences

    async def network_actor(
        request: Request,
        authorization: str | None = Header(default=None),
        service_authorization: str | None = Header(
            default=None, alias=MCP_SERVICE_AUTHORIZATION_HEADER
        ),
    ) -> V0ActorContext:
        route = request.scope.get("route")
        if getattr(route, "operation_id", None) not in MCP_ALLOWED_OPERATION_IDS:
            raise _not_found()
        scheme, _, token = (service_authorization or "").strip().partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise _not_found()
        try:
            claims = await identity_verifier(token, audience)
        except Exception:
            raise _not_found() from None
        if (
            claims.get("email") != account
            or claims.get("email_verified") is not True
            or claims.get("aud") != audience
        ):
            raise _not_found()
        return await resolve_actor(
            authorization, audience=_audience_for(authorization, mcp_audiences)
        )

    app = FastAPI(
        title="AgentDrive MCP internal data plane",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.limiter = limiter
    app.add_middleware(RequestContextMiddleware)
    install_v0_error_handlers(app)
    app.dependency_overrides[v0_actor] = network_actor
    for router in _MCP_ROUTERS:
        app.include_router(router)
    return app


def internal_app_from_env(environment: dict[str, str] | None = None) -> FastAPI:
    """Build the ingress from the supervisor-provided environment."""
    import os

    source = os.environ if environment is None else environment
    return build_internal_app(source.get("AGENTDRIVE_INTERNAL_PROOF", ""))


# Uvicorn factory target. The supervisor runs
# `uvicorn agentdrive.internal_ingress:app --factory --host <loopback>`.
def app() -> FastAPI:  # pragma: no cover - exercised by the supervisor
    setup_logging()
    return internal_app_from_env()
