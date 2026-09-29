"""ASGI middleware.

`HostRedirectMiddleware` collapses configured alias hosts (`LEGACY_HOSTS`)
onto `LEGACY_REDIRECT_HOST` with a 308 Permanent Redirect (method-preserving
— `301` can silently downgrade POST→GET on older clients). It ships no host
of its own: unconfigured, it is inert. This is not the canonical TokenCanopy
machine API, share shell, or content-renderer routing seam.
Runs before all auth work so redirected requests never touch the DB.

`HostSurfaceMiddleware` binds each surface role to its own host. Host
selection happens before prefix selection because the trusted share shell
and isolated public renderer deliberately own overlapping permalink paths.

This module also held `RateLimitHeadersMiddleware` and
`EgressCountingMiddleware`. Both read `core.quota` and `core.usage`,
which are Hub's side of the control/data plane split (§3.1) — tiers are
product entitlement. They are in `archive/`, and v0 usage is two counter
columns on `drives` rather than a metering pipeline.
"""

import logging
import re
from dataclasses import dataclass
from ipaddress import ip_address

# Paths belonging to the trusted SHARE shell.
SHARE_PREFIXES: tuple[str, ...] = ("/a/", "/f/", "/s/", "/v/", "/share-static/")

# Paths belonging to the isolated anonymous artifact renderer. The permalink
# prefixes intentionally overlap with SHARE_PREFIXES; the request host selects
# the role before either tuple is consulted.
PUBLIC_RENDERER_PREFIXES: tuple[str, ...] = (
    "/a/",
    "/f/",
    "/s/",
    "/v/",
    "/public-static/",
)

# Compatibility name for callers outside the B1 host-routing seam. New
# bindings use the role-specific constants above.
PUBLIC_PREFIXES = PUBLIC_RENDERER_PREFIXES

# Paths belonging to the PRIVATE viewer surface (the console's iframe host,
# viewer*.tokencanopyusercontent.com). One prefix on purpose: the shell, its assets, and the
# credentialed doc/content endpoints all live under `/view/` so the host
# binding stays a prefix test.
VIEWER_PREFIXES: tuple[str, ...] = ("/view/",)

# Paths belonging to the MCP transport surface — ADR-0002's single-purpose
# `<product>.mcp.<zone>` host: the transport, and the path-scoped RFC 9728
# document that names it. NOT the root discovery document, which describes
# `/v0`, and not `/v0` itself: this host serves the transport and nothing else,
# which is the property that makes its bare origin safe for Hub to alias.
MCP_PREFIXES: tuple[str, ...] = ("/mcp", "/.well-known/oauth-protected-resource/mcp")

# Paths that answer on every host: liveness, and nothing else.
SHARED_PREFIXES: tuple[str, ...] = ("/health",)

log = logging.getLogger(__name__)

_REGISTERED_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True, slots=True)
class SurfaceBinding:
    """One role's host and the path prefixes that host is allowed to serve.

    `exclusive` (the default) means the prefixes answer on the bound host and
    nowhere else — the content surfaces, whose whole point is that their
    paths leave the API host. `exclusive=False` binds the host to its
    prefixes only: the host serves nothing else, but the prefixes keep
    answering on unbound hosts too. That is the MCP transport during the
    additive ADR-0002 migration, where `/mcp` must keep working on the API
    host while the new origin serves only `/mcp`.
    """

    name: str
    host: str
    prefixes: tuple[str, ...]
    private: bool = False
    exclusive: bool = True


