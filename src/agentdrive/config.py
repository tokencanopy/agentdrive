import re
from ipaddress import ip_address
from typing import Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from agentdrive.feature_settings import FeatureSettings

# Limits surfaced in the public API. Update README.md when these change.
# v0.8: per-artifact size cap moved to `tiers.max_artifact_bytes` (see
# `core/quota`). Path length is still global — it's a validation rule,
# not a quota.
MAX_PATH_LENGTH = 256

# Sentinel value previously shipped as a default for SESSION_SECRET. We now
# reject it explicitly so deployments cannot accidentally keep the dev value.
_INSECURE_DEFAULT_SESSION_SECRET = "dev-secret-change-me-in-production"

_REGISTERED_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _canonical_content_host(host: str) -> tuple[str, bool]:
    """Return a canonical hostname plus whether it is loopback.

    Content surfaces are configured as exact origins, so a registered name
    must be valid after IDNA conversion. Removing its optional DNS root dot
    and lowercasing the A-label prevents equivalent host spellings from being
    configured as supposedly separate surfaces.
    """
    if host.endswith(".."):
        raise ValueError("content origin hostname must not contain multiple trailing dots")
    host = host.removesuffix(".")
    if not host:
        raise ValueError("content origin must include a hostname")

    try:
        parsed_ip = ip_address(host)
    except ValueError:
        pass
    else:
        return parsed_ip.compressed, parsed_ip.is_loopback

    try:
        canonical_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("content origin hostname is not valid IDNA") from exc
    if len(canonical_host) > 253 or not all(
        _REGISTERED_HOST_LABEL.fullmatch(label) for label in canonical_host.split(".")
    ):
        raise ValueError("content origin hostname has invalid registered-name labels")
    # RFC 6761 reserves `localhost` and valid subdomains for loopback. This
    # classification follows IDNA and DNS-label validation so malformed text
    # ending in `.localhost` cannot bypass exact-origin validation.
    loopback = canonical_host == "localhost" or canonical_host.endswith(".localhost")
    return canonical_host, loopback


def _bare_hostname(host: str) -> str | None:
    """The canonical spelling of a bare hostname, or None if it is not one.

    Reuses the content-origin host rules: IDNA-valid registered labels or an
    IP literal, no scheme, port, path, wildcard or whitespace. Used by the
    legacy-alias settings, whose values are compared to the Host header."""
    if not host or any(c in host for c in "/?#@: *\t\r\n"):
        return None
    try:
        canonical, _ = _canonical_content_host(host)
    except ValueError:
        return None
    return canonical


