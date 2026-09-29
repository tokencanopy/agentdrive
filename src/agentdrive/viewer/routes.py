"""The private viewer surface: `/view/` (2026-08-09 private-viewer design).

Four routes, all `include_in_schema=False` — this is a browser surface on the
isolated viewer origin, not part of the `/v0` contract:

  * ``GET /view/``                — the credential-free static shell.
  * ``GET /view/static/{asset}``  — allowlisted assets (shell JS, viewer CSS,
                                    the shared pdf.js bundle).
  * ``GET /view/doc``             — the rendered document, as JSON, for a
                                    viewer credential in ``Authorization``.
  * ``GET /view/content``         — the pinned version's bytes, same auth.

Three rules, inherited from the public surface and tightened:

1. **The credential rides ONLY in the Authorization header.** Never a path or
   query parameter — infrastructure request logs capture both. There is no
   redaction rule for this surface because there is nothing to redact.

2. **Uniform refusal.** `resolve_credential` collapses unknown / expired /
   deleted-target / revoked-grant into one ``None``; both credentialed routes
   answer every ``None`` with the same 404 envelope. A missing or malformed
   header is the one distinct answer (401) — that is a protocol error, not an
   existence probe.

3. **The shell is embeddable by the configured console origins and nothing
   else.** ``frame-ancestors`` lists VIEWER_EMBED_ORIGINS exactly; empty
   config means ``'none'`` — fail closed. The public surface's
   ``frame-ancestors 'none'`` is untouched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..api.v0_errors import V0ApiError
from ..api.v0_rate_limit import enforce_v0_rate_limit
from ..config import settings
from ..core import v0_viewer_sessions as sessions
from ..db import conn
from ..public.page import human_size
from ..public.routes import diagram_frame_csp, preview_input, render_in_thread
from ..rendering.safety import serve_bytes

router = APIRouter(dependencies=[Depends(enforce_v0_rate_limit)])

_UNDOCUMENTED = {"include_in_schema": False}

_TEMPLATES = Path(__file__).parent / "templates"
_STATIC = Path(__file__).parent / "static"
_PUBLIC_STATIC = Path(__file__).parent.parent / "public" / "static"
_APP_STATIC = Path(__file__).parent.parent / "static"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=select_autoescape(["html"]),
)


# An embed origin is `scheme://host[:port]` and nothing else. Matched
# strictly rather than filtered for a few bad values: `https://*` and
# `https://*.evil.test` are wildcards CSP honours (any https origin could
# then frame the viewer), a trailing path silently disagrees with
# `event.origin` in the shell's own check, and whitespace or `;` would
# append directives to the header this value is interpolated into.
_EMBED_ORIGIN = re.compile(
    r"^https?://[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"(?::\d{1,5})?$"
)


def embed_origins() -> list[str]:
    """The exact origins allowed to embed the shell and to hand it a
    credential over postMessage.

    Anything that is not a bare `scheme://host[:port]` is dropped — see
    `_EMBED_ORIGIN`. A misconfigured entry silently narrows the policy
    (fail closed) rather than widening it.
    """
    out: list[str] = []
    for raw in settings.viewer_embed_origins.split(","):
        origin = raw.strip().rstrip("/")
        if origin and _EMBED_ORIGIN.match(origin):
            out.append(origin)
    return out


def _shell_csp() -> str:
    """The shell page policy. Differences from the public page CSP, each
    load-bearing:

      * ``frame-ancestors`` names the configured console origins (the whole
        point of this surface) — or ``'none'`` when unconfigured, so an
        undeployed environment fails closed rather than open.
      * ``connect-src`` adds ``blob:`` (pdf.js loads the credential-fetched
        document from an object URL) and the signed-download host (bytes
        above the streaming threshold 307 there).
      * ``img-src`` adds ``blob:`` — the artifact's own image is fetched
        with the credential and swapped in as an object URL, because an
        ``<img src>`` cannot carry an Authorization header — and, unlike
        the public page, **omits ``https:``**.

        That omission is the one place this policy is deliberately
        stricter than the public one, and parity is the wrong instinct
        here. A remote image in a document is a request the viewer's
        browser makes to a third party the moment the document opens: the
        author learns the reader's IP, user agent, and the time they
        looked. On the public surface that discloses nothing new — the
        document is already published to anyone. On a PRIVATE document it
        turns "who read my artifact, and when" into a signal the author
        can collect silently, with a one-pixel image and no cooperation
        from us. `Referrer-Policy: no-referrer` hides the URL; it does not
        stop the request.

        The cost is that remote images do not render in private view.
        Local artifact images, which is what a drive actually stores, are
        unaffected. A "load remote content" affordance would need a second
        shell response carrying a relaxed policy — CSP cannot be widened
        after the fact — and is deliberately left as follow-up rather than
        shipped as a default-on leak.
    """
    ancestors = " ".join(embed_origins()) or "'none'"
    return (
        "default-src 'none'; script-src 'self'; "
        "connect-src 'self' blob: https://storage.googleapis.com; "
        "worker-src 'self' blob:; img-src 'self' blob: data:; "
        # `media-src` mirrors `img-src` exactly, and for the same reasons: an
        # inline video's bytes are fetched with the credential and handed to
        # the element as an object URL, because a `<video src>` cannot carry
        # an Authorization header — and remote media is omitted on the same
        # privacy argument spelled out above, since a `<video>` pointed at a
        # third party is the same read receipt with a bigger payload. Without
        # this directive `default-src 'none'` refuses the blob and inline
        # video fails silently.
        "media-src 'self' blob:; "
        "style-src 'self'; font-src 'self'; "
        # Same grant as the public page, for the same single purpose: the
        # diagram engine's own same-origin frame. See `_renderer_csp`.
        "frame-src 'self'; "
        "base-uri 'none'; form-action 'none'; "
        f"frame-ancestors {ancestors}"
    )


# The credentialed sub-resources serve agent-authored bytes; they are fetched
# by the shell, never navigated, so the strong public content policy is
# correct here verbatim — script-less, sandboxed, non-frameable.
_CONTENT_CSP = (
    "default-src 'none'; script-src 'none'; object-src 'none'; "
    "img-src 'self' data: https:; style-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox"
)

# Every response on this surface: a credentialed private document — never
# cacheable, never a referrer source.
_PRIVATE = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
}


def _credential_from(authorization: str | None) -> str:
    """The Bearer credential, or a 401. A missing header is a protocol error
    (the shell always sends one), distinct from the uniform 404 that answers
    every bad credential."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise V0ApiError(
            401, "AUTHENTICATION_REQUIRED", "missing viewer credential",
            headers=dict(_PRIVATE),
        )
    credential = authorization[7:].strip()
    if not credential:
        raise V0ApiError(
            401, "AUTHENTICATION_REQUIRED", "missing viewer credential",
            headers=dict(_PRIVATE),
        )
    return credential


