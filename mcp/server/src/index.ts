#!/usr/bin/env node

import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";

import {
  AgentDriveClient,
  StaticTokenProvider,
} from "@tokencanopy/agentdrive-sdk";
import {
  AGENTDRIVE_MCP_RESOURCE,
  AGENTDRIVE_MCP_SCOPES,
  remoteJwks,
  TOKEN_CANOPY_HUB_ISSUER,
  type ProtectedResourceDefinition,
} from "@tokencanopy/mcp-auth";

import {
  createAgentDriveMcpHttpServer,
  MCP_PATH,
  MCP_RESOURCE_METADATA_PATH,
  protectedResourceForAuth,
  type AgentDriveMcpApiKeyIntrospection,
  type AgentDriveMcpApiKeyOptions,
  type AgentDriveMcpAuthOptions,
} from "./http.js";
import type { AgentDriveClientLike } from "./types.js";

export * from "./http.js";
export * from "./server.js";
export * from "./types.js";

const DEFAULT_HUB_JWKS_URL =
  "https://auth.tokencanopy.com/.well-known/jwks.json";
const DEFAULT_MCP_ENVIRONMENT = "production";
const RUNTIME_OVERRIDE_NAMES = [
  "MCP_AUTH_ISSUER",
  "MCP_AUTH_AUDIENCE",
  "MCP_AUTH_METADATA_URL",
  "MCP_AUTH_JWKS_URL",
] as const;
/**
 * `MCP_PUBLIC_BASE_URL` is an override for the same rule's PURPOSE — setting
 * it means "this is a deliberately configured deployment", so it demands an
 * explicit `MCP_ENVIRONMENT` and the full auth tuple like any other override
 * — but it is NOT in the required set. Unset is a legitimate state everywhere
 * (`publish` omits `public_url` and says so); requiring it would refuse to
 * boot a production revision built before the terraform that declares the
 * value is applied, for the sake of one optional result field.
 */
const PUBLIC_BASE_URL_NAME = "MCP_PUBLIC_BASE_URL";

/**
 * How this process authenticates a caller.
 *
 * `jwt` (the default, and what an unset `MCP_AUTH_MODE` means) is the hosted
 * path: Hub-issued, audience-bound access tokens verified against Hub's JWKS.
 * `api-key` is `AUTH_MODE=local` — a self-hosted install with no
 * authorization server, where the credential is an opaque `adk_` key the
 * operator minted and the ingress beside this process resolves it (design
 * §4.2 as amended 2026-09-21).
 *
 * Nothing hosted sets this variable, and its absence is the hosted path, so
 * adding the mode cannot move a hosted deployment.
 */
export const MCP_AUTH_MODES = ["jwt", "api-key"] as const;
export type McpAuthMode = (typeof MCP_AUTH_MODES)[number];
const DEFAULT_MCP_AUTH_MODE: McpAuthMode = "jwt";

/** The ingress route that resolves an opaque key. */
export const INTERNAL_INTROSPECT_PATH = "/_internal/introspect";
/**
 * How long a POSITIVE introspection is reused, per key.
 *
 * It shortens `tools/list` and the burst of calls a client makes right after
 * it, and NOTHING else: every `tools/call` still reaches the ingress with the
 * key, where it is resolved again against the live row. So the window a
 * just-revoked key can still enumerate tools in is 30 seconds, and the window
 * it can still DO anything in is zero. Failures are never cached — a
 * revocation and an outage both take effect on the next request.
 */
export const API_KEY_CACHE_TTL_MS = 30_000;
/** A bound, so a flood of distinct keys cannot grow the map without limit. */
export const API_KEY_CACHE_MAX_ENTRIES = 512;

/** Header carrying the supervisor's per-boot proof to the internal ingress. */
export const INTERNAL_PROOF_HEADER = "x-agentdrive-internal-proof";
export const INTERNAL_SERVICE_AUTHORIZATION_HEADER =
  "x-agentdrive-mcp-service-authorization";
const SERVERLESS_AUTHORIZATION_HEADER = "x-serverless-authorization";

/**
 * Loopback hosts the internal AgentDrive ingress may live on.
 *
 * NUMERIC ONLY. `localhost` is a name, and a name resolves — through
 * `/etc/hosts`, a resolver, or a container DNS policy — so accepting it would
 * mean the one check standing between the per-boot proof and the network
 * depends on something outside this process.
 */
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "[::1]"]);

