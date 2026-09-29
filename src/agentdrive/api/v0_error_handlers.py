"""The `/v0` error envelope, installed on every app that serves `/v0`.

EXTRACTED from `agentdrive.app` when the hosted MCP got its own private
loopback ingress (`agentdrive.internal_ingress`). Both apps mount the same
`/v0` routers, so both must render the same `{"error": {code, message,
details}}` envelope for the same failures — an internal-ingress request that
degraded to FastAPI's `{"detail": ...}` would make the MCP sidecar's typed
SDK errors mean something different from the public API's.

One installer rather than two copies: a handler added here reaches both
surfaces, which is the only way they cannot drift.
"""

from __future__ import annotations

import logging
from contextlib import suppress

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from .cursors import BadCursor
from .v0_errors import (
    V0ApiError,
    v0_api_error_handler,
    v0_validation_error_handler,
)

log = logging.getLogger(__name__)


async def rate_limit_exceeded_json(
    request: Request, exc: Exception
) -> JSONResponse:
    """`RateLimitExceeded` → 429 in the canonical envelope."""
    assert isinstance(exc, RateLimitExceeded)
    response = JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "RATE_LIMITED",
                "message": f"rate limit exceeded: {exc.detail}",
            }
        },
        headers={"Retry-After": "60"},
    )
    # Let slowapi widen the default with its real window / add X-RateLimit-*.
    # It reads `request.state.view_rate_limit`; guard in case it's absent.
    with suppress(Exception):
        response = request.app.state.limiter._inject_headers(
            response, request.state.view_rate_limit
        )
    return response


async def bad_cursor_handler(_request: Request, exc: Exception) -> JSONResponse:
    """`api.cursors.BadCursor` → 400 INVALID_CURSOR.

    Centralized so any leftover route that decodes a cursor via the legacy
    base64 path (and reads its typed fields via `cursor_str`/`cursor_int`/
    `cursor_ts`) surfaces a uniform 400 for a malformed, forged, or
    wrong-typed cursor — no per-route catch, and no 500 from a bad field
    value reaching `datetime.fromisoformat`/asyncpg. Renders the §6.3
    top-level envelope `{"error": {code, message}}`, same as every other /v0
    error path. The wire code matches the sealed-cursor lists
    (`INVALID_CURSOR`), so a caller sees one code for any bad cursor."""
    assert isinstance(exc, BadCursor)
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
    )


async def data_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """`asyncpg.exceptions.DataError` / `OverflowError` → 400 BAD_REQUEST.

    Defense-in-depth backstop: an out-of-range or otherwise unbindable
    value that slips past the input-validation layer and reaches a SQL
    parameter bind (e.g. an integer outside a column's int32/int64 range)
    raises `asyncpg.DataError`; an overflow in Python before the bind
    raises `OverflowError`. Without this handler either degrades to a bare
    Starlette 500. The specific out-of-range guards on the concurrency
    surfaces (If-Match token parse, upload `if_match` bound, version-number
    read) are the primary fixes — this only ensures ANY *future*
    out-of-range/overflow bind still degrades to a clean 4xx.

    Same top-level `{"error": {...}}` envelope as the handlers above."""
    log.warning("out-of-range/overflow bind reached SQL: %s", exc)
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "BAD_REQUEST",
                "message": "a request value was out of the acceptable range",
            }
        },
    )


async def not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Starlette 404/405 → the canonical §6.3 top-level envelope.

    FastAPI's default renders `{"detail": "Not Found"}` — a divergent error
    shape. A client probing an unknown path gets the same `{"error": {code,
    message}}` envelope everywhere, so one parser handles every failure.
    (`/health`'s 503 is a deliberate legacy shape and is unaffected.)"""
    assert isinstance(exc, StarletteHTTPException)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "NOT_FOUND" if exc.status_code == 404 else "METHOD_NOT_ALLOWED",
                "message": str(exc.detail) if isinstance(exc.detail, str) else "not found",
            }
        },
    )


async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Unhandled exception → 500 in the canonical envelope.

    FastAPI's default `{"detail": "Internal Server Error"}` is a second shape
    and (worse) can leak a stack trace in debug. Renders the top-level
    `{"error": {"code": "INTERNAL_ERROR"}}`; the exception is logged by the
    request middleware."""
    log.error("unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "internal error"}},
    )


def install_v0_error_handlers(app: FastAPI) -> None:
    """Register every `/v0` error handler on ``app``.

    §6.3 cutover pin: `v0_api_error_handler` for `V0ApiError` and
    `v0_validation_error_handler` for `RequestValidationError` so FastAPI's
    legacy `{"detail": ...}` wrapper never reaches the wire — every v0 failure
    carries the single top-level `{"error": {code, message, details}}`
    envelope, including auth rejections raised by the dependency layer (no
    double-wrap: the handler renders `V0ApiError` directly).
    """
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_json)
    app.add_exception_handler(V0ApiError, v0_api_error_handler)
    app.add_exception_handler(RequestValidationError, v0_validation_error_handler)
    app.add_exception_handler(BadCursor, bad_cursor_handler)
    app.add_exception_handler(asyncpg.exceptions.DataError, data_error_handler)
    app.add_exception_handler(OverflowError, data_error_handler)
    app.add_exception_handler(StarletteHTTPException, not_found_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
