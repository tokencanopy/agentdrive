"""The public read surface.

Server-rendered because unfurl bots do not run JavaScript: a share link pasted
into Slack must carry its title in the *first* response, so a client-side
render would unfurl as a blank page forever.

Three rules govern everything in here.

1. **Anti-enumeration.** An unknown key, a revoked key, an expired key and a
   key whose target was deleted all produce the same response — same status,
   same headers, same bytes. `resolve_secret` already collapses the four into
   `None`; this module must not re-introduce the distinction by, say, telling
   a browser "revoked" and a program "not found".

2. **The share key is a credential.** It rides in the URL path, so it must not
   be echoed anywhere the response can carry it onward: not into `og:url`, not
   into a canonical link, not into a `Location`, not into the page text.
   `Referrer-Policy: no-referrer` covers the other leak — the referer header
   would otherwise hand the key to every link the reader clicks. (Log
   redaction lives in `observability/middleware.py`.)

3. **Artifact bytes are untrusted and render on our own origin.** The renderer
   escapes rather than passes through HTML; the CSP below is the second line,
   so a hole in the escaping is not automatically stored XSS.

Content negotiation: a document navigation (`text/html`), a bare `*/*` and
an absent `Accept` get a page. A specific non-HTML request gets bytes on the
renderer (and in the direct-renderer rollback); on the trusted share host it
is authorized first, then moved to that same path on the configured renderer
origin with a method-preserving redirect.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from limits import RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from starlette.concurrency import iterate_in_threadpool

from .. import storage
from ..api.v0_errors import V0ApiError
from ..api.v0_rate_limit import enforce_v0_rate_limit
from ..config import settings
from ..core import public_reads, urls, v0_shares
from ..core.kinds import kind_for, safe_content_type
from ..core.usage.gate import usage_gate
from ..core.usage.meter import LimitDecision, UsageCharge, UsageReservation
from ..core.usage.models import EffectiveLimit, Metric, Period, ScopeType
from ..core.usage.policy import default_drive_limits
from ..db import conn
from ..rendering import render as render_module
from ..rendering.render import render_body
from ..rendering.safety import DeliveryClass, serve_bytes
from .page import (
    DOWNLOAD_QUERY,
    human_size,
    render_folder_page,
    render_not_found,
    render_page,
    render_shell,
    render_shell_not_found,
)

log = logging.getLogger(__name__)

# No router-level tags: FastAPI APPENDS route tags to the router's rather
# than replacing them, and the one documented route below must keep exactly
# the tag it was published under.
#
# The rate limit is the same dependency every `/v0` router carries, on the
# same budget. It matters more here than there: this is the only fully
# anonymous surface, and it does the most expensive work per request —
# markdown and Pygments over up to 2 MiB of attacker-authored content, plus
# recursive grant resolution — behind a URL an attacker can publish once and
# then hammer. `_principal_key` falls back to IP buckets when there is no
# bearer token, which is every request here, and the container runs with
# `--proxy-headers` so that IP is the client's rather than the load
# balancer's. Killable globally via `v0_rate_limit_enabled`.
router = APIRouter(dependencies=[Depends(enforce_v0_rate_limit)])

_PUBLIC_PREFILTER_ITEM = RateLimitItemPerMinute(120)
_PUBLIC_PREFILTER_STORAGE = MemoryStorage()
_PUBLIC_PREFILTER = FixedWindowRateLimiter(_PUBLIC_PREFILTER_STORAGE)


def reset_public_prefilter() -> None:
    """Drop process-local public buckets for test isolation."""
    _PUBLIC_PREFILTER_STORAGE.reset()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _enforce_public_prefilter(request: Request) -> None:
    if not _PUBLIC_PREFILTER.hit(_PUBLIC_PREFILTER_ITEM, _client_ip(request), cost=1):
        raise V0ApiError(
            429,
            "RATE_LIMITED",
            "rate limit exceeded; retry after the window resets",
            headers={"Retry-After": "60"},
        )


def _retry_after(decision: LimitDecision) -> str:
    if decision.reset_at is None:
        return "60"
    return str(max(1, math.ceil((decision.reset_at - datetime.now(UTC)).total_seconds())))


def _public_limit_response(
    request: Request, decision: LimitDecision, *, bandwidth: bool = False
) -> Response:
    code = "BANDWIDTH_LIMIT_EXCEEDED" if bandwidth else "RATE_LIMITED"
    message = (
        "This public link has reached its download limit. Please try again later."
        if bandwidth
        else "This public link is receiving too many requests. Please try again later."
    )
    headers = {
        "Cache-Control": "private, no-store",
        "Retry-After": _retry_after(decision),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if _wants_html(request):
        return HTMLResponse(
            "<!doctype html><html><head><meta charset=utf-8>"
            f"<title>AgentDrive</title></head><body><main><h1>{message}</h1>"
            "<p>Need higher limits? Contact the person who shared this link.</p>"
            "</main></body></html>",
            status_code=429,
            headers=headers,
        )
    raise V0ApiError(429, code, message, headers=headers)


async def _resolve_metered_share(
    request: Request, share_key: str, *, prefiltered: bool = False
) -> tuple[dict[str, Any] | None, Response | None]:
    if not prefiltered:
        await _enforce_public_prefilter(request)
    async with conn() as c:
        target = await v0_shares.resolve_secret(c, secret=share_key)
        if target is None or settings.public_usage_limit_mode == "off":
            return target, None
        ip_hmac = hmac.new(
            settings.usage_dimension_hmac_secret.get_secret_value().encode(),
            _client_ip(request).encode(),
            hashlib.sha256,
        ).hexdigest()
        share_id = target["share_id"]
        decision = await usage_gate.meter.charge(
            c,
            UsageCharge(
                operation_key=f"public-request:{secrets.token_urlsafe(18)}",
                metric=Metric.REQUESTS,
                amount=1,
                enforce=settings.public_usage_limit_mode == "enforce",
                limits=(
                    EffectiveLimit(
                        Metric.REQUESTS,
                        ScopeType.SHARE_IP,
                        f"{share_id}:{ip_hmac}",
                        Period.TEN_SECONDS,
                        20,
                    ),
                    EffectiveLimit(
                        Metric.REQUESTS,
                        ScopeType.SHARE_IP,
                        f"{share_id}:{ip_hmac}",
                        Period.MINUTE,
                        60,
                    ),
                    EffectiveLimit(
                        Metric.REQUESTS,
                        ScopeType.SHARE,
                        share_id,
                        Period.MINUTE,
                        600,
                    ),
                ),
            ),
        )
    if not decision.allowed and settings.public_usage_limit_mode == "enforce":
        return target, _public_limit_response(request, decision)
    return target, None

# This surface is HTML for browsers on the share host, not part of the
# agent-facing `/v0` contract the OpenAPI document describes, so its routes
# are hidden from the spec — spread `**_UNDOCUMENTED` onto each one.
#
# It is set per route rather than on the router, because exactly one route
# opts out of the hiding: `GET /s/{share_key}` is a published operation
# (`shares_redeem`) that API clients still call and that still serves them
# bytes. Omitting it would leave the spec silently under-describing a live
# route, and the compatibility gate is right to read that as a breaking
# removal. A router-level flag would win over the route-level one and make
# that exception impossible to express.
_UNDOCUMENTED = {"include_in_schema": False}


# `default-src 'none'` is what blocks script; `img-src` stays permissive
# because markdown legitimately embeds remote images. Consequence, already
# honoured by the page shell: no inline <style> and no inline <script>.
# `script-src 'self'` — NOT `'unsafe-inline'`, and that distinction is the
# whole security argument. Escaping remains the primary defence; this policy
# is the backstop, and `'self'` keeps it load-bearing: an attacker who slipped
# a `<script>` past the renderer still could not run it, because they cannot
# write a file into our origin to point it at. `'unsafe-inline'` would throw
# that away — it permits exactly the injected-inline-script case the backstop
# exists to catch — so the viewer's script is an external file and the page
# carries no inline handlers.
#
# `connect-src 'self'` and `worker-src 'self' blob:` are for pdf.js: it fetches
# the document itself rather than letting an element load it, and it decodes
# in a Web Worker. Both stay same-origin. `blob:` is needed because pdf.js
# bootstraps its worker from a blob URL, which is a same-origin construct the
# page itself creates — it cannot be pointed at anything a third party wrote.
#
# Still no `object-src`: the native `<embed>` it would serve is not used.
# Legacy rejected it on purpose (pdf.js gives selectable text, find-in-page and
# clickable links; `<embed>` gives an opaque box), and it was independently
# confirmed not to render here even with the CSP removed. `frame-src 'self'`
# exists for exactly one frame, the diagram engine's — see `_renderer_csp`.
def _renderer_frame_ancestor() -> str:
    """The one trusted parent of the isolated public renderer.

    In the split deployment this is the exact validated share origin. The
    no-content-origin state is the direct-renderer rollback, where no framing
    is needed and the historical fail-closed policy remains.
    """
    if settings.public_content_base_url:
        return settings.share_base_url
    return "'none'"


def _renderer_csp() -> str:
    return (
        "default-src 'none'; script-src 'self'; connect-src 'self'; "
        "worker-src 'self' blob:; img-src 'self' data: https:; "
        # `media-src 'self'` — same-origin only, and narrower than `img-src`
        # on purpose. A published document's remote IMAGES were already
        # permissive here because markdown legitimately embeds them; remote
        # media has no such established use and a `<video>` is a far larger
        # request to make on a reader's behalf. Inline video on this surface
        # is the artifact's own bytes at `content`, which is same-origin.
        "media-src 'self'; "
        "style-src 'self'; font-src 'self'; "
        # `frame-src 'self'` is for the diagram engine and nothing else: it
        # runs in `diagram-frame.html`, a same-origin document with its own
        # policy, so this page can keep `style-src 'self'` while mermaid gets
        # the inline styles it needs to measure text. Only our scripts can
        # create a frame — the sanitiser drops `<iframe>` and markdown never
        # emits one — so the grant admits no artifact-authored frame.
        "frame-src 'self'; "
        "base-uri 'none'; form-action 'none'; "
        f"frame-ancestors {_renderer_frame_ancestor()}"
    )


# The `/content` sub-routes serve agent-authored bytes verbatim from our own
# origin, so they get a STRICTER policy than the page, not the same one.
#
# The page needs `script-src 'self'` for the theme toggle; raw bytes never do,
# so this grants neither script nor object. `sandbox` would already defang
# them by putting any rendered document in an opaque origin where `'self'`
# matches nothing — but not granting the capability is stronger than granting
# it and relying on a second directive to take it back.
def _content_csp() -> str:
    return (
        "default-src 'none'; script-src 'none'; object-src 'none'; "
        "img-src 'self' data: https:; style-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'none'; "
        f"frame-ancestors {_renderer_frame_ancestor()}; sandbox"
    )


# The active-type table, filename sanitizer, and byte-serving path now live
# in `agentdrive.rendering.safety`, shared with the private viewer adapter.
# This module keeps only the POLICY: which content CSP rides on the bytes.

# `/s/` only. Revocation and caching are in tension and revocation wins: a
# cached page for a revoked link is a leak, and `no-store` additionally leaves
# no copy on the recipient's disk. `/a/` and `/v/` get their own policies.
_NO_STORE = "private, no-store"

# `/a/` — a permalink onto a MOVING head. Public caching is the point (this is
# the URL that gets pasted around), but every hit must revalidate: the
# publisher can replace the head, and an immutably-cached page would keep
# serving the old document for as long as the cache holds it, with no way to
# correct it.
_REVALIDATE = "public, max-age=60, must-revalidate"

# `/v/` names frozen bytes, but its public grant remains revocable. A cache may
# store the response, but must resolve the live authorization decision before
# serving it again.
_VERSION_REVALIDATE = "public, max-age=0, must-revalidate"

# Trusted permalink-shell cache policies. These are intentionally separate
# from the renderer dictionaries above: Task 5 selects one only after the
# existing authorization path resolves, and the shell must never become more
# cacheable than that decision. Keeping the values closed also prevents a
# caller from silently inventing a cache policy at the security seam.
SHELL_CACHE_NO_STORE = "private, no-store"
SHELL_CACHE_REVALIDATE = "public, max-age=60, must-revalidate"
SHELL_CACHE_VERSION_REVALIDATE = "public, max-age=0, must-revalidate"
_SHELL_CACHE_POLICIES = frozenset(
    (SHELL_CACHE_NO_STORE, SHELL_CACHE_REVALIDATE, SHELL_CACHE_VERSION_REVALIDATE)
)


def shell_response_headers(*, cache_control: str) -> dict[str, str]:
    """Security and cache policy for the trusted metadata shell.

    The frame origin comes only from the validated process configuration,
    never from ``Host``, ``Origin``, ``Referer``, or another request value.
    An empty origin is the documented direct-renderer rollback state, where
    no split shell is legal, so this helper fails closed instead of emitting
    a permissive or malformed CSP.
    """
    if cache_control not in _SHELL_CACHE_POLICIES:
        raise ValueError("unsupported trusted-shell cache policy")
    public_origin = settings.public_content_base_url
    if not public_origin:
        raise RuntimeError("PUBLIC_CONTENT_BASE_URL is required for the trusted shell")

    # `script-src 'self'` — and NOT `'unsafe-inline'`, for the same reason the
    # renderer refuses it. The grant is narrow by construction: this document
    # contains no artifact-authored markup at all (that is the property the
    # split exists to hold), every value in it is autoescaped, and the one file
    # it can therefore load is our own. It buys the clipboard, the theme
    # control and sizing the frame to its document — enhancement on top of a
    # page that is already complete and useful without it.
    csp = (
        f"default-src 'none'; frame-src {public_origin}; style-src 'self'; "
        "script-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return {
        "Cache-Control": cache_control,
        "Content-Security-Policy": csp,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def renderer_response_headers(*, cache_control: str) -> dict[str, str]:
    """Security and cache policy for a public renderer response."""
    if cache_control not in _SHELL_CACHE_POLICIES:
        raise ValueError("unsupported public-renderer cache policy")
    return {
        "Cache-Control": cache_control,
        "Content-Security-Policy": _renderer_csp(),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def _renders_as_page(target: dict[str, Any]) -> bool:
    """Would this artifact render as a `page`, judged from the descriptor alone?

    Deliberately decidable without the bytes, because the trusted metadata
    shell has none and must never fetch any — that is the property
    `_render_artifact_shell` exists to hold. It therefore over-applies
    slightly: an oversized HTML artifact renders as a download card, and its
    shell still says `noindex`. Over-applying a robots directive to a
    metadata page costs nothing; under-applying it leaves the impersonation
    §6.3 warns about in a search result.
    """
    if not settings.static_html_rendering_enabled:
        return False
    base = (target.get("content_type") or "").split(";", 1)[0].strip().lower()
    return base == "text/html"


def _surface_role(request: Request) -> str | None:
    """Read only the middleware-owned role; never infer one from headers."""
    role = getattr(request.state, "surface_role", None)
    if role in (None, "share", "public-renderer"):
        return role
    return "invalid"


def _uses_trusted_shell(request: Request) -> bool:
    return _surface_role(request) == "share" and bool(settings.public_content_base_url)


def _wants_embed(request: Request) -> bool:
    """Is the trusted shell's own frame asking for this page?

    Presentation and only presentation: it suppresses the renderer's chrome
    because the shell above the frame already draws it. It gates no content,
    changes no authorization and reveals nothing — which is why a query
    parameter is the entire mechanism, and why a caller who invents it by hand
    gets a page without a header rather than a page they should not have.

    Honoured only where a frame can exist. In the direct-renderer rollback the
    share host serves these routes itself with no shell and no iframe, so the
    flag is refused there and the page keeps its own header.
    """
    if request.query_params.get("embed") != "1":
        return False
    return _surface_role(request) in (None, "public-renderer")


def _renderer_route_allowed(request: Request) -> bool:
    role = _surface_role(request)
    return role in (None, "public-renderer") or (
        role == "share" and not settings.public_content_base_url
    )


def _page_headers(request: Request, *, cache_control: str) -> dict[str, str]:
    if _uses_trusted_shell(request):
        return shell_response_headers(cache_control=cache_control)
    return renderer_response_headers(cache_control=cache_control)


def _renderer_redirect(
    *segments: str,
    cache_control: str,
    trailing_slash: bool = False,
    query: str = "",
) -> Response:
    """Method-preserving redirect to a config-derived renderer URL.

    `query` is one of this module's literal constants, never a caller string:
    the share host must be able to forward a download request without becoming
    a way to attach arbitrary parameters to a renderer URL.
    """
    location = urls.renderer_url(*segments, trailing_slash=trailing_slash)
    if query:
        location = f"{location}?{query}"
    return Response(
        status_code=308,
        headers={
            **shell_response_headers(cache_control=cache_control),
            "Location": location,
        },
    )


def _wants_html(request: Request) -> bool:
    """Does this caller want the page, or the bytes?

    Deliberately generous on the HTML side: a browser navigation, curl's bare
    `*/*` and a missing header all mean "a person is looking at this". A
    specific non-HTML type (`application/json`, `image/png`) means a program
    that wants the object, which is the pre-existing behaviour.
    """
    accept = request.headers.get("accept", "")
    return "text/html" in accept or accept.strip() in ("", "*/*")


def _canonical_redirect(path: str, headers: dict[str, str]) -> Response:
    """308 to `path`, with every id segment percent-encoded.

    The ids in these URLs come straight off the wire. A path param can carry
    decoded control characters — `%0d%0a` in the request line arrives here as
    a real CRLF — and interpolating that into `Location` is response
    splitting. uvicorn happens to refuse the malformed header, but it does so
    by raising, which drops the connection and returns zero bytes: an
    unauthenticated caller can turn any of these routes into an error with a
    crafted link, and the empty reply is a response shape none of the uniform
    404 guarantees cover.

    Encoding fixes it at the source rather than relying on the server to
    reject it. `safe=""` also encodes `/`, so a segment cannot grow new path
    structure, and the redirect still resolves: the router percent-decodes it
    back on the follow-up request.
    """
    return Response(status_code=308, headers={**headers, "Location": path})


def _not_found(*, wants_html: bool, request: Request | None = None) -> Response:
    """The uniform refusal. One shape per Accept, no reason ever given.

    The JSON arm raises rather than returns: `SHARE_NOT_FOUND` in the v0
    envelope is the shipped contract for byte clients and is rendered by the
    central `V0ApiError` handler.
    """
    if not wants_html:
        raise V0ApiError(404, "SHARE_NOT_FOUND", "invalid or expired link")
    if request is not None and _uses_trusted_shell(request):
        return HTMLResponse(
            render_shell_not_found(),
            status_code=404,
            headers=shell_response_headers(cache_control=_NO_STORE),
        )
    return HTMLResponse(
        render_not_found(),
        status_code=404,
        headers=renderer_response_headers(cache_control=_NO_STORE),
    )


# Renders above the text ceiling parse whole containers or walk a large object
# through ranged reads, in a worker thread. A per-format ceiling bounds ONE of
# them; this bounds how many run at once, because ten simultaneous 30 MB deck
# parses on a 1 GiB instance is the failure a ceiling alone does not prevent.
# Small renders never wait on it.
_LARGE_RENDERS = asyncio.Semaphore(3)


async def render_in_thread(
    target: dict[str, Any], data: bytes, source: storage.RangedSource | None, **kwargs: Any
):
    """`render_body` off the event loop, gated when the artifact is large."""
    if target["size_bytes"] > render_module.MAX_RENDER_BYTES:
        async with _LARGE_RENDERS:
            return await asyncio.to_thread(
                render_body, data, target["content_type"], target["name"],
                size_bytes=target["size_bytes"], source=source, **kwargs,
            )
    return await asyncio.to_thread(
        render_body, data, target["content_type"], target["name"],
        size_bytes=target["size_bytes"], source=source, **kwargs,
    )


async def preview_input(target: dict[str, Any]) -> tuple[bytes, storage.RangedSource | None]:
    """What the renderer gets: the whole object when it fits, ranged reads
    when it is oversized but previewable from a fraction, and empty bytes
    plus the true size otherwise — the renderer cards on the size.

    Shared by every render route on both surfaces, so the fetch rule cannot
    drift between them. The ranged reads run inside `render_body`, which the
    caller therefore runs in a worker thread: blocking GETs must not sit on
    the event loop.
    """
    size = target["size_bytes"]
    if size <= render_module.render_ceiling(target["content_type"], target["name"]):
        data = await storage.get(
            target["storage_object"],
            bucket=target.get("storage_bucket"),
            generation=target.get("storage_generation"),
        )
        return data, None
    if render_module.previews_when_large(target["content_type"], target["name"]):
        return b"", storage.RangedSource(
            target["storage_object"],
            size,
            bucket=target.get("storage_bucket"),
            generation=target.get("storage_generation"),
        )
    return b"", None


def _describe(*, path: str, kind: str, size_bytes: int, updated_at: datetime | None) -> str:
    """The `og:description` line: path, kind, size, date — metadata only.

    The unfurl says WHAT this is, never what it says. A content snippet in a
    chat preview is a disclosure decision nobody made (spec §3.3).
    """
    parts = [path, kind, human_size(size_bytes)]
    if updated_at:
        parts.append(f"updated {updated_at:%Y-%m-%d}")
    return " · ".join(parts)


def _meta_line(
    *,
    path: str,
    content_type: str = "",
    size_bytes: int | None = None,
    updated_at: datetime | None = None,
) -> str:
    """The header's second line — and the whole of the provenance strip.

    It absorbed the `type / size / updated / canonical` block that used to sit
    under the document. Three of those four are here; `canonical` is the
    `<link rel="canonical">` in the head and the address bar the reader is
    already looking at, which is where a machine reads it from anyway. Nothing
    is now stated in two places on this page.

    Deliberately NOT `_describe`. That string is the unfurl description and
    names the coarse KIND, because a chat preview has no room for a chip; the
    header draws the chip itself, so this line spends the space on the exact
    media type instead.

    `content_type` is stored, attacker-authored text: `safe_content_type`
    sanitizes it before it becomes a header elsewhere, and it is the same
    value that should reach a reader — the base type, without parameters.
    """
    parts = [p for p in (path,) if p]
    if content_type:
        base = safe_content_type(content_type).split(";", 1)[0].strip()
        if base:
            parts.append(base)
    if size_bytes is not None:
        parts.append(human_size(size_bytes))
    if updated_at:
        parts.append(f"updated {updated_at:%Y-%m-%d}")
    return " · ".join(parts)


def _describe_folder(*, path: str, count: int) -> str:
    """The `og:description` for a folder: where it is and how big it is.

    Deliberately NOT a sample of child names. An unfurl is republished into
    whatever channel the link was pasted in, and the names in a folder are the
    disclosure the listing itself is gated on — a preview that leaked three of
    them would route around the gate for anyone who merely saw the link.
    """
    parts = [p for p in (path, f"{count} item{'' if count == 1 else 's'}") if p]
    return " · ".join(parts)


def _console_artifact_url(target: dict[str, Any]) -> str:
    """Build the owner/member navigation target when the descriptor has ids."""
    drive_id = target.get("drive_id")
    artifact_id = target.get("artifact_id")
    if not drive_id or not artifact_id:
        return ""
    return urls.console_artifact_url(drive_id, artifact_id)


async def _render_artifact(
    target: dict[str, Any],
    *,
    canonical_url: str,
    headers: dict[str, str],
    embed: bool = False,
) -> Response:
    """The document page for an already-authorized artifact descriptor.

    `canonical_url` and `headers` are the two things the three page routes
    disagree about and nothing else is: `/s/` has no non-secret URL to
    advertise and must not be cached, `/a/` advertises its permalink and
    revalidates, and `/v/` advertises its frozen version permalink while
    revalidating its live public grant. Everything below this line is
    identical for all three, which is why they
    share one function rather than three near-copies.
    """
    # Decide BEFORE fetching. The descriptor already carries size_bytes, so an
    # object too large to render never gets downloaded — the old code pulled
    # up to the inline cap into memory and then threw it away.
    data, source = await preview_input(target)
    # size_bytes, not len(data): the fetch above is skipped for oversized
    # objects, and without the real size the renderer would read the empty
    # placeholder as a zero-length document and render a blank page.
    body = await render_in_thread(
        target, data, source,
        # This surface is a top-level page, not a sandboxed frame, and
        # `pdf-visitor.js` wires the button: `view.print()` opens `content`
        # in a new tab, so the browser prints the PDF itself rather than the
        # page chrome around it.
        print_button=True,
    )
    kind = kind_for(target["content_type"], target["name"])
    path = target.get("path") or target["name"]
    if body.mode == "page":
        # `noindex` on THIS surface only, and only for a rendered page. The
        # artifact is already public, so there is nothing here to hide — what
        # a rendered agent-authored page adds is visual impersonation: it can
        # imitate a brand and instruct a stranger out of band. Being
        # impersonated is bad; being impersonated in a search result is worse.
        # (The band that says the same thing to the reader is drawn by the
        # template, which is what keeps it off the private surface.)
        headers = {**headers, "X-Robots-Tag": "noindex"}
    html = render_page(
        body=body,
        title=body.title or target["name"],
        kind=kind,
        size_bytes=target["size_bytes"],
        updated_at=target.get("updated_at"),
        canonical_url=canonical_url,
        path=path,
        name=target["name"],
        content_type=target["content_type"],
        description=_describe(
            path=path,
            kind=kind,
            size_bytes=target["size_bytes"],
            updated_at=target.get("updated_at"),
        ),
        embed=embed,
        console_url=_console_artifact_url(target),
    )
    return HTMLResponse(html, headers=headers)


def _render_artifact_shell(
    target: dict[str, Any],
    *,
    canonical_url: str,
    renderer_url: str,
    download_url: str,
    cache_control: str,
) -> Response:
    """Trusted metadata adapter for an already-authorized descriptor.

    Unlike ``_render_artifact``, this function has no storage call and no
    rendered-body parameter. Artifact-authored bytes therefore cannot enter
    the share-origin document even accidentally.
    """
    kind = kind_for(target["content_type"], target["name"])
    path = target.get("path") or target["name"]
    updated_at = target.get("updated_at")
    html = render_shell(
        title=target["name"],
        description=_describe(
            path=path,
            kind=kind,
            size_bytes=target["size_bytes"],
            updated_at=updated_at,
        ),
        canonical_url=canonical_url,
        renderer_url=renderer_url,
        download_url=download_url,
        console_url=_console_artifact_url(target),
        og_type="article",
        # Display metadata for the header the frame no longer draws. All of it
        # comes off the descriptor this function was already handed — there is
        # still no storage call and no rendered body on this path, which is the
        # property that keeps artifact bytes out of the share-origin document.
        name=target["name"],
        kind=kind,
        content_type=target["content_type"],
        allow_fullscreen=(
            kind == "video"
            and target["size_bytes"] <= render_module.MAX_INLINE_VIDEO_BYTES
        ),
        meta_line=_meta_line(
            path=path,
            content_type=target["content_type"],
            size_bytes=target["size_bytes"],
            updated_at=updated_at,
        ),
    )
    headers = shell_response_headers(cache_control=cache_control)
    if _renders_as_page(target):
        # The share host is the URL a stranger is actually SENT, so it is the
        # one a crawler fetches. Putting `noindex` only on the framed renderer
        # left the page people share indexable — and this shell's own title
        # and description come from an artifact name the author chose.
        headers["X-Robots-Tag"] = "noindex"
    return HTMLResponse(html, headers=headers)


async def _serve_bytes(
    request: Request,
    target: dict[str, Any],
    *,
    headers: dict[str, str],
    force_attachment: bool = False,
) -> Response:
    """This surface's byte path: the shared `serve_bytes` under the public
    content CSP. Kept as a local wrapper so the policy is applied in exactly
    one place per adapter rather than at every call site."""
    original_size = target["size_bytes"]
    start, end, range_headers, status_code = _selected_public_range(
        request.headers.get("range"), original_size
    )
    response_size = end - start
    operation_key = f"public-bytes:{secrets.token_urlsafe(18)}"
    reserved = False

    if settings.public_usage_limit_mode != "off":
        limits = [
            EffectiveLimit(
                Metric.PUBLIC_BYTES,
                ScopeType.WORKSPACE,
                target["workspace_id"],
                Period.DAY,
                settings.public_workspace_bytes_day,
            ),
            EffectiveLimit(
                Metric.PUBLIC_BYTES,
                ScopeType.DRIVE,
                target["drive_id"],
                Period.DAY,
                settings.public_workspace_bytes_day,
            ),
        ]
        share_id = target.get("share_id")
        if share_id:
            limits.append(
                EffectiveLimit(
                    Metric.PUBLIC_BYTES,
                    ScopeType.SHARE,
                    share_id,
                    Period.DAY,
                    target.get("daily_byte_limit")
                    or default_drive_limits().public_share_bytes_day,
                )
            )
        async with conn() as c:
            decision = await usage_gate.meter.reserve(
                c,
                UsageReservation(
                    operation_key=operation_key,
                    metric=Metric.PUBLIC_BYTES,
                    amount=response_size,
                    enforce=settings.public_usage_limit_mode == "enforce",
                    limits=tuple(limits),
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                    expiry_action="commit_reserved",
                ),
            )
        reserved = decision.allowed
        if not decision.allowed and settings.public_usage_limit_mode == "enforce":
            return _public_limit_response(request, decision, bandwidth=True)

    stream_kwargs = {
        "bucket": target.get("storage_bucket"),
        "generation": target.get("storage_generation"),
    }
    if request.headers.get("range") is not None:
        stream_kwargs.update({"start": start, "end": end})
    source = storage.stream(target["storage_object"], **stream_kwargs)

    async def metered_stream():
        yielded = 0
        try:
            if hasattr(source, "__aiter__"):
                async for chunk in source:
                    yielded += len(chunk)
                    yield chunk
            else:
                async for chunk in iterate_in_threadpool(source):
                    yielded += len(chunk)
                    yield chunk
        finally:
            if reserved:
                try:
                    async with conn() as c, c.transaction():
                        await usage_gate.meter.commit(
                            c,
                            operation_key=operation_key,
                            metric=Metric.PUBLIC_BYTES,
                            actual_amount=yielded,
                        )
                        updated = await c.execute(
                            "UPDATE drives SET retrieval_bytes=retrieval_bytes+$2, "
                            "updated_at=now() WHERE id=$1 AND workspace_id=$3",
                            target["drive_id"],
                            yielded,
                            target["workspace_id"],
                        )
                        if updated != "UPDATE 1":
                            raise RuntimeError(
                                "public download accounting target disappeared"
                            )
                except Exception:  # noqa: BLE001 - expiry commits conservatively
                    log.exception(
                        "at=usage_operation_finalize_failure metric=public_bytes"
                    )

    selected_target = {**target, "size_bytes": response_size}
    response = await serve_bytes(
        selected_target,
        headers=headers,
        content_csp=_content_csp(),
        force_attachment=force_attachment,
        delivery_class=DeliveryClass.PUBLIC_METERED,
        byte_stream=metered_stream(),
    )
    response.status_code = status_code
    response.headers.update(range_headers)
    return response


def _selected_public_range(
    value: str | None, size_bytes: int
) -> tuple[int, int, dict[str, str], int]:
    if value is None:
        return 0, size_bytes, {"Accept-Ranges": "bytes"}, 200
    if not value.startswith("bytes=") or "," in value or size_bytes <= 0:
        raise V0ApiError(
            416,
            "INVALID_ARGUMENT",
            "only one satisfiable byte range is supported",
            headers={"Content-Range": f"bytes */{size_bytes}"},
        )
    bounds = value.removeprefix("bytes=").split("-", 1)
    if len(bounds) != 2:
        raise V0ApiError(416, "INVALID_ARGUMENT", "invalid byte range")
    try:
        if not bounds[0]:
            length = int(bounds[1])
            if length <= 0:
                raise ValueError
            start = max(0, size_bytes - length)
            end = size_bytes
        else:
            start = int(bounds[0])
            end = min(size_bytes, int(bounds[1]) + 1) if bounds[1] else size_bytes
            if start < 0 or start >= size_bytes or end <= start:
                raise ValueError
    except ValueError as exc:
        raise V0ApiError(
            416,
            "INVALID_ARGUMENT",
            "byte range is not satisfiable",
            headers={"Content-Range": f"bytes */{size_bytes}"},
        ) from exc
    return (
        start,
        end,
        {
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end - 1}/{size_bytes}",
        },
        206,
    )


async def _authorized_artifact_page(
    request: Request,
    target: dict[str, Any],
    *,
    canonical_url: str,
    renderer_segments: tuple[str, ...],
    cache_control: str,
    include_etag: bool = True,
) -> Response:
    """Select one adapter for an already-authorized artifact descriptor."""
    if _uses_trusted_shell(request):
        return _render_artifact_shell(
            target,
            canonical_url=canonical_url,
            renderer_url=urls.renderer_url(
                *renderer_segments,
                trailing_slash=True,
            ),
            # The bytes behind this page, asked to arrive as a download. Same
            # route and same authorization as the inline `content` the page
            # already links; only the disposition differs.
            download_url=(
                f"{urls.renderer_url(*renderer_segments, 'content')}?{DOWNLOAD_QUERY}"
            ),
            cache_control=cache_control,
        )
    if not _renderer_route_allowed(request):
        return _not_found(wants_html=True, request=request)
    headers = renderer_response_headers(cache_control=cache_control)
    if include_etag:
        headers["ETag"] = f'"{target["etag"]}"'
    return await _render_artifact(
        target,
        canonical_url=canonical_url,
        headers=headers,
        embed=_wants_embed(request),
    )


def _wants_download(request: Request) -> bool:
    """Did the reader ask for these bytes as a file rather than a view?

    The shell's Download link, which crosses an origin and therefore cannot use
    the `download` attribute. Purely a disposition request: it authorizes
    nothing, reveals nothing, and can only make a response stricter — so an
    unrecognized value is simply not a download.
    """
    return request.query_params.get("download") == "1"


async def _authorized_artifact_bytes(
    request: Request,
    target: dict[str, Any],
    *,
    renderer_segments: tuple[str, ...],
    cache_control: str,
    refusal_wants_html: bool,
    renderer_trailing_slash: bool = False,
) -> Response:
    """Select redirect or byte adapter after descriptor authorization."""
    download = _wants_download(request)
    if _uses_trusted_shell(request):
        return _renderer_redirect(
            *renderer_segments,
            cache_control=cache_control,
            trailing_slash=renderer_trailing_slash,
            # Carried across, or a share-host download would arrive on the
            # renderer stripped of the one thing it asked for and open in the
            # tab instead of saving.
            query=DOWNLOAD_QUERY if download else "",
        )
    if not _renderer_route_allowed(request):
        return _not_found(wants_html=refusal_wants_html, request=request)
    return await _serve_bytes(
        request,
        target,
        headers={
            **renderer_response_headers(cache_control=cache_control),
            "ETag": f'"{target["etag"]}"',
        },
        force_attachment=download,
    )


@router.get("/s/{share_key}/content", **_UNDOCUMENTED)
async def share_content(share_key: str, request: Request) -> Response:
    """The raw bytes behind a share page — what `<img src="content">` and the
    download card point at.

    Authorization is the parent page's, verbatim: the same `resolve_secret`,
    the same collapse of unknown/revoked/expired/deleted into one `None`, and
    the same `_not_found` per Accept. A sub-route that resolved more
    permissively than the page it hangs off would be revocation that does not
    revoke — the page 404s while the bytes keep flowing.

    A folder key resolves, but has no bytes; that is the uniform 404 too,
    never a hint that the key itself was good.
    """
    wants_html = _wants_html(request)

    target, limit_response = await _resolve_metered_share(request, share_key)
    if limit_response is not None:
        return limit_response
    if target is None or target["kind"] != "artifact":
        return _not_found(wants_html=wants_html, request=request)

    return await _authorized_artifact_bytes(
        request,
        target,
        renderer_segments=("s", share_key, "content"),
        cache_control=_NO_STORE,
        refusal_wants_html=wants_html,
    )


_STATIC_DIR = Path(__file__).parent / "static"
_APP_STATIC_DIR = Path(__file__).parent.parent / "static"

# An explicit allowlist, URL path -> (file, media type), rather than a
# StaticFiles mount. Two reasons, both learned the hard way here. A Mount
# resolves against `root_path`, so under MOUNT_PREFIX it answers only at
# `/drive/...` while the share host has no such prefix — that is what left
# every public page unstyled in staging. And a dict lookup cannot be walked
# out of, so an anonymous route gains no path-traversal surface.
#
# `pdfview.js` and the vendored pdf.js are served from the APP static dir
# unchanged — the same files the LaTeX preview uses. Reusing rather than
# copying keeps one copy of a 1.9 MB dependency and one implementation of the
# viewer, so a fix in either reaches both surfaces.
_PUBLIC_ASSETS: dict[str, tuple[Path, str]] = {
    "reading.js": (_STATIC_DIR / "reading.js", "text/javascript"),
    "viewer.css": (_STATIC_DIR / "viewer.css", "text/css"),
    "viewer.js": (_STATIC_DIR / "viewer.js", "text/javascript"),
    "pdf-visitor.js": (_STATIC_DIR / "pdf-visitor.js", "text/javascript"),
    "pdfview.js": (_APP_STATIC_DIR / "pdfview.js", "text/javascript"),
    # Diagrams, on the same terms as pdf.js: the engine is vendored, served
    # from the app static dir so both surfaces run one copy, and loaded only
    # by a page whose document actually carries a ```mermaid fence.
    "diagram-visitor.js": (_STATIC_DIR / "diagram-visitor.js", "text/javascript"),
    "diagrams.js": (_STATIC_DIR / "diagrams.js", "text/javascript"),
    "diagram-frame.html": (_STATIC_DIR / "diagram-frame.html", "text/html"),
    "diagram-frame.js": (_STATIC_DIR / "diagram-frame.js", "text/javascript"),
    "vendor/mermaid/mermaid.min.js": (
        _APP_STATIC_DIR / "vendor" / "mermaid" / "mermaid.min.js",
        "text/javascript",
    ),
    "vendor/pdfjs/pdf.min.mjs": (
        _APP_STATIC_DIR / "vendor" / "pdfjs" / "pdf.min.mjs",
        "text/javascript",
    ),
    "vendor/pdfjs/pdf.worker.min.mjs": (
        _APP_STATIC_DIR / "vendor" / "pdfjs" / "pdf.worker.min.mjs",
        "text/javascript",
    ),
    "vendor/pdfjs/pdf_viewer.mjs": (
        _APP_STATIC_DIR / "vendor" / "pdfjs" / "pdf_viewer.mjs",
        "text/javascript",
    ),
    "vendor/pdfjs/pdf_viewer.css": (
        _APP_STATIC_DIR / "vendor" / "pdfjs" / "pdf_viewer.css",
        "text/css",
    ),
    # pdf_viewer.css's page-loading spinner — the only image that css pulls
    # in our configuration (the rest are annotation-editor chrome the
    # viewers never enable). Without it every PDF render logs a 404.
    "vendor/pdfjs/images/loading-icon.gif": (
        _APP_STATIC_DIR / "vendor" / "pdfjs" / "images" / "loading-icon.gif",
        "image/gif",
    ),
}

_SHARE_ASSETS: dict[str, tuple[Path, str]] = {
    "shell.css": (_STATIC_DIR / "shell.css", "text/css"),
    "shell.js": (_STATIC_DIR / "shell.js", "text/javascript"),
}

_ASSET_HEADERS = {
    "Cache-Control": "public, max-age=300, must-revalidate",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

def diagram_frame_csp(page_ancestors: str) -> str:
    """The diagram engine's own document — the ONE asset that is a page.

    It gets the inline styles mermaid needs for text measurement and nothing
    else: scripts only from this origin (the vendored engine and its message
    handler), no connections, no fonts, no navigation. Nothing
    artifact-authored ever loads in it; the parent posts diagram source text
    in and gets an SVG string back.

    `frame-ancestors` names `'self'` — the page that creates the frame — AND
    that page's own permitted ancestors, because the browser checks the whole
    chain, not the immediate parent: the renderer is framed by the share
    shell and the private viewer by the console, so `'self'` alone would
    refuse the engine everywhere it is actually used. `page_ancestors` is the
    page policy's own value; `'none'` (the unframed rollback) contributes
    nothing.
    """
    ancestors = "'self'"
    if page_ancestors and page_ancestors != "'none'":
        ancestors += " " + page_ancestors
    return (
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
        f"img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors {ancestors}"
    )


def asset_headers(media_type: str) -> dict[str, str]:
    headers = dict(_ASSET_HEADERS)
    if media_type == "text/html":
        headers["Content-Security-Policy"] = diagram_frame_csp(_renderer_frame_ancestor())
    return headers


@router.get("/public-static/{asset:path}", **_UNDOCUMENTED)
async def public_asset(asset: str, request: Request) -> Response:
    """The public surface's static assets, from a fixed allowlist.

    A route rather than a mount so it answers at the root in both mount
    modes, and an allowlist rather than a directory so an anonymous caller
    cannot walk out of it.

    Load-bearing, not decorative: the CSP is `style-src 'self'` and
    `script-src 'self'` with nothing inline, so if this 404s the page renders
    unstyled and the PDF viewer never boots.
    """
    if not _renderer_route_allowed(request):
        return _not_found(wants_html=True, request=request)
    entry = _PUBLIC_ASSETS.get(asset)
    if entry is None:
        return _not_found(wants_html=True, request=request)
    path, media_type = entry
    return FileResponse(path, media_type=media_type, headers=asset_headers(media_type))


@router.get("/share-static/{asset:path}", **_UNDOCUMENTED)
async def share_asset(asset: str, request: Request) -> Response:
    """Trusted-shell assets from a fixed allowlist on the share role only."""
    if _surface_role(request) not in (None, "share"):
        return _not_found(wants_html=True, request=request)
    entry = _SHARE_ASSETS.get(asset)
    if entry is None:
        return _not_found(wants_html=True, request=request)
    path, media_type = entry
    return FileResponse(path, media_type=media_type, headers=dict(_ASSET_HEADERS))


@router.get(
    "/s/{share_key}",
    operation_id="shares_redeem",
    include_in_schema=True,
    # Keeps the published operation identical for a spec consumer: same id,
    # same tag, same path. Only the description changes. Retagging it would
    # move it to a different section of every generated client and doc, which
    # the compatibility gate correctly reports as a breaking change.
    tags=["shares-redemption"],
    summary="Redeem Share",
)
async def redeem_share_bare(share_key: str, request: Request) -> Response:
    """The un-slashed form. Browsers are canonicalized onto `/s/{key}/`.

    This keeps the published `shares_redeem` operation id because it remains
    the same redemption operation: same URL, JSON `Accept`, byte/JSON result,
    and uniform 404. In a split deployment the share host authorizes a
    specific non-HTML request and then 308s it to this same path on the
    configured public renderer; the renderer (or direct-renderer rollback)
    returns the existing result. Dropping the route from the spec would have
    described a live operation as gone.

    The page links its own sub-resources relatively (`content`), and relative
    resolution replaces the last path segment: from `/s/KEY` that reaches
    `/s/content`, from `/s/KEY/` it reaches `/s/KEY/content`. The trailing
    slash is what makes an image on a share page load at all. Links already in
    the wild have no slash, so this redirect is how they keep working.

    It is unconditional and runs before any lookup — redirecting only for keys
    that resolve would turn the status code into an existence oracle and undo
    the anti-enumeration property the rest of this module maintains.

    HTML navigation is canonicalized to the trailing-slash form. Non-HTML
    clients are answered in place by the renderer/direct rollback, or moved
    to the renderer only after the credential resolves on the share host.
    """
    await _enforce_public_prefilter(request)
    if _wants_html(request):
        # The key rides in this Location, back to the client that just sent
        # it in the request line. That is the one disclosure the key-secrecy
        # rule permits: it reaches nobody who did not already have it.
        return _canonical_redirect(
            f"/s/{quote(share_key, safe='')}/",
            _page_headers(request, cache_control=_NO_STORE),
        )

    target, limit_response = await _resolve_metered_share(
        request, share_key, prefiltered=True
    )
    if limit_response is not None:
        return limit_response
    if target is None:
        return _not_found(wants_html=False, request=request)
    if target["kind"] == "folder":
        if _uses_trusted_shell(request):
            return _renderer_redirect("s", share_key, cache_control=_NO_STORE)
        if not _renderer_route_allowed(request):
            return _not_found(wants_html=False, request=request)
        return JSONResponse(
            {"resource_type": "folder", "resource_id": target["resource_id"]},
            headers=renderer_response_headers(cache_control=_NO_STORE),
        )
    return await _authorized_artifact_bytes(
        request,
        target,
        renderer_segments=("s", share_key),
        cache_control=_NO_STORE,
        refusal_wants_html=False,
    )


@router.get("/s/{share_key}/", **_UNDOCUMENTED)
async def redeem_share_public(share_key: str, request: Request) -> Response:
    """Redeem a share link. Possession of the key is the credential —
    no authentication, no session, no grant lookup."""
    wants_html = _wants_html(request)

    target, limit_response = await _resolve_metered_share(request, share_key)
    if limit_response is not None:
        return limit_response
    if target is None:
        return _not_found(wants_html=wants_html, request=request)

    if target["kind"] == "folder":
        # No folder viewer exists yet, so a browser gets the uniform 404 —
        # identical to an unknown key, which is the only 404 this surface has.
        # The JSON marker is the shipped v0 contract and is left alone.
        if wants_html:
            return _not_found(wants_html=True, request=request)
        if _uses_trusted_shell(request):
            return _renderer_redirect(
                "s",
                share_key,
                cache_control=_NO_STORE,
                trailing_slash=True,
            )
        if not _renderer_route_allowed(request):
            return _not_found(wants_html=False, request=request)
        return JSONResponse(
            {"resource_type": "folder", "resource_id": target["resource_id"]},
            headers=renderer_response_headers(cache_control=_NO_STORE),
        )

    if not wants_html:
        return await _authorized_artifact_bytes(
            request,
            target,
            renderer_segments=("s", share_key),
            cache_control=_NO_STORE,
            refusal_wants_html=False,
            renderer_trailing_slash=True,
        )
    # NO canonical URL for a share page. The only URL that identifies it is
    # the one carrying the key, and `og:url` is precisely the field an unfurl
    # bot stores and re-publishes. `/a/` and `/v/` have stable, non-secret
    # URLs and do fill this in.
    return await _authorized_artifact_page(
        request,
        target,
        canonical_url="",
        renderer_segments=("s", share_key),
        cache_control=_NO_STORE,
        include_etag=False,
    )


# ── permalinks: /a/{art_id} (head) and /v/{art_id}/{ver_id} (frozen) ─────────
#
# These differ from `/s/` in the one way that matters: there is no credential
# in the URL. An `art_*` id is not a secret — it rides in API responses, logs
# and UIs — so the ONLY thing standing between an artifact and the internet is
# a live `public` grant, and the only thing standing between an id and a
# confirmed-exists signal is that every refusal here is the same refusal.
#
# `public_reads` collapses unknown / unpublished / revoked / expired /
# soft-deleted into one `None` before the value reaches this module, and these
# routes answer every `None` with `_not_found(wants_html=True)` — the same
# page, the same `_NO_STORE` headers, the same bytes, no branch to get wrong.
#
# Unlike `/s/`, the page is served regardless of `Accept`: `/a/` and `/v/` are
# new URLs with no shipped byte contract to preserve, so the byte affordance
# lives at the explicit `/content` sub-route instead of behind content
# negotiation, and the 404 has exactly one shape.


@router.get("/a/{artifact_id}/content", **_UNDOCUMENTED)
async def artifact_content(artifact_id: str, request: Request) -> Response:
    """The head's raw bytes — what `<img src="content">` on `/a/{id}/` points
    at. Same authorization as the page, so revocation revokes both."""
    async with conn() as c:
        target = await public_reads.public_artifact(c, artifact_id)
    if target is None:
        return _not_found(wants_html=True, request=request)
    return await _authorized_artifact_bytes(
        request,
        target,
        renderer_segments=("a", artifact_id, "content"),
        cache_control=_REVALIDATE,
        refusal_wants_html=True,
    )


@router.get("/v/{artifact_id}/{version_id}/content", **_UNDOCUMENTED)
async def version_content(artifact_id: str, version_id: str, request: Request) -> Response:
    """One version's raw bytes. Immutable: this exact URL can never name
    different bytes, because a new upload mints a new version id."""
    async with conn() as c:
        target = await public_reads.public_version(c, artifact_id, version_id)
    if target is None:
        return _not_found(wants_html=True, request=request)
    return await _authorized_artifact_bytes(
        request,
        target,
        renderer_segments=("v", artifact_id, version_id, "content"),
        cache_control=_VERSION_REVALIDATE,
        refusal_wants_html=True,
    )


@router.get("/a/{artifact_id}", **_UNDOCUMENTED)
async def artifact_permalink_bare(artifact_id: str, request: Request) -> Response:
    """The un-slashed form, canonicalized onto `/a/{id}/`.

    Unconditional and before any lookup — no grant check, no row read. A
    redirect that fired only for published artifacts would make the status
    code itself answer "does this id exist and is it public?", which is the
    question the 404 above is built to refuse.

    Unconditional on `Accept` too: unlike `/s/`, this URL has never served
    bytes, so there is no client to keep in place.
    """
    return _canonical_redirect(
        f"/a/{quote(artifact_id, safe='')}/",
        _page_headers(request, cache_control=_REVALIDATE),
    )


@router.get("/v/{artifact_id}/{version_id}", **_UNDOCUMENTED)
async def version_permalink_bare(artifact_id: str, version_id: str, request: Request) -> Response:
    """The un-slashed `/v/` form. Same unconditional canonicalization."""
    return _canonical_redirect(
        f"/v/{quote(artifact_id, safe='')}/{quote(version_id, safe='')}/",
        _page_headers(request, cache_control=_VERSION_REVALIDATE),
    )


@router.get("/a/{artifact_id}/", **_UNDOCUMENTED)
async def artifact_permalink(artifact_id: str, request: Request) -> Response:
    """An artifact's head, readable by anyone only while a live `public`
    grant covers it."""
    async with conn() as c:
        target = await public_reads.public_artifact(c, artifact_id)
    if target is None:
        return _not_found(wants_html=True, request=request)
    canonical_url = urls.artifact_permalink_url(artifact_id)
    return await _authorized_artifact_page(
        request,
        target,
        canonical_url=canonical_url,
        renderer_segments=("a", artifact_id),
        cache_control=_REVALIDATE,
    )


@router.get("/v/{artifact_id}/{version_id}/", **_UNDOCUMENTED)
async def version_permalink(artifact_id: str, version_id: str, request: Request) -> Response:
    """One immutable version of an artifact, under the same public grant.

    The grant is checked on the ARTIFACT, not the version: publishing is a
    property of the artifact, and revoking it must stop every version URL at
    once. A version permalink that outlived the grant would be a permanent
    leak of the document as it stood.
    """
    async with conn() as c:
        target = await public_reads.public_version(c, artifact_id, version_id)
    if target is None:
        return _not_found(wants_html=True, request=request)
    canonical_url = urls.version_permalink_url(artifact_id, version_id)
    return await _authorized_artifact_page(
        request,
        target,
        canonical_url=canonical_url,
        renderer_segments=("v", artifact_id, version_id),
        cache_control=_VERSION_REVALIDATE,
    )


# ── /f/{fld_id}: the one public route that discloses by INCLUSION ────────────
#
# `/a/` and `/v/` answer a question about a resource the caller already named,
# so their whole defence is the uniform 404. A folder page hands back
# resources the caller did NOT name — so a second, sharper property applies:
#
#   every child the listing names is a child this surface would serve.
#
# It is not enforced here. `public_reads.public_folder` filters the children
# with `v0_authz.visibility_lateral` — the same grant resolution `/a/` and
# `/f/` themselves resolve with — so a soft-deleted child never reaches this
# module. One owner, one resolution; a filter re-expressed at the route would
# be the second one that drifts.
#
# The listing is ONE level deep by design. A recursive page would make a
# single click serve an entire published subtree, and the whole subtree is
# already reachable — one grant check at a time, through each child's own
# permalink.


@router.get("/f/{folder_id}", **_UNDOCUMENTED)
async def folder_permalink_bare(folder_id: str, request: Request) -> Response:
    """The un-slashed form, canonicalized onto `/f/{id}/`.

    Unconditional and before any lookup, for the same reason `/a/`'s is: a
    redirect that fired only for published folders would make the status code
    itself answer "does this id exist and is it public?".
    """
    return _canonical_redirect(
        f"/f/{quote(folder_id, safe='')}/",
        _page_headers(request, cache_control=_REVALIDATE),
    )


@router.get("/f/{folder_id}/", **_UNDOCUMENTED)
async def folder_permalink(folder_id: str, request: Request) -> Response:
    """A published folder's immediate children.

    Revalidating rather than immutable: unlike `/v/`, the listing moves every
    time a child is added, renamed or taken down, and a stale copy of it would
    keep advertising a name the publisher has already withdrawn.
    """
    async with conn() as c:
        folder = await public_reads.public_folder(c, folder_id)
    if folder is None:
        return _not_found(wants_html=True, request=request)
    description = _describe_folder(path=folder["path"], count=len(folder["entries"]))
    canonical_url = urls.folder_permalink_url(folder_id)
    if _uses_trusted_shell(request):
        html = render_shell(
            title=folder["name"],
            description=description,
            canonical_url=canonical_url,
            renderer_url=urls.renderer_url("f", folder_id, trailing_slash=True),
            og_type="website",
            # A folder has no bytes, so no download link and no type/size
            # strip — the header states what it is, where it is and how many
            # things are in it, which is the whole of what a listing has.
            kind="folder",
            meta_line=description,
        )
        return HTMLResponse(
            html,
            headers=shell_response_headers(cache_control=_REVALIDATE),
        )
    if not _renderer_route_allowed(request):
        return _not_found(wants_html=True, request=request)
    html = render_folder_page(
        name=folder["name"],
        path=folder["path"],
        entries=folder["entries"],
        canonical_url=canonical_url,
        description=description,
        truncated=folder["truncated"],
        embed=_wants_embed(request),
    )
    return HTMLResponse(
        html,
        headers=renderer_response_headers(cache_control=_REVALIDATE),
    )