/** A 256-bit proof base64url-encodes to 43 characters, which is exactly what
 * the supervisor generates. Anything shorter is a placeholder or a
 * truncation, not a per-boot secret. */
const MIN_INTERNAL_PROOF_LENGTH = 43;

export interface AgentDriveMcpRuntimeConfig {
  environment: string;
  /** `jwt` (hosted) or `api-key` (a self-hosted `AUTH_MODE=local` install). */
  authMode: McpAuthMode;
  /**
   * The private AgentDrive ingress: loopback locally, exact HTTPS mount in
   * the isolated production topology.
   *
   * MCP no longer calls the public `/v0` API: since the 2026-08-28
   * audience split its bearer is bound to `.../mcp`, which public `/v0`
   * rejects, and forwarding a product-audience token instead is precisely the
   * confusion the split removed.
   */
  internalUrl: string;
  /** The supervisor's per-boot proof. Present only in local sidecar mode. */
  internalProof: string | undefined;
  /** Cloud Run API audience. Present only in the isolated-service topology. */
  internalIdentityAudience: string | undefined;
  /** The Hub verifier. Undefined in `api-key` mode, which verifies nothing. */
  auth?: AgentDriveMcpAuthOptions;
  /** Undefined in `api-key` mode: the document is built per request, from the
   * origin the client reached, because a self-hosted install has no
   * configured one to name. */
  protectedResource?: ProtectedResourceDefinition;
  /**
   * ADR-0002: the same verifier for further MCP resources, one per
   * single-purpose origin (`MCP_AUTH_ADDITIONAL_AUDIENCES`). Each shares the
   * issuer, JWKS, and scope vocabulary and differs ONLY in audience and the
   * metadata URL derived from it. Empty for a deployment with one origin, and
   * in `api-key` mode, where a key has no audience to keep apart.
   */
  additionalAuth: AgentDriveMcpAuthOptions[];
  /**
   * The public shell's origin (`https://share.tokencanopy.com`), on which a
   * published resource's permalink lives. Undefined when `MCP_PUBLIC_BASE_URL`
   * is unset: `publish` then returns no `public_url` rather than a guess.
   */
  publicBaseUrl: string | undefined;
}

/**
 * The public shell origin: exactly `https://` + host, nothing else. The value
 * is compared to its own parsed origin, so a path, a trailing slash, a query,
 * credentials, an uppercase host, or an `http:` scheme all fail — a permalink
 * built on a sloppy base is a link handed to a person that does not open.
 */
function exactPublicOrigin(value: string, name: string): string {
  const parsed = absoluteHttpUrl(value, name);
  if (parsed.protocol !== "https:" || parsed.origin !== value) {
    throw new Error(
      `${name} must be exactly an https origin: scheme and host, no path, no trailing slash`,
    );
  }
  return parsed.origin;
}

/**
 * Select one of two closed internal-ingress modes: numeric loopback plus the
 * per-boot proof, or exact HTTPS `/_internal/mcp` plus workload identity.
 */
