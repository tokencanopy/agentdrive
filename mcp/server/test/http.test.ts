import type { AddressInfo } from "node:net";

import { afterEach, beforeAll, describe, expect, it } from "vitest";

import {
  createAgentDriveMcpHttpServer,
  MCP_ORIGIN_HEADER,
  protectedResourceForAuth,
} from "../src/http.js";
import type { AgentDriveClientLike } from "../src/types.js";
import { protectedResourceMetadata } from "@tokencanopy/mcp-auth";
import {
  createTestAuth,
  PRODUCT_AUDIENCE,
  READ_ONLY_SCOPES,
  type TestAuthContext,
} from "./oauth-fixtures.js";

const openServers: ReturnType<typeof createAgentDriveMcpHttpServer>[] = [];
let auth: TestAuthContext;

beforeAll(async () => {
  auth = await createTestAuth();
});

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

async function startServer(
  clientFactory: Parameters<
    typeof createAgentDriveMcpHttpServer
  >[0]["clientFactory"],
  additionalAuth?: Parameters<
    typeof createAgentDriveMcpHttpServer
  >[0]["additionalAuth"],
  publicBaseUrl?: string,
): Promise<string> {
  const server = createAgentDriveMcpHttpServer({
    clientFactory,
    auth: auth.options,
    additionalAuth,
    publicBaseUrl,
  });
  openServers.push(server);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address() as AddressInfo;
  return `http://127.0.0.1:${address.port}`;
}

