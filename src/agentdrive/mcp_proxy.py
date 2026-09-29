"""Bounded same-container proxy for the hosted AgentDrive MCP.

The Node MCP implementation owns MCP protocol behavior and OAuth bearer
verification. FastAPI owns the canonical ``drive.tokencanopy.com`` origin and
only forwards two exact routes to the loopback sidecar configured by
``MCP_PROXY_URL``: the ``/mcp`` transport, and the path-scoped RFC 9728
metadata document that names it. This seam deliberately forwards no cookies,
host headers, or client-controlled forwarding metadata.

The metadata route matters because RFC 9728 lets a client discover a resource
at ``/mcp`` by inserting the well-known segment BEFORE the path, and the Node
sidecar is the authority on its own protected-resource document -- it names
the ``/mcp`` audience and advertises the scope bundle Hub grants for it, which
is maintained separately from the ``/v0`` list even where the strings
coincide. Serving only the root document here answered the path-scoped form
with 404, so a client that asked the specific question fell back to the
general one and was told the wrong resource.

Nothing that crosses this seam is cacheable. Every proxied response -- the
transport's, the sidecar's error responses, and the discovery document --
leaves with exactly one ``Cache-Control: no-store``, whatever the sidecar
sent: a cached discovery document is how a client ends up acting on a stale
resource identifier, and the transport responses are bearer-authenticated.

The upstream path is passed by the route rather than read off the request, so
a mount prefix cannot leak into the sidecar URL: the sidecar always answers at
the origin root regardless of where FastAPI is mounted.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .config import settings

log = logging.getLogger(__name__)

MAX_MCP_REQUEST_BYTES = 2 * 1024 * 1024
MAX_MCP_RESPONSE_BYTES = 4 * 1024 * 1024

_FORWARDED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "content-type",
        "last-event-id",
        "mcp-protocol-version",
        "mcp-session-id",
        "origin",
        "x-request-id",
    }
)
_FORWARDED_RESPONSE_HEADERS = frozenset(
    {
        "allow",
        "content-type",
        "last-event-id",
        "mcp-protocol-version",
        "mcp-session-id",
        "retry-after",
        "vary",
        "www-authenticate",
        "x-request-id",
    }
)


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    retry_after: str | None = None,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


def _rpc_error(status_code: int, rpc_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "jsonrpc": "2.0",
            "error": {"code": rpc_code, "message": message},
            "id": None,
        },
        headers={"Cache-Control": "no-store"},
    )


class RequestBodyTooLargeError(Exception):
    """The MCP request exceeds the Node transport's bounded body limit."""


class ResponseBodyTooLargeError(Exception):
    """The sidecar returned more data than this edge should buffer."""


async def _request_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_MCP_REQUEST_BYTES:
                raise ValueError("too large")
        except ValueError as exc:
            raise RequestBodyTooLargeError from exc

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_MCP_REQUEST_BYTES:
            raise RequestBodyTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


async def _response_body(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_MCP_RESPONSE_BYTES:
            raise ResponseBodyTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


MCP_TRANSPORT_PATH = "/mcp"
MCP_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
# The label telling the sidecar WHICH origin a request arrived on (ADR-0002),
# so it answers with that resource's document, challenge, and audience. Set by
# this edge from the surface HostSurfaceMiddleware selected, and ONLY then;
# the client's own copy is not in `_FORWARDED_REQUEST_HEADERS` and never
# crosses. Spelled identically in `apps/drive/mcp/server/src/http.ts`.
MCP_ORIGIN_HEADER = "x-agentdrive-mcp-origin"


def _target_url(request: Request, upstream_path: str) -> str:
    base = settings.mcp_proxy_url.rstrip("/")
    target = f"{base}{upstream_path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return target


async def proxy_mcp(
    request: Request, upstream_path: str = MCP_TRANSPORT_PATH
) -> Response:
    """Forward one MCP HTTP request to the configured local sidecar."""
    if not settings.mcp_proxy_url:
        return _error(404, "NOT_FOUND", "not found")

    try:
        body = await _request_body(request)
    except RequestBodyTooLargeError:
        return _rpc_error(413, -32000, "MCP request body is too large")

    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in _FORWARDED_REQUEST_HEADERS
    }
    if (
        getattr(request.state, "surface_role", None) == "mcp"
        and settings.mcp_origin_base_url
    ):
        # The request came in on the per-product MCP origin. Unlabelled
        # requests — the legacy transport on the API host — get the sidecar's
        # primary resource, exactly as before the origin existed.
        headers[MCP_ORIGIN_HEADER] = settings.mcp_origin_base_url
    if settings.auth_mode == "local":
        # A self-hosted install (§4.2 as amended): the sidecar builds its RFC
        # 9728 resource and its `WWW-Authenticate` challenge URL from the Host
        # it sees, and without these two headers it sees its own loopback
        # address — every client was told `http://127.0.0.1:8081/mcp`, an
        # address on the CLIENT's machine. The Host is what the client
        # actually reached; the proto is this edge's own scheme unless a TLS
        # terminator in front says otherwise. Hosted deployments never take
        # this branch: their sidecar is labelled with the configured origin
        # above, and `Host` stays out of the forwarded set deliberately.
        host = request.headers.get("host", "").strip()
        if host:
            headers["host"] = host
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        headers["x-forwarded-proto"] = forwarded_proto or request.url.scheme
    timeout = httpx.Timeout(connect=1.0, read=60.0, write=5.0, pool=1.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client, client.stream(
            request.method,
            _target_url(request, upstream_path),
            headers=headers,
            content=body,
        ) as upstream:
            response_body = await _response_body(upstream)
            response_headers = {
                name: value
                for name, value in upstream.headers.items()
                if name.lower() in _FORWARDED_RESPONSE_HEADERS
            }
            # The edge owns caching policy for this seam, so the sidecar's
            # `Cache-Control` is deliberately NOT in the forwarded set. It used
            # to be, with a `setdefault("Cache-Control", …)` after it — but
            # httpx lowercases header names, so the default never matched the
            # forwarded `cache-control` and the response left with two of them
            # (`public, max-age=300` beside `no-store` on the discovery
            # document; `no-store` twice on a 401).
            response_headers["Cache-Control"] = "no-store"
            return Response(
                content=response_body,
                status_code=upstream.status_code,
                headers=response_headers,
            )
    except ResponseBodyTooLargeError:
        log.warning("mcp sidecar response exceeded the edge buffer limit")
        return _error(502, "MCP_BAD_GATEWAY", "MCP service returned an oversized response")
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        # Do not log the URL or headers: the request contains a bearer token
        # and a client-controlled request id. The exception class is enough
        # to diagnose a dead sidecar without retaining either value.
        log.warning("mcp sidecar unavailable: %s", type(exc).__name__)
        return _error(503, "MCP_UNAVAILABLE", "MCP service unavailable", retry_after="5")