function internalTarget(
  value: string,
  environment: NodeJS.ProcessEnv,
): { url: string; proof?: string; audience?: string } {
  const parsed = absoluteHttpUrl(value, "MCP_AGENTDRIVE_INTERNAL_URL");
  const isLoopback =
    parsed.protocol === "http:" && LOOPBACK_HOSTS.has(parsed.hostname);
  if (isLoopback) {
    if (environment.MCP_AGENTDRIVE_INTERNAL_AUDIENCE?.trim()) {
      throw new Error(
        "MCP_AGENTDRIVE_INTERNAL_AUDIENCE must not be set for loopback mode",
      );
    }
    if (parsed.pathname !== "/" && parsed.pathname !== "") {
      throw new Error(
        "MCP_AGENTDRIVE_INTERNAL_URL loopback value must be an origin",
      );
    }
    const proof = environment.MCP_INTERNAL_PROOF?.trim() ?? "";
    if (proof.length < MIN_INTERNAL_PROOF_LENGTH) {
      throw new Error(
        `MCP_INTERNAL_PROOF must be at least ${MIN_INTERNAL_PROOF_LENGTH} characters`,
      );
    }
    return { url: parsed.origin, proof };
  }
  if (parsed.protocol !== "https:") {
    throw new Error(
      "MCP_AGENTDRIVE_INTERNAL_URL must use HTTPS outside numeric loopback",
    );
  }
  if (parsed.pathname !== "/_internal/mcp") {
    throw new Error(
      "MCP_AGENTDRIVE_INTERNAL_URL remote value must end at /_internal/mcp",
    );
  }
  if (environment.MCP_INTERNAL_PROOF?.trim()) {
    throw new Error(
      "MCP_INTERNAL_PROOF must not be set for a remote internal API",
    );
  }
  const audienceValue =
    environment.MCP_AGENTDRIVE_INTERNAL_AUDIENCE?.trim() ?? "";
  if (!audienceValue) {
    throw new Error(
      "MCP_AGENTDRIVE_INTERNAL_AUDIENCE is required for a remote internal API",
    );
  }
  const audience = exactPublicOrigin(
    audienceValue,
    "MCP_AGENTDRIVE_INTERNAL_AUDIENCE",
  );
  if (audience !== parsed.origin) {
    throw new Error(
      "MCP_AGENTDRIVE_INTERNAL_AUDIENCE must be the internal API origin",
    );
  }
  return { url: parsed.toString().replace(/\/$/u, ""), audience };
}

function absoluteHttpUrl(value: string, name: string): URL {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(`${name} must be an absolute HTTP(S) URL`);
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error(`${name} must be an absolute HTTP(S) URL`);
  }
  return parsed;
}

/** The MCP resource identifier: an origin plus the exact `/mcp` path. A bare
 * origin is refused — that spelling IS the public product resource. */
function exactMcpResource(value: string, name: string): string {
  const parsed = absoluteHttpUrl(value, name);
  if (parsed.pathname !== MCP_PATH) {
    throw new Error(
      `${name} must be an origin plus the exact ${MCP_PATH} path`,
    );
  }
  return `${parsed.origin}${MCP_PATH}`;
}

function exactMetadataUrl(value: string, audience: string): string {
  const parsed = absoluteHttpUrl(value, "MCP_AUTH_METADATA_URL");
  const expected = new URL(MCP_RESOURCE_METADATA_PATH, audience);
  if (
    parsed.origin !== expected.origin ||
    parsed.pathname !== expected.pathname
  ) {
    throw new Error(
      "MCP_AUTH_METADATA_URL must be the path-scoped protected-resource metadata URL for MCP_AUTH_AUDIENCE",
    );
  }
  return parsed.toString();
}

/**
 * Resolve the complete hosted-MCP runtime configuration as one unit.
 *
 * Production has safe canonical defaults. Every non-production deployment
 * must provide the full issuer/audience/metadata/JWKS/API-origin tuple, so a
 * staging verifier can never accidentally send an authenticated request to
 * the production AgentDrive API.
 */
