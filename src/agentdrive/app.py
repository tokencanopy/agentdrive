"""AgentDrive FastAPI application.

Mounts the shipped v0 contract-reset surface: 51 bearer-authenticated
`/v0` operations, all beta (drives, folders, artifacts, versions, grants,
shares, search, changes,
the viewer-session mint, plus the four B3 direct-upload session
controls, the B3 download-capability mint, and the two D13 navigation reads — disabled-first behind
`DIRECT_TRANSFER_ENABLED`). Staging additionally mounts eight sheet-session
operations behind `SHEET_SESSIONS_ENABLED`. Also mounts RFC 9728 protected-resource discovery,
`/s/{share_key}` redemption, `/health`, `/static`, and the unauthenticated
`/sdk/python` design reference.
The legacy surface lives unmounted in `archive/` (pinned by
tests/test_archive_is_unwired.py); the conformance suite pins the mounted
`/v0` operations to `src/agentdrive/api/v0-operations.json`.

What is deliberately absent, and why:

  * No quota or usage middleware. Tiers are product entitlement, which
    Hub owns (§3.1); v0 usage is two counter columns on `drives`, not a
    metering pipeline.
  * No tier boot guard. It read a `tiers` table the day-0 schema does
    not have.
  * No local token issuance. Hub is the only authorization server;
    `identity/product_token.py` validates Hub-issued bearers and
    `identity/actor.py` builds the actor context the routes require.
"""

import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from .api import (
    convertors as _api_convertors,  # noqa: F401  (side-effect: registers art_id/fld_id URL convertors before any route is matched)
)
from .api.cors import install_cors
from .api.openapi import add_documented_response_headers
from .api.schemas import HealthDegradedResponse, HealthOut
from .api.stability import add_stability_metadata
from .api.v0_artifacts import router as v0_artifacts_router
from .api.v0_changes import router as v0_changes_router
from .api.v0_deps import check_local_credentials, prime_jwks
from .api.v0_discovery import router as v0_discovery_router
from .api.v0_download_capabilities import router as v0_download_capabilities_router
from .api.v0_drives import router as v0_drives_router
from .api.v0_error_handlers import install_v0_error_handlers
from .api.v0_folders import router as v0_folders_router
from .api.v0_grants import router as v0_grants_router
from .api.v0_navigation import router as v0_navigation_router
from .api.v0_search import router as v0_search_router
from .api.v0_shares import router as v0_shares_router
from .api.v0_sheets import router as v0_sheets_router
from .api.v0_sheets import sheet_sessions_router as v0_sheet_sessions_router
from .api.v0_uploads import router as v0_uploads_router
from .api.v0_versions import router as v0_versions_router
from .api.v0_viewer_sessions import router as v0_viewer_sessions_router
from .config import settings
from .db import close_pool, conn, init_pool
from .internal_ingress import build_network_internal_app
from .mcp_proxy import proxy_mcp
from .middleware import (
    MCP_PREFIXES,
    PUBLIC_RENDERER_PREFIXES,
    SHARE_PREFIXES,
    VIEWER_PREFIXES,
    HostRedirectMiddleware,
    HostSurfaceMiddleware,
    ServiceSurfaceMiddleware,
    SurfaceBinding,
)
from .observability import RequestContextMiddleware, setup_logging
from .public.routes import router as public_router
from .ratelimit import limiter
from .site.routes import router as site_router
from .storage import ensure_store
from .viewer.routes import router as viewer_router

STATIC_DIR = Path(__file__).parent / "static"

setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_mount_config()
    await init_pool()
    # AUTH_MODE=local: no credential tables means the migrations never ran,
    # and that is DELIBERATELY fatal — the alternative is a process that
    # answers 503 to every request forever with a green /health.
    await check_local_credentials()
    ensure_store()
    # Warm the Hub JWKS so the first /v0 request does no network I/O — but a
    # Hub outage at boot must not take the app down (prime is non-fatal; /v0
    # auth answers 503 until a later fetch succeeds).
    await prime_jwks()
    # Build the OpenAPI document once at boot, DELIBERATELY fatal on failure.
    # The spec build reads src/agentdrive/api/v0-operations.json; two staging smoke
    # runs caught the first request-time build transiently 500ing with ENOENT
    # on that file (Cloud Run's lazily-streamed image filesystem — the same
    # revision served 200 two minutes later). Building here (a) moves that
    # window into startup, where the probe gives Cloud Run room to retry;
    # (b) turns a genuinely missing manifest into a revision that never
    # becomes Ready — a loud deploy failure instead of mid-traffic 500s;
    # (c) caches the schema so smoke and first callers get it in sub-ms.
    app.openapi()
    log.info("agentdrive started")
    yield
    await close_pool()
    log.info("agentdrive stopped")


# Version is sourced from the installed `agentdrive` distribution so
# OpenAPI consumers (SDK generators, the Swagger UI badge) see the
# actual deployed build, not a string we forgot to bump.
try:
    _APP_VERSION = _pkg_version("agentdrive")
except PackageNotFoundError:
    _APP_VERSION = "0.0.0+dev"


def check_mount_config() -> None:
    """Fail fast on startup misconfiguration. Called from lifespan
    before serving.

    When MOUNT_PREFIX is set, PUBLIC_BASE_URL must end with it (the app's
    absolute URLs must land inside the mount). The issuer choice
    (AUTH_MODE) is closed by a Settings validator, so it can never reach
    this point misconfigured."""
    prefix = settings.mount_prefix.rstrip("/")
    if not prefix:
        return
    if not settings.public_base_url.rstrip("/").endswith(prefix):
        raise RuntimeError(
            f"MOUNT_PREFIX={settings.mount_prefix!r} but "
            f"PUBLIC_BASE_URL={settings.public_base_url!r} does not end "
            "with it — absolute URLs would escape the mount."
        )


def _openapi_servers() -> list[dict[str, str]]:
    """OpenAPI `servers:` for THIS deployment.

    One derivation for the agent-facing origin -- API_BASE_URL falling back
    to PUBLIC_BASE_URL -- so a generated SDK always points at the environment
    whose tokens it will be handed. A staging spec must never hand out
    production's host.

    Read from `settings` directly now. It used to come from
    `identity.agent_auth.config.jwt_issuer()`, which was the same
    derivation, but that module issued tokens and Hub owns issuance (§3.1).
    """
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    servers = [{"url": origin, "description": "This deployment"}]
    local = "http://127.0.0.1:8000"
    if origin not in (local, "http://localhost:8000"):
        servers.append({"url": local, "description": "Local dev"})
    return servers


app = FastAPI(
    title="AgentDrive",
    version=_APP_VERSION,
    lifespan=lifespan,
    root_path=settings.mount_prefix.rstrip("/") or None,
    # FastAPI otherwise PREPENDS `{"url": root_path}` to `servers`, making
    # servers[0] the bare string "/drive" and pushing this deployment's real
    # origin to second place. SDK generators take servers[0], so a generated
    # client would target a relative path.
    #
    # Note WHERE that injection happens: in the `/openapi.json` ROUTE
    # HANDLER, off `scope["root_path"]` — not in `app.openapi()`. So it is
    # invisible to any test that calls the schema builder directly, and only
    # an HTTP request reveals it. The staging smoke gate is what caught it.
    #
    # A root path is a process/routing compatibility mechanism, not an
    # OpenAPI origin. The canonical machine resource is the absolute
    # API_BASE_URL; the browser console is served by Hub
    # and never reaches this process through a `/drive*` browser edge route.
    # A bare `{"url": "/drive"}` is therefore never a valid server entry.
    root_path_in_servers=False,
    description=(
        "AgentDrive is an agent-focused artifact store: drive-scoped "
        "folders, artifacts, and immutable versions, with local grants, "
        "possession-based share links, drive-scoped search, and a "
        "cursor-resumable change feed. Bearer-authenticated with "
        "Hub-issued product tokens (see "
        "/.well-known/oauth-protected-resource); every mutation takes an "
        "Idempotency-Key, and existing-state mutations take If-Match."
    ),
    # `servers[0]` is THIS deployment's agent-facing origin, derived the
    # same way as the JWT `iss`/`aud` and the RFC 8414 discovery doc
    # (`identity.agent_auth.config.jwt_issuer`). OpenAPI-driven SDK
    # generators (openapi-python-client, openapi-generator) default to
    # this URL when no `--server` flag is passed, so a staging spec must
    # not hand out production's host. Production sets
    # API_BASE_URL=https://drive.tokencanopy.com.
    # Localhost is listed as a secondary entry so generated clients
    # can switch via the OpenAPI `Server Variables` UI without
    # editing the spec — omitted when it would duplicate servers[0]
    # (local dev, where the derivation already resolves to localhost).
    servers=_openapi_servers(),
)
app.state.limiter = limiter


