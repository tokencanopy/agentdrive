"""Per-IP rate limiter, backed by an in-memory token bucket.

In-memory means limits are per-process; behind multiple Cloud Run
instances each replica enforces independently. That's enough to deter
casual abuse (email spam, brute force) — sophisticated attacks need a
shared store (Redis, Memorystore) in a future iteration.

Two key functions:

  - `get_remote_address` (default) — keys by client IP. Used on
    unauthenticated routes like /auth/magic-link.
  - `bearer_or_ip` — keys by the *hashed* bearer token from the
    `Authorization` header, falling back to client IP if absent.
    Used on /v0/* so one principal can't run up usage from many IPs,
    and so the limit applies before any DB lookup -- slowapi middleware
    fires before the FastAPI dependency tree.

`viewer_key` and `poll_key` went with the rendered viewer routes they
keyed; see `archive/render/`.
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from .core.ids import hash_key


def bearer_or_ip(request: Request) -> str:
    """Rate-limit key: hashed bearer token if present, else client IP.

    Hashing isn't for storage (we never persist this) — it just keeps
    the key short and avoids retaining raw secrets inside slowapi's
    in-memory bucket dict. Falling back to IP means even unauthenticated
    abuse (constant 401s) still gets bucketed.
    """
    auth = request.headers.get("authorization")
    if auth and auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ").strip()
        if token:
            return f"key:{hash_key(token)}"
    return f"ip:{get_remote_address(request)}"


# `key_style="endpoint"` buckets by the route function name (e.g.
# `agentdrive.api.routes.put_file`) instead of the request URL path.
# Without this, slowapi's default `"url"` style would give each unique
# file path its own bucket — an attacker could upload to 100 different
# paths and bypass a 100/hour write limit completely.
limiter = Limiter(key_func=get_remote_address, key_style="endpoint")