function httpClient(): AgentDriveClientLike {
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

async function postRpc(
  origin: string,
  body: unknown,
  token: string,
): Promise<Response> {
  return fetch(`${origin}/mcp`, {
    method: "POST",
    headers: {
      Accept: "application/json, text/event-stream",
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "MCP-Protocol-Version": "2025-06-18",
    },
    body: JSON.stringify(body),
  });
}

describe("AgentDrive hosted MCP HTTP boundary", () => {
  it("serves protected-resource metadata and keeps health public", async () => {
    let factoryCalls = 0;
    const origin = await startServer(() => {
      factoryCalls += 1;
      throw new Error(
        "the unauthenticated request must not construct a client",
      );
    });

    const health = await fetch(`${origin}/health`);
    expect(health.status).toBe(200);
    expect(await health.json()).toEqual({ status: "ok" });

    // The PATH-SCOPED document is this server's. It names the /mcp resource
    // and the eight MCP scopes, drives:write included since the audience
    // split made it safe to offer here.
    const pathMetadata = await fetch(
      `${origin}/.well-known/oauth-protected-resource/mcp`,
    );
    expect(pathMetadata.status).toBe(200);
    const metadataBody = await pathMetadata.json();
    expect(metadataBody).toEqual(
      protectedResourceMetadata(protectedResourceForAuth(auth.options)),
    );
    expect(metadataBody.resource).toBe("https://drive.tokencanopy.com/mcp");
    expect(metadataBody.scopes_supported).toContain("drives:write");
    // Never cacheable: a stale copy of this document is a stale resource
    // identifier, and the FastAPI edge pins `no-store` on it too.
    expect(pathMetadata.headers.get("cache-control")).toBe("no-store");

    // The ROOT document belongs to the public /v0 product resource and is
    // served by AgentDrive itself. Answering it here would advertise the
    // wrong resource and the wrong scope list.
    const rootMetadata = await fetch(
      `${origin}/.well-known/oauth-protected-resource`,
    );
    expect(rootMetadata.status).toBe(404);

    const unauthenticated = await fetch(`${origin}/mcp`, {
      method: "POST",
      body: "{}",
    });
    expect(unauthenticated.status).toBe(401);
    // The challenge points at the PATH-SCOPED document, so a client that
    // follows it discovers the MCP resource and never asks for /v0 scopes.
    expect(unauthenticated.headers.get("www-authenticate")).toContain(
      "https://drive.tokencanopy.com/.well-known/oauth-protected-resource/mcp",
    );
    expect(unauthenticated.headers.get("www-authenticate")).not.toContain(
      'error="invalid_token"',
    );
    expect(factoryCalls).toBe(0);
  });

  it.each([
    ["malformed", "Bearer definitely-not-a-jwt"],
    ["wrong audience", "__WRONG_AUDIENCE__"],
    ["product audience", "__PRODUCT_AUDIENCE__"],
    ["expired", "__EXPIRED__"],
  ])("rejects a %s bearer before client construction", async (kind, value) => {
    let authorization = value;
    if (kind === "wrong audience") {
      authorization = `Bearer ${await auth.token({
        audience: "https://chat.tokencanopy.test",
      })}`;
    } else if (kind === "product audience") {
      // A ROOT-audience AgentDrive token. Valid at public /v0, and it must be
      // worthless here -- the other half of the 2026-08-28 audience split.
      authorization = `Bearer ${await auth.token({
        audience: PRODUCT_AUDIENCE,
      })}`;
    } else if (kind === "expired") {
      authorization = `Bearer ${await auth.token({ expiresAt: 1 })}`;
    }
    const origin = await startServer(() => {
      throw new Error("invalid authentication must not construct a client");
    });
    const response = await fetch(`${origin}/mcp`, {
      method: "POST",
      headers: {
        Authorization: authorization,
        "Content-Type": "application/json",
      },
      body: "{}",
    });
    expect(response.status).toBe(401);
    expect(response.headers.get("www-authenticate")).toContain(
      'error="invalid_token"',
    );
    expect(await response.json()).toEqual({ error: "invalid_token" });
  });

  it("rejects a token carrying NO scope of this resource with 403", async () => {
    // The only remaining scope refusal at the transport. A token holding some
    // of the vocabulary authenticates; per-tool authorization decides the
    // rest. Requiring the whole bundle here is what made Hub's own read-only
    // consent option fail at `initialize`.
    const origin = await startServer(() => {
      throw new Error("insufficient scopes must not construct a client");
    });
    const response = await fetch(`${origin}/mcp`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${await auth.token({
          scopes: "jobs:read drives:admin",
        })}`,
        "Content-Type": "application/json",
      },
      body: "{}",
    });
    expect(response.status).toBe(403);
    expect(response.headers.get("www-authenticate")).toContain(
      'error="insufficient_scope"',
    );
    expect(await response.json()).toEqual({ error: "insufficient_scope" });
  });

  it("initializes a READ-ONLY grant and exposes only its read tools", async () => {
    const origin = await startServer(() => httpClient());
    const token = await auth.token({ scopes: READ_ONLY_SCOPES.join(" ") });

    const initialize = await postRpc(
      origin,
      {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2025-06-18",
          capabilities: {},
          clientInfo: { name: "synthetic-client", version: "1.0.0" },
        },
      },
      token,
    );
    expect(initialize.status).toBe(200);

    const toolsList = await postRpc(
      origin,
      { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
      token,
    );
    const names = ((await toolsList.json()).result.tools as { name: string }[])
      .map((tool) => tool.name)
      .sort();
    expect(names).toEqual([
      "list_access_grants",
      "list_artifact_versions",
      "list_changes",
      "list_directory",
      "list_drives",
      "read_artifact",
      "search_drive",
    ]);

    // A read still works.
    const read = await postRpc(
      origin,
      {
        jsonrpc: "2.0",
        id: 3,
        method: "tools/call",
        params: { name: "list_drives", arguments: {} },
      },
      token,
    );
    expect((await read.json()).result.structuredContent).toMatchObject({
      next_cursor: null,
    });
  });

  it("refuses a hand-built mutation call from a read-only grant", async () => {
    // The tool is not listed, so a well-behaved client never offers it. This
    // is the badly-behaved one: raw JSON-RPC naming the tool directly. The
    // SDK must never be reached.
    // A client that throws on ANY property access: if the mutation reached
    // the SDK at all, this fails loudly instead of silently succeeding.
    const origin = await startServer(
      () =>
        new Proxy(
          {},
          {
            get(_target, property) {
              throw new Error(
                `the SDK must not be reached: touched ${String(property)}`,
              );
            },
          },
        ) as unknown as AgentDriveClientLike,
    );
    const token = await auth.token({ scopes: READ_ONLY_SCOPES.join(" ") });
    const call = await postRpc(
      origin,
      {
        jsonrpc: "2.0",
        id: 1,
        method: "tools/call",
        params: {
          name: "delete",
          arguments: {
            drive_id: "drv_0000000000000001",
            entry_id: "art_0000000000000001",
            revision: "rev-1",
          },
        },
      },
      token,
    );
    expect(call.status).toBe(200);
    const body = (await call.json()) as {
      result: { isError: boolean; content: { text: string }[] };
    };
    expect(body.result.isError).toBe(true);
    expect(body.result.content[0].text).toContain("Tool delete not found");
  });

  it("does not turn unrelated paths into MCP responses", async () => {
    const origin = await startServer(() => {
      throw new Error("the unrelated request must not construct a client");
    });

    const response = await fetch(`${origin}/not-mcp`);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({
      error: { code: "not_found", message: "not found" },
    });
  });

  it("limits the protected endpoint to Streamable HTTP methods", async () => {
    const origin = await startServer(() => {
      throw new Error(
        "method validation should happen before client construction",
      );
    });

    const response = await fetch(`${origin}/mcp`, {
      method: "PUT",
      headers: { Authorization: `Bearer ${await auth.token()}` },
    });
    expect(response.status).toBe(405);
    expect(response.headers.get("allow")).toBe("POST, GET, DELETE");
  });

  it("serves initialize, tools/list, and tools/call over authenticated HTTP", async () => {
    const tokens: string[] = [];
    const origin = await startServer((token) => {
      tokens.push(token);
      return httpClient();
    });

    const token = await auth.token();
    const initialize = await postRpc(
      origin,
      {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2025-06-18",
          capabilities: {},
          clientInfo: { name: "synthetic-client", version: "1.0.0" },
        },
      },
      token,
    );
    expect(initialize.status).toBe(200);
    expect((await initialize.json()).result.serverInfo.name).toBe(
      "tokencanopy-agentdrive",
    );

    const toolsList = await postRpc(
      origin,
      {
        jsonrpc: "2.0",
        id: 2,
        method: "tools/list",
        params: {},
      },
      token,
    );
    expect(toolsList.status).toBe(200);
    const tools = (await toolsList.json()).result.tools as Array<{
      name: string;
    }>;
    expect(tools).toHaveLength(24);
    expect(tools.map((tool) => tool.name)).toContain("list_directory");

    const call = await postRpc(
      origin,
      {
        jsonrpc: "2.0",
        id: 3,
        method: "tools/call",
        params: { name: "list_drives", arguments: {} },
      },
      token,
    );
    expect(call.status).toBe(200);
    expect((await call.json()).result.structuredContent).toMatchObject({
      next_cursor: null,
      drives: [{ drive: { id: "drv_0000000000000001" } }],
    });
    expect(tokens).toEqual([token, token, token]);
  });
});

describe("publish and unpublish over authenticated HTTP", () => {
  /** The smallest stateful client the toggle needs: one artifact in a folder
   * under the root, and a grant store the tool reads and writes. */
  function publicToggleClient() {
    type Grant = {
      id: string;
      resourceType: string;
      resourceId: string;
      principalType: string;
      role: string;
      revision: string;
      revokedAt: string | null;
    };
    const grants: Grant[] = [];
    const calls: string[] = [];
    const parents: Record<string, string | null> = {
      fld_0000000000000001: null,
      fld_0000000000000002: "fld_0000000000000001",
    };
    const client = {
      artifacts: {
        async get(_driveId: string, artifactId: string) {
          calls.push(`artifacts.get ${artifactId}`);
          const covered = new Set([
            "drive:drv_0000000000000001",
            "folder:fld_0000000000000001",
            "folder:fld_0000000000000002",
            `artifact:${artifactId}`,
          ]);
          const visible = grants.some(
            (grant) =>
              grant.revokedAt === null &&
              covered.has(`${grant.resourceType}:${grant.resourceId}`),
          );
          return {
            id: artifactId,
            parentId: "fld_0000000000000002",
            effectiveVisibility: visible ? "public" : "private",
          };
        },
      },
      folders: {
        async get(_driveId: string, folderId: string) {
          calls.push(`folders.get ${folderId}`);
          return { id: folderId, parentId: parents[folderId] };
        },
      },
      grants: {
        async list(
          _driveId: string,
          options?: { resourceType?: string; resourceId?: string },
        ) {
          calls.push("grants.list");
          return {
            items: grants.filter(
              (grant) =>
                grant.revokedAt === null &&
                (!options?.resourceType ||
                  grant.resourceType === options.resourceType) &&
                (!options?.resourceId ||
                  grant.resourceId === options.resourceId),
            ),
            nextCursor: null,
          };
        },
        async create(
          _driveId: string,
          input: {
            resourceType: string;
            resourceId: string;
            principalType: string;
            role: string;
          },
        ) {
          calls.push(`grants.create ${input.resourceType} ${input.resourceId}`);
          const grant: Grant = {
            id: `grn_${(grants.length + 1).toString(16).padStart(16, "0")}`,
            resourceType: input.resourceType,
            resourceId: input.resourceId,
            principalType: input.principalType,
            role: input.role,
            revision: "rev_00000000000000a1",
            revokedAt: null,
          };
          grants.push(grant);
          return grant;
        },
        async revoke(_driveId: string, grantId: string, revision: string) {
          calls.push(`grants.revoke ${grantId} ${revision}`);
          const grant = grants.find((candidate) => candidate.id === grantId);
          if (!grant || grant.revision !== revision) {
            throw Object.assign(new Error("stale"), { statusCode: 412 });
          }
          grant.revokedAt = "2026-01-02T00:00:00Z";
          return grant;
        },
      },
    };
    return {
      client: client as unknown as AgentDriveClientLike,
      grants,
      calls,
    };
  }

  async function callTool(
    origin: string,
    token: string,
    id: number,
    name: "publish" | "unpublish",
    args: Record<string, unknown>,
  ) {
    const response = await postRpc(
      origin,
      {
        jsonrpc: "2.0",
        id,
        method: "tools/call",
        params: { name, arguments: args },
      },
      token,
    );
    expect(response.status).toBe(200);
    return (await response.json()).result as {
      isError?: boolean;
      structuredContent?: Record<string, unknown>;
      content: { type: string; text: string }[];
    };
  }

  it("turns public access on, then off, and refuses the inherited case, on the wire", async () => {
    const state = publicToggleClient();
    const origin = await startServer(
      () => state.client,
      undefined,
      "https://share.tokencanopy.test",
    );
    const token = await auth.token();
    const target = {
      drive_id: "drv_0000000000000001",
      resource_type: "artifact",
      resource_id: "art_0000000000000001",
    };

    // 1. Listed for the full grant, with the annotations a client keys on.
    const toolsList = await postRpc(
      origin,
      { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} },
      token,
    );
    const listed = (
      (await toolsList.json()).result.tools as Array<{
        name: string;
        annotations?: Record<string, unknown>;
        inputSchema: { required?: string[] };
      }>
    ).find((tool) => tool.name === "publish");
    expect(listed?.annotations).toMatchObject({ openWorldHint: true });
    expect(listed?.inputSchema.required).toEqual(
      expect.arrayContaining(["drive_id", "resource_type", "resource_id"]),
    );

    // 2. On: creates exactly one viewer-only public grant.
    const on = await callTool(origin, token, 2, "publish", { ...target });
    expect(on.isError).not.toBe(true);
    expect(on.structuredContent).toMatchObject({
      ...target,
      published: true,
      public_url: "https://share.tokencanopy.test/a/art_0000000000000001/",
      grant: { principalType: "public", role: "viewer" },
      inherited_from: null,
      effective_visibility: "public",
      warnings: [],
    });
    // JSON over the wire: the text content and the structured content agree.
    expect(JSON.parse(on.content[0].text)).toEqual(on.structuredContent);

    // 3. Off: revokes it under its revision, and the artifact is private.
    const off = await callTool(origin, token, 3, "unpublish", { ...target });
    expect(off.isError).not.toBe(true);
    expect(off.structuredContent).toMatchObject({
      published: false,
      grant: null,
      effective_visibility: "private",
    });
    expect(off.structuredContent).not.toHaveProperty("public_url");
    expect(state.calls).toContain(
      `grants.revoke ${state.grants[0].id} rev_00000000000000a1`,
    );

    // 4. Public only through the parent folder: refused, naming the grant.
    state.grants.push({
      id: "grn_00000000000000f0",
      resourceType: "folder",
      resourceId: "fld_0000000000000002",
      principalType: "public",
      role: "viewer",
      revision: "rev_00000000000000f0",
      revokedAt: null,
    });
    const inherited = await callTool(origin, token, 4, "unpublish", {
      ...target,
    });
    expect(inherited.isError).toBe(true);
    expect(JSON.parse(inherited.content[0].text)).toEqual({
      error: {
        code: "public_inherited",
        message: expect.stringContaining("grn_00000000000000f0"),
        inherited_from: {
          grant_id: "grn_00000000000000f0",
          resource_type: "folder",
          resource_id: "fld_0000000000000002",
        },
      },
    });
    expect(
      state.calls.filter((call) => call.startsWith("grants.revoke")),
    ).toHaveLength(1);

    // 5. A malformed id never reaches the client.
    const callsBefore = state.calls.length;
    const bad = await callTool(origin, token, 5, "publish", {
      ...target,
      resource_id: "art_doesnotexist",
    });
    expect(bad.isError).toBe(true);
    expect(state.calls).toHaveLength(callsBefore);
  });

  it("is withheld from, and refused for, a read-only grant on the wire", async () => {
    const origin = await startServer(
      () =>
        new Proxy(
          {},
          {
            get(_target, property) {
              throw new Error(
                `the SDK must not be reached: touched ${String(property)}`,
              );
            },
          },
        ) as unknown as AgentDriveClientLike,
    );
    const token = await auth.token({ scopes: READ_ONLY_SCOPES.join(" ") });
    const toolsList = await postRpc(
      origin,
      { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} },
      token,
    );
    const names = (
      (await toolsList.json()).result.tools as { name: string }[]
    ).map((tool) => tool.name);
    expect(names).not.toContain("publish");
    expect(names).not.toContain("unpublish");
    for (const name of ["publish", "unpublish"] as const) {
      const call = await callTool(origin, token, 2, name, {
        drive_id: "drv_0000000000000001",
        resource_type: "artifact",
        resource_id: "art_0000000000000001",
      });
      expect(call.isError).toBe(true);
      expect(call.content[0].text).toContain(`Tool ${name} not found`);
    }
  });
});

describe("ADR-0002: one process, one resource per single-purpose origin", () => {
  const ORIGIN = "https://drive.mcp.tokencanopy.test";
  const ORIGIN_AUDIENCE = `${ORIGIN}/mcp`;
  const ORIGIN_METADATA_URL = `${ORIGIN}/.well-known/oauth-protected-resource/mcp`;
  const originAuth = () => ({
    ...auth.options,
    audience: ORIGIN_AUDIENCE,
    metadataUrl: ORIGIN_METADATA_URL,
  });

  it("serves each origin's own document and challenge, chosen by the edge's label", async () => {
    const origin = await startServer(() => {
      throw new Error("no client for unauthenticated requests");
    }, [originAuth()]);

    // Labelled for the per-product origin: THAT resource's document.
    const labelled = await fetch(
      `${origin}/.well-known/oauth-protected-resource/mcp`,
      { headers: { [MCP_ORIGIN_HEADER]: ORIGIN } },
    );
    expect(labelled.status).toBe(200);
    expect((await labelled.json()).resource).toBe(ORIGIN_AUDIENCE);
    // Unlabelled: the primary, exactly as before this option existed.
    const primary = await fetch(
      `${origin}/.well-known/oauth-protected-resource/mcp`,
    );
    expect((await primary.json()).resource).toBe(
      "https://drive.tokencanopy.com/mcp",
    );
    // A label naming an origin this process was not configured for is a
    // 404, never a fallback to some other resource's identity.
    const unknown = await fetch(
      `${origin}/.well-known/oauth-protected-resource/mcp`,
      { headers: { [MCP_ORIGIN_HEADER]: "https://elsewhere.test" } },
    );
    expect(unknown.status).toBe(404);
    // The challenge on the labelled origin points at ITS document, so a
    // client following it discovers the resource it is actually talking to.
    const challenge = await fetch(`${origin}/mcp`, {
      method: "POST",
      headers: { [MCP_ORIGIN_HEADER]: ORIGIN },
      body: "{}",
    });
    expect(challenge.status).toBe(401);
    expect(challenge.headers.get("www-authenticate")).toContain(
      ORIGIN_METADATA_URL,
    );
    expect(challenge.headers.get("www-authenticate")).not.toContain(
      "drive.tokencanopy.com",
    );
  });

  it("verifies a bearer against the labelled origin's audience and no other", async () => {
    const origin = await startServer(() => httpClient(), [originAuth()]);
    const initialize = {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "test", version: "0.0.0" },
      },
    };
    const originToken = await auth.token({ audience: ORIGIN_AUDIENCE });
    const legacyToken = await auth.token();
    const post = (token: string, label?: string) =>
      fetch(`${origin}/mcp`, {
        method: "POST",
        headers: {
          Accept: "application/json, text/event-stream",
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "MCP-Protocol-Version": "2025-06-18",
          ...(label ? { [MCP_ORIGIN_HEADER]: label } : {}),
        },
        body: JSON.stringify(initialize),
      });

    // The right token on the right origin.
    expect((await post(originToken, ORIGIN)).status).toBe(200);
    // Two resources, two audiences: a token for one is worthless at the
    // other, in BOTH directions — the mutual refusal that IS the boundary.
    const legacyAtOrigin = await post(legacyToken, ORIGIN);
    expect(legacyAtOrigin.status).toBe(401);
    expect(legacyAtOrigin.headers.get("www-authenticate")).toContain(
      'error="invalid_token"',
    );
    const originAtLegacy = await post(originToken);
    expect(originAtLegacy.status).toBe(401);
    // And the primary still works unlabelled, untouched by the addition.
    expect((await post(legacyToken)).status).toBe(200);
  });

  it("refuses two resources on one origin at construction", () => {
    // The label cannot tell them apart, so one would answer for the other.
    expect(() =>
      createAgentDriveMcpHttpServer({
        clientFactory: () => httpClient(),
        auth: auth.options,
        additionalAuth: [auth.options],
      }),
    ).toThrow(/share the origin/);
  });
});
