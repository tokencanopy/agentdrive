import {
  createRemoteJWKSet,
  errors as joseErrors,
  jwtVerify,
  type JWTClaimVerificationOptions,
  type JWTVerifyGetKey,
  type JWTPayload,
} from "jose";

/**
 * The public AgentDrive `/v0` product resource. The audience of a machine
 * `tccred_*` token and of Hub's BFF delegation — NOT of an MCP session token.
 * Exported so a caller can name the distinction; nothing in this package
 * verifies against it.
 */
export const AGENTDRIVE_PRODUCT_RESOURCE = "https://drive.tokencanopy.com";

/**
 * The AgentDrive MCP resource, and the exact `aud` of every MCP session
 * token.
 *
 * DISTINCT FROM the product resource since the 2026-08-28 security
 * remediation. They used to be the same string, which made an MCP session
 * token a fully valid `/v0` product token and reduced the reviewed tool
 * surface to a suggestion.
 */
export const AGENTDRIVE_MCP_RESOURCE = `${AGENTDRIVE_PRODUCT_RESOURCE}/mcp`;

/** RFC 9728 path-scoped document for the `/mcp` resource. The ROOT document
 * at `/.well-known/oauth-protected-resource` is a DIFFERENT document
 * describing public `/v0`, and AgentDrive itself serves it. */
export const AGENTDRIVE_MCP_RESOURCE_METADATA_URL = `${AGENTDRIVE_PRODUCT_RESOURCE}/.well-known/oauth-protected-resource/mcp`;

export const TOKEN_CANOPY_HUB_ISSUER = "https://auth.tokencanopy.com/oidc";

/**
 * The scope VOCABULARY of the AgentDrive MCP resource — every scope any tool
 * can require.
 *
 * Deliberately not a required bundle any more. Requiring all of them made
 * Hub's own read-only consent choice unusable: a read-only grant carries
 * only the read scopes, so the transport answered `insufficient_scope` at
 * `initialize` and the safest consent option was the one that did not work.
 * Per-tool authorization decides what a given token can actually do.
 *
 * `drives:write` joined the vocabulary after the audience split: while the
 * MCP and product audiences were one string it would have granted direct
 * `/v0` drive administration, but an MCP-audience token can no longer reach
 * `/v0`, so the scope gates only the reviewed create-drive tool.
 */
export const AGENTDRIVE_MCP_SCOPES = [
  "drives:read",
  "drives:write",
  "content:read",
  "content:write",
  "changes:read",
  "sharing:read",
  "sharing:write",
  "usage:read",
] as const;

export const AGENTDRIVE_PROTECTED_RESOURCE: ProtectedResourceDefinition = {
  resource: AGENTDRIVE_MCP_RESOURCE,
  authorizationServers: [TOKEN_CANOPY_HUB_ISSUER],
  scopesSupported: AGENTDRIVE_MCP_SCOPES,
  bearerMethodsSupported: ["header"],
  resourceName: "AgentDrive MCP",
};

const remoteJwksResolvers = new WeakSet<object>();

export interface ProtectedResourceDefinition {
  resource: string;
  authorizationServers: readonly string[];
  scopesSupported: readonly string[];
  bearerMethodsSupported: readonly "header"[];
  resourceName?: string;
}

export interface ProtectedResourceMetadata {
  resource: string;
  authorization_servers: string[];
  scopes_supported: string[];
  bearer_methods_supported: "header"[];
  resource_name?: string;
}

export function protectedResourceMetadata(
  definition: ProtectedResourceDefinition,
): ProtectedResourceMetadata {
  return {
    resource: definition.resource,
    authorization_servers: [...definition.authorizationServers],
    scopes_supported: [...definition.scopesSupported],
    bearer_methods_supported: [...definition.bearerMethodsSupported],
    ...(definition.resourceName
      ? { resource_name: definition.resourceName }
      : {}),
  };
}

