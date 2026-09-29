import { describe, expect, it } from "vitest";

import {
  additionalAudiences,
  INTERNAL_PROOF_HEADER,
  internalIngressFetch,
  productionClientFactory,
  productionRuntimeConfig,
} from "../src/index.js";

const INTERNAL_URL = "http://127.0.0.1:8082";
// 43 characters: exactly what `secrets.token_urlsafe(32)` produces.
const INTERNAL_PROOF = "synthetic-per-boot-proof-0123456789abcdefXY";

const PRODUCTION_ENV = {
  MCP_AGENTDRIVE_INTERNAL_URL: INTERNAL_URL,
  MCP_INTERNAL_PROOF: INTERNAL_PROOF,
};

const STAGING_ENV = {
  ...PRODUCTION_ENV,
  MCP_ENVIRONMENT: "staging",
  MCP_AUTH_ISSUER: "https://auth.staging.tokencanopy.test/oidc",
  MCP_AUTH_AUDIENCE: "https://drive.staging.tokencanopy.test/mcp",
  MCP_AUTH_METADATA_URL:
    "https://drive.staging.tokencanopy.test/.well-known/oauth-protected-resource/mcp",
  MCP_AUTH_JWKS_URL:
    "https://auth.staging.tokencanopy.test/.well-known/jwks.json",
};

/**
 * A JWT-mode runtime configuration, narrowed.
 *
 * `auth` and `protectedResource` became optional when `MCP_AUTH_MODE=api-key`
 * arrived — a self-hosted install has no verifier and no configured resource
 * — so the hosted assertions below say which shape they expect once, here,
 * instead of asserting non-null at every field.
 */
function jwtRuntimeConfig(environment: NodeJS.ProcessEnv) {
  const config = productionRuntimeConfig(environment);
  if (config.auth === undefined || config.protectedResource === undefined) {
    throw new Error("expected a JWT-mode runtime configuration");
  }
  return {
    ...config,
    auth: config.auth,
    protectedResource: config.protectedResource,
  };
}