def _not_found() -> V0ApiError:
    """The uniform refusal — one code, one message, no reason ever given."""
    return V0ApiError(
        404, "VIEWER_SESSION_NOT_FOUND", "invalid or expired viewer session",
        headers=dict(_PRIVATE),
    )


async def _resolve(credential: str) -> dict[str, Any]:
    async with conn() as c:
        target = await sessions.resolve_credential(c, credential)
    if target is None:
        raise _not_found()
    return target


@router.get("/view/", **_UNDOCUMENTED)
async def viewer_shell() -> Response:
    """The credential-free shell the console iframes.

    Stable URL, no parameters: the shell learns WHICH document to render
    only from the postMessage handshake, so the iframe src can never leak
    anything and an attacker who can name this URL learns nothing.
    """
    html = _env.get_template("shell.html").render(
        asset_v=ASSET_V,
        # Rendered with Jinja's |tojson, which escapes `<`, `>`, and `&` into
        # \u escapes — JSON that is inert inside a <script> element even if a
        # config value ever contained `</script>`.
        config={"embedOrigins": embed_origins(), "protocol": 1},
    )
    return HTMLResponse(
        html,
        headers={
            **_PRIVATE,
            "Content-Security-Policy": _shell_csp(),
            "X-Content-Type-Options": "nosniff",
        },
    )


