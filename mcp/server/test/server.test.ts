import { Client as McpClient } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { describe, expect, it } from "vitest";

import {
  AgentDriveClient,
  StaticTokenProvider,
} from "@tokencanopy/agentdrive-sdk";
import { AGENTDRIVE_MCP_SCOPES } from "@tokencanopy/mcp-auth";

import {
  agentDriveMcpToolScopes,
  createAgentDriveMcpServer,
} from "../src/server.js";

/** These suites exercise tool BEHAVIOUR, so they run with the full grant.
 * Per-tool authorization has its own suite at the bottom of this file. */
const FULL_GRANT = { scopes: [...AGENTDRIVE_MCP_SCOPES] };
import type { AgentDriveClientLike } from "../src/types.js";

/** A live grant row in the fake's store, in the SDK's camelCase shape. */
type FakeGrant = {
  id: string;
  driveId: string;
  resourceType: string;
  resourceId: string;
  principalType: string;
  principalId: string | null;
  role: string;
  revision: string;
  state: string;
  expiresAt: Date | null;
  revokedAt: Date | null;
};

/** The fake client plus the handles a test uses to seed and inspect it. */
type FakeClientHandle = {
  client: AgentDriveClientLike;
  grants: FakeGrant[];
  /** Artifacts with a live share link. The server counts a live share as
   * `public` (possession of the link is the credential, bounded by no
   * principal), independently of every grant. */
  linkShared: Set<string>;
  /** Every `grants.create` input the tool sent, in order. */
  created: Array<{ input: Record<string, unknown>; idempotencyKey?: string }>;
  /** Every `grants.revoke` call the tool made, in order. */
  revoked: Array<{
    grantId: string;
    revision: string;
    idempotencyKey?: string;
  }>;
  /** Every `drives.delete` call the tool made, in order. */
  deletedDrives: Array<{
    driveId: string;
    revision: string;
    idempotencyKey?: string;
  }>;
  /** Every `drives.restore` call the tool made, in order. */
  restoredDrives: Array<{
    driveId: string;
    revision: string;
    idempotencyKey?: string;
  }>;
  /** Every folder/artifact restore the tool made, in order. */
  restored: Array<{
    type: "folder" | "artifact";
    driveId: string;
    resourceId: string;
    revision: string;
    idempotencyKey?: string;
  }>;
  /** Every `entries.list` options object the tool sent, in order. */
  entryListOptions: Array<Record<string, unknown>>;
  /** Every `drives.list` options object the tool sent, in order. */
  driveListOptions: Array<Record<string, unknown>>;
  /** Every drive id `drives.usage` was called for, in order. */
  usageCalls: string[];
  /** Drive ids the fake answers 404 for on `drives.usage`, as the server
   * does for a soft-deleted drive. */
  deletedDriveIds: Set<string>;
};

/**
 * The fake's folder tree. `fld_…01` is the drive root, `fld_…02` sits under
 * it, `fld_…03` under that; `art_…01` lives in the root, `art_…02` in the
 * deepest folder — so an ancestor grant has somewhere to be inherited from.
 */
const FAKE_FOLDER_PARENTS: Record<string, string | null> = {
  fld_0000000000000001: null,
  fld_0000000000000002: "fld_0000000000000001",
  fld_0000000000000003: "fld_0000000000000002",
};
const FAKE_ARTIFACT_PARENTS: Record<string, string> = {
  art_0000000000000001: "fld_0000000000000001",
  art_0000000000000002: "fld_0000000000000003",
};

function fakeClientWithState(): FakeClientHandle {
  const artifact = {
    id: "art_0000000000000001",
    name: "note.txt",
    revision: "rev-1",
    contentType: "text/plain",
  };
  const grants: FakeGrant[] = [];
  const created: FakeClientHandle["created"] = [];
  const revoked: FakeClientHandle["revoked"] = [];
  const deletedDrives: FakeClientHandle["deletedDrives"] = [];
  const restoredDrives: FakeClientHandle["restoredDrives"] = [];
  const restored: FakeClientHandle["restored"] = [];
  const entryListOptions: FakeClientHandle["entryListOptions"] = [];
  const driveListOptions: FakeClientHandle["driveListOptions"] = [];
  const usageCalls: FakeClientHandle["usageCalls"] = [];
  /** Drives the fake treats as soft-deleted for the purposes of usage. */
  const deletedDriveIds = new Set<string>();
  /** AgentDrive replays a completed mutation's stored response for 24 h
   * when the same principal sends the same Idempotency-Key — verbatim,
   * without checking whether the grant it created is still live. */
  const replays = new Map<string, FakeGrant>();
  let nextGrant = 0x10;
  const live = (grant: FakeGrant) => grant.revokedAt === null;
  const linkShared = new Set<string>();
  /** The rule `v0_authz` applies: public when ANY live public grant covers
   * the resource itself, a folder ancestor, or the drive — or when a live
   * share link exists, which the server counts as public on its own. */
  const visibilityOf = (artifactId: string): string => {
    if (linkShared.has(artifactId)) {
      return "public";
    }
    const covered = new Set<string>([`drive:drv_0000000000000001`]);
    let parent: string | null = FAKE_ARTIFACT_PARENTS[artifactId] ?? null;
    while (parent) {
      covered.add(`folder:${parent}`);
      parent = FAKE_FOLDER_PARENTS[parent] ?? null;
    }
    covered.add(`artifact:${artifactId}`);
    return grants.some(
      (grant) =>
        live(grant) &&
        grant.principalType === "public" &&
        covered.has(`${grant.resourceType}:${grant.resourceId}`),
    )
      ? "public"
      : "private";
  };
  const client: AgentDriveClientLike = {
    drives: {
      async usage(driveId) {
        usageCalls.push(driveId);
        // The server's own rule: `drive_usage` fetches with
        // `include_deleted=False`, so a soft-deleted drive is a 404 here.
        // The old fake returned a constant whatever the state, which is
        // exactly why the broken fan-out passed CI.
        if (deletedDriveIds.has(driveId)) {
          throw Object.assign(new Error("drive not found"), {
            statusCode: 404,
          });
        }
        return { storageBytes: 4, retrievalBytes: 0, meters: {} };
      },
      async list(options) {
        driveListOptions.push({ ...options });
        const state = (options?.state as string | undefined) ?? "active";
        if (state === "deleted") {
          deletedDriveIds.add("drv_0000000000000001");
        }
        return {
          items: [
            {
              id: "drv_0000000000000001",
              rootFolderId: "fld_0000000000000001",
              revision: state === "active" ? "drv-1" : "drv-2",
              state: state === "all" ? "active" : state,
              // `DriveOut` carries these itself; the listing is why the
              // per-drive usage call was redundant as well as fatal.
              storageBytes: 4,
              retrievalBytes: 0,
            },
          ],
          nextCursor: null,
        };
      },
      async create(name) {
        return { id: "drv_0000000000000002", name, revision: "drv-1" };
      },
      async delete(driveId, revision, idempotencyKey) {
        deletedDrives.push({ driveId, revision, idempotencyKey });
        return {
          id: driveId,
          name: "notes",
          revision: "drv-2",
          state: "deleted",
          deletedAt: "2026-01-01T00:00:00Z",
        };
      },
      async restore(driveId, revision, idempotencyKey) {
        restoredDrives.push({ driveId, revision, idempotencyKey });
        return {
          id: driveId,
          name: "notes",
          revision: "drv-3",
          state: "active",
          deletedAt: null,
        };
      },
    },
    entries: {
      async list(_driveId, options) {
        entryListOptions.push({ ...options });
        const state = (options?.state as string | undefined) ?? "active";
        return {
          entries: [
            {
              type: "artifact",
              id: artifact.id,
              name: artifact.name,
              revision: state === "active" ? "rev-1" : "rev-2",
              state: state === "all" ? "active" : state,
              deletedAt: state === "deleted" ? "2026-01-01T00:00:00Z" : null,
            },
          ],
          nextCursor: null,
        };
      },
      async lookup() {
        return {
          id: artifact.id,
          type: "artifact",
          revision: artifact.revision,
          parentId: "fld_0000000000000001",
        };
      },
    },
    folders: {
      async get(_driveId, folderId) {
        if (!(folderId in FAKE_FOLDER_PARENTS)) {
          throw Object.assign(new Error("folder not found"), {
            statusCode: 404,
          });
        }
        return {
          id: folderId,
          parentId: FAKE_FOLDER_PARENTS[folderId],
          revision: "fld-1",
        };
      },
      async create() {
        return { id: "fld_0000000000000002", revision: "fld-1" };
      },
      async update() {
        return { id: "fld_0000000000000002", revision: "fld-2" };
      },
      async delete() {
        return { id: "fld_0000000000000002", state: "deleted" };
      },
      async restore(driveId, folderId, revision, idempotencyKey) {
        restored.push({
          type: "folder",
          driveId,
          resourceId: folderId,
          revision,
          idempotencyKey,
        });
        // The server returns a cascade: the folder plus what came back
        // with it.
        return {
          folder: { id: folderId, revision: "fld-3", state: "active" },
          cascade: { folders: 1, artifacts: 2 },
        };
      },
    },
    artifacts: {
      async get(_driveId, artifactId) {
        const parentId = FAKE_ARTIFACT_PARENTS[artifactId];
        if (parentId === undefined) {
          throw Object.assign(new Error("artifact not found"), {
            statusCode: 404,
          });
        }
        return {
          ...artifact,
          id: artifactId,
          parentId,
          effectiveVisibility: visibilityOf(artifactId),
        };
      },
      async create(_driveId, _name, content) {
        const contentBytes =
          typeof content === "string"
            ? new TextEncoder().encode(content).byteLength
            : content instanceof Uint8Array
              ? content.byteLength
              : 0;
        return { ...artifact, id: "art_0000000000000002", contentBytes };
      },
      async update() {
        return { ...artifact, revision: "rev-2" };
      },
      async delete() {
        return { ...artifact, state: "deleted" };
      },
      async restore(driveId, artifactId, revision, idempotencyKey) {
        restored.push({
          type: "artifact",
          driveId,
          resourceId: artifactId,
          revision,
          idempotencyKey,
        });
        return {
          ...artifact,
          id: artifactId,
          revision: "rev-3",
          state: "active",
        };
      },
      async content() {
        return new Blob(["synthetic content"], { type: "text/plain" });
      },
    },
    versions: {
      async list() {
        return {
          items: [{ id: "ver_0000000000000001", revision: "ver-1" }],
          nextCursor: null,
        };
      },
      async append() {
        return { id: "ver_0000000000000002", revision: "ver-2" };
      },
    },
    search: {
      async find() {
        return {
          items: [{ id: artifact.id, type: "artifact" }],
          nextCursor: null,
        };
      },
    },
    changes: {
      async list() {
        return { items: [], nextCursor: null, hasMore: false };
      },
    },
    grants: {
      async list(_driveId, options) {
        const filter = options ?? {};
        const matching = grants.filter(
          (grant) =>
            (filter.state !== "active" || live(grant)) &&
            (filter.resourceType === undefined ||
              grant.resourceType === filter.resourceType) &&
            (filter.resourceId === undefined ||
              grant.resourceId === filter.resourceId) &&
            (filter.principalType === undefined ||
              grant.principalType === filter.principalType),
        );
        // Keyset paging the way the server does it: `limit` items per page,
        // the cursor is the index to resume from.
        const limit = typeof filter.limit === "number" ? filter.limit : 100;
        const start =
          typeof filter.cursor === "string" ? Number(filter.cursor) : 0;
        const items = matching.slice(start, start + limit);
        const nextCursor =
          start + limit < matching.length ? String(start + limit) : null;
        return { items, nextCursor };
      },
      async create(driveId, input, idempotencyKey) {
        created.push({ input: { ...input }, idempotencyKey });
        const replayed = idempotencyKey && replays.get(idempotencyKey);
        if (replayed) return { ...replayed };
        const grant: FakeGrant = {
          id: `grn_${(nextGrant++).toString(16).padStart(16, "0")}`,
          driveId,
          resourceType: input.resourceType,
          resourceId: input.resourceId,
          principalType: input.principalType,
          principalId: null,
          role: input.role,
          revision: "grt-rev-1",
          state: "active",
          expiresAt: input.expiresAt ?? null,
          revokedAt: null,
        };
        if (grant.expiresAt && grant.expiresAt.getTime() <= Date.now()) {
          grant.state = "expired";
        }
        grants.push(grant);
        if (idempotencyKey) replays.set(idempotencyKey, { ...grant });
        return grant;
      },
      async revoke(_driveId, grantId, revision, idempotencyKey) {
        revoked.push({ grantId, revision, idempotencyKey });
        const grant = grants.find((candidate) => candidate.id === grantId);
        if (!grant) {
          throw Object.assign(new Error("grant not found"), {
            statusCode: 404,
          });
        }
        if (grant.revision !== revision) {
          throw Object.assign(new Error("stale revision"), {
            statusCode: 412,
          });
        }
        grant.revokedAt = new Date("2026-01-02T00:00:00Z");
        grant.state = "revoked";
        grant.revision = "grt-rev-2";
        return grant;
      },
    },
    shares: {
      async create() {
        return {
          id: "shr_test",
          secret: "synthetic-share-secret",
          url: "https://share.invalid/s/synthetic-share-secret/",
        };
      },
    },
    uploads: {
      async begin() {
        return {
          id: "upld_00000000000000ab",
          state: "awaiting_transfer",
          target: "https://upload.invalid/session/synthetic",
          targetDisclosed: true,
          revision: "rev_00000000000000ab",
        };
      },
      async read() {
        return { id: "upld_00000000000000ab", state: "awaiting_transfer" };
      },
      async complete() {
        return { id: "upld_00000000000000ab", state: "completed" };
      },
      async cancel() {
        return { id: "upld_00000000000000ab", state: "cancelled" };
      },
    },
  };
  return {
    client,
    grants,
    created,
    revoked,
    linkShared,
    deletedDrives,
    restoredDrives,
    restored,
    entryListOptions,
    driveListOptions,
    usageCalls,
    deletedDriveIds,
  };
}

