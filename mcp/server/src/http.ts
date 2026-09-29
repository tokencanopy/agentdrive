import {
  createServer,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  authenticateBearer,
  bearerChallenge,
  protectedResourceMetadata,
  writeBearerFailure,
  type BearerAuthenticationOptions,
  type BearerAuthenticationResult,
  type ProtectedResourceDefinition,
} from "@tokencanopy/mcp-auth";

import { createAgentDriveMcpServer } from "./server.js";
import type { AgentDriveClientFactory } from "./types.js";

export const MCP_PATH = "/mcp";
export const MCP_ORIGIN = "https://drive.tokencanopy.com";
/**
 * The header the FastAPI edge sets to say WHICH origin a request arrived on
 * (ADR-0002: one process, one resource per single-purpose origin). Its value
 * is one of this server's configured origins, set by the edge from the
 * surface it selected — never copied from the client, whose own copy the
 * edge's forwarding allowlist drops. Absent means the primary resource, so a
 * deployment with one origin is unchanged; an unknown value is a 404, not a
 * fallback, because a request the edge did not label for a configured origin
 * is not one this process should answer.
 */
export const MCP_ORIGIN_HEADER = "x-agentdrive-mcp-origin";
/**
 * The ROOT RFC 9728 document path. This server does NOT serve it: since the
 * 2026-08-28 audience split it describes the public `/v0` product resource,
 * with the full `/v0` scope list, and AgentDrive's own discovery route owns
 * it. Kept as a named constant only so the path-scoped form below is
 * obviously derived from it.
 */
export const RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource";
/** The MCP resource's own document — the ONLY one this server serves. */
export const MCP_RESOURCE_METADATA_PATH = `${RESOURCE_METADATA_PATH}${MCP_PATH}`;
const MAX_REQUEST_BYTES = 2 * 1024 * 1024;

class RequestBodyTooLargeError extends Error {
  constructor() {
    super("MCP request body is too large");
    this.name = "RequestBodyTooLargeError";
  }
}

/**
 * What `/_internal/introspect` says about a presented API key.
 *
 * `AUTH_MODE=local` only. The sidecar holds no database, so it cannot resolve
 * an opaque key itself; AgentDrive's loopback ingress does, behind the
 * supervisor's per-boot proof, and answers with the same actor its own `/v0`
 * boundary would have built.
 */
export interface AgentDriveMcpApiKeyIntrospection {
  subject: string;
  workspaceId: string;
  scopes: string[];
}

/**
 * The api-key authenticator (§4.2 as amended 2026-09-21).
 *
 * `"unauthorized"` is the caller's key being wrong — revoked, expired,
 * unknown, malformed — and becomes the SAME 401 a bad JWT gets. `"unavailable"`
 * is the ingress being unreachable or answering something unexpected, and
 * becomes a 503: a credential store that is down is not a credential that is
 * bad, and telling the two apart is what stops a restart looking like a
 * revocation.
 */
export interface AgentDriveMcpApiKeyOptions {
  introspect: (
    key: string,
  ) => Promise<
    AgentDriveMcpApiKeyIntrospection | "unauthorized" | "unavailable"
  >;
  /** The scope vocabulary the RFC 9728 document advertises. */
  scopesSupported: readonly string[];
}

export interface AgentDriveMcpHttpOptions {
  clientFactory: AgentDriveClientFactory;
  /** The PRIMARY resource — Hub issuer, audience, JWKS resolver, and the MCP
   * scope vocabulary. Selected when no `MCP_ORIGIN_HEADER` is present.
   * Required in JWT mode, absent in api-key mode. */
  auth?: AgentDriveMcpAuthOptions;
  /**
   * Opaque API keys instead of Hub JWTs (`AUTH_MODE=local`). Mutually
   * exclusive with `auth`: a deployment either has an authorization server or
   * it does not, and a process that would accept both is one misconfiguration
   * away from accepting a key where a consent-issued token was intended.
   */
  apiKey?: AgentDriveMcpApiKeyOptions;
  /**
   * Further resources this one process serves, each on its own origin
   * (ADR-0002's `<product>.mcp.<zone>`), selected per request by
   * `MCP_ORIGIN_HEADER`. A bearer is verified against the SELECTED resource's
   * audience and no other: the origins are separate RFC 8707 resources with
   * separate audiences, and neither accepts the other's token.
   */
  additionalAuth?: readonly AgentDriveMcpAuthOptions[];
  /** The public shell origin `publish` builds permalinks on; undefined omits
   * `public_url` with a warning. See `AgentDriveMcpToolOptions`. */
  publicBaseUrl?: string;
}

