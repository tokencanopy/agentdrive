import type { AddressInfo } from "node:net";

import { afterEach, describe, expect, it } from "vitest";

import { createAgentDriveMcpHttpServer } from "../src/http.js";
import {
  API_KEY_CACHE_MAX_ENTRIES,
  API_KEY_CACHE_TTL_MS,
  createApiKeyIntrospector,
  INTERNAL_INTROSPECT_PATH,
  INTERNAL_PROOF_HEADER,
  productionRuntimeConfig,
} from "../src/index.js";
import type { AgentDriveClientLike } from "../src/types.js";

const PROOF = "synthetic-per-boot-proof-0123456789abcdefXY";
const INTERNAL_URL = "http://127.0.0.1:8082";
const KEY = "adk_" + "k".repeat(40);
const SCOPES = [
  "drives:read",
  "drives:write",
  "usage:read",
  "content:read",
] as const;

const LOCAL_ENV = {
  MCP_AUTH_MODE: "api-key",
  MCP_ENVIRONMENT: "local",
  MCP_AGENTDRIVE_INTERNAL_URL: INTERNAL_URL,
  MCP_INTERNAL_PROOF: PROOF,
} satisfies NodeJS.ProcessEnv;

const openServers: ReturnType<typeof createAgentDriveMcpHttpServer>[] = [];

afterEach(async () => {
  await Promise.all(
    openServers.splice(0).map(
      (server) =>
        new Promise<void>((resolve, reject) => {
          if (!server.listening) {
            resolve();
            return;
          }
          server.close((error) => (error ? reject(error) : resolve()));
        }),
    ),
  );
});

function driveClient(): AgentDriveClientLike {
  return {
    drives: {
      async list() {
        return {
          items: [
            {
              id: "drv_0000000000000001",
              rootFolderId: "fld_0000000000000001",
              revision: "rev-1",
            },
          ],
          nextCursor: null,
        };
      },
      async usage() {
        return { storageBytes: 0, retrievalBytes: 0 };
      },
    },
  } as unknown as AgentDriveClientLike;
}

async function startApiKeyServer(
  introspect: Parameters<typeof createAgentDriveMcpHttpServer>[0] extends never
    ? never
    : (
        key: string,
      ) => ReturnType<
        ReturnType<typeof createApiKeyIntrospector>["introspect"]
      >,
  onToken?: (token: string) => void,
): Promise<string> {
  const server = createAgentDriveMcpHttpServer({
    clientFactory: (accessToken) => {
      onToken?.(accessToken);
      return driveClient();
    },
    apiKey: { introspect, scopesSupported: SCOPES },
  });
  openServers.push(server);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address() as AddressInfo;
  return `http://127.0.0.1:${address.port}`;
}

const resolved = {
  subject: "tcagt_0123456789abcdef",
  workspaceId: "default",
  scopes: ["drives:read", "usage:read", "content:read"],
};

// ---------------------------------------------------------------------------
// Runtime configuration
// ---------------------------------------------------------------------------

describe("MCP_AUTH_MODE", () => {
  it("is absent for every hosted deployment, and its absence is the JWT path", () => {
    const hosted = productionRuntimeConfig({
      MCP_AGENTDRIVE_INTERNAL_URL: "http://127.0.0.1:8082",
      MCP_INTERNAL_PROOF: PROOF,
    });
    expect(hosted.authMode).toBe("jwt");
    expect(hosted.auth?.audience).toBe("https://drive.tokencanopy.com/mcp");
    expect(hosted.protectedResource).toBeDefined();
  });

  it("accepts api-key + a non-production environment with no issuer tuple", () => {
    const config = productionRuntimeConfig(LOCAL_ENV);
    expect(config.authMode).toBe("api-key");
    expect(config.environment).toBe("local");
    expect(config.internalUrl).toBe(INTERNAL_URL);
    expect(config.internalProof).toBe(PROOF);
    // Nothing to verify against, so nothing is configured — the four names
    // the JWT branch demands together are absent, deliberately.
    expect(config.auth).toBeUndefined();
    expect(config.protectedResource).toBeUndefined();
    expect(config.additionalAuth).toEqual([]);
  });

  it.each([
    ["an unknown mode", { ...LOCAL_ENV, MCP_AUTH_MODE: "apikey" }],
    ["no environment", { ...LOCAL_ENV, MCP_ENVIRONMENT: "" }],
    // The mode exists BECAUSE the deployment is not the hosted one; letting
    // it inherit the production label would make a log line lie.
    ["the production label", { ...LOCAL_ENV, MCP_ENVIRONMENT: "production" }],
    [
      "a stale issuer",
      { ...LOCAL_ENV, MCP_AUTH_ISSUER: "https://hub.example.test/oidc" },
    ],
    [
      "a stale audience",
      { ...LOCAL_ENV, MCP_AUTH_AUDIENCE: "https://drive.example.test/mcp" },
    ],
    ["no internal url", { ...LOCAL_ENV, MCP_AGENTDRIVE_INTERNAL_URL: "" }],
    ["a short proof", { ...LOCAL_ENV, MCP_INTERNAL_PROOF: "too-short" }],
  ])("refuses %s", (_name, environment) => {
    expect(() => productionRuntimeConfig(environment)).toThrow();
  });
});