function fakeClient(): AgentDriveClientLike {
  return fakeClientWithState().client;
}

/** Seed one live public viewer grant, the way the console or REST would. */
function seedPublicGrant(
  handle: FakeClientHandle,
  resourceType: "drive" | "folder" | "artifact",
  resourceId: string,
  id = `grn_seed${handle.grants.length.toString(16).padStart(12, "0")}`,
): FakeGrant {
  const grant: FakeGrant = {
    id,
    driveId: "drv_0000000000000001",
    resourceType,
    resourceId,
    principalType: "public",
    principalId: null,
    role: "viewer",
    revision: `rev-${id}`,
    state: "active",
    expiresAt: null,
    revokedAt: null,
  };
  handle.grants.push(grant);
  return grant;
}

describe("AgentDrive B7 MCP", () => {
  it("carries the one-time transfer target through the REAL SDK to the caller", async () => {
    // Every other test here fakes the client, so nothing exercised the seam
    // that actually broke: the SDK deserialized the begin 201 with the 200's
    // model and dropped `transfer`, the only field carrying the upload URL.
    // Both sides were tested against their own idea of the contract and both
    // passed, while direct upload could not work at all. This drives the real
    // client over a stubbed socket so the wire shape is what is asserted.
    const wire = {
      upload: {
        id: "upld_00000000000000ab",
        drive_id: "drv_0000000000000001",
        state: "active",
        target: {
          kind: "artifact",
          parent_folder_id: "fld_0000000000000001",
          name: "report.pdf",
        },
        content: {
          size_bytes: 4096,
          media_type: "application/pdf",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
        expires_at: "2026-01-01T04:00:00Z",
        target_disclosed: true,
        restart_required: false,
        result: null,
        failure: null,
        cleanup: { state: "none" },
        transfer: {
          chunk_protocol: "gcs-xml-resumable",
          initiation: {
            url: "https://storage.invalid/initiate",
            method: "POST",
            required_headers: { "x-goog-resumable": "start" },
            expires_at: "2026-01-01T04:00:00Z",
          },
          chunks: { method: "PUT", required_headers: {} },
        },
      },
    };
    const realClient = new AgentDriveClient({
      baseUrl: "https://drive.invalid",
      tokenProvider: new StaticTokenProvider("synthetic"),
      // 201 is the ONLY response that ever carries the target.
      fetchApi: async () =>
        new Response(JSON.stringify(wire), {
          status: 201,
          headers: { "content-type": "application/json" },
        }),
    });

    const server = createAgentDriveMcpServer(
      realClient as unknown as AgentDriveClientLike,
      FULL_GRANT,
    );
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      const tools = await client.listTools();
      const description =
        tools.tools.find((tool) => tool.name === "begin_file_upload")
          ?.description ?? "";
      const response = await client.callTool({
        name: "begin_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          parent_id: "fld_0000000000000001",
          name: "report.pdf",
          size_bytes: 4096,
          media_type: "application/pdf",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
      });
      const text = (
        response.content as Array<{ type?: string; text?: string }>
      ).find((item) => item.type === "text")?.text;
      const payload = JSON.parse(text as string);

      const documentedTransferPaths = [
        ...description.matchAll(/`(upload\.transfer\.[^`]+)`/gu),
      ].map((match) => match[1]);
      expect(documentedTransferPaths).toEqual([
        "upload.transfer.initiation.url",
        "upload.transfer.initiation.requiredHeaders",
        "upload.transfer.chunks.requiredHeaders",
      ]);
      for (const path of documentedTransferPaths) {
        const value = path
          .split(".")
          .reduce<unknown>(
            (current, segment) =>
              current !== null && typeof current === "object"
                ? (current as Record<string, unknown>)[segment]
                : undefined,
            payload,
          );
        expect(value, `documented response path ${path}`).not.toBeUndefined();
      }

      // The agent must be able to reach the URL the tool description names.
      expect(payload.upload.transfer.initiation.url).toBe(
        "https://storage.invalid/initiate",
      );
      expect(payload.upload.transfer.initiation.requiredHeaders).toEqual({
        "x-goog-resumable": "start",
      });
      // And `target` remains the destination, not a URL -- the confusion that
      // put the wrong instruction in the tool description.
      expect(payload.upload.target).toEqual({
        kind: "artifact",
        parentFolderId: "fld_0000000000000001",
        name: "report.pdf",
      });
    } finally {
      await client.close();
      await server.close();
    }
  });

  it("tells an agent where the upload URL actually is, and that it is POST first", async () => {
    // The description is the ONLY instruction an agent gets. It used to say
    // `upload.target` was "a signed URL" and to send the file with one PUT.
    // Both are wrong: `target` is the destination inside the drive, the URL
    // lives at `upload.transfer.initiation.url`, and the transfer is GCS XML
    // resumable -- POST the initiation URL to get a session URI from
    // `Location`, then PUT to that. An agent following the old text could not
    // upload anything, and the target is disclosed only once, so each attempt
    // burned a session.
    const server = createAgentDriveMcpServer(fakeClient(), FULL_GRANT);
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      const tools = await client.listTools();
      const description =
        tools.tools.find((tool) => tool.name === "begin_file_upload")
          ?.description ?? "";

      expect(description).toContain("upload.transfer.initiation.url");
      // The one-shot disclosure is the reason a wrong instruction is
      // expensive rather than merely annoying.
      expect(description).toContain("exactly once");
      // Must not send an agent back to the field that is not a URL.
      expect(description).not.toMatch(/`upload\.target`[^.]*signed URL/u);
    } finally {
      await client.close();
      await server.close();
    }
  });

  it("freezes exactly the twenty-four tools and keeps stable structured output", async () => {
    const server = createAgentDriveMcpServer(fakeClient(), FULL_GRANT);
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      const tools = await client.listTools();
      expect(tools.tools.map((tool) => tool.name).sort()).toEqual([
        "begin_file_upload",
        "cancel_file_upload",
        "complete_file_upload",
        "create_artifact",
        "create_drive",
        "create_folder",
        "create_share_link",
        "delete",
        "delete_drive",
        "get_file_upload",
        "list_access_grants",
        "list_artifact_versions",
        "list_changes",
        "list_directory",
        "list_drives",
        "move",
        "publish",
        "read_artifact",
        "replace_artifact_content",
        "restore",
        "restore_drive",
        "search_drive",
        "unpublish",
        "update_artifact_metadata",
      ]);
      const response = await client.callTool({
        name: "read_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          include_content: true,
        },
      });
      const text = (
        response.content as Array<{ type?: string; text?: string }>
      ).find((item) => item.type === "text")?.text;
      expect(text).toBeDefined();
      const payload = JSON.parse(text as string);
      expect(payload).toMatchObject({
        drive_id: "drv_0000000000000001",
        artifact: { id: "art_0000000000000001" },
        content_type: "text/plain",
      });
      expect(payload).toMatchObject({
        content_base64: "c3ludGhldGljIGNvbnRlbnQ=",
      });

      const metadataTool = tools.tools.find(
        (tool) => tool.name === "update_artifact_metadata",
      );
      expect(metadataTool).toBeDefined();
      expect(metadataTool?.description).toContain("Provide metadata or labels");
      expect(
        tools.tools.find((tool) => tool.name === "read_artifact")?.description,
      ).toContain("exactly one of artifact_id or path");
      expect(
        tools.tools.find((tool) => tool.name === "move")?.description,
      ).toContain("parent_id, parent_path, or name");
    } finally {
      await client.close();
      await server.close();
    }
  });

  it("separates new artifacts, content replacement, and metadata updates", async () => {
    const server = createAgentDriveMcpServer(fakeClient(), FULL_GRANT);
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      const created = await client.callTool({
        name: "create_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          name: "binary.dat",
          content: { encoding: "base64", value: "AQID" },
        },
      });
      const createdText = (
        created.content as Array<{ type?: string; text?: string }>
      ).find((item) => item.type === "text")?.text;
      expect(JSON.parse(createdText as string)).toMatchObject({
        artifact: { id: "art_0000000000000002", contentBytes: 3 },
      });

      const replaced = await client.callTool({
        name: "replace_artifact_content",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          revision: "rev-1",
          content: { encoding: "text", value: "replacement" },
        },
      });
      const replacedText = (
        replaced.content as Array<{ type?: string; text?: string }>
      ).find((item) => item.type === "text")?.text;
      expect(JSON.parse(replacedText as string)).toEqual({
        version: { id: "ver_0000000000000002", revision: "ver-2" },
      });

      const metadata = await client.callTool({
        name: "update_artifact_metadata",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          revision: "rev-1",
          metadata: { classification: "note" },
          labels: ["draft"],
        },
      });
      expect(metadata.isError).not.toBe(true);

      const missingMetadata = await client.callTool({
        name: "update_artifact_metadata",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          revision: "rev-1",
        },
      });
      expect(missingMetadata.isError).toBe(true);
    } finally {
      await client.close();
      await server.close();
    }
  });

  it("hands back a signed upload target instead of carrying bytes", async () => {
    // The point of these tools: an agent gets a URL and transfers the file
    // itself, so the payload never crosses the model. A regression here would
    // most likely look like the target going missing from the response, which
    // silently pushes callers back onto inline create_artifact.
    const server = createAgentDriveMcpServer(fakeClient(), FULL_GRANT);
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      const begun = await client.callTool({
        name: "begin_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          parent_id: "fld_0000000000000001",
          name: "report.pdf",
          size_bytes: 4096,
          media_type: "application/pdf",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
      });
      expect(begun.isError).not.toBe(true);
      const begunText = (
        begun.content as Array<{ type?: string; text?: string }>
      ).find((item) => item.type === "text")?.text;
      expect(JSON.parse(begunText as string)).toMatchObject({
        upload: { target: "https://upload.invalid/session/synthetic" },
      });

      // A NEW VERSION of an existing artifact is the other destination.
      const versionTarget = await client.callTool({
        name: "begin_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          size_bytes: 10,
          media_type: "text/plain",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
      });
      expect(versionTarget.isError).not.toBe(true);

      // Both destinations at once is ambiguous -- the API models them as a
      // union, so accepting both here would silently pick one.
      const ambiguous = await client.callTool({
        name: "begin_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          parent_id: "fld_0000000000000001",
          name: "report.pdf",
          size_bytes: 10,
          media_type: "text/plain",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
      });
      expect(ambiguous.isError).toBe(true);

      // Neither destination.
      const destinationless = await client.callTool({
        name: "begin_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          size_bytes: 10,
          media_type: "text/plain",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
      });
      expect(destinationless.isError).toBe(true);

      // CRC32C, not CRC32 or MD5: a hex digest is the likeliest wrong guess,
      // and it must fail at the boundary rather than after a transfer.
      const hexChecksum = await client.callTool({
        name: "begin_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          parent_id: "fld_0000000000000001",
          name: "report.pdf",
          size_bytes: 10,
          media_type: "text/plain",
          checksum: { algorithm: "crc32c", value: "deadbeef" },
        },
      });
      expect(hexChecksum.isError).toBe(true);

      // media_type must be a bare type/subtype; parameters are rejected.
      const parameterized = await client.callTool({
        name: "begin_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          parent_id: "fld_0000000000000001",
          name: "notes.txt",
          size_bytes: 10,
          media_type: "text/plain; charset=utf-8",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
      });
      expect(parameterized.isError).toBe(true);

      const completed = await client.callTool({
        name: "complete_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          upload_id: "upld_00000000000000ab",
        },
      });
      expect(completed.isError).not.toBe(true);
      const completedText = (
        completed.content as Array<{ type?: string; text?: string }>
      ).find((item) => item.type === "text")?.text;
      expect(JSON.parse(completedText as string)).toMatchObject({
        upload: { state: "completed" },
      });
    } finally {
      await client.close();
      await server.close();
    }
  });

  it("sends the upload request in the shape the SDK model serializes", async () => {
    // The previous test asserted only the RESPONSE, so it passed while the
    // request was malformed. The generated client serializes a model, not a
    // body: `...ToJSON` reads `mediaType` and emits `media_type`, and the
    // target's `instanceOf` discriminator tests for `parentFolderId` before
    // matching either `oneOf` branch. Wire names therefore serialize to
    // `undefined` fields and an empty `{}` target, and the API answers 400
    // describing nothing. That shipped to production once. Capture the
    // argument and assert its shape.
    const captured: unknown[] = [];
    const clientLike = fakeClient();
    clientLike.uploads = {
      ...clientLike.uploads,
      async begin(_driveId: string, request: unknown) {
        captured.push(request);
        return {
          id: "upld_00000000000000ab",
          target: "https://upload.invalid/session/synthetic",
        };
      },
    };
    const server = createAgentDriveMcpServer(clientLike, FULL_GRANT);
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      await client.callTool({
        name: "begin_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          parent_id: "fld_0000000000000001",
          name: "report.pdf",
          size_bytes: 4096,
          media_type: "application/pdf",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
      });
      expect(captured).toHaveLength(1);
      expect(captured[0]).toEqual({
        target: {
          kind: "artifact",
          parentFolderId: "fld_0000000000000001",
          name: "report.pdf",
        },
        content: {
          sizeBytes: 4096,
          mediaType: "application/pdf",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
      });

      await client.callTool({
        name: "begin_file_upload",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          size_bytes: 10,
          media_type: "text/plain",
          checksum: { algorithm: "crc32c", value: "AAAAAA==" },
        },
      });
      expect(captured).toHaveLength(2);
      expect((captured[1] as { target: unknown }).target).toEqual({
        kind: "version",
        artifactId: "art_0000000000000001",
      });
    } finally {
      await client.close();
      await server.close();
    }
  });

  it("derives semantic idempotency keys when mutations omit one", async () => {
    const clientLike = fakeClient();
    const observedKeys: unknown[] = [];
    clientLike.artifacts.create = async (
      _driveId,
      _name,
      _content,
      options,
    ) => {
      observedKeys.push(options?.idempotencyKey);
      return { id: "art_0000000000000002" };
    };
    const server = createAgentDriveMcpServer(clientLike, FULL_GRANT);
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      const firstResponse = await client.callTool({
        name: "create_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          name: "note.txt",
          content: { encoding: "text", value: "content" },
        },
      });
      const secondResponse = await client.callTool({
        name: "create_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          name: "note.txt",
          content: { encoding: "text", value: "content" },
        },
      });
      expect(firstResponse.isError).not.toBe(true);
      expect(secondResponse.isError).not.toBe(true);
      expect(observedKeys).toHaveLength(2);
      expect(observedKeys[0]).toBe(observedKeys[1]);
      expect(observedKeys[0]).toMatch(/^mcp-[0-9a-f]{64}$/u);
    } finally {
      await client.close();
      await server.close();
    }
  });

  it("requires a revision for existing-state writes at the schema boundary", async () => {
    const server = createAgentDriveMcpServer(fakeClient(), FULL_GRANT);
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      const response = await client.callTool({
        name: "delete",
        arguments: {
          drive_id: "drv_0000000000000001",
          type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(response.isError).toBe(true);

      const invalidRead = await client.callTool({
        name: "read_artifact",
        arguments: { drive_id: "drv_0000000000000001" },
      });
      expect(invalidRead.isError).toBe(true);

      const invalidMove = await client.callTool({
        name: "move",
        arguments: {
          drive_id: "drv_0000000000000001",
          type: "artifact",
          resource_id: "art_0000000000000001",
          revision: "rev-1",
        },
      });
      expect(invalidMove.isError).toBe(true);
    } finally {
      await client.close();
      await server.close();
    }
  });

  it("rejects ambiguous, unsafe, and type-incompatible tool input", async () => {
    const server = createAgentDriveMcpServer(fakeClient(), FULL_GRANT);
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      const ambiguousDirectory = await client.callTool({
        name: "list_directory",
        arguments: {
          drive_id: "drv_0000000000000001",
          parent_id: "fld_0000000000000001",
          path: "docs",
        },
      });
      expect(ambiguousDirectory.isError).toBe(true);

      const ambiguousRead = await client.callTool({
        name: "read_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          path: "docs/note.txt",
        },
      });
      expect(ambiguousRead.isError).toBe(true);

      const ambiguousMove = await client.callTool({
        name: "move",
        arguments: {
          drive_id: "drv_0000000000000001",
          type: "artifact",
          resource_id: "art_0000000000000001",
          revision: "rev-1",
          parent_id: "fld_0000000000000001",
          parent_path: "docs",
        },
      });
      expect(ambiguousMove.isError).toBe(true);

      const mismatchedMove = await client.callTool({
        name: "move",
        arguments: {
          drive_id: "drv_0000000000000001",
          type: "folder",
          resource_id: "art_0000000000000001",
          revision: "rev-1",
          name: "renamed.txt",
        },
      });
      expect(mismatchedMove.isError).toBe(true);

      const unsafeCreate = await client.callTool({
        name: "create_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          name: ".env",
          content: { encoding: "text", value: "secret" },
        },
      });
      expect(unsafeCreate.isError).toBe(true);

      const unsafeMove = await client.callTool({
        name: "move",
        arguments: {
          drive_id: "drv_0000000000000001",
          type: "artifact",
          resource_id: "art_0000000000000001",
          revision: "rev-1",
          name: "id_rsa",
        },
      });
      expect(unsafeMove.isError).toBe(true);

      const emptyCreate = await client.callTool({
        name: "create_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          name: "empty.txt",
          content: { encoding: "text", value: "" },
        },
      });
      expect(emptyCreate.isError).toBe(true);

      const unicodeName = await client.callTool({
        name: "create_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          name: "🌿".repeat(255),
          content: { encoding: "text", value: "content" },
        },
      });
      expect(unicodeName.isError).not.toBe(true);

      const unicodeNameTooLong = await client.callTool({
        name: "create_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          name: "🌿".repeat(256),
          content: { encoding: "text", value: "content" },
        },
      });
      expect(unicodeNameTooLong.isError).toBe(true);

      // A malformed id is now caught at the TOOL boundary. It used to satisfy
      // the declared `[A-Za-z0-9_-]+` pattern, reach AgentDrive, and come back
      // INVALID_ARGUMENT -- indistinguishable from a well-formed id that does
      // not exist (ARTIFACT_NOT_FOUND). Matching the real minted shape
      // (prefix + 16 lowercase hex) makes the two outcomes mean what they say.
      const malformedId = await client.callTool({
        name: "read_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_doesnotexist",
        },
      });
      expect(malformedId.isError).toBe(true);

      const uppercaseId = await client.callTool({
        name: "read_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000ABCDEF",
        },
      });
      expect(uppercaseId.isError).toBe(true);

      const wellFormedId = await client.callTool({
        name: "read_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
        },
      });
      expect(wellFormedId.isError).not.toBe(true);

      const invalidBase64 = await client.callTool({
        name: "create_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          name: "binary.dat",
          content: { encoding: "base64", value: "not-base64" },
        },
      });
      expect(invalidBase64.isError).toBe(true);
      const invalidBase64Text = (
        invalidBase64.content as Array<{ type?: string; text?: string }>
      ).find((item) => item.type === "text")?.text;
      expect(JSON.parse(invalidBase64Text as string)).toMatchObject({
        error: { code: "invalid_input" },
      });

      const unknownField = await client.callTool({
        name: "create_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          name: "note.txt",
          content: { encoding: "text", value: "content" },
          path: "docs/note.txt",
        },
      });
      expect(unknownField.isError).toBe(true);

      const invalidGrantFilter = await client.callTool({
        name: "list_access_grants",
        arguments: {
          drive_id: "drv_0000000000000001",
          resource_id: "art_0000000000000001",
        },
      });
      expect(invalidGrantFilter.isError).toBe(true);

      const invalidGrantType = await client.callTool({
        name: "list_access_grants",
        arguments: {
          drive_id: "drv_0000000000000001",
          resource_type: "artifact_version",
        },
      });
      expect(invalidGrantType.isError).toBe(true);

      const mismatchedGrant = await client.callTool({
        name: "list_access_grants",
        arguments: {
          drive_id: "drv_0000000000000001",
          resource_type: "folder",
          resource_id: "art_0000000000000001",
        },
      });
      expect(mismatchedGrant.isError).toBe(true);

      const mismatchedShare = await client.callTool({
        name: "create_share_link",
        arguments: {
          drive_id: "drv_0000000000000001",
          resource_type: "artifact_version",
          resource_id: "art_0000000000000001",
        },
      });
      expect(mismatchedShare.isError).toBe(true);

      const invalidChangesStart = await client.callTool({
        name: "list_changes",
        arguments: { drive_id: "drv_0000000000000001" },
      });
      expect(invalidChangesStart.isError).toBe(true);

      const ambiguousChangesStart = await client.callTool({
        name: "list_changes",
        arguments: {
          drive_id: "drv_0000000000000001",
          cursor: "cur_test",
          start: "now",
        },
      });
      expect(ambiguousChangesStart.isError).toBe(true);

      const validChangesStart = await client.callTool({
        name: "list_changes",
        arguments: { drive_id: "drv_0000000000000001", start: "beginning" },
      });
      expect(validChangesStart.isError).not.toBe(true);

      const oversizedMetadata = await client.callTool({
        name: "update_artifact_metadata",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          revision: "rev-1",
          metadata: { note: "x".repeat(17_000) },
        },
      });
      expect(oversizedMetadata.isError).toBe(true);

      let deeplyNested: unknown = "leaf";
      for (let index = 0; index < 10; index += 1) deeplyNested = [deeplyNested];
      const deeplyNestedMetadata = await client.callTool({
        name: "update_artifact_metadata",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          revision: "rev-1",
          metadata: { nested: deeplyNested },
        },
      });
      expect(deeplyNestedMetadata.isError).toBe(true);

      const artifactRecursiveDelete = await client.callTool({
        name: "delete",
        arguments: {
          drive_id: "drv_0000000000000001",
          type: "artifact",
          resource_id: "art_0000000000000001",
          revision: "rev-1",
          recursive: true,
        },
      });
      expect(artifactRecursiveDelete.isError).toBe(true);
    } finally {
      await client.close();
      await server.close();
    }
  });

  it("enforces the read limit before buffering a streamed response", async () => {
    const clientLike = fakeClient();
    let blobFallbackCalled = false;
    clientLike.artifacts.contentResponse = async () =>
      new Response("012345", {
        headers: { "content-length": "6", "content-type": "text/plain" },
      });
    clientLike.artifacts.content = async () => {
      blobFallbackCalled = true;
      return new Blob(["012345"], { type: "text/plain" });
    };
    const server = createAgentDriveMcpServer(clientLike, FULL_GRANT);
    const client = new McpClient({
      name: "synthetic-client",
      version: "1.0.0",
    });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      client.connect(clientTransport),
    ]);
    try {
      const response = await client.callTool({
        name: "read_artifact",
        arguments: {
          drive_id: "drv_0000000000000001",
          artifact_id: "art_0000000000000001",
          include_content: true,
          max_bytes: 3,
        },
      });
      expect(response.isError).toBe(true);
      expect(blobFallbackCalled).toBe(false);
    } finally {
      await client.close();
      await server.close();
    }
  });
});

describe("publish and unpublish", () => {
  const DRIVE = "drv_0000000000000001";
  const FUTURE_YEAR = new Date().getUTCFullYear() + 2;

  const PUBLIC_BASE = "https://share.tokencanopy.test";

  async function connect(
    handle: FakeClientHandle,
    options: { publicBaseUrl?: string } = {},
  ): Promise<{ mcp: McpClient; close: () => Promise<void> }> {
    const server = createAgentDriveMcpServer(
      handle.client,
      FULL_GRANT,
      options,
    );
    const mcp = new McpClient({ name: "public-test", version: "1.0.0" });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      mcp.connect(clientTransport),
    ]);
    return {
      mcp,
      close: async () => {
        await mcp.close();
        await server.close();
      },
    };
  }

  function payloadOf(response: Record<string, unknown>) {
    const text = (
      response.content as Array<{ type?: string; text?: string }>
    ).find((item) => item.type === "text")?.text;
    return JSON.parse(text as string);
  }

  it("creates a public viewer grant on a private artifact", async () => {
    const handle = fakeClientWithState();
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(response.isError).not.toBe(true);
      expect(handle.created).toHaveLength(1);
      expect(handle.created[0].input).toEqual({
        principalType: "public",
        resourceType: "artifact",
        resourceId: "art_0000000000000001",
        role: "viewer",
        expiresAt: null,
      });
      // No DERIVED key: the listing already makes a retry a no-op, and a
      // stable key would replay a revoked grant (see the on → off → on test).
      expect(handle.created[0].idempotencyKey).toBeUndefined();
      expect(handle.revoked).toHaveLength(0);
      const payload = payloadOf(response);
      expect(payload).toMatchObject({
        drive_id: DRIVE,
        resource_type: "artifact",
        resource_id: "art_0000000000000001",
        published: true,
        grant: { principalType: "public", role: "viewer" },
        inherited_from: null,
        effective_visibility: "public",
      });
      // No public origin configured: the permalink is omitted, not guessed,
      // and the warning says why.
      expect(payload).not.toHaveProperty("public_url");
      expect(payload.warnings).toEqual([
        expect.stringContaining("MCP_PUBLIC_BASE_URL"),
      ]);
      expect(response.structuredContent).toMatchObject({ published: true });
    } finally {
      await close();
    }
  });

  it("returns the permanent public URL when the deployment knows the share host", async () => {
    const handle = fakeClientWithState();
    const { mcp, close } = await connect(handle, {
      publicBaseUrl: PUBLIC_BASE,
    });
    try {
      const artifact = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(artifact.isError).not.toBe(true);
      expect(payloadOf(artifact)).toMatchObject({
        published: true,
        public_url: `${PUBLIC_BASE}/a/art_0000000000000001/`,
        warnings: [],
      });

      const folder = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "folder",
          resource_id: "fld_0000000000000002",
        },
      });
      expect(payloadOf(folder)).toMatchObject({
        published: true,
        public_url: `${PUBLIC_BASE}/f/fld_0000000000000002/`,
      });

      // The address is a property of the resource, so a no-op publish
      // returns it too; unpublish never does.
      const again = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(payloadOf(again).public_url).toBe(
        `${PUBLIC_BASE}/a/art_0000000000000001/`,
      );
      const off = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(off.isError).not.toBe(true);
      expect(payloadOf(off)).not.toHaveProperty("public_url");
    } finally {
      await close();
    }
  });

  it("passes expires_at through, with any RFC 3339 offset, and honours a caller key", async () => {
    const handle = fakeClientWithState();
    const { mcp, close } = await connect(handle);
    try {
      const first = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
          expires_at: `${FUTURE_YEAR}-03-01T02:00:00+02:00`,
          idempotency_key: "caller-key-1",
        },
      });
      expect(first.isError).not.toBe(true);
      expect(handle.created[0].input.expiresAt).toEqual(
        new Date(`${FUTURE_YEAR}-03-01T00:00:00.000Z`),
      );
      expect(handle.created[0].idempotencyKey).toBe("caller-key-1");
    } finally {
      await close();
    }
  });

  it("refuses a past expires_at at the schema, before any read", async () => {
    const handle = fakeClientWithState();
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
          expires_at: "2000-01-01T00:00:00Z",
        },
      });
      // Schema refusals come back as the MCP SDK's own validation error,
      // before the handler runs — the client is never touched.
      expect(response.isError).toBe(true);
      expect(handle.created).toHaveLength(0);
    } finally {
      await close();
    }
  });

  it("really creates again after on → off → on (no stored-response replay)", async () => {
    // AgentDrive replays a completed mutation for 24 h by Idempotency-Key.
    // A key derived from the arguments alone is identical on every "turn
    // on" for the same resource, so the third call here would get the
    // FIRST grant's stored 201 back — a revoked row reported as public.
    const handle = fakeClientWithState();
    const { mcp, close } = await connect(handle);
    const args = {
      drive_id: DRIVE,
      resource_type: "artifact",
      resource_id: "art_0000000000000001",
    };
    try {
      const on = await mcp.callTool({
        name: "publish",
        arguments: { ...args },
      });
      expect(payloadOf(on)).toMatchObject({ published: true });
      const off = await mcp.callTool({
        name: "unpublish",
        arguments: { ...args },
      });
      expect(payloadOf(off)).toMatchObject({
        published: false,
        effective_visibility: "private",
      });
      const again = await mcp.callTool({
        name: "publish",
        arguments: { ...args },
      });
      const payload = payloadOf(again);
      expect(handle.created).toHaveLength(2);
      const liveDirect = handle.grants.filter(
        (grant) =>
          grant.revokedAt === null &&
          grant.resourceId === "art_0000000000000001",
      );
      expect(liveDirect).toHaveLength(1);
      expect(payload).toMatchObject({
        published: true,
        grant: { id: liveDirect[0].id },
        effective_visibility: "public",
      });
      expect(payload.grant.id).not.toBe(handle.grants[0].id);
    } finally {
      await close();
    }
  });

  it("reports a grant the server calls expired as not public", async () => {
    // Belt and braces for the schema refusal above: if a stale or skewed
    // grant comes back `expired`, `public` follows the server's word.
    const handle = fakeClientWithState();
    const expired = seedPublicGrant(handle, "artifact", "art_0000000000000001");
    expired.state = "expired";
    expired.expiresAt = new Date("2000-01-01T00:00:00Z");
    // The server's `active` listing already excludes it; mirror that.
    expired.revokedAt = new Date("2000-01-01T00:00:00Z");
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(handle.created).toHaveLength(1);
      expect(payloadOf(response)).toMatchObject({ published: true });
      expect(payloadOf(response).grant.id).not.toBe(expired.id);
    } finally {
      await close();
    }
  });

  it("warns instead of silently ignoring expires_at on an already-public resource", async () => {
    const handle = fakeClientWithState();
    const existing = seedPublicGrant(
      handle,
      "artifact",
      "art_0000000000000001",
    );
    const { mcp, close } = await connect(handle, {
      publicBaseUrl: PUBLIC_BASE,
    });
    try {
      const response = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
          expires_at: `${FUTURE_YEAR}-03-01T00:00:00Z`,
        },
      });
      expect(response.isError).not.toBe(true);
      expect(handle.created).toHaveLength(0);
      const payload = payloadOf(response);
      expect(payload.grant.id).toBe(existing.id);
      expect(payload.warnings).toHaveLength(1);
      expect(payload.warnings[0]).toContain("expires_at ignored");
      expect(payload.warnings[0]).toContain(existing.id);
    } finally {
      await close();
    }
  });

  it("creates a direct grant even when an ancestor already makes it public", async () => {
    const handle = fakeClientWithState();
    const driveGrant = seedPublicGrant(handle, "drive", DRIVE);
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(response.isError).not.toBe(true);
      expect(handle.created).toHaveLength(1);
      expect(payloadOf(response)).toMatchObject({
        published: true,
        inherited_from: { grant_id: driveGrant.id, resource_type: "drive" },
        effective_visibility: "public",
      });
    } finally {
      await close();
    }
  });

  it("is not_found before any grant is touched when the resource is missing", async () => {
    const handle = fakeClientWithState();
    seedPublicGrant(handle, "drive", DRIVE);
    const { mcp, close } = await connect(handle);
    try {
      for (const args of [
        { resource_type: "artifact", resource_id: "art_00000000000000ff" },
        { resource_type: "folder", resource_id: "fld_00000000000000ff" },
      ]) {
        for (const name of ["publish", "unpublish"]) {
          const response = await mcp.callTool({
            name,
            arguments: { drive_id: DRIVE, ...args },
          });
          expect(response.isError).toBe(true);
          expect(payloadOf(response).error.code).toBe("not_found");
        }
      }
      expect(handle.created).toHaveLength(0);
      expect(handle.revoked).toHaveLength(0);
    } finally {
      await close();
    }
  });

  it("sees a direct grant on the second page, and refuses an unbounded listing", async () => {
    const handle = fakeClientWithState();
    // 150 public grants on other artifacts push the target's onto page 2.
    for (let index = 0; index < 150; index += 1) {
      seedPublicGrant(handle, "folder", "fld_0000000000000002");
    }
    const target = seedPublicGrant(handle, "artifact", "art_0000000000000001");
    const { mcp, close } = await connect(handle);
    try {
      const off = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(off.isError).not.toBe(true);
      expect(handle.revoked.map((call) => call.grantId)).toEqual([target.id]);

      // Past 10 pages of 100 the tool refuses rather than paging on:
      // 150 + 900 live grants is 1050, one page over the bound.
      for (let index = 0; index < 900; index += 1) {
        seedPublicGrant(handle, "folder", "fld_0000000000000002");
      }
      const unbounded = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(unbounded.isError).toBe(true);
      expect(payloadOf(unbounded).error.code).toBe("public_grants_unbounded");
      expect(handle.created).toHaveLength(0);
    } finally {
      await close();
    }
  });

  it("refuses a folder chain that loops, touching no grant", async () => {
    const handle = fakeClientWithState();
    const original = handle.client.folders.get;
    handle.client.folders.get = async (driveId, folderId) =>
      folderId === "fld_0000000000000002"
        ? { id: folderId, parentId: "fld_0000000000000003" }
        : original(driveId, folderId);
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000002",
        },
      });
      expect(response.isError).toBe(true);
      expect(payloadOf(response).error.code).toBe("folder_ancestry_unbounded");
      expect(handle.created).toHaveLength(0);
    } finally {
      await close();
    }
  });

  it("does not out-strict the server when an ancestor folder is unreadable", async () => {
    // `folders_read` needs viewer on THAT folder and grants inherit
    // downward only, so a manager whose grant sits on a subfolder cannot
    // read above it — yet `grants_create` on the resource succeeds.
    const handle = fakeClientWithState();
    const original = handle.client.folders.get;
    handle.client.folders.get = async (driveId, folderId) => {
      if (folderId === "fld_0000000000000002") {
        throw Object.assign(new Error("forbidden"), { statusCode: 404 });
      }
      return original(driveId, folderId);
    };
    const { mcp, close } = await connect(handle, {
      publicBaseUrl: PUBLIC_BASE,
    });
    try {
      const response = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000002",
        },
      });
      expect(response.isError).not.toBe(true);
      expect(handle.created).toHaveLength(1);
      const payload = payloadOf(response);
      expect(payload.published).toBe(true);
      expect(payload.warnings).toHaveLength(1);
      expect(payload.warnings[0]).toContain("fld_0000000000000002");
    } finally {
      await close();
    }
  });

  it("does not turn a landed change into an error when the re-read fails", async () => {
    const handle = fakeClientWithState();
    const original = handle.client.artifacts.get;
    let reads = 0;
    handle.client.artifacts.get = async (driveId, artifactId) => {
      reads += 1;
      if (reads > 1) {
        throw Object.assign(new Error("upstream down"), { statusCode: 503 });
      }
      return original(driveId, artifactId);
    };
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(response.isError).not.toBe(true);
      expect(handle.created).toHaveLength(1);
      expect(payloadOf(response)).toMatchObject({
        published: true,
        effective_visibility: null,
      });
      expect(payloadOf(response).warnings[0]).toContain(
        "effective_visibility unavailable",
      );
    } finally {
      await close();
    }
  });

  it("is a no-op when a live direct public grant already exists", async () => {
    const handle = fakeClientWithState();
    const existing = seedPublicGrant(
      handle,
      "artifact",
      "art_0000000000000001",
    );
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(response.isError).not.toBe(true);
      expect(handle.created).toHaveLength(0);
      expect(handle.revoked).toHaveLength(0);
      expect(payloadOf(response)).toMatchObject({
        published: true,
        grant: { id: existing.id },
        effective_visibility: "public",
      });
    } finally {
      await close();
    }
  });

  it("revokes every live direct public grant when turned off", async () => {
    const handle = fakeClientWithState();
    const first = seedPublicGrant(handle, "artifact", "art_0000000000000001");
    const second = seedPublicGrant(handle, "artifact", "art_0000000000000001");
    // A revoked row and a grant on ANOTHER resource must both be left alone.
    const stale = seedPublicGrant(handle, "artifact", "art_0000000000000001");
    stale.revokedAt = new Date("2026-01-01T00:00:00Z");
    stale.state = "revoked";
    const other = seedPublicGrant(handle, "folder", "fld_0000000000000002");
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(response.isError).not.toBe(true);
      expect(
        handle.revoked.map((call) => [call.grantId, call.revision]),
      ).toEqual([
        [first.id, `rev-${first.id}`],
        [second.id, `rev-${second.id}`],
      ]);
      expect(handle.revoked[0].idempotencyKey).toMatch(/^mcp-[0-9a-f]{64}$/u);
      expect(handle.revoked[0].idempotencyKey).not.toBe(
        handle.revoked[1].idempotencyKey,
      );
      expect(other.revokedAt).toBeNull();
      expect(payloadOf(response)).toMatchObject({
        published: false,
        grant: null,
        inherited_from: null,
        effective_visibility: "private",
        warnings: [],
      });
    } finally {
      await close();
    }
  });

  it("says why an artifact is still public once its own grant is gone", async () => {
    // A share link is a second read path the server counts as public, so
    // `published: false` lands beside `effective_visibility: "public"`. That
    // is correct and reads as a contradiction; the warning is what turns it
    // into an instruction (the 2026-09-11 gate check hit exactly this).
    const handle = fakeClientWithState();
    seedPublicGrant(handle, "artifact", "art_0000000000000001");
    handle.linkShared.add("art_0000000000000001");
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(response.isError).not.toBe(true);
      expect(handle.revoked).toHaveLength(1);
      const payload = payloadOf(response);
      expect(payload).toMatchObject({
        published: false,
        grant: null,
        inherited_from: null,
        effective_visibility: "public",
      });
      expect(payload.warnings).toHaveLength(1);
      expect(payload.warnings[0]).toMatch(/share link/u);
      expect(payload.warnings[0]).toMatch(/revoke/iu);
    } finally {
      await close();
    }
  });

  it("names the ancestor grant that keeps an artifact public after its own is revoked", async () => {
    // art_…02 sits under fld_…03 → fld_…02 → the root, so a grant on fld_…02
    // is a real ancestor of it.
    const handle = fakeClientWithState();
    const ancestor = seedPublicGrant(handle, "folder", "fld_0000000000000002");
    seedPublicGrant(handle, "artifact", "art_0000000000000002");
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000002",
        },
      });
      expect(response.isError).not.toBe(true);
      expect(handle.revoked).toHaveLength(1);
      const payload = payloadOf(response);
      expect(payload.effective_visibility).toBe("public");
      expect(payload.inherited_from).toMatchObject({ grant_id: ancestor.id });
      expect(payload.warnings).toHaveLength(1);
      expect(payload.warnings[0]).toContain(ancestor.id);
      expect(payload.warnings[0]).toContain("fld_0000000000000002");
    } finally {
      await close();
    }
  });

  it("refuses to turn off access that is only inherited from the drive", async () => {
    const handle = fakeClientWithState();
    const driveGrant = seedPublicGrant(handle, "drive", DRIVE);
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(response.isError).toBe(true);
      expect(handle.revoked).toHaveLength(0);
      const payload = payloadOf(response);
      expect(payload.error.code).toBe("public_inherited");
      expect(payload.error.message).toContain(driveGrant.id);
      expect(payload.error.message).toContain("drive");
      expect(payload.error.inherited_from).toEqual({
        grant_id: driveGrant.id,
        resource_type: "drive",
        resource_id: DRIVE,
      });
    } finally {
      await close();
    }
  });

  it("names the NEAREST ancestor folder grant, walking the folder chain", async () => {
    const handle = fakeClientWithState();
    seedPublicGrant(handle, "drive", DRIVE);
    const nearest = seedPublicGrant(handle, "folder", "fld_0000000000000002");
    const { mcp, close } = await connect(handle);
    try {
      // art_…02 lives in fld_…03 → fld_…02 → fld_…01 (root) → drive.
      const response = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000002",
        },
      });
      expect(response.isError).toBe(true);
      expect(payloadOf(response).error.inherited_from).toEqual({
        grant_id: nearest.id,
        resource_type: "folder",
        resource_id: "fld_0000000000000002",
      });
    } finally {
      await close();
    }
  });

  it("still revokes a direct grant when an ancestor also makes it public, and says so", async () => {
    const handle = fakeClientWithState();
    const ancestor = seedPublicGrant(handle, "drive", DRIVE);
    const direct = seedPublicGrant(handle, "artifact", "art_0000000000000001");
    const { mcp, close } = await connect(handle);
    try {
      const response = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
        },
      });
      expect(response.isError).not.toBe(true);
      expect(handle.revoked.map((call) => call.grantId)).toEqual([direct.id]);
      expect(payloadOf(response)).toMatchObject({
        published: false,
        grant: null,
        inherited_from: { grant_id: ancestor.id, resource_type: "drive" },
        // The server's word, not ours: still public through the drive.
        effective_visibility: "public",
      });
    } finally {
      await close();
    }
  });

  it("handles a folder, whose visibility the service does not compute", async () => {
    const handle = fakeClientWithState();
    const { mcp, close } = await connect(handle);
    try {
      const on = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "folder",
          resource_id: "fld_0000000000000003",
        },
      });
      expect(on.isError).not.toBe(true);
      expect(handle.created[0].input).toMatchObject({
        resourceType: "folder",
        resourceId: "fld_0000000000000003",
      });
      expect(payloadOf(on)).toMatchObject({
        resource_type: "folder",
        published: true,
        grant: { resourceType: "folder" },
        inherited_from: null,
        effective_visibility: null,
      });

      const off = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "folder",
          resource_id: "fld_0000000000000003",
        },
      });
      expect(off.isError).not.toBe(true);
      expect(handle.revoked).toHaveLength(1);
      expect(payloadOf(off)).toMatchObject({
        published: false,
        grant: null,
        effective_visibility: null,
      });
    } finally {
      await close();
    }
  });

  it("rejects a mismatched id, expires_at without public, and drive resources at the schema", async () => {
    const handle = fakeClientWithState();
    const { mcp, close } = await connect(handle);
    try {
      const mismatched = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "folder",
          resource_id: "art_0000000000000001",
        },
      });
      expect(mismatched.isError).toBe(true);
      const expiryOnOff = await mcp.callTool({
        name: "unpublish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "artifact",
          resource_id: "art_0000000000000001",
          expires_at: `${FUTURE_YEAR}-03-01T00:00:00.000Z`,
        },
      });
      expect(expiryOnOff.isError).toBe(true);
      const drive = await mcp.callTool({
        name: "publish",
        arguments: {
          drive_id: DRIVE,
          resource_type: "drive",
          resource_id: DRIVE,
        },
      });
      expect(drive.isError).toBe(true);
      expect(handle.created).toHaveLength(0);
      expect(handle.revoked).toHaveLength(0);
    } finally {
      await close();
    }
  });

  it("tells the agent, in the description, what public means", async () => {
    const handle = fakeClientWithState();
    const { mcp, close } = await connect(handle);
    try {
      const tool = (await mcp.listTools()).tools.find(
        (candidate) => candidate.name === "publish",
      );
      expect(tool).toBeDefined();
      expect(tool?.description).toMatch(/anyone with the link/iu);
      expect(tool?.description).toMatch(/no account/iu);
      expect(tool?.description).toMatch(/viewer/iu);
      expect(tool?.annotations).toMatchObject({
        readOnlyHint: false,
        idempotentHint: true,
        openWorldHint: true,
      });
    } finally {
      await close();
    }
  });
});

describe("per-tool scope authorization", () => {
  const READ_ONLY = [
    "drives:read",
    "usage:read",
    "content:read",
    "changes:read",
    "sharing:read",
  ];

  async function connect(
    authorization: { scopes: readonly string[] },
    client: AgentDriveClientLike = fakeClient(),
  ): Promise<McpClient> {
    const server = createAgentDriveMcpServer(client, authorization);
    const mcp = new McpClient({ name: "scope-test", version: "1.0.0" });
    const [clientTransport, serverTransport] =
      InMemoryTransport.createLinkedPair();
    await Promise.all([
      server.connect(serverTransport),
      mcp.connect(clientTransport),
    ]);
    return mcp;
  }

  it("declares required scopes for every registered tool", async () => {
    const scopes = agentDriveMcpToolScopes();
    const mcp = await connect({ scopes: [...AGENTDRIVE_MCP_SCOPES] });
    const listed = (await mcp.listTools()).tools
      .map((tool) => tool.name)
      .sort();
    expect(listed).toEqual([...scopes.keys()].sort());
    // A tool cannot be registered without scopes: the sink's parameter is a
    // non-empty tuple, so this is a type-level guarantee. Assert it holds at
    // runtime too, in case a future refactor casts around the type.
    for (const [name, required] of scopes) {
      expect(required.length, `${name} declares no scopes`).toBeGreaterThan(0);
      for (const scope of required) {
        expect(AGENTDRIVE_MCP_SCOPES).toContain(scope);
      }
    }
  });

  it("maps every tool to the scope its /v0 routes actually require", () => {
    // Verified against AgentDrive's own `_SCOPE_*` constants in
    // src/agentdrive/api/v0_*.py, not against SDK method names. `v0_uploads`
    // requires content:write on ALL FOUR of its routes, its GET included, so
    // get_file_upload is a write tool.
    expect(Object.fromEntries(agentDriveMcpToolScopes())).toEqual({
      list_drives: ["drives:read", "usage:read"],
      list_directory: ["content:read"],
      search_drive: ["content:read"],
      read_artifact: ["content:read"],
      list_artifact_versions: ["content:read"],
      list_changes: ["changes:read"],
      list_access_grants: ["sharing:read"],
      create_drive: ["drives:write"],
      delete_drive: ["drives:write"],
      restore_drive: ["drives:write"],
      create_artifact: ["content:write"],
      replace_artifact_content: ["content:write"],
      update_artifact_metadata: ["content:write"],
      create_folder: ["content:write"],
      move: ["content:write"],
      delete: ["content:write"],
      restore: ["content:write"],
      create_share_link: ["sharing:write"],
      // Lists grants to find the current state, reads the artifact or folder
      // for its parent chain and `effective_visibility` (`v0_artifacts` and
      // `v0_folders` gate reads on content:read), and creates/revokes.
      publish: ["sharing:read", "sharing:write", "content:read"],
      unpublish: ["sharing:read", "sharing:write", "content:read"],
      begin_file_upload: ["content:write"],
      get_file_upload: ["content:write"],
      complete_file_upload: ["content:write"],
      cancel_file_upload: ["content:write"],
    });
  });

  it("exposes the full twenty-four-tool surface to a full read/write grant", async () => {
    const mcp = await connect({ scopes: [...AGENTDRIVE_MCP_SCOPES] });
    expect((await mcp.listTools()).tools).toHaveLength(24);
  });

  it("withholds publish and unpublish from a sharing-only grant missing content:read", async () => {
    const mcp = await connect({ scopes: ["sharing:read", "sharing:write"] });
    const names = (await mcp.listTools()).tools.map((tool) => tool.name);
    expect(names).toContain("create_share_link");
    expect(names).not.toContain("publish");
    // The handler re-checks independently of registration.
    const result = await mcp.callTool({
      name: "publish",
      arguments: {
        drive_id: "drv_0000000000000001",
        resource_type: "artifact",
        resource_id: "art_0000000000000001",
      },
    });
    expect(result.isError).toBe(true);
  });

  // Directory-listing requirement, not a stylistic one: the Claude connector
  // directory rejects a tool that carries no display title, and it decides
  // per-call confirmation from the read-only/destructive hint. Both are read
  // off the LIVE server when a listing is submitted or resynced, so a tool
  // added without them breaks the listing rather than a local build.
  it("gives every tool a title and a read-only or destructive hint", async () => {
    const mcp = await connect({ scopes: [...AGENTDRIVE_MCP_SCOPES] });
    for (const tool of (await mcp.listTools()).tools) {
      expect(tool.title ?? tool.annotations?.title, tool.name).toBeTruthy();
      const annotations = tool.annotations ?? {};
      expect(
        annotations.readOnlyHint === true ||
          typeof annotations.destructiveHint === "boolean",
        tool.name,
      ).toBe(true);
    }
  });

  it("lists only read tools for a read-only grant", async () => {
    const mcp = await connect({ scopes: READ_ONLY });
    const names = (await mcp.listTools()).tools.map((tool) => tool.name).sort();
    expect(names).toEqual([
      "list_access_grants",
      "list_artifact_versions",
      "list_changes",
      "list_directory",
      "list_drives",
      "read_artifact",
      "search_drive",
    ]);
  });

  it("withholds list_drives from a grant missing usage:read", async () => {
    // Two scopes, both required: the tool calls drives.list AND, for an
    // active drive, drives.usage — a partial grant would fail halfway
    // through with an upstream 403.
    const mcp = await connect({ scopes: ["drives:read", "content:read"] });
    const names = (await mcp.listTools()).tools.map((tool) => tool.name);
    expect(names).not.toContain("list_drives");
    expect(names).toContain("list_directory");
  });

  it("withholds create_drive from a grant missing drives:write", async () => {
    // Every pre-split grant looks like this: the full vocabulary minus
    // drives:write. The new tool must be invisible to it, not merely broken.
    const legacy = AGENTDRIVE_MCP_SCOPES.filter(
      (scope) => scope !== "drives:write",
    );
    const mcp = await connect({ scopes: legacy });
    const names = (await mcp.listTools()).tools.map((tool) => tool.name);
    expect(names).not.toContain("create_drive");
    // And a hand-built call naming it anyway is refused by the second gate.
    const result = (await mcp.callTool({
      name: "create_drive",
      arguments: { name: "Research" },
    })) as { isError?: boolean; content: { text: string }[] };
    expect(result.isError).toBe(true);
  });

  it("creates a drive through the SDK with an idempotency key", async () => {
    const observed: Array<{
      name: string;
      options?: { metadata?: Record<string, unknown>; idempotencyKey?: string };
    }> = [];
    const clientLike = fakeClient();
    clientLike.drives.create = async (name, options) => {
      observed.push({ name, options });
      return { id: "drv_0000000000000002", name, revision: "drv-1" };
    };
    const mcp = await connect({ scopes: ["drives:write"] }, clientLike);
    const names = (await mcp.listTools()).tools.map((tool) => tool.name);
    expect(names).toEqual(["create_drive", "delete_drive", "restore_drive"]);
    const response = (await mcp.callTool({
      name: "create_drive",
      arguments: { name: "Research Notes" },
    })) as {
      isError?: boolean;
      structuredContent?: { drive?: { id?: string } };
    };
    expect(response.isError).not.toBe(true);
    expect(response.structuredContent?.drive).toMatchObject({
      id: "drv_0000000000000002",
      name: "Research Notes",
    });
    // The mutation convention: a caller-supplied key wins, and an omitted
    // one falls back to the semantic hash — never a missing header.
    expect(observed).toHaveLength(1);
    expect(observed[0]!.options?.idempotencyKey).toMatch(/^mcp-[0-9a-f]{64}$/u);

    const repeat = (await mcp.callTool({
      name: "create_drive",
      arguments: { name: "Research Notes", idempotency_key: "caller-key-1" },
    })) as { isError?: boolean };
    expect(repeat.isError).not.toBe(true);
    expect(observed[1]!.options?.idempotencyKey).toBe("caller-key-1");
  });

  it("deletes a drive under its current revision", async () => {
    const handle = fakeClientWithState();
    const mcp = await connect({ scopes: ["drives:write"] }, handle.client);
    expect((await mcp.listTools()).tools.map((tool) => tool.name)).toEqual([
      "create_drive",
      "delete_drive",
      "restore_drive",
    ]);
    const response = (await mcp.callTool({
      name: "delete_drive",
      arguments: {
        drive_id: "drv_0000000000000001",
        revision: "drv-1",
      },
    })) as {
      isError?: boolean;
      structuredContent?: { drive?: Record<string, unknown> };
    };
    expect(response.isError).not.toBe(true);
    // The post-delete revision comes back, which is what a restore needs.
    expect(response.structuredContent?.drive).toMatchObject({
      id: "drv_0000000000000001",
      revision: "drv-2",
      state: "deleted",
    });
    expect(handle.deletedDrives).toEqual([
      {
        driveId: "drv_0000000000000001",
        revision: "drv-1",
        idempotencyKey: expect.stringMatching(/^mcp-[0-9a-f]{64}$/u),
      },
    ]);

    const repeat = (await mcp.callTool({
      name: "delete_drive",
      arguments: {
        drive_id: "drv_0000000000000001",
        revision: "drv-1",
        idempotency_key: "caller-key-1",
      },
    })) as { isError?: boolean };
    expect(repeat.isError).not.toBe(true);
    expect(handle.deletedDrives[1]!.idempotencyKey).toBe("caller-key-1");
  });

  it("requires the revision on delete_drive, and refuses a folder id", async () => {
    // The revision is AgentDrive's If-Match. Letting it be optional here
    // would turn a lost update into a deleted drive.
    const handle = fakeClientWithState();
    const mcp = await connect({ scopes: ["drives:write"] }, handle.client);
    const missing = (await mcp.callTool({
      name: "delete_drive",
      arguments: { drive_id: "drv_0000000000000001" },
    })) as { isError?: boolean };
    expect(missing.isError).toBe(true);
    const wrongId = (await mcp.callTool({
      name: "delete_drive",
      arguments: { drive_id: "fld_0000000000000001", revision: "drv-1" },
    })) as { isError?: boolean };
    expect(wrongId.isError).toBe(true);
    expect(handle.deletedDrives).toEqual([]);
  });

  it("withholds delete_drive from a grant missing drives:write", async () => {
    const handle = fakeClientWithState();
    const legacy = AGENTDRIVE_MCP_SCOPES.filter(
      (scope) => scope !== "drives:write",
    );
    const mcp = await connect({ scopes: legacy }, handle.client);
    expect(
      (await mcp.listTools()).tools.map((tool) => tool.name),
    ).not.toContain("delete_drive");
    const result = (await mcp.callTool({
      name: "delete_drive",
      arguments: {
        drive_id: "drv_0000000000000001",
        revision: "drv-1",
      },
    })) as { isError?: boolean };
    expect(result.isError).toBe(true);
    expect(handle.deletedDrives).toEqual([]);
  });

  it("restores a drive under its post-delete revision", async () => {
    const handle = fakeClientWithState();
    const mcp = await connect({ scopes: ["drives:write"] }, handle.client);
    const response = (await mcp.callTool({
      name: "restore_drive",
      arguments: {
        drive_id: "drv_0000000000000001",
        revision: "drv-2",
        idempotency_key: "caller-key-1",
      },
    })) as {
      isError?: boolean;
      structuredContent?: { drive?: Record<string, unknown> };
    };
    expect(response.isError).not.toBe(true);
    expect(response.structuredContent?.drive).toMatchObject({
      id: "drv_0000000000000001",
      state: "active",
    });
    expect(handle.restoredDrives).toEqual([
      {
        driveId: "drv_0000000000000001",
        revision: "drv-2",
        idempotencyKey: "caller-key-1",
      },
    ]);
  });

  it("restores a folder with its cascade, and an artifact on its own", async () => {
    const handle = fakeClientWithState();
    const mcp = await connect({ scopes: ["content:write"] }, handle.client);
    const folder = (await mcp.callTool({
      name: "restore",
      arguments: {
        drive_id: "drv_0000000000000001",
        type: "folder",
        resource_id: "fld_0000000000000002",
        revision: "fld-2",
      },
    })) as {
      isError?: boolean;
      structuredContent?: { type?: string; resource?: Record<string, unknown> };
    };
    expect(folder.isError).not.toBe(true);
    expect(folder.structuredContent?.type).toBe("folder");
    // A folder comes back with the subtree that went down with it, so the
    // cascade counts must survive the tool's response mapping.
    expect(folder.structuredContent?.resource).toMatchObject({
      cascade: { folders: 1, artifacts: 2 },
    });

    const artifact = (await mcp.callTool({
      name: "restore",
      arguments: {
        drive_id: "drv_0000000000000001",
        type: "artifact",
        resource_id: "art_0000000000000001",
        revision: "rev-2",
      },
    })) as { isError?: boolean; structuredContent?: { type?: string } };
    expect(artifact.isError).not.toBe(true);
    expect(artifact.structuredContent?.type).toBe("artifact");

    expect(handle.restored.map((call) => call.type)).toEqual([
      "folder",
      "artifact",
    ]);
    for (const call of handle.restored) {
      expect(call.idempotencyKey).toMatch(/^mcp-[0-9a-f]{64}$/u);
    }
  });

  it("refuses a restore whose resource_id contradicts its type", async () => {
    // Same cross-check `delete` makes. Without it the tool would send a
    // folder id down the artifact path and the server would answer 404
    // about a resource the agent never named.
    const handle = fakeClientWithState();
    const mcp = await connect({ scopes: ["content:write"] }, handle.client);
    const crossed = (await mcp.callTool({
      name: "restore",
      arguments: {
        drive_id: "drv_0000000000000001",
        type: "artifact",
        resource_id: "fld_0000000000000002",
        revision: "fld-2",
      },
    })) as { isError?: boolean };
    expect(crossed.isError).toBe(true);
    const noRevision = (await mcp.callTool({
      name: "restore",
      arguments: {
        drive_id: "drv_0000000000000001",
        type: "folder",
        resource_id: "fld_0000000000000002",
      },
    })) as { isError?: boolean };
    expect(noRevision.isError).toBe(true);
    // `restore` takes no recursive flag: the server always brings the
    // deleted subtree back with its folder.
    const recursive = (await mcp.callTool({
      name: "restore",
      arguments: {
        drive_id: "drv_0000000000000001",
        type: "folder",
        resource_id: "fld_0000000000000002",
        revision: "fld-2",
        recursive: true,
      },
    })) as { isError?: boolean };
    expect(recursive.isError).toBe(true);
    expect(handle.restored).toEqual([]);
  });

  it("passes list_directory's state filter through, and defaults to active", async () => {
    // This is the only way an agent can reach a soft-deleted entry: a
    // deleted entry 404s on read, so without the filter the revision a
    // restore needs is unobtainable.
    const handle = fakeClientWithState();
    const mcp = await connect({ scopes: ["content:read"] }, handle.client);
    await mcp.callTool({
      name: "list_directory",
      arguments: { drive_id: "drv_0000000000000001" },
    });
    expect(handle.entryListOptions[0]!.state).toBeUndefined();

    const deleted = (await mcp.callTool({
      name: "list_directory",
      arguments: { drive_id: "drv_0000000000000001", state: "deleted" },
    })) as {
      isError?: boolean;
      structuredContent?: { entries?: Record<string, unknown>[] };
    };
    expect(deleted.isError).not.toBe(true);
    expect(handle.entryListOptions[1]!.state).toBe("deleted");
    // The entry carries the post-delete revision restore wants.
    expect(deleted.structuredContent?.entries?.[0]).toMatchObject({
      state: "deleted",
      revision: "rev-2",
    });

    const unknown = (await mcp.callTool({
      name: "list_directory",
      arguments: { drive_id: "drv_0000000000000001", state: "archived" },
    })) as { isError?: boolean };
    expect(unknown.isError).toBe(true);
    expect(handle.entryListOptions).toHaveLength(2);
  });

  it("passes list_drives' state filter through, and defaults to active", async () => {
    // The other half of the recovery story: a soft-deleted drive reads 404,
    // so without this filter the revision `restore_drive` needs survives
    // only inside the `delete_drive` response that produced it.
    const handle = fakeClientWithState();
    const mcp = await connect(
      { scopes: ["drives:read", "usage:read"] },
      handle.client,
    );
    await mcp.callTool({ name: "list_drives", arguments: {} });
    expect(handle.driveListOptions[0]!.state).toBe("active");

    const deleted = (await mcp.callTool({
      name: "list_drives",
      arguments: { state: "deleted" },
    })) as {
      isError?: boolean;
      structuredContent?: {
        drives?: { drive?: Record<string, unknown>; usage?: unknown }[];
      };
    };
    expect(deleted.isError).not.toBe(true);
    expect(handle.driveListOptions[1]!.state).toBe("deleted");
    // The deleted drive comes back WITH its post-delete revision, which is
    // the whole point: restore_drive has nowhere else to read it. Its usage
    // is null rather than an error, because `/v0/drives/{id}/usage` is an
    // active-drive read — the fan-out that ignored this failed the entire
    // page.
    expect(deleted.structuredContent?.drives?.[0]?.drive).toMatchObject({
      state: "deleted",
      revision: "drv-2",
      storageBytes: 4,
    });
    expect(deleted.structuredContent?.drives?.[0]?.usage).toBeNull();
    expect(handle.usageCalls).toEqual(["drv_0000000000000001"]);

    // `all` is the wildcard everywhere since #681; `any` is not a value.
    const wildcard = (await mcp.callTool({
      name: "list_drives",
      arguments: { state: "all" },
    })) as { isError?: boolean };
    expect(wildcard.isError).not.toBe(true);
    const stale = (await mcp.callTool({
      name: "list_drives",
      arguments: { state: "any" },
    })) as { isError?: boolean };
    expect(stale.isError).toBe(true);
    expect(handle.driveListOptions).toHaveLength(3);
  });

  it("still reports usage for an active drive", async () => {
    // The negative cases below all assert `usage: null`, so on their own
    // they pass just as happily if the tool stopped reporting usage
    // ENTIRELY — which is what a wrong `state` spelling would do, since
    // `drive.state !== "active"` is true for every drive when the field is
    // missing. This pins the direction that would otherwise fail silently.
    const handle = fakeClientWithState();
    const mcp = await connect(
      { scopes: ["drives:read", "usage:read"] },
      handle.client,
    );
    const listed = (await mcp.callTool({
      name: "list_drives",
      arguments: {},
    })) as {
      isError?: boolean;
      structuredContent?: {
        drives?: { drive?: Record<string, unknown>; usage?: unknown }[];
      };
    };
    expect(listed.isError).not.toBe(true);
    expect(listed.structuredContent?.drives?.[0]?.drive).toMatchObject({
      state: "active",
    });
    expect(listed.structuredContent?.drives?.[0]?.usage).toMatchObject({
      storageBytes: 4,
    });
    expect(handle.usageCalls).toEqual(["drv_0000000000000001"]);
  });

  it("survives a drive deleted between the listing and its usage read", async () => {
    // `state` is not the only way a 404 reaches the usage call: another
    // actor can delete a drive in the window between the two requests. A
    // page that fails because someone else deleted something is worse than
    // a page carrying one null, and before this the whole `Promise.all`
    // rejected.
    const handle = fakeClientWithState();
    handle.deletedDriveIds.add("drv_0000000000000001");
    const mcp = await connect(
      { scopes: ["drives:read", "usage:read"] },
      handle.client,
    );
    const listed = (await mcp.callTool({
      name: "list_drives",
      arguments: {},
    })) as {
      isError?: boolean;
      structuredContent?: {
        drives?: { drive?: Record<string, unknown>; usage?: unknown }[];
      };
    };
    expect(listed.isError).not.toBe(true);
    expect(listed.structuredContent?.drives?.[0]?.drive).toMatchObject({
      id: "drv_0000000000000001",
    });
    expect(listed.structuredContent?.drives?.[0]?.usage).toBeNull();
  });

  it("does not swallow a usage failure that is not a 404", async () => {
    // The catch is narrow on purpose. A 403 or a 500 from usage is a real
    // failure and must surface, not read as "this drive has no usage".
    const handle = fakeClientWithState();
    handle.client.drives.usage = async () => {
      throw Object.assign(new Error("upstream is unwell"), {
        statusCode: 503,
      });
    };
    const mcp = await connect(
      { scopes: ["drives:read", "usage:read"] },
      handle.client,
    );
    const listed = (await mcp.callTool({
      name: "list_drives",
      arguments: {},
    })) as { isError?: boolean };
    expect(listed.isError).toBe(true);
  });

  it("re-checks scopes in the handler, independently of what was registered", async () => {
    // The second gate is not a restatement of the first: it reads the
    // authorization object at CALL time. Narrowing the grant after
    // registration proves a registration bug alone cannot authorize a call.
    const authorization = { scopes: [...AGENTDRIVE_MCP_SCOPES] as string[] };
    const touched: string[] = [];
    const mcp = await connect(
      authorization,
      new Proxy(
        {},
        {
          get(_target, property) {
            touched.push(String(property));
            throw new Error("the SDK must not be reached");
          },
        },
      ) as unknown as AgentDriveClientLike,
    );
    authorization.scopes = READ_ONLY;

    const result = (await mcp.callTool({
      name: "create_folder",
      arguments: { drive_id: "drv_0000000000000001", name: "docs" },
    })) as { isError?: boolean; content: { text: string }[] };
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toContain("insufficient_scope");
    expect(touched).toEqual([]);
  });

  it("never touches the SDK client while enumerating tool scopes", () => {
    // agentDriveMcpToolScopes runs the definition pass with a client that
    // throws on any access, so this is a real assertion, not a comment.
    expect(() => agentDriveMcpToolScopes()).not.toThrow();
  });
});
