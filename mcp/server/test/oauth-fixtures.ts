import {
  createLocalJWKSet,
  exportJWK,
  generateKeyPair,
  SignJWT,
  type JWK,
} from "jose";

import {
  AGENTDRIVE_MCP_RESOURCE,
  AGENTDRIVE_MCP_RESOURCE_METADATA_URL,
  AGENTDRIVE_MCP_SCOPES,
  AGENTDRIVE_PRODUCT_RESOURCE,
  type BearerAuthenticationOptions,
} from "@tokencanopy/mcp-auth";

/** The public `/v0` audience. A token carrying it must be refused at `/mcp`. */
export const PRODUCT_AUDIENCE = AGENTDRIVE_PRODUCT_RESOURCE;

/** The read-only half of the vocabulary — what Hub's read-only consent grants. */
export const READ_ONLY_SCOPES = AGENTDRIVE_MCP_SCOPES.filter((scope) =>
  scope.endsWith(":read"),
);

import type { AgentDriveMcpAuthOptions } from "../src/http.js";

export const TEST_ISSUER = "https://auth.tokencanopy.test/oidc";

export interface TestAuthContext {
  options: AgentDriveMcpAuthOptions;
  token(overrides?: {
    issuer?: string;
    audience?: string | string[];
    scopes?: string;
    expiresAt?: number;
  }): Promise<string>;
}

export async function createTestAuth(): Promise<TestAuthContext> {
  const pair = await generateKeyPair("RS256", { extractable: true });
  const publicJwk: JWK = await exportJWK(pair.publicKey);
  publicJwk.kid = "test-key";
  publicJwk.alg = "RS256";
  publicJwk.use = "sig";
  const jwks = createLocalJWKSet({ keys: [publicJwk] });
  const options: BearerAuthenticationOptions = {
    issuer: TEST_ISSUER,
    audience: AGENTDRIVE_MCP_RESOURCE,
    metadataUrl: AGENTDRIVE_MCP_RESOURCE_METADATA_URL,
    recognizedScopes: AGENTDRIVE_MCP_SCOPES,
    jwks,
    algorithms: ["RS256"],
  };

  return {
    options,
    async token(overrides = {}) {
      const now = Math.floor(Date.now() / 1000);
      return new SignJWT({
        scope: overrides.scopes ?? AGENTDRIVE_MCP_SCOPES.join(" "),
        workspace_id: "tcws_test",
        membership_id: "tcmem_test",
        workspace_role: "member",
        // RFC 9068 section 2.2 makes `client_id` REQUIRED on a JWT access
        // token, so every human token hub mints carries one. Omitting it
        // modelled a token hub never issues — see the same correction in
        // apps/drive/mcp/auth/test/auth.test.ts.
        client_id: "tcmcp_test",
      })
        .setProtectedHeader({ alg: "RS256", kid: "test-key", typ: "at+jwt" })
        .setIssuer(overrides.issuer ?? TEST_ISSUER)
        .setSubject("tcusr_test")
        .setAudience(overrides.audience ?? AGENTDRIVE_MCP_RESOURCE)
        .setIssuedAt(now)
        .setExpirationTime(overrides.expiresAt ?? now + 300)
        .setJti("tctok_test")
        .sign(pair.privateKey);
    },
  };
}
