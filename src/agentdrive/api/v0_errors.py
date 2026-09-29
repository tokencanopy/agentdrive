"""Uniform /v0 error envelope and exception type (§5.3, §6.3).

The ONLY JSON error envelope on the v0 cutover surface is top-level:

    {"error": {"code": "...", "message": "...", "details": {...}}}

`v0_api_error_handler` renders that shape directly — never through
FastAPI's historical ``{"detail": ...}`` wrapper — so a raised
:class:`V0ApiError` (including one raised by the dependency layer) cannot
double-wrap.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .error_codes import ERROR_CODES


class V0ApiError(Exception):
    """A predictable public v0 failure with one machine-readable code.

    ``status_code`` is explicit (the design doc does not map codes to
    statuses), and ``code`` must be registered in ``ERROR_CODES``.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unregistered error code {code!r}")
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = dict(details) if details is not None else None
        self.headers = dict(headers) if headers is not None else {}


async def v0_api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`V0ApiError` without FastAPI's ``detail`` wrapper."""

    if not isinstance(exc, V0ApiError):
        raise exc

    error: dict[str, Any] = {
        "code": exc.code,
        "message": exc.message,
    }
    if exc.details is not None:
        error["details"] = exc.details

    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        **exc.headers,
        "Content-Type": "application/json; charset=utf-8",
    }
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
        headers=headers,
    )


async def v0_validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render strict v0 validation failures through the same top-level envelope."""

    if not isinstance(exc, RequestValidationError):
        raise exc
    details = public_validation_details(exc.errors())
    return await v0_api_error_handler(
        _request,
        V0ApiError(
            422,
            "VALIDATION_ERROR",
            "The request does not match the AgentDrive v0 contract.",
            details=details,
        ),
    )


def _public_validation_reason(error_type: str) -> str:
    """Collapse framework-specific Pydantic codes into stable API reasons."""

    if error_type == "missing":
        return "required"
    if error_type == "extra_forbidden":
        return "unknown_field"
    return "invalid_value"


def public_validation_details(
    errors: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """Strip Pydantic internals down to the stable public field shape."""

    return {
        "fields": [
            {
                "location": ".".join(str(part) for part in error["loc"]),
                "reason": _public_validation_reason(error["type"]),
            }
            for error in errors
        ]
    }