export function productionRuntimeConfig(
  environment: NodeJS.ProcessEnv = process.env,
): AgentDriveMcpRuntimeConfig {
  const authMode = readAuthMode(environment);
  if (authMode === "api-key") {
    // A self-hosted install (§4.2 as amended). There is no issuer, no
    // audience, no JWKS and no metadata URL, so the four-name tuple the
    // branch below demands would be four values with nothing behind them.
    // `MCP_ENVIRONMENT` is still required, and still may not be
    // "production": this mode exists precisely because the deployment is NOT
    // the hosted one, and letting it inherit the production label would make
    // a log line lie about which topology is running.
    const deployment = environment.MCP_ENVIRONMENT?.trim();
    if (!deployment) {
      throw new Error("MCP_ENVIRONMENT is required with MCP_AUTH_MODE=api-key");
    }
    if (deployment === DEFAULT_MCP_ENVIRONMENT) {
      throw new Error(
        "MCP_AUTH_MODE=api-key is a self-hosted mode and cannot run as MCP_ENVIRONMENT=production",
      );
    }
    const configured = RUNTIME_OVERRIDE_NAMES.filter((name) =>
      environment[name]?.trim(),
    );
    if (configured.length > 0) {
      throw new Error(
        `MCP_AUTH_MODE=api-key has no issuer or audience; unset: ${configured.join(", ")}`,
      );
    }
    const localInternal = internalTarget(
      environment.MCP_AGENTDRIVE_INTERNAL_URL?.trim() ?? "",
      environment,
    );
    const localPublicBase = environment[PUBLIC_BASE_URL_NAME]?.trim();
    return {
      environment: deployment,
      authMode,
      internalUrl: localInternal.url,
      internalProof: localInternal.proof,
      internalIdentityAudience: localInternal.audience,
      additionalAuth: [],
      publicBaseUrl: localPublicBase
        ? exactPublicOrigin(localPublicBase, PUBLIC_BASE_URL_NAME)
        : undefined,
    };
  }
  const configuredDeployment = environment.MCP_ENVIRONMENT?.trim();
  const hasOverrides = [...RUNTIME_OVERRIDE_NAMES, PUBLIC_BASE_URL_NAME].some(
    (name) => environment[name] !== undefined,
  );
  if (hasOverrides && !configuredDeployment) {
    throw new Error(
      "MCP_ENVIRONMENT is required when overriding the production MCP configuration",
    );
  }
  const deployment = configuredDeployment || DEFAULT_MCP_ENVIRONMENT;
  const requireExplicit = deployment !== DEFAULT_MCP_ENVIRONMENT;
  if (requireExplicit || hasOverrides) {
    const missing = RUNTIME_OVERRIDE_NAMES.filter(
      (name) => !environment[name]?.trim(),
    );
    if (missing.length > 0) {
      throw new Error(
        `MCP runtime configuration must set together: ${missing.join(", ")}`,
      );
    }
  }

  const issuer = environment.MCP_AUTH_ISSUER?.trim() ?? TOKEN_CANOPY_HUB_ISSUER;
  const audience = exactMcpResource(
    environment.MCP_AUTH_AUDIENCE?.trim() ?? AGENTDRIVE_MCP_RESOURCE,
    "MCP_AUTH_AUDIENCE",
  );
  const internal = internalTarget(
    environment.MCP_AGENTDRIVE_INTERNAL_URL?.trim() ?? "",
    environment,
  );
  const issuerUrl = absoluteHttpUrl(issuer, "MCP_AUTH_ISSUER");
  const metadataUrl = exactMetadataUrl(
    environment.MCP_AUTH_METADATA_URL?.trim() ??
      new URL(MCP_RESOURCE_METADATA_PATH, audience).toString(),
    audience,
  );
  const jwksUrl = absoluteHttpUrl(
    environment.MCP_AUTH_JWKS_URL?.trim() ?? DEFAULT_HUB_JWKS_URL,
    "MCP_AUTH_JWKS_URL",
  );
  if (jwksUrl.origin !== issuerUrl.origin) {
    throw new Error("MCP_AUTH_JWKS_URL must share the issuer origin");
  }

  const configuredPublicBase = environment[PUBLIC_BASE_URL_NAME]?.trim();
  const publicBaseUrl = configuredPublicBase
    ? exactPublicOrigin(configuredPublicBase, PUBLIC_BASE_URL_NAME)
    : undefined;

  const auth: AgentDriveMcpAuthOptions = {
    issuer,
    audience,
    metadataUrl,
    // The VOCABULARY, not a required bundle. A read-only grant is a
    // legitimate grant; per-tool authorization decides what it can do.
    recognizedScopes: AGENTDRIVE_MCP_SCOPES,
    jwks: remoteJwks(jwksUrl),
  };
  return {
    environment: deployment,
    authMode,
    internalUrl: internal.url,
    internalProof: internal.proof,
    internalIdentityAudience: internal.audience,
    auth,
    protectedResource: protectedResourceForAuth(auth),
    additionalAuth: additionalAudiences(
      environment.MCP_AUTH_ADDITIONAL_AUDIENCES,
      auth,
    ),
    publicBaseUrl,
  };
}

/**
 * Parse `MCP_AUTH_ADDITIONAL_AUDIENCES`: a comma-separated list of exact
 * `/mcp` resources, each on an origin distinct from the primary's and from
 * each other. The metadata URL is DERIVED from each audience — the
 * path-scoped document on that same origin — because that is the only URL
 * the edge serves for it, and taking it as a second value would be one more
 * pair to hold in step.
 */