interface ServedResource {
  origin: string;
  /** Absent in api-key mode, which verifies nothing. */
  auth?: AgentDriveMcpAuthOptions;
  protectedResource: ProtectedResourceDefinition;
}

/**
 * The resource a SELF-HOSTED install names for itself: the origin the client
 * actually reached, plus `/mcp`.
 *
 * Read off the request rather than configured, because a standalone install
 * has no fixed origin — it is whatever the operator put in front of it — and
 * making them declare one to get a discovery document would be a setting that
 * exists only to be typed. Reflecting `Host` in an unauthenticated document
 * is safe HERE specifically because the document's whole content is "this is
 * the resource, and there is NO authorization server": the list an attacker
 * would want to poison is empty by construction.
 */
function localResource(
  request: IncomingMessage,
  scopesSupported: readonly string[],
): ServedResource {
  const forwarded = request.headers["x-forwarded-proto"];
  const proto =
    (typeof forwarded === "string" ? forwarded.split(",")[0]?.trim() : "") ||
    "http";
  const scheme = proto === "https" ? "https" : "http";
  const host = request.headers.host?.trim() || "localhost";
  let origin: string;
  try {
    origin = new URL(`${scheme}://${host}`).origin;
  } catch {
    origin = "http://localhost";
  }
  return {
    origin,
    protectedResource: {
      resource: `${origin}${MCP_PATH}`,
      // No OAuth server to discover: the credential is a key the operator
      // minted with `agentdrive-keys`. An honest empty list, matching what
      // AgentDrive's own `/v0` document says in the same mode.
      authorizationServers: [],
      scopesSupported,
      bearerMethodsSupported: ["header"],
      resourceName: "AgentDrive MCP",
    },
  };
}

/** Parse `Authorization: Bearer <key>` the way `mcp-auth` parses a JWT. */
function bearerValue(
  authorization: string | string[] | undefined,
): string | undefined {
  if (typeof authorization !== "string") return undefined;
  const [scheme, ...rest] = authorization.trim().split(/\s+/u);
  if (scheme?.toLowerCase() !== "bearer") return undefined;
  const value = rest.join(" ").trim();
  return value.length > 0 ? value : undefined;
}

/**
 * Authenticate a presented opaque key through the internal ingress.
 *
 * The failure shapes are deliberately the ones `mcp-auth` produces, so a
 * client meeting a 401 here sees exactly what it sees against the hosted
 * transport: the same status, the same `error="invalid_token"`, the same
 * `resource_metadata` pointer. Only the thing behind the boundary differs.
 */
async function authenticateApiKey(
  authorization: string | string[] | undefined,
  apiKey: AgentDriveMcpApiKeyOptions,
  resource: string,
): Promise<BearerAuthenticationResult> {
  const metadataUrl = `${new URL(resource).origin}${MCP_RESOURCE_METADATA_PATH}`;
  const key = bearerValue(authorization);
  if (key === undefined) {
    // No credential at all: a bare challenge, no error attribute (RFC 6750
    // §3 — an error code is for a credential that was PRESENTED and refused).
    return {
      ok: false,
      status: 401,
      challenge: bearerChallenge({ metadataUrl }),
    };
  }
  const resolved = await apiKey.introspect(key);
  if (resolved === "unavailable") {
    return {
      ok: false,
      status: 503,
      error: "temporarily_unavailable",
      challenge: bearerChallenge({
        metadataUrl,
        error: "temporarily_unavailable",
      }),
    };
  }
  if (resolved === "unauthorized") {
    return {
      ok: false,
      status: 401,
      error: "invalid_token",
      challenge: bearerChallenge({ metadataUrl, error: "invalid_token" }),
    };
  }
  // Intersected with this resource's vocabulary, exactly as `authenticateBearer`
  // intersects a JWT's `scope` claim: a scope the MCP surface does not
  // recognise authorizes nothing here however the API spells it.
  const scopes = [...new Set(resolved.scopes)].filter((scope) =>
    apiKey.scopesSupported.includes(scope),
  );
  if (scopes.length === 0) {
    // A grant with nothing this resource recognises is `insufficient_scope`,
    // not `invalid_token`: the credential is fine, it just cannot do anything
    // here — the same distinction `mcp-auth` draws.
    return {
      ok: false,
      status: 403,
      error: "insufficient_scope",
      challenge: bearerChallenge({
        metadataUrl,
        error: "insufficient_scope",
        scope: apiKey.scopesSupported.join(" "),
      }),
    };
  }
  return {
    ok: true,
    accessToken: key,
    // The claim shape `mcp-auth` returns for a Hub token, filled from the
    // introspection rather than from a signature. There is no `jti` to carry
    // — a key is not a token — so it names the subject, which is what the
    // protocol layer below uses it for.
    claims: {
      sub: resolved.subject,
      scope: scopes.join(" "),
      workspace_id: resolved.workspaceId,
      membership_id: `local_${resolved.workspaceId}`,
      jti: `local_${resolved.subject}`,
    },
    scopes,
  };
}

