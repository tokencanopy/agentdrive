"""ASGI middleware that stamps `[req=…]` and `[<METHOD path>]` on every
log line emitted during a request, and round-trips `X-Request-Id`.

Sits at the outermost layer so even auth failures (401s before any
handler runs) carry the request prefix — which is when you most need
to grep the logs.

Honors an incoming `X-Request-Id` (load balancers and clients can set
one) provided it looks safe; otherwise mints a fresh 8-char hex ID.
The validation is deliberately tight because anything we accept here
ends up in log records, and a malicious value could pollute downstream
log parsing.
"""

import logging
import re
import secrets
import time

from ..config import settings
from . import context as ctx
from .redaction import redact_share_path

log = logging.getLogger("agentdrive.request")

# `X-Request-Id` whitelist: alphanumeric + `-_` (lets us accept GCP's
# trace IDs and most LB-minted formats), 8–128 chars. Anything else is
# rejected and we mint our own, so the prefix stays unambiguous.
_VALID_RID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _mint_request_id() -> str:
    """8-char hex ID. Short enough for grep ergonomics, wide enough
    (2^32 namespace) that collisions inside a single deployment window
    are vanishingly unlikely."""
    return secrets.token_hex(4)


def _log_safe_path(path: str) -> str:
    """Redact bare or configured-mount share credentials before logging."""
    return redact_share_path(path, settings.mount_prefix)


def _extract_request_id(scope: dict) -> str:
    """Pull `X-Request-Id` from the request, validate it, or mint a new
    one. Returns a string suitable for logging — never raw user input."""
    for k, v in scope.get("headers", ()):
        if k == b"x-request-id":
            # latin-1 is total over bytes (every byte is a valid char),
            # so no decode errors are possible — `errors=...` would be
            # dead code. The regex validator below is the real defense.
            candidate = v.decode("latin-1").strip()
            if _VALID_RID.fullmatch(candidate):
                return candidate
            # Invalid — fall through to mint.
            break
    return _mint_request_id()


class RequestContextMiddleware:
    """Outermost middleware: sets `request_id` + `operation` ContextVars,
    stamps `X-Request-Id` on the response, logs request start/end.

    Wraps the entire app so every log emitted by deeper layers (auth dep,
    handlers, service layer, DB pool) inherits the prefix automatically
    via `ContextFilter`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # Lifespan, websocket, etc. — pass through untouched.
            await self.app(scope, receive, send)
            return

        rid = _extract_request_id(scope)
        op = f"{scope.get('method', '?')} {_log_safe_path(scope.get('path', '?'))}"

        status_holder = [0]

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                status_holder[0] = message.get("status", 0)
                # Inject `X-Request-Id` on the response so clients (and
                # the user filing a bug report) can correlate. Strip
                # any pre-existing `x-request-id` header so we never
                # emit two — duplicate headers have unspecified client
                # behavior (some pick first, some last).
                existing = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                message["headers"] = existing + [
                    (b"x-request-id", rid.encode("ascii")),
                ]
            await send(message)

        # Set ContextVars inside nested try/finally so a hypothetical
        # raise from a later set() can't leak the earlier tokens.
        # Reset order is reverse-of-set (standard LIFO idiom).
        #
        # The `drive_id` / `user_id` / `organization_id` stakes (set
        # None first) let the auth dep overwrite without holding its
        # own token — our reset rolls back any value the dep set.
        # Prevents cross-request leaks in environments (notably tests)
        # where ASGI requests share the calling task's ContextVar scope.
        req_token = ctx.request_id.set(rid)
        try:
            op_token = ctx.operation.set(op)
            try:
                drive_token = ctx.drive_id.set(None)
                user_token = ctx.user_id.set(None)
                org_token = ctx.organization_id.set(None)
                try:
                    start = time.perf_counter()
                    log.info("at=request.start")
                    try:
                        await self.app(scope, receive, wrapped_send)
                        elapsed = round((time.perf_counter() - start) * 1000, 1)
                        log.info(
                            "at=request.end status=%s ms=%s",
                            status_holder[0] or "?",
                            elapsed,
                        )
                    except Exception:
                        elapsed = round((time.perf_counter() - start) * 1000, 1)
                        log.exception("at=request.exception ms=%s", elapsed)
                        raise
                finally:
                    # Reset in reverse-of-set order (LIFO).
                    ctx.organization_id.reset(org_token)
                    ctx.user_id.reset(user_token)
                    ctx.drive_id.reset(drive_token)
            finally:
                ctx.operation.reset(op_token)
        finally:
            ctx.request_id.reset(req_token)