class HostSurfaceMiddleware:
    """Bind each surface to its own host.

    Every host is a Cloud Run domain mapping onto one service, so by default
    every path answers on every host. This refuses the mismatches: a bound
    host serves only its own surface's prefixes, and a surface's prefixes
    answer only on their bound host.

    `surfaces` is a list of named `SurfaceBinding` values — a LIST, not a
    dict, because two configured surfaces on one host would silently collide
    as dict keys and unbind whichever was written first. Duplicate prefixes
    on different hosts are valid; duplicate configured hosts are not.

    `private=True` means the surface must never answer on an unbound host
    **once the deployment is multi-host**: if any surface is bound and this
    one is not, its prefixes are refused everywhere rather than answering
    everywhere. That closes the case where a real deployment ships the
    private viewer before its isolated origin exists and quietly serves
    credentialed artifact bytes from the main API host. `private=False`
    keeps the pre-binding behavior (the public read surface, whose content
    is public by definition).

    When NO surface is bound at all, the whole binding is inert — that is
    single-origin local dev and the test suite, where one host serves
    everything and there is no other origin for anything to leak onto.

    **Mount prefix.** `scope["path"]` is the RAW path; Starlette strips
    `root_path` only when it matches routes. The product-scoped deployment
    and bounded compatibility paths can present either `/drive/view/` or
    `/view/` to this process, so a raw `startswith("/view/")` test would let
    the mounted form bypass host isolation. Both forms are normalized before
    any prefix test. This is a server-routing invariant, not a browser-hosting
    prescription: Hub serves the console and has no `/drive*` edge route to
    AgentDrive.

    A refusal is a bare 404 — the same answer an unknown path gets — so the
    binding itself discloses nothing about what lives elsewhere.
    """

    def __init__(self, app, surfaces, mount_prefix: str = ""):
        self.app = app
        self.mount_prefix = mount_prefix.rstrip("/")
        self.surfaces: list[SurfaceBinding] = []
        seen: set[str] = set()
        for raw_binding in surfaces:
            # Keep older internal test wrappers source-compatible while the
            # application seam itself moves to named bindings. The routing
            # model below stores only SurfaceBinding values.
            if isinstance(raw_binding, SurfaceBinding):
                binding = raw_binding
            else:
                host, prefixes, private = raw_binding
                binding = SurfaceBinding(
                    "viewer" if private else "share",
                    host,
                    tuple(prefixes),
                    private,
                )
            normalized = _normalize_binding_host(binding.host)
            if not normalized:
                # Unconfigured: a private surface is refused everywhere
                # (below); a public one keeps answering everywhere.
                self.surfaces.append(
                    SurfaceBinding(
                        binding.name,
                        "",
                        tuple(binding.prefixes),
                        binding.private,
                        binding.exclusive,
                    )
                )
                continue
            if normalized in seen:
                raise RuntimeError(
                    f"two surfaces are bound to the same host {normalized!r}; "
                    "each surface needs its own origin or neither is isolated"
                )
            seen.add(normalized)
            self.surfaces.append(
                SurfaceBinding(
                    binding.name,
                    normalized,
                    tuple(binding.prefixes),
                    binding.private,
                    binding.exclusive,
                )
            )

    def _candidates(self, path: str) -> tuple[str, ...]:
        """The path as this process may see it, prefixed and bare.

        Both are tested because the product-scoped deployment and bounded
        compatibility paths legitimately expose prefixed and bare forms.
        """
        if self.mount_prefix and (
            path == self.mount_prefix or path.startswith(f"{self.mount_prefix}/")
        ):
            stripped = path[len(self.mount_prefix) :] or "/"
            return (path, stripped)
        return (path,)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.surfaces:
            await self.app(scope, receive, send)
            return
        bound_hosts = {binding.host for binding in self.surfaces if binding.host}
        if not bound_hosts:
            # Single-origin deployment (local dev, tests): nothing to bind
            # against, and no second origin for a surface to leak onto.
            await self.app(scope, receive, send)
            return
        candidates = self._candidates(scope.get("path", ""))
        if any(candidate in SHARED_PREFIXES for candidate in candidates):
            await self.app(scope, receive, send)
            return
        host = _host_from_scope(scope)
        if not host:
            await _plain_404(send)
            return

        # Select by Host first. Prefixes overlap by design between the share
        # shell and public renderer, so choosing an owner by path first would
        # always give the first binding both roles' traffic.
        selected = next(
            (binding for binding in self.surfaces if binding.host == host),
            None,
        )
        if selected is not None:
            if not any(c.startswith(selected.prefixes) for c in candidates):
                # A bound host answers its own surface and nothing else.
                await _plain_404(send)
                return
            if selected.name == "share" and any(
                key.lower() == b"cookie" for key, _ in scope.get("headers", ())
            ):
                # Defense-in-depth and an edge deployment probe. Never decode,
                # reflect, or log the header value.
                await _plain_404(send)
                return
            scope.setdefault("state", {})["surface_role"] = selected.name
            await self.app(scope, receive, send)
            return

        owners = [
            binding
            for binding in self.surfaces
            if any(c.startswith(binding.prefixes) for c in candidates)
        ]
        if any(
            (binding.host and binding.exclusive) or binding.private for binding in owners
        ):
            # Bound surface paths answer only on the selected host. An unbound
            # private surface also answers nowhere once any host is configured.
            # A NON-exclusive binding (the MCP transport) keeps answering here:
            # its host is bound to its prefixes, not the other way round.
            await _plain_404(send)
            return
        await self.app(scope, receive, send)


class ServiceSurfaceMiddleware:
    """Restrict a dedicated Cloud Run service to one surface role.

    Host binding still selects the configured custom origin. This additional
    process boundary ensures the service's unavoidable ``run.app`` origin
    cannot expose authenticated API, viewer, share-shell, or MCP routes.
    """

    def __init__(self, app, role: str = "all"):
        self.app = app
        self.role = role

    async def __call__(self, scope, receive, send):
        if self.role == "all" or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in SHARED_PREFIXES:
            await self.app(scope, receive, send)
            return
        if self.role == "public-renderer" and path.startswith(
            PUBLIC_RENDERER_PREFIXES
        ):
            scope.setdefault("state", {})["surface_role"] = "public-renderer"
            await self.app(scope, receive, send)
            return
        await _plain_404(send)