function servedResources(options: {
  auth: AgentDriveMcpAuthOptions;
  additionalAuth?: readonly AgentDriveMcpAuthOptions[];
}): {
  primary: ServedResource;
  byOrigin: ReadonlyMap<string, ServedResource>;
} {
  const byOrigin = new Map<string, ServedResource>();
  let primary: ServedResource | undefined;
  for (const auth of [options.auth, ...(options.additionalAuth ?? [])]) {
    validateAuthMetadata(auth);
    const resource: ServedResource = {
      origin: new URL(auth.audience).origin,
      auth,
      protectedResource: protectedResourceForAuth(auth),
    };
    if (byOrigin.has(resource.origin)) {
      // Two resources on one origin cannot be told apart by the header, so
      // one would silently answer for the other — the pre-split shape again.
      throw new Error(
        `MCP auth: two resources share the origin ${resource.origin}; each MCP resource needs its own origin`,
      );
    }
    byOrigin.set(resource.origin, resource);
    primary ??= resource;
  }
  return { primary: primary!, byOrigin };
}

function selectResource(
  request: IncomingMessage,
  resources: ReturnType<typeof servedResources>,
): ServedResource | undefined {
  const label = request.headers[MCP_ORIGIN_HEADER];
  if (label === undefined) return resources.primary;
  // Node joins repeated headers with ", ", which no configured origin
  // contains, so a duplicated label resolves nothing — fail closed.
  if (typeof label !== "string") return undefined;
  return resources.byOrigin.get(label);
}

export type AgentDriveMcpAuthOptions = BearerAuthenticationOptions;

export function protectedResourceForAuth(
  auth: AgentDriveMcpAuthOptions,
): ProtectedResourceDefinition {
  return {
    resource: auth.audience,
    authorizationServers: [auth.issuer],
    scopesSupported: auth.recognizedScopes,
    bearerMethodsSupported: ["header"],
    resourceName: "AgentDrive MCP",
  };
}

/**
 * Fail closed on a misconfigured audience.
 *
 * The audience must be the `/mcp` resource, not the product origin. Accepting
 * a bare origin here is exactly the pre-2026-08-28 configuration that made an
 * MCP session token a valid `/v0` product token, so it is refused rather than
 * quietly honoured. The metadata URL must be that audience's PATH-SCOPED
 * RFC 9728 document; the root document belongs to public `/v0`.
 */
function validateAuthMetadata(auth: AgentDriveMcpAuthOptions): void {
  const audience = new URL(auth.audience);
  const metadata = new URL(auth.metadataUrl);
  if (
    !["http:", "https:"].includes(audience.protocol) ||
    audience.username ||
    audience.password ||
    audience.pathname !== MCP_PATH ||
    audience.search ||
    audience.hash ||
    metadata.origin !== audience.origin ||
    metadata.pathname !== MCP_RESOURCE_METADATA_PATH ||
    metadata.search ||
    metadata.hash
  ) {
    throw new Error(
      "MCP auth audience must be the /mcp resource and metadataUrl its path-scoped protected-resource document",
    );
  }
}

/**
 * Create the stateless Node HTTP server used by the hosted `/mcp` surface.
 *
 * A fresh MCP server, transport, and static-token SDK client are created for
 * every request. This is intentional: a bearer token must never be retained
 * in a process-global session or accidentally reused for another agent.
 */