# Outermost middleware: redirect configured alias hosts (LEGACY_HOSTS →
# LEGACY_REDIRECT_HOST) before any DB work. Inert when none are configured.
app.add_middleware(RequestContextMiddleware)
app.add_middleware(HostRedirectMiddleware)
# CORS for the console's origins. Inert until CORS_ALLOWED_ORIGINS names one,
# so dev and the public surface are unaffected.
install_cors(app)


def _surface_host(url: str) -> str:
    """A validated base-URL setting → the bare host used for role selection."""
    return urlsplit(url).hostname or ""


def _surface_bindings() -> list[SurfaceBinding]:
    """Build the role map from the validated exact origins.

    Without a public-content origin, the share host keeps the existing
    direct-renderer paths as the documented rollback. Once that origin is
    present, the two roles keep only their distinct asset prefixes while the
    shared permalink prefixes are selected by Host.

    The MCP binding is the odd one out: NON-exclusive, so `/mcp` keeps
    answering on the API host (the legacy transport) while the configured
    origin, when there is one, answers `/mcp` and its discovery document and
    nothing else (ADR-0002). Unconfigured, it is inert.
    """
    public_host = _surface_host(settings.public_content_base_url)
    share_prefixes = SHARE_PREFIXES
    if not public_host:
        share_prefixes += tuple(
            prefix for prefix in PUBLIC_RENDERER_PREFIXES if prefix not in share_prefixes
        )
    return [
        SurfaceBinding("share", _surface_host(settings.share_base_url), share_prefixes),
        SurfaceBinding("public-renderer", public_host, PUBLIC_RENDERER_PREFIXES),
        SurfaceBinding(
            "viewer",
            _surface_host(settings.viewer_base_url),
            VIEWER_PREFIXES,
            private=True,
        ),
        SurfaceBinding(
            "mcp",
            _surface_host(settings.mcp_origin_base_url),
            MCP_PREFIXES,
            # Non-exclusive while the legacy transport on the API host is
            # still served; exclusive once it is retired, at which point
            # `/mcp` answers on the origin and nowhere else.
            exclusive=settings.mcp_legacy_retired,
        ),
    ]


# Assign each configured host its surface role before route matching. With no
# configured hosts the middleware is inert for local single-origin use. An
# unconfigured private viewer fails closed as soon as any other host is bound.
app.add_middleware(
    HostSurfaceMiddleware,
    surfaces=_surface_bindings(),
    mount_prefix=settings.mount_prefix,
)
app.add_middleware(ServiceSurfaceMiddleware, role=settings.service_surface_role)


# EXTRACTED to `api/v0_error_handlers.py` when the hosted MCP got its own
# private loopback ingress (`agentdrive.internal_ingress`). Both apps mount
# the same `/v0` routers, so both must render the same error envelope for the
# same failures; one installer is the only way they cannot drift. The
# rationale for each individual handler moved with it.
install_v0_error_handlers(app)


