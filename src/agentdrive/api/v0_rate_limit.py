"""Per-principal rate limit for the /v0 surface.

A generous default ceiling applied uniformly to every /v0 operation, keyed
by the hashed bearer token (falling back to client IP for unauthenticated
requests, e.g. constant 401s). This is an abuse guard, not a tier column —
the values are deliberately generous for a legitimately bursty agent and
only finite for an abuser.

Implementation: the `limits` library (the engine under `slowapi`, already a
dependency via `ratelimit.py`) with in-memory storage. In-memory means the
limit is enforced per process; behind multiple Cloud Run replicas each
enforces independently — acceptable for the launch value, with the storage
abstraction in place so a Redis backend is a drop-in later (§6.5).

The dependency is wired as ``dependencies=[Depends(enforce_v0_rate_limit)]``
on every v0 router, so one change gates the whole surface.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends, Request
from limits import RateLimitItemPerMinute, parse_many
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from ..config import settings
from ..core.ids import hash_key
from .v0_errors import V0ApiError

# One bucket per principal; per-minutes items below are the rolling guard.
_storage = MemoryStorage()
_strategy = FixedWindowRateLimiter(_storage)

# A single shared limit definition, kept cheap to parse per call.
_ITEMS = None


def _limits() -> list[RateLimitItemPerMinute]:
    global _ITEMS
    if _ITEMS is None:
        _ITEMS = list(parse_many(f"{settings.v0_rate_limit_per_minute}/minute"))
    return _ITEMS


def _principal_key(request: Request) -> str:
    """Rate-limit key: hashed bearer if present, else client IP.

    Mirrors ``ratelimit.bearer_or_ip``: hashing keeps the key short and
    avoids retaining a raw secret in the bucket dict; falling back to IP
    buckets unauthenticated abuse too."""
    auth = request.headers.get("authorization")
    if auth and auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ").strip()
        if token:
            return f"key:{hash_key(token)}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def _retry_after(item: RateLimitItemPerMinute, key: str) -> int:
    """Seconds until the key's window resets, or 60s if unknown."""
    try:
        stats = _storage.get_window_stats(item, key)
        remaining = stats.reset_time - time.time()
        if remaining > 0:
            return int(remaining) or 1
    except Exception:
        pass
    return 60


async def enforce_v0_rate_limit(request: Request) -> None:
    """Reject a /v0 request once the principal exceeds the per-minute budget.

    Gated by ``settings.v0_rate_limit_enabled`` (default ON for v0): with it
    False this short-circuits to allow and never touches the bucket, so the
    enforcement can be killed globally without redeploying routes."""
    if not settings.v0_rate_limit_enabled:
        return
    key = _principal_key(request)
    for item in _limits():
        if not _strategy.hit(item, key, cost=1):
            raise V0ApiError(
                429,
                "RATE_LIMITED",
                "rate limit exceeded; retry after the window resets",
                headers={"Retry-After": str(_retry_after(item, key))},
            )


RateLimit = Annotated[None, Depends(enforce_v0_rate_limit)]


def reset() -> None:
    """Drop all rate-limit buckets (test isolation only).

    The full suite drives hundreds of requests as the same test principal;
    without a per-test reset the per-minute budget leaks across tests and
    unrelated later tests start 429ing. The root `client` fixture calls this
    alongside the slowapi `limiter.reset()`.
    """
    _storage.reset()