export type BearerError =
  "invalid_token" | "insufficient_scope" | "temporarily_unavailable";

export function bearerChallenge(options: {
  metadataUrl: string;
  error?: BearerError;
  scope?: string;
}): string {
  const parameters = [`resource_metadata="${quoted(options.metadataUrl)}"`];
  if (options.error) parameters.push(`error="${options.error}"`);
  if (options.scope) parameters.push(`scope="${quoted(options.scope)}"`);
  return `Bearer ${parameters.join(", ")}`;
}

function quoted(value: string): string {
  return value.replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

export interface TokenCanopyAccessTokenClaims extends JWTPayload {
  sub: string;
  scope: string;
  workspace_id: string;
  membership_id: string;
  jti: string;
  workspace_role?: "owner" | "admin" | "member";
  client_id?: string;
  credential_id?: string;
  runtime_id?: string;
  sponsor_id?: string;
}

export interface BearerAuthenticationOptions {
  issuer: string;
  audience: string;
  metadataUrl: string;
  /**
   * The resource's scope vocabulary. Authentication intersects the presented
   * scopes with this and returns the result; a token carrying NONE of them
   * authorizes nothing and is refused with `insufficient_scope`.
   */
  recognizedScopes: readonly string[];
  /**
   * An optional HARD bundle every token must carry. Empty for AgentDrive MCP:
   * a read-only grant is a legitimate grant, and requiring the whole bundle
   * is what made Hub's read-only consent option fail at `initialize`. Kept as
   * a seam for a future resource whose surface genuinely has no read-only
   * mode.
   */
  requiredScopes?: readonly string[];
  jwks: JWTVerifyGetKey;
  algorithms?: readonly string[];
  clockTolerance?: JWTClaimVerificationOptions["clockTolerance"];
}

/**
 * What a verified request carries into the protocol layer. The scope set is
 * the authorization decision input — it is NOT a claim that every scope was
 * checked, only that these are the ones this token holds for this resource.
 */
export type BearerAuthenticationResult =
  | {
      ok: true;
      accessToken: string;
      claims: TokenCanopyAccessTokenClaims;
      /** Presented scopes ∩ `recognizedScopes`, deduplicated. */
      scopes: string[];
    }
  | {
      ok: false;
      status: 401 | 403 | 503;
      error?: BearerError;
      challenge: string;
    };

export interface BearerFailureResponse {
  statusCode: number;
  setHeader(name: string, value: string): void;
  end(body?: string): void;
}

export function writeBearerFailure(
  response: BearerFailureResponse,
  failure: Extract<BearerAuthenticationResult, { ok: false }>,
): void {
  response.statusCode = failure.status;
  response.setHeader("cache-control", "no-store");
  if (failure.status === 503) response.setHeader("retry-after", "5");
  if (failure.challenge) {
    response.setHeader("www-authenticate", failure.challenge);
  }
  if (failure.error) {
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify({ error: failure.error }));
  } else {
    response.end();
  }
}

export function remoteJwks(jwksUrl: string | URL): JWTVerifyGetKey {
  const resolver = createRemoteJWKSet(new URL(jwksUrl));
  remoteJwksResolvers.add(resolver);
  return resolver;
}

