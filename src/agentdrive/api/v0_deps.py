"""Shared FastAPI dependencies for /v0 routes (§3, §6.1).

Under ``AUTH_MODE=hub`` — the hosted product — Hub is the only authorization
server, and this module verifies Hub-issued bearers offline against Hub's
published JWKS. The JWKS is a *document*, not a connection, so verification
is pure CPU on the request path — but fetching it is network I/O that must
never run inside the event loop, and the document is only as fresh as the
last fetch. That lifecycle lives here:

  * the app lifespan primes the JWKS once at boot (worker thread, non-fatal);
  * a token whose ``kid`` is not in the cached document (Hub rotated) triggers
    ONE rate-limited, single-flight re-fetch and one verification retry;
  * while no JWKS is available at all, /v0 auth answers 503 AUTH_UNAVAILABLE
    (our unavailability) instead of blaming the caller's token.

Under ``AUTH_MODE=local`` — a self-hosted install with no Hub — there is no
issuer and no verifier at all. The bearer is an opaque ``adk_…`` API key
resolved against ``local_api_keys`` (open-source design §4.2, amended
2026-09-21); anything else is simply refused, since nothing in that
deployment signs a token. A database failure during resolution is the same
503 AUTH_UNAVAILABLE an unreachable JWKS is: our boundary, not their
credential.

The two modes never mix. An ``adk_`` bearer under ``hub`` is refused exactly
as any other non-JWT is, and hosted behaviour is byte-identical to before
this module learned about keys.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from jwt import PyJWKClient

from ..config import settings
from ..identity.actor import V0ActorContext
from ..identity.product_token import InvalidToken, ProductTokenVerifier, UnknownKeyError
from .v0_errors import V0ApiError

log = logging.getLogger(__name__)

# Retry-After (seconds) on the 503 AUTH_UNAVAILABLE the /v0 boundary answers
# while Hub's JWKS is unreachable.
AUTH_UNAVAILABLE_RETRY_AFTER = 30

# Floor (seconds, monotonic) between JWKS re-fetches driven by unknown-kid
# tokens. A flood of bad-kid tokens — the signature of a rotation, or of a
# client minting tokens this deployment should not accept — must not stampede
# Hub; one fetch per window is plenty, and a single fetch repairs the cache.
_JWKS_REFRESH_GATE_S = 30.0


class _JwksStore:
    """Cached Hub JWKS + verifier for one (issuer, audience) pair.

    ``refresh`` is the only path that touches the network, and it is
    single-flight (an :class:`asyncio.Lock`) and rate-limited (a monotonic
    gate) so concurrent bad-kid requests collapse into one fetch.
    """

    def __init__(self, *, issuer: str, audience: str) -> None:
        self._issuer = issuer
        self._audience = audience
        self._verifier: ProductTokenVerifier | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_fetch: float | None = None

    @property
    def verifier(self) -> ProductTokenVerifier | None:
        """The current verifier, or None when no JWKS has been fetched."""
        return self._verifier

    async def prime(self) -> None:
        """Lifespan warm: one fetch attempt, never fatal.

        A Hub outage at boot must not take the app down — /v0 auth answers
        503 until a later request-path refresh succeeds. ``refresh`` already
        swallows fetch errors; this only guards against a verifier build
        failing on a document with no usable keys.
        """
        try:
            await self.refresh()
        except Exception:
            log.error("hub JWKS fetch failed at startup", exc_info=True)

    async def refresh(self, *, force: bool = False) -> bool:
        """Re-fetch the JWKS (worker thread), single-flight and rate-limited.

        Returns True when a verifier is usable afterwards — a fresh fetch
        succeeded, or the cached one is still current and within the gate.
        Returns False when nothing is usable (fetch failed, or the gate is
        closed without a cached verifier). Never raises.
        """
        async with self._refresh_lock:
            within_gate = (
                self._last_fetch is not None
                and time.monotonic() - self._last_fetch < _JWKS_REFRESH_GATE_S
            )
            if within_gate and not force:
                return self._verifier is not None
            try:
                jwks = await asyncio.to_thread(_fetch_jwks, self._issuer)
                self._verifier = ProductTokenVerifier(
                    issuer=self._issuer, audience=self._audience, jwks=jwks
                )
                self._last_fetch = time.monotonic()
                return True
            except Exception as e:
                # A failed fetch also stamps the gate: a Hub that is down stays
                # down, and hammering it at request rate helps no one.
                self._last_fetch = time.monotonic()
                log.warning("hub JWKS fetch failed: %s", e)
                return False


# Per-(issuer, audience) stores, so a settings change (tests, reconfig) is
# reflected on the next lookup instead of reusing a stale verifier.
#
# TWO audiences share this map since the 2026-08-28 MCP audience split: the
# public `/v0` product audience, and the `/mcp` audience the private internal
# ingress verifies. They are separate verifiers precisely so a token minted
# for one is refused by the other -- that refusal is the boundary.
_jwks_stores: dict[tuple[str, str], _JwksStore] = {}


def _store(audience: str | None = None) -> _JwksStore:
    key = (settings.hub_issuer, audience or settings.hub_product_audience)
    store = _jwks_stores.get(key)
    if store is None:
        store = _JwksStore(issuer=key[0], audience=key[1])
        _jwks_stores[key] = store
    return store


def reset() -> None:
    """Drop all cached JWKS state. Test seam: the next request re-fetches."""
    _jwks_stores.clear()


async def prime_jwks(audience: str | None = None) -> None:
    """Warm the Hub JWKS at startup. Called from the app lifespan.

    `audience` selects which verifier to build: the public app primes the
    product audience, the internal ingress primes the `/mcp` one.

    A no-op under `AUTH_MODE=local`: there is no issuer to fetch from, and a
    standalone install must not make a network call at boot to an origin it
    was never configured with.
    """
    if settings.auth_mode == "local":
        return
    await _store(audience).prime()


def _fetch_jwks(issuer: str) -> dict:
    """The issuer's JWKS as a dict.

    Hub-mode only: fetched from AUTH_JWKS_URL when set, else from
    ``<issuer>/jwks`` (Hub is a panva oidc-provider whose endpoints live
    under the issuer). product_token has no fetch helper — its verifier takes
    a document — so this is the one place that produces one. Local mode never
    reaches here: it has no issuer and resolves opaque keys instead.
    """
    url = settings.auth_jwks_url.strip() or f"{issuer}/jwks"
    return PyJWKClient(url).fetch_data()


async def resolve_api_key(token: str) -> V0ActorContext:
    """Resolve an opaque local API key, or refuse the request.

    `AUTH_MODE=local` only. Every bearer this deployment sees comes through
    here, because nothing in a standalone install signs a token: a value that
    is not a live `adk_…` key is a 401 with the same challenge a bad JWT
    would get, and there is deliberately no second path to fall through to.

    A Postgres failure is 503 AUTH_UNAVAILABLE, never a 500 and never
    fail-open (§6): the credential store being unreachable is our boundary
    being down, exactly as an unreachable JWKS is under Hub.

    The SHAPE check runs before the pool is touched. Hub-mode verification is
    pure CPU, so an unauthenticated flood costs this deployment nothing; here
    every bearer would otherwise borrow a Postgres connection before anything
    looked at it, and the cheapest bad bearer to send is one that was never
    going to be a key at all.
    """
    from ..db import conn
    from ..identity.api_keys import looks_like_key, resolve

    if not looks_like_key(token):
        raise _invalid_token()
    try:
        async with conn() as c:
            actor = await resolve(c, token)
    except Exception as exc:
        log.warning("local API key store unavailable: %s", type(exc).__name__)
        raise _unavailable() from None
    if actor is None:
        raise _invalid_token()
    return actor


async def check_local_credentials() -> None:
    """Boot-time refusal for AUTH_MODE=local: the credential tables exist.

    §4.2's "Failure semantics": with no key file and no fetch, the only
    boot-time auth check local mode has left is that the migrations ran. It
    matters because the alternative is silent — `resolve_api_key` maps an
    `UndefinedTableError` to 503 AUTH_UNAVAILABLE like any other database
    failure, so an install that skipped `apply_schema` would answer 503 to
    every request forever while `/health` stayed green.

    Called from both lifespans AFTER the pool is open, and a no-op under Hub,
    where Hub owns credentials and these tables are never read.
    """
    if settings.auth_mode != "local":
        return
    from ..db import conn

    async with conn() as c:
        # `LIMIT 0`: the tables' existence is the whole question, and reading
        # a credential row at boot would be reading one for no reason.
        await c.execute("SELECT 1 FROM local_api_keys LIMIT 0")
        await c.execute("SELECT 1 FROM local_principals LIMIT 0")
    log.info("local API keys ready")


def _bearer_from(headers: Mapping[str, str | None]) -> str | None:
    auth = (headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


def _challenge(error: str | None = None) -> dict[str, str]:
    """RFC 6750 §3 WWW-Authenticate challenge for the /v0 surface.

    Advertises where the RFC 9728 protected-resource metadata lives, derived
    from the SAME origin v0_discovery uses. A missing credential gets a bare
    challenge (no error attribute); a present-but-invalid one adds
    `error="invalid_token"` (§3: a challenge for a rejected request carries
    the error code; a request with no credentials does not).
    """
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    metadata = f'{origin}/.well-known/oauth-protected-resource'
    if error is None:
        return {"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'}
    return {
        "WWW-Authenticate": f'Bearer error="{error}", resource_metadata="{metadata}"'
    }


def _unavailable() -> V0ApiError:
    """503 for OUR auth boundary being down (Hub JWKS unreachable).

    The caller presented (or not) a token correctly; the failure is upstream
    of them. Retry-After invites a bounded retry instead of a mystery 401.
    """
    return V0ApiError(
        503,
        "AUTH_UNAVAILABLE",
        "token verification is temporarily unavailable",
        headers={"Retry-After": str(AUTH_UNAVAILABLE_RETRY_AFTER)},
    )


def _invalid_token() -> V0ApiError:
    """401 for a bearer Hub did not issue / no longer signs."""
    return V0ApiError(
        401, "AUTHENTICATION_REQUIRED", "invalid token",
        headers=_challenge(error="invalid_token"),
    )


async def _verify(token: str, store: _JwksStore) -> V0ActorContext:
    """Verify ``token`` against the store, refreshing once on unknown kid.

    A verifier is guaranteed to exist when this is called (callers check
    ``store.verifier`` first). An unknown ``kid`` — most likely Hub rotated —
    triggers one rate-limited re-fetch and one retry; if the retry still
    cannot verify the token it is rejected like any other invalid token,
    NOT a 503 (we hold a JWKS; the token simply is not signed by it).
    """
    try:
        claims = store.verifier.verify(token)
    except UnknownKeyError:
        await store.refresh()
        try:
            claims = store.verifier.verify(token)
        except InvalidToken:
            raise _invalid_token() from None
    except InvalidToken:
        raise _invalid_token() from None
    return V0ActorContext.from_claims(claims)


async def resolve_actor(
    authorization: str | None, *, audience: str | None = None
) -> V0ActorContext:
    """Verify a bearer for one audience and return its actor context.

    Exported so the private internal ingress
    (`agentdrive.internal_ingress`) can reuse EXACTLY this verification with
    its own audience rather than reimplementing it. The audience is the only
    difference between the two boundaries; everything else -- the JWKS
    lifecycle, the unknown-kid retry, the 503-vs-401 distinction, the claim
    checks -- has to be identical, and copying it is how it stops being.
    """
    token = _bearer_from({"authorization": authorization})
    if not token:
        raise V0ApiError(
            401, "AUTHENTICATION_REQUIRED", "missing bearer token",
            headers=_challenge(),
        )
    if settings.auth_mode == "local":
        # One credential, every surface (§4.2). The `audience` argument is
        # deliberately ignored here rather than removed: the hosted
        # `/v0`-versus-`/mcp` split exists because an MCP session token is
        # obtained through an OAuth consent flow with its own resource, and a
        # self-hoster who minted their own key IS the principal. There is no
        # delegated credential to keep apart, so there is nothing to split.
        return await resolve_api_key(token)
    store = _store(audience)
    if store.verifier is None:
        # No JWKS yet — this request is the nudge to fetch. Until one lands,
        # the boundary is unavailable (ours, not the caller's).
        await store.refresh()
    if store.verifier is None:
        raise _unavailable()
    return await _verify(token, store)


async def v0_actor(authorization: str | None = Header(default=None)) -> V0ActorContext:
    return await resolve_actor(authorization)


async def resolve_drive(
    drive_id: str, actor: Annotated[V0ActorContext, Depends(v0_actor)]
) -> V0ActorContext:
    """Resolve a ``/v0/drives/{drive_id}`` route param against the actor.

    Minimal drive-scope resolver for this slice: it confirms the requested
    drive is inside the actor's workspace. Deeper resource-level gating is
    layered in later slices.
    """
    if not drive_id.startswith("drv_"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")
    return actor


def known_params(*allowed: str) -> Callable[..., Any]:
    """Reject any query parameter outside ``allowed`` (§6.3).

    Every /v0 route attaches this via the route decorator's
    ``dependencies=[...]``, carrying exactly the filter set the operation
    declares. It depends on ``v0_actor`` itself, so authentication resolves
    (401) before the query check — an unauthenticated request is never
    answered with 400.
    """

    async def _dep(
        request: Request,
        actor: Annotated[V0ActorContext, Depends(v0_actor)],
    ) -> None:
        extra = set(request.query_params) - set(allowed)
        if extra:
            raise V0ApiError(
                400, "INVALID_QUERY", f"unknown query parameter(s): {sorted(extra)}"
            )

    return _dep


def precondition_http(exc: Any) -> V0ApiError:
    """Map a core :class:`PreconditionError` to the uniform wire envelope.

    A stale 412 carries the resource's current revision as
    ``details.current_revision`` — the If-Match value the client needs to
    retry without a second read. A 428 (If-Match absent) carries no details.
    """
    details = None
    if exc.status == 412 and getattr(exc, "current_revision", None) is not None:
        details = {"current_revision": exc.current_revision}
    return V0ApiError(exc.status, exc.code, exc.message, details=details)