// ---------------------------------------------------------------------------
// The introspection client
// ---------------------------------------------------------------------------

describe("the api-key introspector", () => {
  function stub(handler: (request: Request) => Response | Promise<Response>): {
    calls: Request[];
    fetchApi: typeof fetch;
  } {
    const calls: Request[] = [];
    const fetchApi = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input as RequestInfo, init);
      calls.push(request);
      return handler(request);
    }) as unknown as typeof fetch;
    return { calls, fetchApi };
  }

  const body = {
    subject: resolved.subject,
    principal_type: "agent",
    workspace_id: resolved.workspaceId,
    scopes: resolved.scopes,
    workspace_role: null,
    sponsor_id: "tcusr_fedcba9876543210",
    key_id: "adk_kkkkkkkk",
  };

  it("POSTs the key and the boot proof to the ingress", async () => {
    const { calls, fetchApi } = stub(() => Response.json(body));
    const introspector = createApiKeyIntrospector({
      internalUrl: INTERNAL_URL,
      internalProof: PROOF,
      scopesSupported: SCOPES,
      fetchApi,
    });
    expect(await introspector.introspect(KEY)).toEqual(resolved);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.method).toBe("POST");
    expect(calls[0]!.url).toBe(`${INTERNAL_URL}${INTERNAL_INTROSPECT_PATH}`);
    expect(calls[0]!.headers.get("authorization")).toBe(`Bearer ${KEY}`);
    expect(calls[0]!.headers.get(INTERNAL_PROOF_HEADER)).toBe(PROOF);
  });

  it("caches a positive answer per key for 30s and never a failure", async () => {
    let answer: Response = Response.json(body);
    let clock = 1_000;
    const { calls, fetchApi } = stub(() => answer.clone());
    const introspector = createApiKeyIntrospector({
      internalUrl: INTERNAL_URL,
      internalProof: PROOF,
      scopesSupported: SCOPES,
      fetchApi,
      now: () => clock,
    });
    await introspector.introspect(KEY);
    await introspector.introspect(KEY);
    expect(calls).toHaveLength(1);
    // A DIFFERENT key is a different entry.
    await introspector.introspect("adk_" + "z".repeat(40));
    expect(calls).toHaveLength(2);
    // The cache shortens tools/list; it is not authorization. Every
    // tools/call reaches the ingress with the key and is resolved again,
    // so the window a revoked key can still enumerate tools in is this one.
    answer = new Response(null, { status: 401 });
    clock += API_KEY_CACHE_TTL_MS + 1;
    expect(await introspector.introspect(KEY)).toBe("unauthorized");
    expect(calls).toHaveLength(3);
    // Failures are never cached: a revocation and an outage both take effect
    // on the next request rather than on the next expiry.
    expect(await introspector.introspect(KEY)).toBe("unauthorized");
    expect(calls).toHaveLength(4);
  });

  it("is bounded, so a flood of distinct keys cannot grow it without limit", async () => {
    const { calls, fetchApi } = stub(() => Response.json(body));
    const introspector = createApiKeyIntrospector({
      internalUrl: INTERNAL_URL,
      internalProof: PROOF,
      scopesSupported: SCOPES,
      fetchApi,
    });
    const first = "adk_" + "a".repeat(40);
    await introspector.introspect(first);
    for (let i = 0; i < API_KEY_CACHE_MAX_ENTRIES; i += 1) {
      await introspector.introspect(`adk_${String(i).padStart(40, "0")}`);
    }
    const before = calls.length;
    // The oldest entry was evicted, so the first key is asked again.
    await introspector.introspect(first);
    expect(calls.length).toBe(before + 1);
  });

  it.each([
    ["a 401", () => new Response(null, { status: 401 }), "unauthorized"],
    ["a 404", () => new Response(null, { status: 404 }), "unavailable"],
    ["a 503", () => new Response(null, { status: 503 }), "unavailable"],
    ["garbage", () => new Response("not json"), "unavailable"],
    [
      "a body with no subject",
      () => Response.json({ scopes: [] }),
      "unavailable",
    ],
    [
      "a body with non-string scopes",
      () => Response.json({ ...body, scopes: [1, 2] }),
      "unavailable",
    ],
    [
      "a transport failure",
      () => {
        throw new Error("connection refused");
      },
      "unavailable",
    ],
  ])("maps %s to %s", async (_name, handler, expected) => {
    const { fetchApi } = stub(handler as () => Response);
    const introspector = createApiKeyIntrospector({
      internalUrl: INTERNAL_URL,
      internalProof: PROOF,
      scopesSupported: SCOPES,
      fetchApi,
    });
    expect(await introspector.introspect(KEY)).toBe(expected);
  });
});