export function additionalAudiences(
  value: string | undefined,
  primary: AgentDriveMcpAuthOptions,
): AgentDriveMcpAuthOptions[] {
  const entries = (value ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0);
  const seen = new Set([new URL(primary.audience).origin]);
  return entries.map((entry) => {
    const audience = exactMcpResource(entry, "MCP_AUTH_ADDITIONAL_AUDIENCES");
    const origin = new URL(audience).origin;
    if (seen.has(origin)) {
      throw new Error(
        `MCP_AUTH_ADDITIONAL_AUDIENCES: ${audience} shares an origin with another configured MCP resource; each needs its own`,
      );
    }
    seen.add(origin);
    return {
      ...primary,
      audience,
      metadataUrl: new URL(MCP_RESOURCE_METADATA_PATH, audience).toString(),
    };
  });
}

/**
 * Build the production protected-resource verifier configuration.
 *
 * The defaults target production. Staging or another future Token Canopy MCP
 * must override the issuer, audience, metadata URL, and JWKS URL explicitly;
 * a mismatched issuer or audience fails closed before the SDK client is
 * constructed.
 */
export function productionAuthOptions(
  environment: NodeJS.ProcessEnv = process.env,
): AgentDriveMcpAuthOptions {
  const runtime = productionRuntimeConfig(environment);
  if (runtime.auth === undefined) {
    throw new Error("MCP_AUTH_MODE=api-key has no JWT verifier configuration");
  }
  return runtime.auth;
}

function readAuthMode(environment: NodeJS.ProcessEnv): McpAuthMode {
  const raw = environment.MCP_AUTH_MODE?.trim();
  if (!raw) return DEFAULT_MCP_AUTH_MODE;
  if (!(MCP_AUTH_MODES as readonly string[]).includes(raw)) {
    throw new Error(
      `MCP_AUTH_MODE must be one of: ${MCP_AUTH_MODES.join(", ")}`,
    );
  }
  return raw as McpAuthMode;
}

/**
 * Resolve an opaque `adk_` key through AgentDrive's loopback ingress.
 *
 * The sidecar holds no database and must not grow one: the API is the
 * authority on what a key means, and this is a question asked of it, not a
 * second implementation of the answer. The per-boot proof goes on the request
 * for the same reason every other internal call carries it — it is not
 * authorization, it is what makes the port indistinguishable from a closed
 * route to anything in the container that was not handed it at boot.
 *
 * ONLY a positive answer is cached, keyed by sha256 of the key so the key
 * itself is never a map key, for `API_KEY_CACHE_TTL_MS`. A 401 and a 503 are
 * always re-asked, so a revocation takes effect on the next `tools/call` and
 * an ingress restart is not mistaken for a bad credential.
 */
export function createApiKeyIntrospector(options: {
  internalUrl: string;
  internalProof: string | undefined;
  scopesSupported: readonly string[];
  fetchApi?: typeof fetch;
  now?: () => number;
}): AgentDriveMcpApiKeyOptions {
  const fetchApi = options.fetchApi ?? fetch;
  const now = options.now ?? Date.now;
  const endpoint = new URL(
    INTERNAL_INTROSPECT_PATH,
    `${new URL(options.internalUrl).origin}/`,
  ).toString();
  const cache = new Map<
    string,
    { expiresAt: number; value: AgentDriveMcpApiKeyIntrospection }
  >();
  return {
    scopesSupported: options.scopesSupported,
    async introspect(key) {
      const digest = createHash("sha256").update(key).digest("hex");
      const cached = cache.get(digest);
      if (cached && cached.expiresAt > now()) return cached.value;
      cache.delete(digest);
      const headers = new Headers({ authorization: `Bearer ${key}` });
      if (options.internalProof) {
        headers.set(INTERNAL_PROOF_HEADER, options.internalProof);
      }
      let response: Response;
      try {
        response = await fetchApi(endpoint, {
          method: "POST",
          headers,
          redirect: "manual",
          signal: AbortSignal.timeout(5_000),
        });
      } catch {
        return "unavailable";
      }
      if (response.status === 401) return "unauthorized";
      if (!response.ok) return "unavailable";
      let body: unknown;
      try {
        body = await response.json();
      } catch {
        return "unavailable";
      }
      const resolved = readIntrospection(body);
      if (resolved === undefined) return "unavailable";
      // Evict the oldest entry rather than growing: insertion order is Map's,
      // and a bound matters more here than a perfect eviction policy.
      if (cache.size >= API_KEY_CACHE_MAX_ENTRIES) {
        const oldest = cache.keys().next();
        if (!oldest.done) cache.delete(oldest.value);
      }
      cache.set(digest, {
        expiresAt: now() + API_KEY_CACHE_TTL_MS,
        value: resolved,
      });
      return resolved;
    },
  };
}

