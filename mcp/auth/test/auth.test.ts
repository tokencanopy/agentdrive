import {
  createLocalJWKSet,
  exportJWK,
  errors as joseErrors,
  generateKeyPair,
  SignJWT,
  type JWK,
  type JWTVerifyGetKey,
} from "jose";
import { beforeAll, describe, expect, it, vi } from "vitest";

import {
  AGENTDRIVE_PROTECTED_RESOURCE,
  authenticateBearer,
  bearerChallenge,
  protectedResourceMetadata,
  type ProtectedResourceDefinition,
  writeBearerFailure,
} from "../src/index.js";

const ISSUER = "https://auth.tokencanopy.test/oidc";
const RESOURCE = "https://drive.tokencanopy.test";
const METADATA_URL = `${RESOURCE}/.well-known/oauth-protected-resource`;
/** The synthetic resource's scope VOCABULARY, not a required bundle. */
const RECOGNIZED_SCOPES = [
  "drives:read",
  "content:read",
  "content:write",
  "changes:read",
] as const;

const definition: ProtectedResourceDefinition = {
  resource: RESOURCE,
  authorizationServers: [ISSUER],
  scopesSupported: [
    "drives:read",
    "content:read",
    "content:write",
    "changes:read",
  ],
  bearerMethodsSupported: ["header"],
  resourceName: "Synthetic Drive MCP",
};

let privateKey: CryptoKey;
let untrustedPrivateKey: CryptoKey;
let jwks: ReturnType<typeof createLocalJWKSet>;

beforeAll(async () => {
  const pair = await generateKeyPair("RS256", { extractable: true });
  privateKey = pair.privateKey;
  untrustedPrivateKey = (await generateKeyPair("RS256", { extractable: true }))
    .privateKey;
  const publicJwk: JWK = await exportJWK(pair.publicKey);
  publicJwk.kid = "test-key";
  publicJwk.alg = "RS256";
  publicJwk.use = "sig";
  jwks = createLocalJWKSet({ keys: [publicJwk] });
});

async function token(
  overrides: {
    issuer?: string;
    audience?: string | string[];
    scopes?: string;
    expiresAt?: number;
    subject?: string;
    claims?: Record<string, unknown>;
  } = {},
  signingKey = privateKey,
): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({
    scope: overrides.scopes ?? RECOGNIZED_SCOPES.join(" "),
    workspace_id: "tcws_test",
    membership_id: "tcmem_test",
    workspace_role: "member",
    // RFC 9068 section 2.2 makes `client_id` REQUIRED on every JWT access
    // token, so hub mints it on human tokens too. Omitting it here modelled a
    // token hub never issues, and that gap hid a bug that rejected every real
    // human token. `tcmcp_*` is the namespace hub's RFC 7591 endpoint mints;
    // the machine-credential space (`tccred_*`) is deliberately NOT it.
    client_id: "tcmcp_test",
    ...overrides.claims,
  })
    .setProtectedHeader({ alg: "RS256", kid: "test-key", typ: "at+jwt" })
    .setIssuer(overrides.issuer ?? ISSUER)
    .setSubject(overrides.subject ?? "tcusr_test")
    .setAudience(overrides.audience ?? RESOURCE)
    .setIssuedAt(now)
    .setExpirationTime(overrides.expiresAt ?? now + 300)
    .setJti("tctok_test")
    .sign(signingKey);
}

function recordingResponse() {
  return {
    statusCode: 0,
    headers: {} as Record<string, string>,
    body: "",
    setHeader(name: string, value: string) {
      this.headers[name.toLowerCase()] = value;
    },
    end(body = "") {
      this.body = body;
    },
  };
}

