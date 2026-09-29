"""CORS for `/v0`, scoped to the console's origins.

The console at `app.tokencanopy.com` is a different origin from
`drive.tokencanopy.com`, so a browser will not send `Authorization` there
without a successful preflight first. Nothing in the console works until
this does, and the failure is invisible server-side: the API sees a
well-formed OPTIONS and answers 405, while the browser quietly refuses to
make the real request.

Deliberately NOT `allow_origins=["*"]`. These endpoints take a bearer token,
and a wildcard invites any page on the internet to spend one it tricked a
browser into attaching. The allowlist is explicit and empty by default, so a
new environment grants nothing until someone names it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import settings

# CORS applies to `/v0` and nothing else.
#
# Scoped rather than global for two reasons. The public read surface
# (`/s/`, `/a/`, `/f/`, `/v/`) is same-origin HTML that needs none, and
# granting cross-origin reads there would hand out a capability nobody asked
# for. And a global policy answers preflights for every other surface too,
# including ones with their own rules — which is how this was caught: an
# app-wide CORSMiddleware started replying to `/v1/agenttag` preflights that
# a test expected to go ungranted.
_CORS_PREFIX = "/v0"

# The request headers `/v0` actually needs. `Idempotency-Key` and `If-Match`
# are not CORS-safelisted, so a request carrying them is preflighted and
# fails unless they are named here — which would leave the console able to
# read everything and write nothing.
_REQUEST_HEADERS = [
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "If-Match",
    "If-None-Match",
]

# `ETag` is not a safelisted RESPONSE header: without this the browser hides
# it from JavaScript even though the server sent it. The console would then
# have no value to echo back in `If-Match`, and every update would fail its
# precondition for a reason nothing in the UI could explain.
#
# `Location` matters for the 307 on large downloads; `Retry-After` for the
# 429 the rate limit returns.
_EXPOSED_HEADERS = ["ETag", "Location", "Retry-After"]


def allowed_origins() -> list[str]:
    """The configured origins, wildcard rejected.

    `*` is filtered rather than honoured: a deployment that sets it has
    almost certainly not thought about the bearer token, and failing closed
    on a misconfiguration is cheaper than discovering it from a report.
    """
    return [
        origin
        for raw in settings.cors_allowed_origins.split(",")
        if (origin := raw.strip()) and origin != "*"
    ]


def cors_kwargs() -> dict[str, Any]:
    """The policy, as its own function so tests can drive it directly.

    `app` is a process-wide singleton built at import time, so a test cannot
    reconfigure the installed middleware by patching settings afterwards. It
    re-wraps the app with these kwargs instead — the same expression
    `install_cors` uses, so the tested policy and the shipped one cannot
    drift apart.
    """
    return {
        "allow_origins": allowed_origins(),
        "allow_methods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        "allow_headers": _REQUEST_HEADERS,
        "expose_headers": _EXPOSED_HEADERS,
        # No cookies: the console carries a bearer token, so credentialed
        # requests buy nothing — and refusing them is what keeps a
        # misconfigured origin from becoming a session-riding hole.
        "allow_credentials": False,
        "max_age": 600,
    }


class ScopedCORSMiddleware:
    """Apply `CORSMiddleware` to `/v0` only; pass everything else through.

    Starlette's CORS middleware is app-wide by construction — it answers
    every preflight it sees. Wrapping it in a path check is what keeps the
    policy where it belongs, and keeps it from silently becoming the answer
    for surfaces that have their own.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.cors = CORSMiddleware(app, **cors_kwargs())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith(_CORS_PREFIX):
            await self.cors(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install_cors(app: FastAPI) -> None:
    """Install the policy, always.

    Not conditional on the allowlist being non-empty, for the same reason
    `HostSurfaceMiddleware` is always installed and inert when unconfigured:
    a middleware that only exists in some deployments cannot be verified as
    wired in any of them, and "is it installed?" then becomes a question
    about environment variables rather than about code.

    An empty allowlist denies just as completely — no origin matches — so
    the shipped default is unchanged.
    """
    app.add_middleware(ScopedCORSMiddleware)