/** The ingress's answer, checked rather than trusted: a malformed body is a
 * boundary that is not behaving, which is `unavailable`, not an actor. */
function readIntrospection(
  body: unknown,
): AgentDriveMcpApiKeyIntrospection | undefined {
  if (typeof body !== "object" || body === null) return undefined;
  const record = body as Record<string, unknown>;
  const subject = record.subject;
  const workspaceId = record.workspace_id;
  const scopes = record.scopes;
  if (typeof subject !== "string" || subject.length === 0) return undefined;
  if (typeof workspaceId !== "string" || workspaceId.length === 0) {
    return undefined;
  }
  if (!Array.isArray(scopes) || scopes.some((s) => typeof s !== "string")) {
    return undefined;
  }
  return { subject, workspaceId, scopes: scopes as string[] };
}

/** Keep the public metadata document aligned with the verifier environment. */
export function productionProtectedResource(
  auth: AgentDriveMcpAuthOptions = productionAuthOptions(),
): ProtectedResourceDefinition {
  return protectedResourceForAuth(auth);
}

/**
 * Attach the selected internal credential to internal-ingress requests ONLY.
 *
 * The proof is not authorization — the ingress still verifies the MCP JWT and
 * the routes still intersect its scopes with live local grants. It exists so
 * a process that merely reaches the loopback port cannot tell the ingress
 * from a closed route. The origin check is what keeps it off every other
 * request the SDK makes; `transferFetchApi`, which the SDK uses for signed
 * storage targets, is deliberately not wrapped at all.
 */
export function internalIngressFetch(
  internalUrl: string,
  internalProof: string | undefined,
  fetchApi: typeof fetch = fetch,
  identityTokenProvider?: () => Promise<string>,
): typeof fetch {
  const configuredTarget = new URL(internalUrl);
  const internalOrigin = configuredTarget.origin;
  const internalPrefix = configuredTarget.pathname.replace(/\/$/, "");
  return async (input, init) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const requested = new URL(url);
    if (requested.origin !== internalOrigin) {
      return fetchApi(input, init);
    }
    if (
      internalPrefix &&
      requested.pathname !== internalPrefix &&
      !requested.pathname.startsWith(`${internalPrefix}/`)
    ) {
      requested.pathname = `${internalPrefix}${requested.pathname}`;
    }
    const headers = new Headers(
      input instanceof Request ? input.headers : undefined,
    );
    new Headers(init?.headers).forEach((value, name) =>
      headers.set(name, value),
    );
    if (internalProof) headers.set(INTERNAL_PROOF_HEADER, internalProof);
    if (identityTokenProvider) {
      const identity = `Bearer ${await identityTokenProvider()}`;
      headers.set(SERVERLESS_AUTHORIZATION_HEADER, identity);
      headers.set(INTERNAL_SERVICE_AUTHORIZATION_HEADER, identity);
    }
    const response = await fetchApi(requested, {
      ...init,
      headers,
      redirect: "manual",
    });
    if (![301, 302, 303, 307, 308].includes(response.status)) {
      return response;
    }
    const location = response.headers.get("location");
    if (!location) return response;
    const target = new URL(location, url);
    if (target.origin === internalOrigin) {
      throw new Error("internal AgentDrive redirects are not allowed");
    }
    if (target.protocol !== "https:") {
      throw new Error("AgentDrive content redirects must use HTTPS");
    }
    const method = (
      init?.method ?? (input instanceof Request ? input.method : "GET")
    ).toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      throw new Error("AgentDrive only follows external redirects for reads");
    }
    const externalHeaders = new Headers(headers);
    for (const name of [
      INTERNAL_PROOF_HEADER,
      "authorization",
      "cookie",
      "proxy-authorization",
      SERVERLESS_AUTHORIZATION_HEADER,
      INTERNAL_SERVICE_AUTHORIZATION_HEADER,
    ]) {
      externalHeaders.delete(name);
    }
    return fetchApi(target, {
      ...init,
      method,
      headers: externalHeaders,
      redirect: "follow",
    });
  };
}

