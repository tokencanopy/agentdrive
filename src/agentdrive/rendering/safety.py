"""Byte-safety for untrusted artifact bytes served from our own origins.

Shared by the public surface and the private viewer so there is exactly one
implementation of the three levers that keep attacker-authored bytes from
executing: `nosniff`, the `Content-Disposition` decision for active document
types, and a sandboxed content CSP. The CSP text itself is a parameter — the
two surfaces' policies differ only in provenance (module constant vs
config-derived), never in strength, and the caller owns that choice.

`serve_bytes` is the one byte-serving path: stream small objects, 307 to a
signed storage URL above the threshold. A second copy of this logic was the
drift the 2026-08-09 private-viewer design forbids.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any

from fastapi.responses import Response, StreamingResponse

from .. import storage
from ..config import settings
from ..content_disposition import build_content_disposition
from ..core.kinds import safe_content_type

# Media types a browser will execute if it ever treats the response as a
# document. `text/html` and `image/svg+xml` are the two an agent realistically
# produces; the rest are the same capability wearing a different label (XML
# carries XSLT, XHTML is HTML). Serving these as `attachment` removes the only
# path that makes them a document — a top-level navigation — while leaving
# `<img src="content">` working, because a subresource fetch ignores
# Content-Disposition and script inside an SVG loaded through `<img>` never
# runs. Everything else stays `inline`; an attachment on every response would
# make the viewer download files nobody asked for.
ACTIVE_TYPES = frozenset({
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
    "application/xslt+xml",
    "text/xsl",
    "application/mathml+xml",
})


class DeliveryClass(StrEnum):
    PRIVATE_SIGNED = "private_signed"
    PUBLIC_METERED = "public_metered"


def byte_safety_headers(
    target: dict[str, Any], *, content_csp: str, force_attachment: bool = False
) -> dict[str, str]:
    """The three levers that keep untrusted bytes from executing on our origin.

    `nosniff` closes type confusion — the declared type is attacker-chosen too,
    and `text/plain` carrying markup is the classic sniffing bypass.
    `Content-Disposition` decides whether a navigation renders or downloads.
    The sandboxed CSP is the backstop for whatever the first two miss.

    The Content-Type stays honest where it can be: rewriting SVG to
    `application/octet-stream` would break the legitimate image case, and with
    these three headers the declared type no longer decides what executes. It
    is sanitized rather than trusted, though — see `safe_content_type`.

    `force_attachment` is the reader asking for the bytes rather than a view of
    them. It only ever tightens: a type already served as `attachment` is
    unaffected, and no flag can turn an active type back into a document. The
    download control moved onto the trusted shell, which is a different origin
    from the bytes — and the `download` attribute is ignored across origins, so
    a link alone would have opened a JSON artifact instead of saving it. This
    is the header that does what that attribute cannot.
    """
    base = safe_content_type(target["content_type"]).split(";", 1)[0].strip().lower()
    disposition = "attachment" if (force_attachment or base in ACTIVE_TYPES) else "inline"
    return {
        "Content-Security-Policy": content_csp,
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": build_content_disposition(disposition, target["name"]),
    }


async def serve_bytes(
    target: dict[str, Any],
    *,
    headers: dict[str, str],
    content_csp: str,
    force_attachment: bool = False,
    delivery_class: DeliveryClass = DeliveryClass.PRIVATE_SIGNED,
    byte_stream: AsyncIterator[bytes] | None = None,
) -> Response:
    """Raw bytes for an already-authorized target.

    `headers` is the caller's cache/referrer policy; the safety headers are
    added here rather than by the caller, because they are not a per-route
    choice — every raw-byte response on either surface needs them.

    Above the signed-download threshold this redirects to storage rather than
    proxying. The 307 carries NO Content-Type or Content-Length: it has an
    empty body, and claiming a length it does not send is an HTTP framing
    violation that hangs keep-alive clients.
    """
    headers = {
        **headers,
        **byte_safety_headers(
            target, content_csp=content_csp, force_attachment=force_attachment
        ),
    }
    # Nothing validates `content_type` on upload, so the stored value can hold
    # a CRLF. Emitting that raw is a header-injection attempt; the server
    # refuses it, but by raising — the connection drops and the reader gets
    # zero bytes, permanently, for that artifact. Sanitize before it can
    # become a header, here and in the signed URL we ask storage to sign.
    content_type = safe_content_type(target["content_type"])
    # The signed-redirect shortcut is for artifact-CAS content only. A
    # transfer-bucket object's signed GET is the packet-4 capability mint's
    # job — it validates namespace, generation, and expiry and fails
    # closed; this signer would mint against the default host with none of
    # that. Oversized transfer-bucket content streams instead.
    if (
        delivery_class is DeliveryClass.PRIVATE_SIGNED
        and byte_stream is None
        and target.get("storage_bucket") is None
        and target["size_bytes"] > settings.download_signed_min_bytes
    ):
        # No `force_attachment` here on purpose: `signed_download_url` always
        # signs `Content-Disposition: attachment`, so the flag is already the
        # behaviour on this branch.
        signed = await storage.signed_download_url(
            target["storage_object"],
            content_type=content_type,
            filename=target["name"],
            ttl_s=settings.download_url_ttl_s,
            bucket=target.get("storage_bucket"),
            generation=target.get("storage_generation"),
        )
        if signed is not None:
            return Response(status_code=307, headers={**headers, "Location": signed})
    return StreamingResponse(
        byte_stream
        or storage.stream(
            target["storage_object"],
            bucket=target.get("storage_bucket"),
            generation=target.get("storage_generation"),
        ),
        status_code=200,
        headers={
            **headers,
            "Content-Type": content_type,
            "Content-Length": str(target["size_bytes"]),
        },
    )