class _RevalidatedStaticFiles(StaticFiles):
    """StaticFiles that serves `Cache-Control: no-cache`.

    Static refs are unversioned (`/static/editor.js`, no `?v=` hash),
    so without an explicit cache policy browsers apply heuristic
    freshness and keep serving a stale editor.js / agentdrive.css for
    minutes-to-hours after a deploy. `no-cache` means "cache, but
    revalidate" — unchanged files still answer 304 via the ETag that
    StaticFiles already emits, so the cost is one conditional request
    per asset per page load. If assets ever grow content-hashed names,
    drop this subclass and serve them `immutable` instead."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


app.mount("/static", _RevalidatedStaticFiles(directory=str(STATIC_DIR)), name="static")
# The public surface's stylesheet is served by a ROUTE in `public/routes.py`,
# not a mount. Starlette resolves a Mount against `root_path` but matches a
# plain route at the root, so under MOUNT_PREFIX=/drive (staging compatibility
# after the hub migration) a mount here would answer only at
# `/drive/public-static/...` — while the page, served on the share host where
# no /drive prefix exists, links `/public-static/viewer.css`, and
# PUBLIC_PREFIXES would refuse the prefixed form anyway. Every public page
# would render unstyled. Caught by the staging rehearsal.

# The public read surface: `/s/`, `/a/`, `/f/`, `/v/` as server-rendered
# pages. It supersedes the JSON-only redemption route that used to own
# `/s/{share_key}` and 404'd every browser, and reproduces that route's
# byte-serving behaviour for non-HTML clients unchanged.
app.include_router(public_router)
app.include_router(site_router)

# The private viewer surface: the console-iframed shell + credentialed
# document/byte routes under `/view/`, host-gated to VIEWER_BASE_URL.
app.include_router(viewer_router)

app.include_router(v0_drives_router)
app.include_router(v0_folders_router)
app.include_router(v0_grants_router)
app.include_router(v0_shares_router)
app.include_router(v0_navigation_router)
app.include_router(v0_search_router)
app.include_router(v0_artifacts_router)
app.include_router(v0_sheets_router)
if settings.sheet_sessions_enabled:
    app.include_router(v0_sheet_sessions_router)
app.include_router(v0_changes_router)
app.include_router(v0_versions_router)
app.include_router(v0_uploads_router)
app.include_router(v0_download_capabilities_router)
app.include_router(v0_viewer_sessions_router)
app.include_router(v0_discovery_router)

if settings.mcp_internal_service_account_email:
    app.mount(
        "/_internal/mcp",
        build_network_internal_app(
            settings.mcp_internal_service_account_email,
            settings.mcp_internal_service_audience,
        ),
    )


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def hosted_mcp(request: Request):
    """Forward the hosted MCP transport to the localhost Node sidecar."""
    return await proxy_mcp(request)



_fastapi_openapi = app.openapi


def _contract_openapi() -> dict:
    if app.openapi_schema is None:
        raw = _fastapi_openapi()
        # `FastAPI.openapi()` CACHES the raw schema on `app.openapi_schema`
        # as a side effect before returning it. If enrichment below raises,
        # that leftover cache would make every LATER request serve the raw,
        # unenriched spec with a 200 — exactly how the missing-manifest
        # image masked itself in staging (one 500, then silently degraded).
        # Clear it so an enrichment failure stays loud on every request.
        app.openapi_schema = None
        spec = add_documented_response_headers(raw)
        app.openapi_schema = add_stability_metadata(spec)
    return app.openapi_schema


app.openapi = _contract_openapi  # type: ignore[method-assign]


@app.get(
    "/health",
    response_model=HealthOut,
    operation_id="health",
    responses={
        503: {
            "model": HealthDegradedResponse,
            "description": "The database reachability probe failed.",
        }
    },
)
async def health():
    """Liveness + DB-reachability probe. Used by Cloud Run / k8s healthchecks
    and any uptime monitor. Returns 200 only if the DB pool can serve a
    trivial query; 503 otherwise so the orchestrator can pull the instance
    out of rotation.

    NOTE: route is `/health`, NOT `/healthz`. Google's edge infrastructure
    intercepts `/healthz` (legacy kubernetes-reserved path) and returns a
    generic 404 before traffic reaches Cloud Run — discovered the hard way
    during the first prod deploy. Don't rename back."""
    try:
        async with conn() as c:
            await c.fetchval("SELECT 1")
        return {"status": "ok"}
    except Exception as e:
        log.error("health DB probe failed: %s", e)
        raise HTTPException(
            503, detail={"status": "degraded", "error": "database unreachable"}
        ) from e