# The same allowlist-dict pattern as the public surface, for the same two
# reasons: a route answers at the root in both mount modes (a Mount resolves
# against root_path), and a dict cannot be path-traversed. `viewer.css` and
# the pdf.js bundle are the SAME FILES the public surface serves — one
# implementation of the document styling and the PDF engine, two hosts.
_VIEWER_ASSETS: dict[str, tuple[Path, str]] = {
    "shell.js": (_STATIC / "shell.js", "text/javascript"),
    "reading.js": (_PUBLIC_STATIC / "reading.js", "text/javascript"),
    "viewer.css": (_PUBLIC_STATIC / "viewer.css", "text/css"),
    "pdfview.js": (_APP_STATIC / "pdfview.js", "text/javascript"),
    # Diagrams: the same module and the same vendored engine the public
    # surface serves, loaded by the shell only when a document carries one.
    "diagrams.js": (_PUBLIC_STATIC / "diagrams.js", "text/javascript"),
    "diagram-frame.html": (_PUBLIC_STATIC / "diagram-frame.html", "text/html"),
    "diagram-frame.js": (_PUBLIC_STATIC / "diagram-frame.js", "text/javascript"),
    "vendor/mermaid/mermaid.min.js": (
        _APP_STATIC / "vendor" / "mermaid" / "mermaid.min.js", "text/javascript"),
    "vendor/pdfjs/pdf.min.mjs": (
        _APP_STATIC / "vendor" / "pdfjs" / "pdf.min.mjs", "text/javascript"),
    "vendor/pdfjs/pdf.worker.min.mjs": (
        _APP_STATIC / "vendor" / "pdfjs" / "pdf.worker.min.mjs", "text/javascript"),
    "vendor/pdfjs/pdf_viewer.mjs": (
        _APP_STATIC / "vendor" / "pdfjs" / "pdf_viewer.mjs", "text/javascript"),
    "vendor/pdfjs/pdf_viewer.css": (
        _APP_STATIC / "vendor" / "pdfjs" / "pdf_viewer.css", "text/css"),
    # pdf_viewer.css's page-loading spinner — the only image that css pulls
    # in our configuration (the rest are annotation-editor chrome the
    # viewers never enable). Without it every PDF render logs a 404.
    "vendor/pdfjs/images/loading-icon.gif": (
        _APP_STATIC / "vendor" / "pdfjs" / "images" / "loading-icon.gif",
        "image/gif"),
}

_ASSET_HEADERS = {
    "Cache-Control": "public, max-age=300, must-revalidate",
    "Referrer-Policy": "no-referrer",
}


def _asset_version() -> str:
    import hashlib

    h = hashlib.sha256()
    for name in ("shell.js", "viewer.css", "reading.js", "diagrams.js", "diagram-frame.js"):
        f = _VIEWER_ASSETS[name][0]
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


ASSET_V = _asset_version()


@router.get("/view/static/{asset:path}", **_UNDOCUMENTED)
async def viewer_asset(asset: str) -> Response:
    entry = _VIEWER_ASSETS.get(asset)
    if entry is None:
        raise V0ApiError(404, "NOT_FOUND", "not found", headers=dict(_PRIVATE))
    path, media_type = entry
    headers = dict(_ASSET_HEADERS)
    if media_type == "text/html":
        # The diagram engine's document: its own policy, see the public
        # surface's `diagram_frame_csp`. One function, two hosts — with THIS
        # shell's ancestors, because the browser checks the whole chain.
        headers["Content-Security-Policy"] = diagram_frame_csp(
            " ".join(embed_origins()) or "'none'"
        )
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/view/doc", **_UNDOCUMENTED)
async def viewer_doc(
    authorization: str | None = Header(default=None),
) -> Response:
    """The rendered document for a viewer credential, as JSON.

    JSON rather than an HTML page so the credential can ride in a header:
    an iframe navigation cannot carry one, a fetch can. The shell injects
    ``html`` into its own DOM — the value is the same escaped output the
    public viewer serves, produced by the same renderer.

    ``binding`` echoes the session's pinned drive/artifact/version so the
    shell (and the console behind it) can verify the credential it was
    handed matches the artifact it believes it is showing.
    """
    credential = _credential_from(authorization)
    target = await _resolve(credential)

    data, source = await preview_input(target)
    body = await render_in_thread(
        target, data, source,
        # Drawn here, but `shell.js` REMOVES it unless the embedding console
        # declared `print: true` on the credential message. The server cannot
        # know which console build is on the other side of the frame; the
        # shell can, so the runtime decision is left to it and the default
        # stays closed.
        print_button=True,
    )
    return JSONResponse(
        {
            "binding": target["binding"],
            "name": target["name"],
            "path": target["path"],
            "content_type": target["content_type"],
            "size_bytes": target["size_bytes"],
            "size_human": human_size(target["size_bytes"]),
            "updated_at": (
                target["updated_at"].strftime("%Y-%m-%d")
                if target["updated_at"] else ""
            ),
            "mode": body.mode,
            "title": body.title or target["name"],
            "html": body.html,
            "diagrams": body.diagrams,
        },
        headers={**_PRIVATE, "X-Content-Type-Options": "nosniff"},
    )


@router.get("/view/content", **_UNDOCUMENTED)
async def viewer_content(
    authorization: str | None = Header(default=None),
) -> Response:
    """The pinned version's raw bytes, under the same credential and the
    same refusal as `/view/doc` — a sub-route that resolved more permissively
    than the document would be revocation that does not revoke."""
    credential = _credential_from(authorization)
    target = await _resolve(credential)
    return await serve_bytes(
        target,
        headers={**_PRIVATE, "ETag": f'"{target["etag"]}"'},
        content_csp=_CONTENT_CSP,
    )