/**
 * Production wiring: one static Hub-issued token per MCP request, sent to the
 * workload-identity-protected ingress rather than the public `/v0` surface.
 */
export function productionClientFactory(
  accessToken: string,
  internalUrl: string,
  internalProof?: string,
  internalIdentityAudience?: string,
) {
  const identityTokenProvider = internalIdentityAudience
    ? metadataIdentityTokenProvider(internalIdentityAudience)
    : undefined;
  const client = new AgentDriveClient({
    // SDK 0.0.4 deliberately accepts an origin only. The fetch adapter adds
    // the private mount prefix for remote mode while loopback stays unchanged.
    baseUrl: new URL(internalUrl).origin,
    tokenProvider: new StaticTokenProvider(accessToken),
    fetchApi: internalIngressFetch(
      internalUrl,
      internalProof,
      fetch,
      identityTokenProvider,
    ),
  });
  const artifacts = client.artifacts as typeof client.artifacts & {
    contentResponse: (driveId: string, artifactId: string) => Promise<Response>;
  };
  artifacts.contentResponse = async (driveId, artifactId) => {
    const response = await client.invoke("artifacts_content", () =>
      client.generated.artifacts.artifactsContentRaw({ driveId, artifactId }),
    );
    return response.raw;
  };
  return client as unknown as AgentDriveClientLike;
}

function metadataIdentityTokenProvider(
  audience: string,
  fetchApi: typeof fetch = fetch,
): () => Promise<string> {
  let cached: { token: string; expiresAt: number } | undefined;
  return async () => {
    const now = Math.floor(Date.now() / 1000);
    if (cached && cached.expiresAt - now > 60) return cached.token;
    const endpoint = new URL(
      "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity",
    );
    endpoint.searchParams.set("audience", audience);
    endpoint.searchParams.set("format", "full");
    const response = await fetchApi(endpoint, {
      headers: { "Metadata-Flavor": "Google" },
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) {
      throw new Error("could not mint the MCP service identity token");
    }
    const token = (await response.text()).trim();
    const payload = JSON.parse(
      Buffer.from(token.split(".")[1] ?? "", "base64url").toString("utf8"),
    ) as { exp?: unknown };
    if (typeof payload.exp !== "number") {
      throw new Error("MCP service identity token has no expiry");
    }
    cached = { token, expiresAt: payload.exp };
    return token;
  };
}

if (
  process.argv[1] &&
  pathToFileURL(process.argv[1]).href === import.meta.url
) {
  const port = Number(process.env.PORT ?? 8080);
  const host = process.env.MCP_BIND_HOST?.trim() || "127.0.0.1";
  const runtime = productionRuntimeConfig();
  const clientFactory = (accessToken: string) =>
    productionClientFactory(
      accessToken,
      runtime.internalUrl,
      runtime.internalProof,
      runtime.internalIdentityAudience,
    );
  const server = createAgentDriveMcpHttpServer(
    runtime.authMode === "api-key"
      ? {
          clientFactory,
          apiKey: createApiKeyIntrospector({
            internalUrl: runtime.internalUrl,
            internalProof: runtime.internalProof,
            scopesSupported: AGENTDRIVE_MCP_SCOPES,
          }),
          publicBaseUrl: runtime.publicBaseUrl,
        }
      : {
          clientFactory,
          auth: runtime.auth,
          additionalAuth: runtime.additionalAuth,
          publicBaseUrl: runtime.publicBaseUrl,
        },
  );
  // Loopback remains the safe local default; production Terraform explicitly
  // binds 0.0.0.0 in the dedicated Cloud Run service. Internal credentials
  // are never logged; audiences are public identifiers.
  server.listen(port, host, () => {
    const audiences =
      runtime.auth === undefined
        ? "local API keys"
        : [runtime.auth, ...runtime.additionalAuth]
            .map((auth) => auth.audience)
            .join(", ");
    console.error(
      `AgentDrive MCP listening on ${host}:${port} for ${audiences}`,
    );
  });
}