describe("AgentDrive MCP runtime configuration", () => {
  it("uses the canonical production tuple by default", () => {
    const config = jwtRuntimeConfig(PRODUCTION_ENV);
    expect(config.environment).toBe("production");
    // The MCP audience is the /mcp resource, NOT the public product origin.
    expect(config.auth.audience).toBe("https://drive.tokencanopy.com/mcp");
    expect(config.auth.metadataUrl).toBe(
      "https://drive.tokencanopy.com/.well-known/oauth-protected-resource/mcp",
    );
    expect(config.internalUrl).toBe(INTERNAL_URL);
  });

  it("advertises the scope vocabulary rather than a required bundle", () => {
    const config = jwtRuntimeConfig(PRODUCTION_ENV);
    expect(config.auth.requiredScopes).toBeUndefined();
    expect(config.auth.recognizedScopes).toContain("content:read");
    expect(config.auth.recognizedScopes).toContain("content:write");
    expect(config.protectedResource.scopesSupported).toEqual(
      config.auth.recognizedScopes,
    );
  });

  it("keeps a complete staging verifier together", () => {
    const config = jwtRuntimeConfig(STAGING_ENV);
    expect(config.environment).toBe("staging");
    expect(config.auth.audience).toBe(STAGING_ENV.MCP_AUTH_AUDIENCE);
    expect(config.auth.issuer).toBe(STAGING_ENV.MCP_AUTH_ISSUER);
    expect(config.protectedResource).toMatchObject({
      resource: STAGING_ENV.MCP_AUTH_AUDIENCE,
      authorizationServers: [STAGING_ENV.MCP_AUTH_ISSUER],
    });
  });

  it.each([
    [
      "partial override",
      {
        ...PRODUCTION_ENV,
        MCP_ENVIRONMENT: "staging",
        MCP_AUTH_AUDIENCE: STAGING_ENV.MCP_AUTH_AUDIENCE,
      },
    ],
    [
      "non-production without overrides",
      { ...PRODUCTION_ENV, MCP_ENVIRONMENT: "staging" },
    ],
  ])("rejects %s configuration", (_name, environment) => {
    expect(() => productionRuntimeConfig(environment)).toThrow(
      "MCP runtime configuration must set together",
    );
  });

  it("leaves the public origin undefined when MCP_PUBLIC_BASE_URL is unset", () => {
    expect(
      productionRuntimeConfig(PRODUCTION_ENV).publicBaseUrl,
    ).toBeUndefined();
    expect(productionRuntimeConfig(STAGING_ENV).publicBaseUrl).toBeUndefined();
  });

  it("accepts exactly an https origin as the public base", () => {
    const config = productionRuntimeConfig({
      ...STAGING_ENV,
      MCP_PUBLIC_BASE_URL: "https://share.staging.tokencanopy.test",
    });
    expect(config.publicBaseUrl).toBe("https://share.staging.tokencanopy.test");
  });

  it.each([
    ["a path", "https://share.staging.tokencanopy.test/a"],
    ["a trailing slash", "https://share.staging.tokencanopy.test/"],
    ["a non-https scheme", "http://share.staging.tokencanopy.test"],
    ["a query", "https://share.staging.tokencanopy.test?x=1"],
    ["not a URL", "share.staging.tokencanopy.test"],
  ])("refuses %s as the public base", (_name, value) => {
    expect(() =>
      productionRuntimeConfig({ ...STAGING_ENV, MCP_PUBLIC_BASE_URL: value }),
    ).toThrow(/MCP_PUBLIC_BASE_URL/u);
  });

  it("treats the public base as an override: it demands the explicit tuple", () => {
    // Set alone against the production defaults it is an override like the
    // others, so it requires MCP_ENVIRONMENT and the full auth tuple — but it
    // is never itself required, so an unset value boots everywhere.
    expect(() =>
      productionRuntimeConfig({
        ...PRODUCTION_ENV,
        MCP_PUBLIC_BASE_URL: "https://share.tokencanopy.test",
      }),
    ).toThrow("MCP_ENVIRONMENT is required");
    expect(() =>
      productionRuntimeConfig({
        ...PRODUCTION_ENV,
        MCP_ENVIRONMENT: "production",
        MCP_PUBLIC_BASE_URL: "https://share.tokencanopy.test",
      }),
    ).toThrow("MCP runtime configuration must set together");
  });

  it("requires an explicit environment when any production value is overridden", () => {
    expect(() =>
      productionRuntimeConfig({ ...STAGING_ENV, MCP_ENVIRONMENT: undefined }),
    ).toThrow("MCP_ENVIRONMENT is required");
  });

  it("refuses a bare product origin as the token audience", () => {
    // The pre-2026-08-28 spelling. Accepting it here would restore exactly
    // the confusion the audience split removed: the MCP would verify tokens
    // that public /v0 also accepts.
    expect(() =>
      productionRuntimeConfig({
        ...STAGING_ENV,
        MCP_AUTH_AUDIENCE: "https://drive.staging.tokencanopy.test",
      }),
    ).toThrow("must be an origin plus the exact /mcp path");
  });

  it("rejects metadata that is not the audience's path-scoped document", () => {
    for (const metadataUrl of [
      // Right origin, ROOT document -- that one describes public /v0.
      "https://drive.staging.tokencanopy.test/.well-known/oauth-protected-resource",
      // Right document, wrong origin.
      "https://drive.tokencanopy.test/.well-known/oauth-protected-resource/mcp",
    ]) {
      expect(() =>
        productionRuntimeConfig({
          ...STAGING_ENV,
          MCP_AUTH_METADATA_URL: metadataUrl,
        }),
      ).toThrow("MCP_AUTH_METADATA_URL must be");
    }
  });

  it("refuses an unprotected or ambiguously addressed internal data plane", () => {
    for (const internalUrl of [
      "https://drive.tokencanopy.com",
      "http://10.0.0.5:8082",
      "http://agentdrive.internal:8082",
      // A NAME, not an address. `localhost` resolves -- through /etc/hosts, a
      // resolver, or a container DNS policy -- so accepting it would put the
      // one check between the per-boot proof and the network outside this
      // process's control.
      "http://localhost:8082",
      "",
    ]) {
      expect(() =>
        productionRuntimeConfig({
          ...PRODUCTION_ENV,
          MCP_AGENTDRIVE_INTERNAL_URL: internalUrl,
        }),
      ).toThrow("MCP_AGENTDRIVE_INTERNAL_URL");
    }
  });

  it("refuses to start without a per-boot proof of usable length", () => {
    for (const proof of [undefined, "", "short", "a".repeat(42)]) {
      expect(() =>
        productionRuntimeConfig({
          ...PRODUCTION_ENV,
          MCP_INTERNAL_PROOF: proof,
        }),
      ).toThrow("MCP_INTERNAL_PROOF");
    }
  });

  it("accepts a remote internal API only with an explicit service audience", () => {
    const remote = {
      MCP_AGENTDRIVE_INTERNAL_URL:
        "https://agentdrive-abc-uc.a.run.app/_internal/mcp",
      MCP_AGENTDRIVE_INTERNAL_AUDIENCE: "https://agentdrive-abc-uc.a.run.app",
    };
    const config = productionRuntimeConfig(remote);
    expect(config.internalUrl).toBe(remote.MCP_AGENTDRIVE_INTERNAL_URL);
    expect(config.internalIdentityAudience).toBe(
      remote.MCP_AGENTDRIVE_INTERNAL_AUDIENCE,
    );
    expect(config.internalProof).toBeUndefined();

    expect(() =>
      productionRuntimeConfig({
        MCP_AGENTDRIVE_INTERNAL_URL: remote.MCP_AGENTDRIVE_INTERNAL_URL,
      }),
    ).toThrow("MCP_AGENTDRIVE_INTERNAL_AUDIENCE");
    expect(() =>
      productionRuntimeConfig({
        ...remote,
        MCP_INTERNAL_PROOF: INTERNAL_PROOF,
      }),
    ).toThrow("must not be set for a remote internal API");
    expect(() =>
      productionRuntimeConfig({
        MCP_AGENTDRIVE_INTERNAL_URL: INTERNAL_URL,
        MCP_INTERNAL_PROOF: INTERNAL_PROOF,
        MCP_AGENTDRIVE_INTERNAL_AUDIENCE: "https://agentdrive-abc-uc.a.run.app",
      }),
    ).toThrow("must not be set for loopback mode");
  });

  it("points the SDK client at the loopback ingress, never the public origin", () => {
    const client = productionClientFactory(
      "synthetic-access-token",
      INTERNAL_URL,
      INTERNAL_PROOF,
    ) as unknown as { baseUrl: string };
    expect(client.baseUrl).toBe(INTERNAL_URL);
  });

  it("constructs the SDK in remote mode with an origin-only base URL", () => {
    const internalUrl = "https://agentdrive-abc-uc.a.run.app/_internal/mcp";
    const client = productionClientFactory(
      "synthetic-access-token",
      internalUrl,
      undefined,
      "https://agentdrive-abc-uc.a.run.app",
    ) as unknown as { baseUrl: string };

    expect(client.baseUrl).toBe("https://agentdrive-abc-uc.a.run.app");
  });
});