def validate_exact_origin(value: str, *, setting: str) -> str:
    """Require a bare, exact http(s) origin (B3 §9): scheme + host [+ port],
    no path/query/fragment/credentials, no wildcard, https outside loopback.
    Returns the canonical ``scheme://netloc`` spelling.

    THE shared origin validator for the direct-transfer boundary: `Settings`
    uses it for the canonical browser origin and both provider endpoints,
    and `storage_transfers.XmlTransferStorage` uses the same function in its
    constructor — so a non-Settings caller cannot weaken the boundary
    (packet-2 rereview should-fix)."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{setting} must be an absolute http(s) origin")
    if "*" in value:
        raise ValueError(f"{setting} must be an exact origin, never a wildcard")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{setting} must not include credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"{setting} must not include a path, query, or fragment")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError(f"{setting} port must be numeric") from exc
    _, loopback = _canonical_content_host(parts.hostname)
    if parts.scheme == "http" and not loopback:
        raise ValueError(f"{setting} must use https outside loopback")
    return f"{parts.scheme}://{parts.netloc}"


def validate_loopback_http_origin(value: str, *, setting: str) -> str:
    """Require an explicit, plaintext HTTP origin on the loopback interface.

    The hosted MCP is a same-container sidecar. Keeping this setting to a
    loopback origin prevents a deployment typo from turning the FastAPI route
    into an arbitrary outbound proxy, while an explicit port avoids silently
    targeting a different local service.
    """
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{setting} must be a loopback HTTP origin") from exc
    if parts.scheme != "http" or not hostname:
        raise ValueError(f"{setting} must be a loopback HTTP origin")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{setting} must not include credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"{setting} must not include a path, query, or fragment")
    if port is None:
        raise ValueError(f"{setting} must include an explicit port")
    canonical_host, loopback = _canonical_content_host(hostname)
    if not loopback:
        raise ValueError(f"{setting} must target a loopback host")
    rendered_host = f"[{canonical_host}]" if ":" in canonical_host else canonical_host
    return f"http://{rendered_host}:{port}"


# THE authoritative hard ceiling on a signed-download-capability TTL (B3
# §5.7: short-lived by design). One constant on purpose (packet-4 follow-up
# from the packet 2/3 reviews): boot validation of
# DIRECT_DOWNLOAD_CAPABILITY_TTL_SECONDS below and the signer boundary in
# ``storage_transfers`` both fence against THIS value, so the two bounds
# cannot drift apart.
MAX_DOWNLOAD_CAPABILITY_TTL_SECONDS = 3600


class Settings(FeatureSettings):
    database_url: str
    database_pool_max: int = 10
    # Which object store holds artifact bytes. "gcs" is the hosted product's
    # (and needs GCS_BUCKET); "fs" is a directory on this host (and needs
    # STORAGE_FS_ROOT), the self-hosted install's store. The choice is made
    # once per process; `storage/factory.py` builds the store from it.
    storage_backend: str = "gcs"
    storage_fs_root: str = ""
    # Required when the backend is "gcs"; ignored otherwise (validated below,
    # so a filesystem install boots without inventing a bucket name).
    gcs_bucket: str = ""
    # The project the GCS client is constructed with. Irrelevant under the
    # emulator and under Cloud Run's ambient credentials, which is why the
    # historical literal is the default.
    gcs_project: str = "agentdrive-local"
    service_surface_role: str = "all"
    drive_limits_claim_required: bool = False
    drive_limit_safety_storage_bytes_drive: int = 100 * 1024**3
    drive_limit_safety_storage_bytes_workspace: int = 500 * 1024**3
    drive_limit_safety_upload_bytes_hour_principal: int = 100 * 1024**3
    drive_limit_safety_upload_bytes_hour_workspace: int = 250 * 1024**3
    drive_limit_safety_download_bytes_day_workspace: int = 500 * 1024**3
    drive_limit_safety_download_bytes_month_workspace: int = 2 * 1024**4
    drive_limit_safety_public_share_bytes_day: int = 50 * 1024**3
    max_file_bytes: int = 1024**3
    usage_dimension_hmac_secret: SecretStr = SecretStr("local-development-only")
    public_usage_limit_mode: str = "shadow"
    upload_byte_limit_mode: str = "enforce"
    private_download_limit_mode: str = "enforce"
    public_workspace_bytes_day: int = 25 * 1024**3
    share_default_ttl_seconds: int = 7 * 24 * 3600
    share_max_ttl_seconds: int = 30 * 24 * 3600
    share_max_active_workspace: int = 100
    share_max_active_resource: int = 10
    gcs_emulator_host: str | None = None

    @field_validator(
        "drive_limit_safety_storage_bytes_drive",
        "drive_limit_safety_storage_bytes_workspace",
        "drive_limit_safety_upload_bytes_hour_principal",
        "drive_limit_safety_upload_bytes_hour_workspace",
        "drive_limit_safety_download_bytes_day_workspace",
        "drive_limit_safety_download_bytes_month_workspace",
        "drive_limit_safety_public_share_bytes_day",
        "public_workspace_bytes_day",
    )
    @classmethod
    def _drive_limit_safety_ceiling_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("AgentDrive limit safety ceilings must be positive")
        return value

    @field_validator(
        "public_usage_limit_mode",
        "upload_byte_limit_mode",
        "private_download_limit_mode",
    )
    @classmethod
    def _usage_limit_mode_closed(cls, value: str) -> str:
        if value not in {"off", "shadow", "enforce"}:
            raise ValueError("usage limit mode must be off, shadow, or enforce")
        return value

    @field_validator("database_pool_max")
    @classmethod
    def _database_pool_max_bounded(cls, value: int) -> int:
        if value < 1 or value > 50:
            raise ValueError("database_pool_max must be between 1 and 50")
        return value

    @field_validator("service_surface_role")
    @classmethod
    def _service_surface_role_closed(cls, value: str) -> str:
        if value not in {"all", "public-renderer"}:
            raise ValueError("service_surface_role must be all or public-renderer")
        return value
    # Signed direct-from-GCS downloads (large-download-design.md). Artifacts at
    # or above `download_signed_min_bytes` are offered as a short-lived signed
    # GCS URL (client fetches GCS directly) instead of proxy-streaming through
    # Cloud Run. Falls back to the proxy when signing is unavailable (emulator
    # or missing IAM signBlob). `download_url_ttl_s` is the signed-URL lifetime.
    download_signed_min_bytes: int = 16 * 1024 * 1024  # 16 MiB
    download_url_ttl_s: int = 900  # 15 min
    # Compatibility/process base for archived absolute-URL helpers and the
    # mount-prefix startup invariant. It is not the Hub console route. Once
    # SHARE_BASE_URL / PUBLIC_CONTENT_BASE_URL are configured, supported
    # public permalinks and renderer URLs do not use this value.
    public_base_url: str = "http://localhost:8000"
    # Authenticated Token Canopy workspace origin used by public artifact
    # viewers for the owner/member return link. This is a navigation target,
    # not an API origin or an authorization bridge.
    console_base_url: str = "http://localhost:3000"
    # FastAPI root path for the product-scoped API deployment. Paired with
    # public_base_url, which must END with the prefix when one is set (startup
    # self-check in app.py); that compatibility constraint does not mean this
    # service owns the human console.
    mount_prefix: str = ""
    # Where the credential `/v0` accepts comes from. "hub" (the default) is
    # the Token Canopy Hub, the hosted product's only authorization server;
    # "hub-oidc" is accepted as an alias for one release. "local" is a
    # self-hosted install (2026-09-19 open-source design §4.2, amended
    # 2026-09-21): there is NO issuer at all — the credential is an opaque
    # `adk_…` API key minted by `python -m agentdrive.keys` and resolved
    # against `local_api_keys` on every request. The value is closed by
    # `_auth_mode_closed` below, which replaced the old boot-time check: a
    # misconfiguration is a Settings error, never a listening socket.
    auth_mode: str = "hub"
    # The issuer whose tokens are accepted. A HUB-MODE setting: Hub's OIDC
    # issuer (a panva oidc-provider, whose JWKS is at <issuer>/jwks). Read
    # from AUTH_ISSUER, or HUB_ISSUER for one release of alias. Local mode
    # has no issuer and ignores it.
    hub_issuer: str = Field(
        default="http://localhost:8080/oidc",
        validation_alias=AliasChoices("AUTH_ISSUER", "HUB_ISSUER", "hub_issuer"),
    )
    # Where the issuer's JWKS is fetched from. A HUB-MODE setting: empty keeps
    # the <issuer>/jwks convention, and setting it decouples verification from
    # that convention — which is what a later bring-your-own-OIDC mode (§4.11)
    # builds on. Refused under "local", which verifies nothing.
    auth_jwks_url: str = ""
    # RETIRED (#723's local JWT issuer; §4.2 as amended 2026-09-21). Nothing
    # reads it: a standalone install holds no signing key. It is still
    # DECLARED so an upgrade that leaves it set in the environment or a `.env`
    # is refused by name below rather than silently ignored — `extra="ignore"`
    # would drop an undeclared key and leave the operator believing their key
    # file was in effect.
    auth_local_signing_key_file: str = ""
    # Audience required on the PRODUCT TOKENS the /v0 surface verifies (the
    # RFC 9728 protected resource's audience, contract §3). Hub mints these
    # tokens for the agent-facing origin, and a token minted for another
    # product (aud=e2a) must be rejected. Validated as an absolute http(s) URL — the audience
    # IS the origin where the API is served.
    hub_product_audience: str = "https://drive.tokencanopy.com"
    # Audience required on the tokens the PRIVATE internal ingress verifies —
    # the hosted MCP's own resource, `<product audience>/mcp`.
    #
    # A SEPARATE audience since the 2026-08-28 security remediation. It used
    # to be the same string as `hub_product_audience`, which made a token
    # minted for a coding agent's MCP session a fully valid `/v0` product
    # token: the reviewed MCP tool surface was not a boundary, because any
    # holder could call `/v0` directly and reach the `drives:write` operations
    # the MCP never exposes.
    #
    # Empty derives it from `hub_product_audience`, so no deployment has to
    # keep two strings in step. A model validator refuses a value that is not
    # exactly that origin plus `/mcp`.
    hub_mcp_audience: str = ""
    # Local/transition-only loopback ingress. Production uses the network
    # mount and dedicated workload identity below.
    internal_ingress_host: str = "127.0.0.1"
    internal_ingress_port: int = 8082
    # Exact product resource advertised to agent runtimes in RFC 9728
    # discovery and OpenAPI `servers[0]`. Production is
    # `https://drive.tokencanopy.com`; staging uses its separately configured
    # staging resource. Empty falls back to `public_base_url` only for local
    # development compatibility.
    api_base_url: str = ""

    # The hosted MCP runs as a localhost Node sidecar in the same Cloud Run
    # container. Empty disables the proxy (the local/dev and staging-safe
    # default); production Terraform sets the explicit loopback origin.
    mcp_proxy_url: str = ""
    # Dedicated hosted-MCP service identity accepted by the network-internal
    # data-plane mount. Both values are required together; empty disables the
    # mount in local development. The audience is the exact API origin for
    # which the MCP workload mints a Google-signed ID token.
    mcp_internal_service_account_email: str = ""
    mcp_internal_service_audience: str = ""
    # ADR-0002: the MCP transport's OWN single-purpose origin, e.g.
    # `https://drive.mcp.tokencanopy.com`. Empty (the default) binds nothing.
    # When set, HostSurfaceMiddleware answers ONLY `/mcp` and its path-scoped
    # RFC 9728 document on this host — never `/v0`, never the root discovery
    # document that describes it — and the internal ingress verifies
    # `<this origin>/mcp` beside the legacy `/mcp` audience. The legacy
    # transport on the API host is unchanged: the migration is additive.
    # Hub must register the same `<origin>/mcp` resource, or every token
    # minted for this origin is refused here with `invalid_target` upstream.
    mcp_origin_base_url: str = ""
    # ADR-0002's end state. True retires the legacy transport on the API
    # host: `/mcp` and its path-scoped discovery document answer ONLY on
    # `mcp_origin_base_url` (the binding becomes exclusive), and the internal
    # ingress accepts only `<origin>/mcp`. Requires `mcp_origin_base_url`.
    # Hub retires its side with TC_AGENTDRIVE_MCP_LEGACY_RETIRED; a token
    # bound to the legacy audience is refused at every layer from then on.
    mcp_legacy_retired: bool = False

    # Legacy alias hosts. A request whose Host is in LEGACY_HOSTS (comma-
    # separated bare hostnames) is answered with a 308 to the same path and
    # query on LEGACY_REDIRECT_HOST. Both empty — the default, and what a
    # standalone install wants — means HostRedirectMiddleware is inert. The
    # hosted product sets them for its retiring alias domains; the values
    # used to be compiled into middleware.py, which made every install carry
    # another company's domains.
    legacy_hosts: str = ""
    legacy_redirect_host: str = ""

    # Origins allowed to call `/v0` from a browser. The console at
    # app.tokencanopy.com is a different origin from drive.tokencanopy.com, so
    # without this it cannot make a single call — and the failure is visible
    # only in the browser, since the API just answers a preflight it does not
    # recognise.
    #
    # Empty means NO cross-origin access. Never `*`: these endpoints take a
    # bearer token, so a wildcard would let any page spend one it tricked a
    # browser into attaching. `allowed_origins()` filters `*` for that reason.
    cors_allowed_origins: str = ""

    # The public read surface's origin (share.tokencanopy.com). Empty in dev,
    # where one origin serves everything. When set, HostSurfaceMiddleware binds
    # the permalink routes to THIS host and refuses them elsewhere — and
    # refuses /v0 here.
    share_base_url: str = ""

    # The anonymous artifact renderer's origin — a separate registrable
    # domain from the share host (e.g. public.example-usercontent.test).
    # Empty preserves the direct share
    # renderer for local development and the documented rollback state. When
    # set, it requires a configured `share_base_url`: the share host is the
    # only trusted shell allowed to frame this untrusted content surface.
    public_content_base_url: str = ""

    # The PRIVATE viewer surface's origin (e.g. viewer.example-usercontent.test)
    # — the isolated host the console iframes. Deliberately a different
    # registrable domain from tokencanopy.com: the Hub scopes its session
    # cookie to `.tokencanopy.com`, and rendered artifact bytes are
    # attacker-influenced content that must never be served on a host that
    # receives that cookie.
    # Empty in dev, where one origin serves everything. When set,
    # HostSurfaceMiddleware binds `/view/` to THIS host and refuses it
    # elsewhere — and refuses /v0 and the public prefixes here.
    viewer_base_url: str = ""

    # Origins allowed to EMBED the private viewer (`frame-ancestors` on the
    # viewer shell, and the only origins the shell will accept postMessage
    # credentials from). Comma-separated exact origins; `*` is filtered out
    # exactly as it is for CORS. Empty means the shell is not embeddable and
    # the postMessage handshake accepts nothing — fail closed.
    viewer_embed_origins: str = ""

    # Lifetime of a minted viewer-session credential. Short on purpose: the
    # console fetches the rendered document and bytes within seconds of the
    # mint, so the credential only needs to survive that window (plus a
    # retry). Clamped to [60, 300] by a validator — a viewer session must
    # never approach the product token's one-hour lifetime.
    viewer_session_ttl_seconds: int = 180

    # Render a `text/html` artifact as a document (2026-08-26 static-HTML
    # rendering design) instead of as escaped source. Default OFF: the
    # capability is declared before it is activated, and both viewers keep
    # today's behaviour until it is flipped.
    #
    # Only the RENDERING decision moves. `text/html` is still served
    # `Content-Disposition: attachment` from the byte routes — rendering and
    # downloading are separate decisions — and no CSP changes in either
    # direction. Author script does not execute with this on, for two
    # independent reasons on either surface: `rendering/sanitize.py` emits no
    # `script` element and no `on*` attribute, and the page CSP refuses both
    # even if it did. The private viewer has a third — it stages the markup in
    # a `<template>`, and `innerHTML` never runs a script — which the public
    # renderer, interpolating server-side, does not.
    static_html_rendering_enabled: bool = False

    # SESSION_SECRET no longer signs a browser cookie (that plane was archived
    # at the v0 reset) but it is still live: `core/cursors.py` derives the
    # sealed-cursor key from it, so every paginated list depends on it. No
    # default; missing or weak values fail fast at startup.
    session_secret: str

    # The /v0 surface's per-principal abuse guard: on by default, generous
    # for a legitimately bursty agent, finite for an abuser.
    v0_rate_limit_enabled: bool = True
    v0_rate_limit_per_minute: int = 600

    # ── B3 direct transfers (2026-08-14 direct-transfer design §9) ──────────
    # Disabled by default and FAIL CLOSED: while the flag is false every
    # transfer control returns 503 TRANSFER_DISABLED (packet 3), and an
    # enabled-but-partial policy prevents boot rather than silently weakening
    # enforcement (`_direct_transfer_fails_closed` below). B8 owns every
    # launch value; nothing here encodes a beta tier or quota entitlement.
    # The hard logical ceilings are in LOGICAL committed version bytes and,
    # once enabled, apply to EVERY version producer — inline writes cannot
    # bypass them.
    #
    # Packet 2 lands the REST of the §9 interface so the completeness check
    # can be real. `None`/`""` defaults are deliberate: there is no shipped
    # numeric policy, and enablement demands every field (security review
    # I4 — a couple of fields must never be able to flip the surface on).
    # ── sheet edit sessions (2026-08-22 design §8) ──────────────────────
    # The workbook cap is a PARSE-MEMORY and latency bound, not a storage
    # bound -- nothing is materialized -- and it is WHOLE-WORKBOOK, so a
    # forty-tab model reaches it at 5,000 cells a tab. That is the shape to
    # measure it against, not one wide sheet.
    sheet_max_workbook_cells: int = 200_000
    sheet_max_read_cells: int = 50_000
    sheet_max_write_cells: int = 10_000
    sheet_max_edits_per_session: int = 1_000
    sheet_max_cells_per_session: int = 200_000
    sheet_lease_seconds_default: int = 900
    sheet_lease_seconds_max: int = 3_600
    sheet_max_open_sessions_per_drive: int = 20
    direct_transfer_enabled: bool = False
    # Accepted declared-size window. Zero-byte policy is EXPLICIT (§5.2):
    # `allow_zero_bytes` must be set when enabled and must agree with
    # `min_bytes` — zero is never implicitly allowed or rejected.
    direct_transfer_min_bytes: int | None = None
    direct_transfer_max_bytes: int | None = None
    direct_transfer_allow_zero_bytes: bool | None = None
    # Product session deadline (`expires_at`) — bounded by the provider's
    # ~one-week resumable lifetime, past which recovery promises would be
    # dishonest. Terminal retention + GC grace pace the cleanup machine.
    direct_transfer_session_ttl_seconds: int | None = None
    direct_transfer_terminal_retention_seconds: int | None = None
    direct_transfer_gc_grace_seconds: int | None = None
    direct_transfer_max_active_sessions_principal: int | None = None
    direct_transfer_max_active_sessions_workspace: int | None = None
    direct_transfer_max_active_sessions_drive: int | None = None
    direct_transfer_rate_principal: int | None = None
    direct_transfer_rate_workspace: int | None = None
    direct_transfer_rate_drive: int | None = None
    direct_transfer_hard_logical_version_bytes_workspace: int | None = None
    direct_transfer_hard_logical_version_bytes_drive: int | None = None
    # The ONE trusted origin supplied to every GCS XML resumable initiation.
    # Never request-derived (§5.6): CORS gates conforming browsers; it is
    # not bearer origin binding, and no request field can influence it.
    direct_transfer_canonical_browser_origin: str = ""
    # Provider endpoints as bare origins (https outside loopback; loopback
    # http stays possible for the fake-gcs emulator). The XML adapter builds
    # `{endpoint}/{bucket}/{object}` itself — never from caller input.
    direct_transfer_upload_endpoint: str = ""
    direct_transfer_download_endpoint: str = ""
    # The DEDICATED transfer bucket (§8) — never the artifact CAS bucket —
    # with disjoint, slash-terminated scratch/immutable namespaces.
    direct_transfer_bucket: str = ""
    direct_transfer_scratch_prefix: str = ""
    direct_transfer_immutable_prefix: str = ""
    # Signed GET lifetime (§5.7) — short by design; hard-bounded below.
    direct_download_capability_ttl_seconds: int | None = None

    # `extra="ignore"` lets devs keep client-side env vars (e.g.
    # `AGENTDRIVE_API_KEY`, used by the CLI/SDK to auth against the REST API)
    # in the same `.env` without tripping pydantic's strict-mode rejection.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("console_base_url")
    @classmethod
    def _console_origin_is_exact(cls, v: str) -> str:
        """Empty means "there is no console": a self-hosted install has no
        `app.tokencanopy.com/drive` and must not render an `Open in console`
        button to a dead URL on every share page. Anything else is an exact
        origin, as before."""
        if not v.strip():
            return ""
        return validate_exact_origin(v, setting="CONSOLE_BASE_URL")

    @field_validator(
        "share_base_url", "public_content_base_url", "viewer_base_url", "mcp_origin_base_url"
    )
    @classmethod
    def _content_origin_is_exact(cls, v: str) -> str:
        """Validate the isolated content-surface configuration as an origin.

        These values decide which Host headers may reach an untrusted-content
        surface. Accepting a path, credentials, wildcard, or production HTTP
        here would make the later host-routing boundary ambiguous or unsafe.
        Empty remains a deliberate local-development / rollback setting.
        """
        if not v:
            return ""

        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("content origin must be an absolute http(s) origin")
        if parts.username is not None or parts.password is not None:
            raise ValueError("content origin must not include credentials")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("content origin must not include a path, query, or fragment")

        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("content origin port must be numeric") from exc

        host, loopback = _canonical_content_host(parts.hostname)

        if parts.scheme == "http" and not loopback:
            raise ValueError("content origin must use https outside loopback")
        if port is not None and not loopback:
            raise ValueError("content origin must not include a production port")

        rendered_host = f"[{host}]" if ":" in host else host
        rendered_port = f":{port}" if port is not None else ""
        return f"{parts.scheme}://{rendered_host}{rendered_port}"

    @field_validator("mcp_proxy_url")
    @classmethod
    def _mcp_proxy_is_loopback(cls, v: str) -> str:
        if not v:
            return ""
        return validate_loopback_http_origin(v, setting="MCP_PROXY_URL")

    @model_validator(mode="after")
    def _mcp_internal_service_identity_is_complete(self) -> Self:
        account = self.mcp_internal_service_account_email.strip()
        audience = self.mcp_internal_service_audience.strip()
        if bool(account) != bool(audience):
            raise ValueError(
                "MCP_INTERNAL_SERVICE_ACCOUNT_EMAIL and "
                "MCP_INTERNAL_SERVICE_AUDIENCE must be configured together"
            )
        if not account:
            return self
        if not account.endswith(".iam.gserviceaccount.com") or "@" not in account:
            raise ValueError("MCP internal service account must be a service-account email")
        parts = urlsplit(audience)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise ValueError("MCP internal service audience must be an exact HTTPS origin")
        object.__setattr__(self, "mcp_internal_service_account_email", account)
        object.__setattr__(self, "mcp_internal_service_audience", audience.rstrip("/"))
        return self

    @field_validator("internal_ingress_host")
    @classmethod
    def _internal_ingress_host_is_loopback(cls, v: str) -> str:
        """The internal ingress binds loopback or it does not bind.

        NUMERIC only. `localhost` is a name, and a name resolves — through
        `/etc/hosts`, a resolver, or a container DNS policy — so accepting it
        would put the one thing keeping this surface off the network outside
        this process's control. Startup validation, deliberately: a
        misconfiguration must be a boot failure, never a listening socket.
        """
        host = (v or "").strip()
        if host not in ("127.0.0.1", "::1"):
            raise ValueError(
                "INTERNAL_INGRESS_HOST must be a numeric loopback address "
                "(127.0.0.1 or ::1)"
            )
        return host

    @model_validator(mode="after")
    def _local_mode_has_no_issuer(self) -> Self:
        """Local mode has no issuer, no audience and no key material.

        Opaque API keys (§4.2 as amended 2026-09-21) removed every one of
        those: there is nothing to sign with, nothing to publish, nothing to
        fetch, and no `/mcp`-versus-`/v0` audience to split, because an
        operator who minted a key on their own box IS the principal. A
        standalone install therefore needs no explicit `PUBLIC_BASE_URL`
        either — the origin used to be load-bearing only because the issuer,
        the audiences and the sidecar's JWT tuple were all derived from it.

        What remains is a refusal: none of the hub-mode auth settings are
        read here, so carrying one is a stale configuration a self-hoster
        would reasonably believe was in effect. They are refused BY NAME
        rather than ignored, and the list is every one of them rather than
        the two the issuer used: #723 REQUIRED `AUTH_ISSUER` to equal the
        install's origin and derived `HUB_PRODUCT_AUDIENCE` from it, so an
        upgrade from that release most likely carries both, and silently
        dropping them is the same defect as silently dropping the key file.

        `PUBLIC_BASE_URL` is deliberately NOT required any more — nothing in
        authentication derives from the origin. It still decides what a share
        link says, so an install reachable from anywhere but this machine
        should set it; that is a documentation matter, not a boot refusal,
        because a laptop quickstart on the default is exactly right.
        """
        if self.auth_mode != "local":
            return self
        if self.auth_local_signing_key_file.strip():
            raise ValueError(
                "AUTH_LOCAL_SIGNING_KEY_FILE is not read under AUTH_MODE=local any "
                "more: this installation issues no tokens and holds no signing key. "
                "Unset it and mint an API key with `python -m agentdrive.keys create`"
            )
        if self.auth_jwks_url.strip():
            raise ValueError(
                "AUTH_JWKS_URL is not read under AUTH_MODE=local, which verifies "
                "nothing; unset it (fronting a local install with an OIDC issuer is a "
                "later mode)"
            )
        stale = sorted(
            name
            for name in ("hub_issuer", "hub_product_audience", "hub_mcp_audience")
            if name in self.model_fields_set
        )
        if stale:
            raise ValueError(
                "AUTH_ISSUER / HUB_PRODUCT_AUDIENCE / HUB_MCP_AUDIENCE are hub-mode "
                "settings and are not read under AUTH_MODE=local, which has no issuer "
                f"and no token audience; unset {', '.join(stale)}"
            )
        return self

    @model_validator(mode="after")
    def _mcp_audience_is_the_product_origin_plus_mcp(self) -> Self:
        """Derive, or check, the `/mcp` audience.

        The two audiences must share one origin and must never be equal: they
        are the same string is precisely the defect the 2026-08-28 split
        closed. Deriving by default means a deployment cannot get them out of
        step by editing one; validating an explicit value means a deployment
        that DOES set it cannot set it to something else.
        """
        expected = f"{self.hub_product_audience.rstrip('/')}/mcp"
        configured = (self.hub_mcp_audience or "").strip()
        if not configured:
            object.__setattr__(self, "hub_mcp_audience", expected)
            return self
        if configured != expected:
            raise ValueError(
                "HUB_MCP_AUDIENCE must be HUB_PRODUCT_AUDIENCE plus the exact "
                f"/mcp path (expected {expected!r})"
            )
        return self

    @model_validator(mode="after")
    def _content_origins_are_compatible(self) -> Self:
        """Keep configured content surfaces host-distinct without inventing
        an origin for a partially configured environment."""
        if self.public_content_base_url and not self.share_base_url:
            raise ValueError(
                "PUBLIC_CONTENT_BASE_URL requires SHARE_BASE_URL for its trusted shell"
            )

        configured_hosts = [
            _canonical_content_host(urlsplit(origin).hostname)[0]
            for origin in (
                self.share_base_url,
                self.public_content_base_url,
                self.viewer_base_url,
                self.mcp_origin_base_url,
            )
            if origin
        ]
        if len(configured_hosts) != len(set(configured_hosts)):
            raise ValueError("configured content origins must use distinct hosts")
        return self

    @model_validator(mode="after")
    def _mcp_origin_is_not_the_api_host(self) -> Self:
        """The MCP origin is an alias-bearing host, so it must be single-purpose.

        On the API host the bare origin IS the `/v0` product resource, and Hub
        will refuse to register the alias there (ADR-0002). Refusing the same
        value here keeps the two halves from being configured apart: a host
        that serves `/v0` can never be the one that serves only `/mcp`.
        """
        if not self.mcp_origin_base_url:
            if self.mcp_legacy_retired:
                raise ValueError(
                    "MCP_LEGACY_RETIRED requires MCP_ORIGIN_BASE_URL: retiring the legacy "
                    "transport with no origin to replace it would serve MCP nowhere"
                )
            return self
        api_host = urlsplit(self.api_base_url or self.public_base_url).hostname
        mcp_host = urlsplit(self.mcp_origin_base_url).hostname
        if api_host and mcp_host and (
            _canonical_content_host(api_host)[0] == _canonical_content_host(mcp_host)[0]
        ):
            raise ValueError(
                "MCP_ORIGIN_BASE_URL must not be the API host: its bare origin is the "
                "/v0 product resource, and an MCP-only origin serves nothing else"
            )
        return self

    @property
    def hub_mcp_audiences(self) -> tuple[str, ...]:
        """Every audience the internal ingress accepts.

        The legacy `/mcp` resource on the API host, plus the per-product
        origin's `<origin>/mcp` when one is configured. Each carries the exact
        `/mcp` path; none is a bare origin, so none is a product audience.
        """
        if self.mcp_legacy_retired:
            return (f"{self.mcp_origin_base_url}/mcp",)
        audiences = [self.hub_mcp_audience]
        if self.mcp_origin_base_url:
            audiences.append(f"{self.mcp_origin_base_url}/mcp")
        return tuple(audiences)

    @model_validator(mode="after")
    def _direct_transfer_fails_closed(self) -> Self:
        """An enabled direct-transfer configuration must be COMPLETE and
        bounded (B3 §9): origin, exact bucket/prefixes, endpoints, TTLs,
        session/rate limits, and the hard ceilings — or the process does not
        become ready. A partial enabled policy must never silently disable
        enforcement; disabled is the one honest partial state.

        Packet 2 replaced packet 1's outright refusal with this full
        completeness check (security review I4: a couple of fields must not
        be able to flip the transfer surface on — enablement now demands
        every §9 field). Note that passing here only means the CONFIG is
        coherent: packet 2 mounts no transfer route, and packet 3's controls
        additionally gate on runtime readiness."""
        if not self.direct_transfer_enabled:
            return self

        numeric = {
            "DIRECT_TRANSFER_MIN_BYTES": self.direct_transfer_min_bytes,
            "DIRECT_TRANSFER_MAX_BYTES": self.direct_transfer_max_bytes,
            "DIRECT_TRANSFER_SESSION_TTL_SECONDS":
                self.direct_transfer_session_ttl_seconds,
            "DIRECT_TRANSFER_TERMINAL_RETENTION_SECONDS":
                self.direct_transfer_terminal_retention_seconds,
            "DIRECT_TRANSFER_GC_GRACE_SECONDS":
                self.direct_transfer_gc_grace_seconds,
            "DIRECT_TRANSFER_MAX_ACTIVE_SESSIONS_PRINCIPAL":
                self.direct_transfer_max_active_sessions_principal,
            "DIRECT_TRANSFER_MAX_ACTIVE_SESSIONS_WORKSPACE":
                self.direct_transfer_max_active_sessions_workspace,
            "DIRECT_TRANSFER_MAX_ACTIVE_SESSIONS_DRIVE":
                self.direct_transfer_max_active_sessions_drive,
            "DIRECT_TRANSFER_RATE_PRINCIPAL": self.direct_transfer_rate_principal,
            "DIRECT_TRANSFER_RATE_WORKSPACE": self.direct_transfer_rate_workspace,
            "DIRECT_TRANSFER_RATE_DRIVE": self.direct_transfer_rate_drive,
            "DIRECT_TRANSFER_HARD_LOGICAL_VERSION_BYTES_WORKSPACE":
                self.direct_transfer_hard_logical_version_bytes_workspace,
            "DIRECT_TRANSFER_HARD_LOGICAL_VERSION_BYTES_DRIVE":
                self.direct_transfer_hard_logical_version_bytes_drive,
            "DIRECT_DOWNLOAD_CAPABILITY_TTL_SECONDS":
                self.direct_download_capability_ttl_seconds,
        }
        strings = {
            "DIRECT_TRANSFER_CANONICAL_BROWSER_ORIGIN":
                self.direct_transfer_canonical_browser_origin,
            "DIRECT_TRANSFER_UPLOAD_ENDPOINT": self.direct_transfer_upload_endpoint,
            "DIRECT_TRANSFER_DOWNLOAD_ENDPOINT":
                self.direct_transfer_download_endpoint,
            "DIRECT_TRANSFER_BUCKET": self.direct_transfer_bucket,
            "DIRECT_TRANSFER_SCRATCH_PREFIX": self.direct_transfer_scratch_prefix,
            "DIRECT_TRANSFER_IMMUTABLE_PREFIX": self.direct_transfer_immutable_prefix,
        }
        missing = [name for name, value in numeric.items() if value is None]
        missing += [name for name, value in strings.items() if not value]
        if missing or self.direct_transfer_allow_zero_bytes is None:
            if self.direct_transfer_allow_zero_bytes is None:
                missing.append("DIRECT_TRANSFER_ALLOW_ZERO_BYTES")
            raise ValueError(
                "DIRECT_TRANSFER_ENABLED=true with an incomplete §9 "
                "configuration surface; refusing to weaken enforcement. "
                f"Missing: {', '.join(sorted(missing))}"
            )

        # Bounded, not merely present. `min_bytes` may legitimately be 0
        # (explicit zero-byte policy below); everything else must be > 0.
        if self.direct_transfer_min_bytes < 0:
            raise ValueError("DIRECT_TRANSFER_MIN_BYTES must be >= 0")
        for name, value in numeric.items():
            if name != "DIRECT_TRANSFER_MIN_BYTES" and value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.direct_transfer_min_bytes > self.direct_transfer_max_bytes:
            raise ValueError(
                "DIRECT_TRANSFER_MIN_BYTES must not exceed DIRECT_TRANSFER_MAX_BYTES"
            )
        # §5.2: zero-byte acceptance is an explicit configured decision and
        # must agree with the size window — no silent contradiction.
        if self.direct_transfer_allow_zero_bytes and self.direct_transfer_min_bytes != 0:
            raise ValueError(
                "DIRECT_TRANSFER_ALLOW_ZERO_BYTES=true requires "
                "DIRECT_TRANSFER_MIN_BYTES=0"
            )
        if not self.direct_transfer_allow_zero_bytes and self.direct_transfer_min_bytes == 0:
            raise ValueError(
                "DIRECT_TRANSFER_ALLOW_ZERO_BYTES=false requires "
                "DIRECT_TRANSFER_MIN_BYTES >= 1"
            )
        # A product deadline past GCS's ~one-week resumable-session lifetime
        # would promise recoverability the external bearer cannot deliver.
        if self.direct_transfer_session_ttl_seconds > 7 * 24 * 3600:
            raise ValueError(
                "DIRECT_TRANSFER_SESSION_TTL_SECONDS must not exceed the "
                "provider's ~one-week resumable lifetime (604800)"
            )
        # §5.7: a signed GET target is short-lived by design. The one shared
        # ceiling — the signer boundary fences against the same constant.
        if (
            self.direct_download_capability_ttl_seconds
            > MAX_DOWNLOAD_CAPABILITY_TTL_SECONDS
        ):
            raise ValueError(
                "DIRECT_DOWNLOAD_CAPABILITY_TTL_SECONDS must not exceed "
                f"{MAX_DOWNLOAD_CAPABILITY_TTL_SECONDS}"
            )

        validate_exact_origin(
            self.direct_transfer_canonical_browser_origin,
            setting="DIRECT_TRANSFER_CANONICAL_BROWSER_ORIGIN",
        )
        validate_exact_origin(
            self.direct_transfer_upload_endpoint,
            setting="DIRECT_TRANSFER_UPLOAD_ENDPOINT",
        )
        download_origin = validate_exact_origin(
            self.direct_transfer_download_endpoint,
            setting="DIRECT_TRANSFER_DOWNLOAD_ENDPOINT",
        )
        # The download endpoint hosts disclosed bearer targets: HTTPS-only,
        # loopback INCLUDED (the loopback-HTTP exception above exists for
        # the upload/emulator surface, never for a signed bearer). The
        # storage_transfers signer boundary enforces the same rule.
        if not download_origin.startswith("https://"):
            raise ValueError(
                "DIRECT_TRANSFER_DOWNLOAD_ENDPOINT must be https — signed "
                "download bearers are HTTPS-only, loopback included"
            )

        # §8: a DEDICATED bucket — scratch/adopted transfer objects never
        # share the artifact CAS bucket, whose lifecycle rules differ.
        if self.direct_transfer_bucket == self.gcs_bucket:
            raise ValueError(
                "DIRECT_TRANSFER_BUCKET must be a dedicated bucket, not the "
                "artifact bucket"
            )
        for name, prefix in (
            ("DIRECT_TRANSFER_SCRATCH_PREFIX", self.direct_transfer_scratch_prefix),
            ("DIRECT_TRANSFER_IMMUTABLE_PREFIX", self.direct_transfer_immutable_prefix),
        ):
            if not prefix.endswith("/") or prefix.startswith("/") or "//" in prefix:
                raise ValueError(
                    f"{name} must be a relative, slash-terminated namespace"
                )
        scratch = self.direct_transfer_scratch_prefix
        immutable = self.direct_transfer_immutable_prefix
        if scratch.startswith(immutable) or immutable.startswith(scratch):
            raise ValueError(
                "scratch and immutable transfer prefixes must be disjoint "
                "namespaces — overlapping prefixes would let cleanup and "
                "adoption see each other's objects"
            )
        if self.storage_backend != "gcs":
            raise ValueError(
                "DIRECT_TRANSFER_ENABLED=true requires STORAGE_BACKEND=gcs: the "
                "browser-initiated resumable protocol is GCS-only and the filesystem "
                "store cannot honour it"
            )
        return self

    @field_validator("hub_product_audience")
    @classmethod
    def _hub_product_audience_is_absolute_url(cls, v: str) -> str:
        """The product-token audience is the agent-facing ORIGIN (contract
        §3), so it must be an absolute http(s) URL. Rejects a bare product
        name like "agentdrive" (the archived sign-in client id) — the exact
        conflation this setting exists to undo."""
        from urllib.parse import urlsplit

        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(
                "HUB_PRODUCT_AUDIENCE must be an absolute http(s) URL — "
                "contract §3 pins the product-token audience to the "
                "agent-facing origin (e.g. https://drive.tokencanopy.com)."
            )
        return v

    @field_validator("viewer_session_ttl_seconds")
    @classmethod
    def _viewer_ttl_bounds(cls, v: int) -> int:
        """A viewer credential is a per-view capability, not a session: long
        enough to fetch a document and retry once, never long enough to be
        worth stealing. Reject rather than clamp, so a misconfigured
        environment fails at boot instead of silently running with a
        different lifetime than the operator asked for."""
        if not 60 <= v <= 300:
            raise ValueError("VIEWER_SESSION_TTL_SECONDS must be between 60 and 300")
        return v

    @field_validator("storage_backend")
    @classmethod
    def _storage_backend_closed(cls, v: str) -> str:
        backend = (v or "").strip().lower()
        if backend not in ("gcs", "fs"):
            raise ValueError(f'STORAGE_BACKEND must be "gcs" or "fs"; got {v!r}')
        return backend

    @model_validator(mode="after")
    def _storage_backend_is_configured(self) -> Self:
        """Each backend's own setting is required only when it is the one in
        use, so neither install has to invent the other's configuration."""
        if self.storage_backend == "gcs" and not self.gcs_bucket.strip():
            raise ValueError("GCS_BUCKET is required when STORAGE_BACKEND=gcs")
        if self.storage_backend == "fs":
            import os

            root = self.storage_fs_root.strip()
            if not root:
                raise ValueError("STORAGE_FS_ROOT is required when STORAGE_BACKEND=fs")
            # Absolute, so the store's location never depends on the process's
            # working directory (its identity is persisted inside it either way).
            resolved = os.path.abspath(os.path.expanduser(root))
            if os.path.exists(resolved) and not os.path.isdir(resolved):
                raise ValueError(f"STORAGE_FS_ROOT {resolved!r} exists and is not a directory")
            object.__setattr__(self, "storage_fs_root", resolved)
        return self

    @field_validator("auth_mode")
    @classmethod
    def _auth_mode_closed(cls, v: str) -> str:
        """Close the issuer choice at Settings construction.

        "hub-oidc" was the value every hosted environment carried while the
        archived WorkOS sign-in plane still existed beside Hub; it normalizes
        to "hub" so those environments keep booting through one release of
        alias. Anything else is a misconfiguration, and it fails here rather
        than at the first request."""
        mode = (v or "").strip().lower()
        if mode == "hub-oidc":
            return "hub"
        if mode not in ("hub", "local"):
            raise ValueError(
                'AUTH_MODE must be "hub" (or its one-release alias "hub-oidc") or '
                f'"local"; got {v!r}'
            )
        return mode

    @field_validator("legacy_hosts")
    @classmethod
    def _legacy_hosts_are_bare_hostnames(cls, v: str) -> str:
        canonical = []
        for entry in (v or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            host = _bare_hostname(entry)
            if host is None:
                raise ValueError(
                    f"LEGACY_HOSTS entry {entry!r} must be a bare hostname (no scheme, "
                    "port, path, wildcard or whitespace); it is compared to the Host header"
                )
            canonical.append(host)
        return ",".join(dict.fromkeys(canonical))

    @field_validator("legacy_redirect_host")
    @classmethod
    def _legacy_redirect_host_is_bare(cls, v: str) -> str:
        entry = (v or "").strip()
        if not entry:
            return ""
        host = _bare_hostname(entry)
        if host is None:
            raise ValueError(
                f"LEGACY_REDIRECT_HOST {entry!r} must be a bare hostname (no scheme, "
                "port, path, wildcard or whitespace); it is the host of every Location"
            )
        return host

    @model_validator(mode="after")
    def _legacy_redirect_is_all_or_nothing(self) -> Self:
        """Half a redirect is a misconfiguration: aliases with nowhere to go,
        or a target nothing is sent to. Refuse at boot rather than install a
        silent no-op — and never redirect the target onto itself."""
        hosts = self.legacy_host_set
        if bool(hosts) != bool(self.legacy_redirect_host):
            raise ValueError(
                "LEGACY_HOSTS and LEGACY_REDIRECT_HOST must be configured together"
            )
        if self.legacy_redirect_host in hosts:
            raise ValueError(
                "LEGACY_REDIRECT_HOST must not be one of the LEGACY_HOSTS it redirects"
            )
        return self

    @property
    def legacy_host_set(self) -> frozenset[str]:
        """The configured alias hosts, lowercased. Recomputed on access: the
        string is tiny, and reading it per request is what lets the redirect
        follow the environment instead of the module."""
        return frozenset(h.strip().lower() for h in self.legacy_hosts.split(",") if h.strip())

    @field_validator("session_secret")
    @classmethod
    def _session_secret_required(cls, v: str) -> str:
        if not v:
            raise ValueError("SESSION_SECRET is required")
        if v == _INSECURE_DEFAULT_SESSION_SECRET:
            raise ValueError(
                "SESSION_SECRET cannot be the documented default. "
                "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        if len(v) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        return v

settings = Settings()
