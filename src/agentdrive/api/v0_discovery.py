"""Protected-resource discovery for the reset v0 surface (RFC 9728).

Post-cutover, AgentDrive is a pure RESOURCE server: Hub is the only
authorization server — it mints every product access token the reset `/v0`
routers and the curated MCP transport accept. AgentDrive keeps ONLY the
protected-resource metadata document so an OAuth client that meets a
`WWW-Authenticate: Bearer resource_metadata="…"` challenge can discover
WHERE to go for a token. This document's `authorization_servers` points at
Hub, never at this host.

The resource-root document describes the public `/v0` API. The RFC 9728
path-inserted `/mcp` variant describes a DIFFERENT resource with a DIFFERENT
audience, `<origin>/mcp`, and is answered by the MCP sidecar itself — see the
2026-08-28 security remediation. They share an origin, not an identity.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..config import settings
from .v0_errors import V0ApiError

router = APIRouter(tags=["discovery"])

# The scopes this deployment's /v0 routers actually enforce — kept in sync
# with the `_SCOPE_*` constants across the verticals.
_SCOPES_SUPPORTED = [
    "drives:read",
    "drives:write",
    "content:read",
    "content:write",
    "changes:read",
    "sharing:read",
    "sharing:write",
    "usage:read",
]


def _document(resource: str) -> dict:
    # Under AUTH_MODE=hub, Hub is the ONLY issuer whose tokens this
    # deployment accepts; RFC 8414 discovery for the client continues at
    # `{hub_issuer}/.well-known/openid-configuration`. Under AUTH_MODE=local
    # there is no authorization server to discover at all — the credential is
    # an opaque API key the operator minted with `agentdrive-keys` and pastes
    # in as a static bearer — and the document says so honestly with an empty
    # list rather than naming an origin that serves no OAuth endpoints. There
    # is no `/jwks` either: nothing in a standalone install signs anything.
    servers = [] if settings.auth_mode == "local" else [settings.hub_issuer.rstrip("/")]
    return {
        "resource": resource,
        "authorization_servers": servers,
        "bearer_methods_supported": ["header"],
        "scopes_supported": _SCOPES_SUPPORTED,
    }


@router.get(
    "/.well-known/oauth-protected-resource",
    operation_id="oauth_protected_resource",
    summary="Protected-resource metadata (RFC 9728)",
    description=(
        "Names the reset v0 surface as a protected resource and points "
        "clients at Hub — the only authorization server whose product "
        "tokens this deployment accepts."
    ),
)
async def oauth_protected_resource() -> dict:
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    return _document(origin)


@router.get(
    "/.well-known/oauth-protected-resource/mcp",
    operation_id="oauth_protected_resource_mcp",
    summary="Protected-resource metadata for the MCP endpoint (RFC 9728)",
    description=(
        "Path-inserted protected-resource metadata for the hosted MCP "
        "transport. It names a DIFFERENT resource from the root document — "
        "`<origin>/mcp`, with the separately maintained MCP scope list — and "
        "is served by the MCP sidecar, which is the authority on its own tool "
        "set. "
        "Deployments with no sidecar answer 404: there is no `/mcp` resource "
        "to describe."
    ),
)
async def oauth_protected_resource_mcp(request: Request) -> dict:
    """The document for the `/mcp` resource, which is NOT the `/v0` one.

    The AUDIENCE differs, since the 2026-08-28 security remediation. The two
    used to be one string, which made a token minted for an MCP session a
    fully valid `/v0` product token and reduced the reviewed tool surface to a
    suggestion. That mutual refusal is the boundary (`docs/adr/0001`).

    The SCOPE LIST does not differ today, and that is deliberate. The sidecar
    advertises Hub's `AGENTDRIVE_MCP_SCOPES` (`apps/hub/src/oauth/resources.ts`),
    which presently carries the same eight strings as the root document,
    `drives:write` included. That scope was withheld from MCP clients while
    the two audiences were one string — granting it to an MCP session was
    granting direct `/v0` drive administration to any token holder — and was
    re-included after the split, because an MCP token can no longer reach
    `/v0` at all: it now gates only the reviewed tool surface, and a read-only
    consent choice still withholds it. The two lists are maintained
    separately and gate different audiences; they coincide, they are not
    shared, and this document must not be "narrowed" to make them look
    different. The sidecar answers for itself so the documents cannot drift,
    because there is exactly one of each.

    With NO sidecar this is a 404, not the root document. Serving the root
    document here would now tell a client the `/mcp` resource is the product
    origin — a client that believed it would request a product-audience token
    that the MCP transport (if one appeared) would refuse. A 404 says the
    truthful thing: this deployment has no `/mcp` resource.
    """
    if settings.mcp_proxy_url:
        from ..mcp_proxy import MCP_RESOURCE_METADATA_PATH, proxy_mcp

        # FastAPI returns a Response as-is, so the documented dict schema for
        # this operation is unchanged.
        return await proxy_mcp(request, upstream_path=MCP_RESOURCE_METADATA_PATH)  # type: ignore[return-value]
    raise V0ApiError(404, "NOT_FOUND", "this deployment has no MCP resource")