describe("internalIngressFetch", () => {
  /** Records the headers of the last call. A holder rather than a bare `let`
   * so TypeScript does not narrow it to `undefined` inside a loop. */
  function recorder(): {
    fetchApi: typeof fetch;
    lastHeaders: () => Headers | undefined;
    reset: () => void;
  } {
    let headers: Headers | undefined;
    return {
      lastHeaders: () => headers,
      reset: () => {
        headers = undefined;
      },
      fetchApi: (async (_input, init) => {
        headers = new Headers(init?.headers);
        return new Response(null, { status: 204 });
      }) as typeof fetch,
    };
  }

  it("attaches the per-boot proof to internal-ingress requests", async () => {
    const { fetchApi, lastHeaders } = recorder();
    const wrapped = internalIngressFetch(
      INTERNAL_URL,
      INTERNAL_PROOF,
      fetchApi,
    );
    await wrapped(`${INTERNAL_URL}/v0/drives`);
    expect(lastHeaders()?.get(INTERNAL_PROOF_HEADER)).toBe(INTERNAL_PROOF);
  });

  it("never attaches the proof to any other origin", async () => {
    // A signed storage target, a redirect, or a misconfiguration must not
    // carry the container's per-boot secret onto the network.
    const { fetchApi, lastHeaders, reset } = recorder();
    const wrapped = internalIngressFetch(
      INTERNAL_URL,
      INTERNAL_PROOF,
      fetchApi,
    );
    for (const url of [
      "https://drive.tokencanopy.com/v0/drives",
      "https://storage.googleapis.test/signed",
      "http://127.0.0.1:9999/v0/drives",
    ]) {
      reset();
      await wrapped(url);
      expect(lastHeaders()).toBeDefined();
      expect(lastHeaders()?.get(INTERNAL_PROOF_HEADER)).toBeNull();
    }
  });

  it("preserves the caller's other headers", async () => {
    const { fetchApi, lastHeaders } = recorder();
    const wrapped = internalIngressFetch(
      INTERNAL_URL,
      INTERNAL_PROOF,
      fetchApi,
    );
    await wrapped(`${INTERNAL_URL}/v0/drives`, {
      headers: { authorization: "Bearer synthetic", "x-request-id": "r1" },
    });
    expect(lastHeaders()?.get("authorization")).toBe("Bearer synthetic");
    expect(lastHeaders()?.get("x-request-id")).toBe("r1");
  });

  it("accepts a Request or URL, not only a string", async () => {
    // The SDK's generated runtime builds a Request; the origin check must not
    // depend on the caller's input shape.
    const { fetchApi, lastHeaders, reset } = recorder();
    const wrapped = internalIngressFetch(
      INTERNAL_URL,
      INTERNAL_PROOF,
      fetchApi,
    );
    await wrapped(new URL(`${INTERNAL_URL}/v0/drives`));
    expect(lastHeaders()?.get(INTERNAL_PROOF_HEADER)).toBe(INTERNAL_PROOF);
    reset();
    await wrapped(new Request(`${INTERNAL_URL}/v0/drives`));
    expect(lastHeaders()?.get(INTERNAL_PROOF_HEADER)).toBe(INTERNAL_PROOF);
  });

  it("does not forward the proof or bearer across an external redirect", async () => {
    const calls: Array<{
      url: string;
      headers: Headers;
      redirect?: RequestRedirect;
    }> = [];
    const fetchApi = (async (input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      calls.push({
        url,
        headers: new Headers(init?.headers),
        redirect: init?.redirect,
      });
      if (calls.length === 1) {
        return new Response(null, {
          status: 307,
          headers: { location: "https://storage.googleapis.test/signed" },
        });
      }
      return new Response("bytes", { status: 200 });
    }) as typeof fetch;
    const wrapped = internalIngressFetch(
      INTERNAL_URL,
      INTERNAL_PROOF,
      fetchApi,
    );

    const response = await wrapped(`${INTERNAL_URL}/v0/content`, {
      headers: { authorization: "Bearer synthetic" },
    });

    expect(response.status).toBe(200);
    expect(calls).toHaveLength(2);
    expect(calls[0]?.redirect).toBe("manual");
    expect(calls[0]?.headers.get(INTERNAL_PROOF_HEADER)).toBe(INTERNAL_PROOF);
    expect(calls[1]?.url).toBe("https://storage.googleapis.test/signed");
    expect(calls[1]?.headers.get(INTERNAL_PROOF_HEADER)).toBeNull();
    expect(calls[1]?.headers.get("authorization")).toBeNull();
  });

  it("authenticates remote internal calls as the dedicated MCP service", async () => {
    const seen: Array<{ url: string; headers: Headers }> = [];
    const fetchApi = (async (input, init) => {
      seen.push({ url: String(input), headers: new Headers(init?.headers) });
      return new Response(null, { status: 204 });
    }) as typeof fetch;
    const wrapped = internalIngressFetch(
      "https://agentdrive-abc-uc.a.run.app/_internal/mcp",
      undefined,
      fetchApi,
      async () => "synthetic-service-identity",
    );

    // This is the origin-only URL SDK 0.0.4 actually produces. The adapter
    // owns insertion of the private mount prefix in remote mode.
    await wrapped("https://agentdrive-abc-uc.a.run.app/v0/drives", {
      headers: { authorization: "Bearer synthetic-user" },
    });

    expect(seen[0]?.url).toBe(
      "https://agentdrive-abc-uc.a.run.app/_internal/mcp/v0/drives",
    );
    expect(seen[0]?.headers.get("x-serverless-authorization")).toBe(
      "Bearer synthetic-service-identity",
    );
    expect(seen[0]?.headers.get("x-agentdrive-mcp-service-authorization")).toBe(
      "Bearer synthetic-service-identity",
    );
    expect(seen[0]?.headers.get("authorization")).toBe("Bearer synthetic-user");
  });
});