export function createAgentDriveMcpHttpServer(
  options: AgentDriveMcpHttpOptions,
): Server {
  if ((options.auth === undefined) === (options.apiKey === undefined)) {
    throw new Error(
      "MCP auth: configure exactly one of `auth` (Hub JWTs) and `apiKey` (local API keys)",
    );
  }
  if (options.apiKey && options.additionalAuth?.length) {
    // ADR-0002's per-origin resources exist because each has its OWN
    // audience. A key has no audience, so there is nothing to tell apart.
    throw new Error("MCP auth: additionalAuth is a JWT-mode concept");
  }
  const apiKey = options.apiKey;
  const resources = apiKey
    ? undefined
    : servedResources({
        auth: options.auth!,
        additionalAuth: options.additionalAuth,
      });
  return createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url ?? "/", MCP_ORIGIN).pathname;
      if (pathname === "/health" || pathname === "/ready") {
        if (request.method !== "GET") {
          methodNotAllowed(response, "GET");
          return;
        }
        json(response, 200, { status: "ok" });
        return;
      }
      // Everything below is served FOR one resource: its document, its
      // challenge, its audience. Decided once, here, from the edge's label —
      // or, in api-key mode, from the request itself, because a self-hosted
      // install has no configured origin to name and no authorization server
      // to send anyone to.
      const resource = apiKey
        ? localResource(request, apiKey.scopesSupported)
        : selectResource(request, resources!);
      if (resource === undefined) {
        json(response, 404, {
          error: { code: "not_found", message: "not found" },
        });
        return;
      }
      // The PATH-SCOPED document only. Serving the root form here would
      // answer for the public `/v0` product resource, which this process is
      // not the authority for: it names a different audience, and its scope
      // list is maintained separately even though the strings coincide.
      //
      // `no-store`, not a max-age: a cached discovery document is how a
      // client ends up acting on a stale resource identifier, and the edge
      // in front of this process pins the same directive on everything it
      // proxies so the two layers cannot disagree.
      if (pathname === MCP_RESOURCE_METADATA_PATH) {
        if (request.method !== "GET") {
          methodNotAllowed(response, "GET");
          return;
        }
        json(
          response,
          200,
          protectedResourceMetadata(resource.protectedResource),
        );
        return;
      }
      if (pathname !== MCP_PATH) {
        json(response, 404, {
          error: { code: "not_found", message: "not found" },
        });
        return;
      }

      const authentication = apiKey
        ? await authenticateApiKey(
            request.headers.authorization,
            apiKey,
            resource.protectedResource.resource,
          )
        : await authenticateBearer(
            request.headers.authorization,
            resource.auth!,
          );
      if (!authentication.ok) {
        writeBearerFailure(response, authentication);
        return;
      }
      if (!["POST", "GET", "DELETE"].includes(request.method ?? "")) {
        methodNotAllowed(response, "POST, GET, DELETE");
        return;
      }

      let body: unknown;
      if (request.method === "POST") {
        try {
          body = await readJson(request);
        } catch (error) {
          if (error instanceof RequestBodyTooLargeError) {
            rpcError(response, 413, -32000, "MCP request body is too large");
          } else {
            rpcError(response, 400, -32700, "Invalid JSON request");
          }
          return;
        }
      }
      const server = createAgentDriveMcpServer(
        options.clientFactory(authentication.accessToken),
        { scopes: authentication.scopes },
        { publicBaseUrl: options.publicBaseUrl },
      );
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,
        enableJsonResponse: true,
      });
      let connected = false;
      try {
        await server.connect(transport);
        connected = true;
        await transport.handleRequest(request, response, body);
      } finally {
        if (connected) {
          await server.close();
        } else {
          await transport.close();
        }
      }
    } catch (error) {
      if (response.headersSent) {
        response.destroy(error instanceof Error ? error : undefined);
        return;
      }
      rpcError(response, 500, -32603, "MCP server error");
    }
  });
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.byteLength;
    if (size > MAX_REQUEST_BYTES) throw new RequestBodyTooLargeError();
    chunks.push(buffer);
  }
  if (size === 0) return undefined;
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function json(response: ServerResponse, status: number, value: unknown): void {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  response.end(body);
}

function methodNotAllowed(response: ServerResponse, allow: string): void {
  response.writeHead(405, {
    Allow: allow,
    "Cache-Control": "no-store",
  });
  response.end();
}

function rpcError(
  response: ServerResponse,
  status: number,
  code: number,
  message: string,
): void {
  json(response, status, {
    jsonrpc: "2.0",
    error: { code, message },
    id: null,
  });
}