export async function authenticateBearer(
  authorization: string | string[] | undefined,
  options: BearerAuthenticationOptions,
): Promise<BearerAuthenticationResult> {
  if (authorization === undefined) return missingToken(options.metadataUrl);
  const parsed = parseBearer(authorization);
  if (parsed.kind === "missing") return missingToken(options.metadataUrl);
  if (parsed.kind === "invalid") return invalidToken(options.metadataUrl);
  const accessToken = parsed.value;

  try {
    const { payload, protectedHeader } = await jwtVerify(
      accessToken,
      options.jwks,
      {
        issuer: options.issuer,
        audience: options.audience,
        algorithms: [...(options.algorithms ?? ["RS256"])],
        typ: "at+jwt",
        requiredClaims: [
          "iss",
          "sub",
          "aud",
          "scope",
          "iat",
          "exp",
          "jti",
          "workspace_id",
          "membership_id",
        ],
        ...(options.clockTolerance === undefined
          ? {}
          : { clockTolerance: options.clockTolerance }),
      },
    );
    if (
      protectedHeader.alg === "none" ||
      payload.aud !== options.audience ||
      !validPlatformClaims(payload)
    ) {
      return invalidToken(options.metadataUrl);
    }

    const presented = uniqueScopes(payload.scope);
    // A scope value outside RFC 6749's `scope-token` charset means the token
    // is malformed, not merely unprivileged. Refuse it as a bad token rather
    // than silently dropping the value and proceeding.
    if (presented === undefined) return invalidToken(options.metadataUrl);

    const recognized = new Set(options.recognizedScopes);
    const scopes = presented.filter((scope) => recognized.has(scope));

    const required = options.requiredScopes ?? [];
    const insufficient =
      !required.every((scope) => scopes.includes(scope)) || scopes.length === 0;
    if (insufficient) {
      return {
        ok: false,
        status: 403,
        error: "insufficient_scope",
        challenge: bearerChallenge({
          metadataUrl: options.metadataUrl,
          error: "insufficient_scope",
          scope: (required.length > 0
            ? required
            : options.recognizedScopes
          ).join(" "),
        }),
      };
    }

    return {
      ok: true,
      accessToken,
      claims: payload,
      scopes,
    };
  } catch (error) {
    // Authentication failures are intentionally collapsed. The JOSE error
    // can include attacker-controlled token fields and is never logged here.
    if (
      error instanceof joseErrors.JWKSTimeout ||
      error instanceof joseErrors.JWKSInvalid ||
      (error instanceof TypeError &&
        remoteJwksResolvers.has(options.jwks as object))
    ) {
      return temporarilyUnavailable(options.metadataUrl);
    }
    if (error instanceof joseErrors.JOSEError || error instanceof TypeError) {
      return invalidToken(options.metadataUrl);
    }
    return invalidToken(options.metadataUrl);
  }
}

function parseBearer(
  authorization: string | string[] | undefined,
):
  { kind: "missing" } | { kind: "invalid" } | { kind: "token"; value: string } {
  if (typeof authorization !== "string") return { kind: "missing" };
  if (!/^Bearer(?:[ \t]+|$)/iu.test(authorization)) {
    return { kind: "missing" };
  }
  const match = /^Bearer[ \t]+([A-Za-z0-9_~+./=-]+)$/iu.exec(authorization);
  const value = match?.[1];
  return value ? { kind: "token", value } : { kind: "invalid" };
}

/**
 * RFC 6749 section 3.3: `scope = scope-token *( SP scope-token )`, where
 * `scope-token = 1*( %x21 / %x23-5B / %x5D-7E )` — printable ASCII except
 * space, double quote, and backslash.
 *
 * Returns `undefined` when any token is outside that grammar, so the caller
 * can refuse the token instead of quietly ignoring the offending value.
 */
const SCOPE_TOKEN = /^[\x21\x23-\x5B\x5D-\x7E]+$/u;

function uniqueScopes(scope: string): string[] | undefined {
  const tokens = scope.split(" ").filter(Boolean);
  if (!tokens.every((token) => SCOPE_TOKEN.test(token))) return undefined;
  return [...new Set(tokens)];
}