def _normalize_binding_host(host: str) -> str:
    """Validate and canonicalize a configured bare host (never a host:port)."""
    normalized = host.strip().lower()
    if not normalized:
        return ""
    if normalized.startswith("[") or any(char in normalized for char in "/?#@"):
        raise RuntimeError(
            f"surface host {host!r} must be a bare hostname — a port "
            "or path never matches the Host header and would leave "
            "the surface unbound"
        )
    try:
        return ip_address(normalized).compressed
    except ValueError:
        pass
    registered_host = _canonical_registered_host(normalized)
    if ":" in normalized or registered_host is None:
        raise RuntimeError(
            f"surface host {host!r} must be a bare hostname — a port "
            "or path never matches the Host header and would leave "
            "the surface unbound"
        )
    return registered_host


async def _plain_404(send) -> None:
    body = b'{"error":{"code":"NOT_FOUND","message":"not found"}}'
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class HostRedirectMiddleware:
    """308-redirect configured alias hosts onto their redirect target.

    The hosted product's retiring alias family (`adrv.ai`, `adrive.run` and
    the older deploy hostname, all landing on `agentdrive.run`) is expressed
    through `LEGACY_HOSTS` / `LEGACY_REDIRECT_HOST`; the middleware itself
    knows no domain. Constructed bare, it reads those settings on every HTTP
    request — a few string operations — so the environment, not the module,
    decides which hosts are aliases, and a standalone install with none
    configured forwards every Host untouched. Explicit `canonical_host` /
    `legacy_hosts` arguments pin a fixed set instead (unit tests use that).

    308 (not 301) because some HTTP clients silently downgrade POST to
    GET when following 301; 308 is the explicit method-preserving
    permanent redirect. New public API traffic should hit the canonical
    origin — any POST that lands on an alias is either a compatibility
    client, a misconfigured client, or a typo, and either way we want the
    response to survive the redirect intact so the caller sees a real error
    code, not a silent method-flip.

    Installed as the outermost middleware so we never spend DB pool /
    quota work on a request we're going to redirect."""

    def __init__(self, app, canonical_host: str | None = None, legacy_hosts=None):
        self.app = app
        if (canonical_host is None) != (legacy_hosts is None):
            raise ValueError("canonical_host and legacy_hosts must be given together")
        self._static: tuple[str, frozenset[str]] | None = None
        if canonical_host is not None:
            self._static = (
                canonical_host.lower(),
                frozenset(h.lower() for h in legacy_hosts),
            )

    def _resolve(self) -> tuple[str, frozenset[str]]:
        if self._static is not None:
            return self._static
        from .config import settings

        return settings.legacy_redirect_host, settings.legacy_host_set

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        canonical_host, legacy_hosts = self._resolve()
        host = _host_from_scope(scope)
        if not legacy_hosts or host not in legacy_hosts:
            await self.app(scope, receive, send)
            return

        # Build canonical URL preserving raw path + query so e.g.
        # `adrv.ai/v0/agents/x?foo=bar` lands at
        # `agentdrive.run/v0/agents/x?foo=bar`. raw_path preserves
        # percent-encoding the user supplied; falling back to the
        # decoded `path` is fine for the asciionly routes we serve.
        raw_path = scope.get("raw_path") or scope["path"].encode()
        query_string = scope.get("query_string", b"")
        location = b"https://" + canonical_host.encode() + raw_path
        if query_string:
            location += b"?" + query_string

        await send(
            {
                "type": "http.response.start",
                "status": 308,
                "headers": [
                    (b"location", location),
                    (b"cache-control", b"public, max-age=86400"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})


def _host_from_scope(scope) -> str:
    """Return one canonical Host value, or empty for a malformed request."""
    values = [v for k, v in scope.get("headers", ()) if k.lower() == b"host"]
    if len(values) != 1:
        return ""
    value = values[0].decode("latin-1").strip().lower()
    if not value:
        return ""

    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            return ""
        literal = value[1:close]
        suffix = value[close + 1 :]
        if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
            return ""
        try:
            parsed = ip_address(literal)
        except ValueError:
            return ""
        return parsed.compressed if parsed.version == 6 else ""

    if value.count(":") > 1:
        # IPv6 in an HTTP Host header must use brackets.
        return ""
    hostname, separator, port = value.partition(":")
    if separator and not port.isdigit():
        return ""
    try:
        return ip_address(hostname).compressed
    except ValueError:
        return _canonical_registered_host(hostname) or ""


def _canonical_registered_host(host: str) -> str | None:
    """Canonicalize an ASCII/IDNA registered host without accepting junk."""
    if host.endswith(".."):
        return None
    host = host.removesuffix(".")
    if not host or any(char.isspace() for char in host):
        return None
    try:
        canonical = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if len(canonical) > 253 or not all(
        _REGISTERED_HOST_LABEL.fullmatch(label) for label in canonical.split(".")
    ):
        return None
    return canonical