describe("protected resource metadata", () => {
  it("emits the exact RFC 9728 document from a typed resource definition", () => {
    expect(protectedResourceMetadata(definition)).toEqual({
      resource: RESOURCE,
      authorization_servers: [ISSUER],
      scopes_supported: [
        "drives:read",
        "content:read",
        "content:write",
        "changes:read",
      ],
      bearer_methods_supported: ["header"],
      resource_name: "Synthetic Drive MCP",
    });
  });

  it("freezes the exact AgentDrive private-preview metadata document", () => {
    expect(protectedResourceMetadata(AGENTDRIVE_PROTECTED_RESOURCE)).toEqual({
      resource: "https://drive.tokencanopy.com/mcp",
      authorization_servers: ["https://auth.tokencanopy.com/oidc"],
      scopes_supported: [
        "drives:read",
        "drives:write",
        "content:read",
        "content:write",
        "changes:read",
        "sharing:read",
        "sharing:write",
        "usage:read",
      ],
      bearer_methods_supported: ["header"],
      resource_name: "AgentDrive MCP",
    });
  });
});

describe("Bearer authentication", () => {
  const options = () => ({
    issuer: ISSUER,
    audience: RESOURCE,
    metadataUrl: METADATA_URL,
    recognizedScopes: RECOGNIZED_SCOPES,
    jwks,
    algorithms: ["RS256"] as const,
  });

  it("keeps MCP authentication failures at the HTTP transport boundary", async () => {
    const cases = [
      undefined,
      "Basic synthetic",
      "Bearer malformed",
      `Bearer ${await token({ expiresAt: 1 })}`,
      `Bearer ${await token({ audience: "https://wrong.test" })}`,
    ];
    for (const authorization of cases) {
      const result = await authenticateBearer(authorization, options());
      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("expected authentication failure");
      const response = recordingResponse();
      writeBearerFailure(response, result);
      expect(response.statusCode).toBe(401);
      expect(response.headers["www-authenticate"]).toContain(
        `resource_metadata="${METADATA_URL}"`,
      );
      if (authorization === undefined || authorization.startsWith("Basic")) {
        expect(response.body).toBe("");
      } else {
        expect(JSON.parse(response.body)).toEqual({ error: "invalid_token" });
      }
    }

    // A token carrying NONE of this resource's vocabulary. A PARTIAL grant
    // is accepted -- per-tool authorization decides what it can do.
    const insufficientResult = await authenticateBearer(
      `Bearer ${await token({ scopes: "jobs:read" })}`,
      options(),
    );
    expect(insufficientResult.ok).toBe(false);
    if (insufficientResult.ok) throw new Error("expected scope failure");
    const insufficient = recordingResponse();
    writeBearerFailure(insufficient, insufficientResult);
    expect(insufficient.statusCode).toBe(403);
    expect(insufficient.headers["www-authenticate"]).toContain(
      'error="insufficient_scope"',
    );
  });

  it("accepts a Hub-signed, exact-audience token with all required scopes", async () => {
    const accessToken = await token();
    const result = await authenticateBearer(`Bearer ${accessToken}`, options());
    expect(result).toMatchObject({
      ok: true,
      claims: {
        iss: ISSUER,
        aud: RESOURCE,
        sub: "tcusr_test",
        workspace_id: "tcws_test",
        membership_id: "tcmem_test",
        workspace_role: "member",
      },
      scopes: RECOGNIZED_SCOPES,
    });
  });

  it("accepts a human token carrying the RFC 9068 client_id hub actually mints", async () => {
    // The regression. `client_id` is REQUIRED on every JWT access token, so
    // every human token hub issues carries one. Treating its presence as an
    // agent marker rejected all of them, and no fixture noticed because the
    // human fixture omitted the claim entirely.
    const result = await authenticateBearer(
      `Bearer ${await token({
        claims: {
          client_id: "https://claude.ai/oauth/claude-code-client-metadata",
        },
      })}`,
      options(),
    );
    expect(result.ok).toBe(true);
  });

  it("rejects a human token whose client is a machine credential", async () => {
    // The check `client_id` CAN perform, and the reason it stays in the
    // contract rather than being dropped: a `tccred_*` client on a human
    // subject is incoherent. Simply removing `client_id` from the agent-claim
    // set would let this through.
    const result = await authenticateBearer(
      `Bearer ${await token({ claims: { client_id: "tccred_test" } })}`,
      options(),
    );
    expect(result).toMatchObject({ ok: false, error: "invalid_token" });
  });

  it("accepts a human token with no client_id, as hub's BFF path mints", async () => {
    // The claim is OPTIONAL on a human token. Hub's BFF omits it by contract
    // (no OAuth client exists behind a browser session) while its OAuth path
    // must emit it, so requiring it would reject the console and forbidding it
    // would reject MCP. Only the namespace test is sound -- and this stays in
    // step with AgentDrive's own verifier, which guards the same audience.
    const result = await authenticateBearer(
      `Bearer ${await token({ claims: { client_id: undefined } })}`,
      options(),
    );
    expect(result.ok).toBe(true);
  });

  it("accepts the mutually exclusive agent claim shape", async () => {
    const result = await authenticateBearer(
      `Bearer ${await token({
        subject: "tcagt_test",
        claims: {
          membership_id: "tcagm_test",
          client_id: "tccred_test",
          credential_id: "tccred_test",
          runtime_id: "tcrun_test",
          sponsor_id: "tcusr_sponsor",
          workspace_role: undefined,
        },
      })}`,
      options(),
    );
    expect(result.ok).toBe(true);
  });

  it.each([
    ["empty subject", { subject: "" }],
    [
      "human token carrying agent claims",
      {
        claims: {
          client_id: "tccred_test",
          credential_id: "tccred_test",
          runtime_id: "tcrun_test",
          sponsor_id: "tcusr_test",
        },
      },
    ],
    [
      "human membership with an agent prefix",
      { claims: { membership_id: "tcagm_test" } },
    ],
  ])("rejects %s before scope authorization", async (_name, overrides) => {
    const result = await authenticateBearer(
      `Bearer ${await token(overrides)}`,
      options(),
    );
    expect(result).toMatchObject({
      ok: false,
      status: 401,
      error: "invalid_token",
    });
  });

  it.each([
    ["empty", "Bearer "],
    ["invalid", "Bearer definitely-not-a-jwt"],
  ])("returns a transport-safe 401 for a %s bearer", async (_name, header) => {
    const result = await authenticateBearer(header, options());
    expect(result).toEqual({
      ok: false,
      status: 401,
      error: "invalid_token",
      challenge: bearerChallenge({
        metadataUrl: METADATA_URL,
        error: "invalid_token",
      }),
    });
  });

  it("omits invalid_token from the challenge when credentials are missing", async () => {
    const result = await authenticateBearer(undefined, options());
    expect(result).toEqual({
      ok: false,
      status: 401,
      challenge: bearerChallenge({ metadataUrl: METADATA_URL }),
    });
  });

  it("treats unsupported schemes as missing credentials and accepts case-insensitive Bearer", async () => {
    const unsupported = await authenticateBearer("Basic synthetic", options());
    expect(unsupported).toEqual({
      ok: false,
      status: 401,
      challenge: bearerChallenge({ metadataUrl: METADATA_URL }),
    });

    const mixedCase = await authenticateBearer(
      `bEaReR ${await token()}`,
      options(),
    );
    expect(mixedCase.ok).toBe(true);
  });

  it.each([
    ["expired", () => token({ expiresAt: 1 })],
    ["wrong issuer", () => token({ issuer: "https://evil.test/oidc" })],
    ["wrong audience", () => token({ audience: "https://chat.test" })],
    ["wrong signature", () => token({}, untrustedPrivateKey)],
    [
      "multi-valued audience",
      () => token({ audience: [RESOURCE, "https://chat.test"] }),
    ],
  ])("returns 401 for a %s token", async (_name, makeToken) => {
    const result = await authenticateBearer(
      `Bearer ${await makeToken()}`,
      options(),
    );
    expect(result).toMatchObject({
      ok: false,
      status: 401,
      error: "invalid_token",
    });
    if (!result.ok) expect(result.challenge).toContain(METADATA_URL);
  });

  it("returns a Bearer insufficient_scope challenge without weakening to a tool error", async () => {
    const result = await authenticateBearer(
      `Bearer ${await token({ scopes: "jobs:read" })}`,
      options(),
    );
    expect(result).toEqual({
      ok: false,
      status: 403,
      error: "insufficient_scope",
      challenge: bearerChallenge({
        metadataUrl: METADATA_URL,
        error: "insufficient_scope",
        scope: RECOGNIZED_SCOPES.join(" "),
      }),
    });
  });

  it("accepts a PARTIAL grant and reports exactly what it holds", async () => {
    // The regression this locks. Requiring the whole bundle made Hub's own
    // read-only consent option fail at `initialize`: a read-only grant
    // carries a strict subset, so the safest choice on the consent screen was
    // the one that did not work. Authentication now returns a scope SET and
    // per-tool authorization decides the rest.
    const result = await authenticateBearer(
      `Bearer ${await token({ scopes: "drives:read content:read" })}`,
      options(),
    );
    expect(result).toMatchObject({ ok: true });
    if (!result.ok) throw new Error("expected authentication success");
    expect(result.scopes).toEqual(["drives:read", "content:read"]);
  });

  it("drops scopes outside this resource's vocabulary from the context", async () => {
    // A client may hold scopes for another resource, or ones this deployment
    // does not implement. They are not an error and they are not authority.
    const result = await authenticateBearer(
      `Bearer ${await token({
        scopes: "content:read drives:write sharing:write",
      })}`,
      options(),
    );
    if (!result.ok) throw new Error("expected authentication success");
    expect(result.scopes).toEqual(["content:read"]);
  });

  it("deduplicates repeated scopes", async () => {
    const result = await authenticateBearer(
      `Bearer ${await token({ scopes: "content:read content:read" })}`,
      options(),
    );
    if (!result.ok) throw new Error("expected authentication success");
    expect(result.scopes).toEqual(["content:read"]);
  });

  it("refuses a token whose scope value is outside the RFC 6749 grammar", async () => {
    // A malformed scope is a malformed TOKEN, not merely an unprivileged one.
    // Silently discarding the offending value would let a quoting or
    // injection bug upstream go unnoticed.
    for (const scopes of [
      'content:read "quoted"',
      "content:read back\\slash",
      "content:read with\u0000nul",
      "content:read \u00e9accent",
    ]) {
      const result = await authenticateBearer(
        `Bearer ${await token({ scopes })}`,
        options(),
      );
      expect(result).toMatchObject({ ok: false, error: "invalid_token" });
    }
  });

  it("returns 503 when the remote JWKS verifier is temporarily unavailable", async () => {
    const unavailableJwks: JWTVerifyGetKey = async () => {
      throw new joseErrors.JWKSTimeout();
    };
    const result = await authenticateBearer(`Bearer ${await token()}`, {
      ...options(),
      jwks: unavailableJwks,
    });
    expect(result).toMatchObject({
      ok: false,
      status: 503,
      error: "temporarily_unavailable",
    });
    if (result.ok) throw new Error("expected temporary authentication failure");
    const response = recordingResponse();
    writeBearerFailure(response, result);
    expect(response.statusCode).toBe(503);
    expect(response.headers["retry-after"]).toBe("5");
  });

  it("never logs or returns the token on authentication failure", async () => {
    const accessToken = await token({ audience: "https://wrong.test" });
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const result = await authenticateBearer(
        `Bearer ${accessToken}`,
        options(),
      );
      expect(JSON.stringify(result)).not.toContain(accessToken);
      expect(consoleError).not.toHaveBeenCalled();
      expect(consoleWarn).not.toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
      consoleWarn.mockRestore();
    }
  });
});