function validPlatformClaims(
  payload: JWTPayload,
): payload is TokenCanopyAccessTokenClaims {
  const requiredStrings = [
    payload.sub,
    payload.scope,
    payload.workspace_id,
    payload.membership_id,
    payload.jti,
  ];
  const clientId = payload.client_id;
  const credentialId = payload.credential_id;
  const runtimeId = payload.runtime_id;
  const sponsorId = payload.sponsor_id;
  if (
    requiredStrings.some(
      (value) => typeof value !== "string" || value.trim().length === 0,
    )
  ) {
    return false;
  }

  // AGENT-ONLY claims. `client_id` is deliberately NOT one of them: RFC 9068
  // section 2.2 makes it a REQUIRED claim of every JWT access token, so
  // oidc-provider mints it on human tokens too and its mere presence
  // discriminates nothing. Treating it as an agent marker rejected every
  // human token Hub issues, which is what made the MCP unreachable from
  // Claude Code and Codex alike.
  const hasAgentOnlyClaim = [credentialId, runtimeId, sponsorId].some(
    (value) => value !== undefined,
  );
  // What DOES discriminate is the client id's namespace. `tccred_*` is the
  // machine-credential space the token endpoint dispatches on
  // (src/product-tokens/client-credentials.ts), and hub's env validation
  // forbids an OIDC client id from sitting in it, so the two token families
  // are cleanly separated by prefix. The agent branch below independently
  // requires this prefix; the human branch requires its absence.
  const isCredentialClient =
    typeof clientId === "string" && clientId.startsWith("tccred_");
  const hasWorkspaceRole = payload.workspace_role !== undefined;
  if (
    typeof payload.sub !== "string" ||
    typeof payload.membership_id !== "string"
  ) {
    return false;
  }

  if (payload.sub.startsWith("tcusr_")) {
    return (
      payload.membership_id.startsWith("tcmem_") &&
      !hasAgentOnlyClaim &&
      // OPTIONAL, but never a machine credential. Hub mints human tokens two
      // ways and only one carries the claim: the BFF omits it by contract
      // (issuance design §2.3 — no OAuth client exists behind a browser
      // session), while the OAuth path must emit it per RFC 9068 §2.2.
      // Requiring it would reject the first family; forbidding it rejects the
      // second. Only the namespace test is sound. Kept deliberately identical
      // to AgentDrive's `product_token.py`: in production both verifiers guard
      // the SAME audience, and a disagreement between them is exactly the
      // drift that made this surface unreachable.
      !isCredentialClient &&
      (payload.workspace_role === "owner" ||
        payload.workspace_role === "admin" ||
        payload.workspace_role === "member")
    );
  }
  if (payload.sub.startsWith("tcagt_")) {
    const agentClaims = [clientId, credentialId, runtimeId, sponsorId];
    if (
      typeof clientId !== "string" ||
      typeof credentialId !== "string" ||
      typeof runtimeId !== "string" ||
      typeof sponsorId !== "string"
    ) {
      return false;
    }
    return (
      payload.membership_id.startsWith("tcagm_") &&
      !hasWorkspaceRole &&
      agentClaims.every(
        (value) => typeof value === "string" && value.trim().length > 0,
      ) &&
      clientId === credentialId &&
      clientId.startsWith("tccred_") &&
      runtimeId.startsWith("tcrun_") &&
      sponsorId.startsWith("tcusr_")
    );
  }
  return false;
}

function invalidToken(metadataUrl: string): BearerAuthenticationResult {
  return {
    ok: false,
    status: 401,
    error: "invalid_token",
    challenge: bearerChallenge({ metadataUrl, error: "invalid_token" }),
  };
}

function missingToken(metadataUrl: string): BearerAuthenticationResult {
  return {
    ok: false,
    status: 401,
    // RFC 6750 omits the error parameter when the request provided no
    // authentication. RFC 9728's resource_metadata pointer still tells an
    // MCP client where to begin authorization.
    challenge: bearerChallenge({ metadataUrl }),
  };
}

function temporarilyUnavailable(
  metadataUrl: string,
): BearerAuthenticationResult {
  return {
    ok: false,
    status: 503,
    error: "temporarily_unavailable",
    challenge: bearerChallenge({ metadataUrl }),
  };
}