describe("MCP_AUTH_ADDITIONAL_AUDIENCES (ADR-0002)", () => {
  it("is empty unless configured, so a one-origin deployment is unchanged", () => {
    expect(productionRuntimeConfig(PRODUCTION_ENV).additionalAuth).toEqual([]);
    expect(
      productionRuntimeConfig({
        ...PRODUCTION_ENV,
        MCP_AUTH_ADDITIONAL_AUDIENCES: " , ",
      }).additionalAuth,
    ).toEqual([]);
  });

  it("derives each extra resource from its audience and shares the verifier", () => {
    const config = jwtRuntimeConfig({
      ...PRODUCTION_ENV,
      MCP_AUTH_ADDITIONAL_AUDIENCES: "https://drive.mcp.tokencanopy.com/mcp",
    });
    expect(config.additionalAuth).toHaveLength(1);
    const extra = config.additionalAuth[0]!;
    expect(extra.audience).toBe("https://drive.mcp.tokencanopy.com/mcp");
    // The metadata URL is derived, never a second value to hold in step.
    expect(extra.metadataUrl).toBe(
      "https://drive.mcp.tokencanopy.com/.well-known/oauth-protected-resource/mcp",
    );
    expect(extra.issuer).toBe(config.auth.issuer);
    expect(extra.jwks).toBe(config.auth.jwks);
    expect(extra.recognizedScopes).toBe(config.auth.recognizedScopes);
  });

  it.each([
    [
      "a bare origin — that spelling is a product resource",
      "https://drive.mcp.tokencanopy.com",
    ],
    ["a trailing slash", "https://drive.mcp.tokencanopy.com/mcp/"],
    ["the primary's own origin", "https://drive.tokencanopy.com/mcp"],
    [
      "two entries on one origin",
      "https://a.mcp.tokencanopy.com/mcp,https://a.mcp.tokencanopy.com/mcp",
    ],
  ])("refuses %s", (_name, value) => {
    expect(() =>
      additionalAudiences(value, jwtRuntimeConfig(PRODUCTION_ENV).auth),
    ).toThrow();
  });
});