// ---------------------------------------------------------------------------
// The HTTP boundary
// ---------------------------------------------------------------------------

describe("the api-key HTTP boundary", () => {
  it("serves an RFC 9728 document with no authorization server", async () => {
    const origin = await startApiKeyServer(async () => resolved);
    const response = await fetch(
      `${origin}/.well-known/oauth-protected-resource/mcp`,
    );
    expect(response.status).toBe(200);
    const document = await response.json();
    // Honest: there is no OAuth flow to discover, and the resource is the
    // origin the client actually reached rather than a hosted string a
    // self-hoster never configured.
    expect(document.authorization_servers).toEqual([]);
    expect(document.resource).toBe(`${origin}/mcp`);
    expect(document.scopes_supported).toEqual([...SCOPES]);
    expect(document.bearer_methods_supported).toEqual(["header"]);
    expect(response.headers.get("cache-control")).toBe("no-store");
    // The ROOT document still belongs to AgentDrive's own `/v0` surface.
    expect(
      (await fetch(`${origin}/.well-known/oauth-protected-resource`)).status,
    ).toBe(404);
  });

  it("refuses a missing, unknown or unresolvable key like a bad JWT", async () => {
    let seen = 0;
    const origin = await startApiKeyServer(async (key) => {
      seen += 1;
      if (key === "adk_down") return "unavailable";
      return key === KEY ? resolved : "unauthorized";
    });

    const missing = await fetch(`${origin}/mcp`, {
      method: "POST",
      body: "{}",
    });
    expect(missing.status).toBe(401);
    // RFC 6750 §3: no error code for a credential that was never presented.
    expect(missing.headers.get("www-authenticate")).toContain(
      `${origin}/.well-known/oauth-protected-resource/mcp`,
    );
    expect(missing.headers.get("www-authenticate")).not.toContain("error=");
    expect(seen).toBe(0);

    const wrong = await fetch(`${origin}/mcp`, {
      method: "POST",
      headers: {
        Authorization: "Bearer adk_nope",
        "Content-Type": "application/json",
      },
      body: "{}",
    });
    expect(wrong.status).toBe(401);
    expect(wrong.headers.get("www-authenticate")).toContain(
      'error="invalid_token"',
    );

    // The ingress being unreachable is OUR boundary being down, not the
    // caller's key being bad — and a restart must not look like a revocation.
    const down = await fetch(`${origin}/mcp`, {
      method: "POST",
      headers: {
        Authorization: "Bearer adk_down",
        "Content-Type": "application/json",
      },
      body: "{}",
    });
    expect(down.status).toBe(503);
    expect(down.headers.get("retry-after")).toBe("5");
  });

  it("forwards the caller's key unchanged and filters tools by its scopes", async () => {
    const forwarded: string[] = [];
    const origin = await startApiKeyServer(
      async () => resolved,
      (token) => forwarded.push(token),
    );
    const call = async (body: unknown) =>
      fetch(`${origin}/mcp`, {
        method: "POST",
        headers: {
          Accept: "application/json, text/event-stream",
          Authorization: `Bearer ${KEY}`,
          "Content-Type": "application/json",
          "MCP-Protocol-Version": "2025-06-18",
        },
        body: JSON.stringify(body),
      });

    const initialize = await call({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "test", version: "0" },
      },
    });
    expect(initialize.status).toBe(200);

    const listed = await call({ jsonrpc: "2.0", id: 2, method: "tools/list" });
    const tools = (await listed.json()).result.tools as { name: string }[];
    const names = tools.map((tool) => tool.name);
    // The presented grant decides the surface: `drives:read` + `content:read`
    // lists drives but never creates one.
    expect(names).toContain("list_drives");
    expect(names).not.toContain("create_drive");

    const called = await call({
      jsonrpc: "2.0",
      id: 3,
      method: "tools/call",
      params: { name: "list_drives", arguments: {} },
    });
    expect(called.status).toBe(200);
    // The key itself goes to the ingress: the sidecar mints nothing and
    // rewrites nothing, so the API resolves the same credential again.
    expect(forwarded).toContain(KEY);
  });

  it("refuses a key whose grant holds nothing this surface recognises", async () => {
    const origin = await startApiKeyServer(async () => ({
      ...resolved,
      scopes: ["jobs:read"],
    }));
    const response = await fetch(`${origin}/mcp`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${KEY}`,
        "Content-Type": "application/json",
      },
      body: "{}",
    });
    // The credential is fine; it just cannot do anything here.
    expect(response.status).toBe(403);
    expect(response.headers.get("www-authenticate")).toContain(
      'error="insufficient_scope"',
    );
  });

  it("refuses to be configured with both an authorization server and keys", () => {
    expect(() =>
      createAgentDriveMcpHttpServer({
        clientFactory: () => driveClient(),
      }),
    ).toThrow(/exactly one/u);
  });
});
