/**
 * The sidecar's WIRE SHAPE, judged against AgentDrive's own contract.
 *
 * `server.test.ts` stubs the AgentDrive client at the facade level, so it
 * asserts what a tool handler passes IN and never what leaves the process.
 * Three layers live below that stub — facade, generated client, query
 * serialization — and a break in any of them is invisible to every other test
 * in this package.
 *
 * That span is not hypothetical. The SDK facade DEFAULTS its filter arguments
 * rather than omitting them (0.0.3 sent `lifecycle: options.lifecycle ??
 * 'active'` on every list call), so when `/v0` renamed that parameter to
 * `state` the sidecar began sending an undeclared one on calls that never
 * mentioned it. AgentDrive answers an undeclared query parameter with
 * `400 INVALID_ARGUMENT` — `known_params()` — so `list_drives`,
 * `list_access_grants` and the public-grant scan behind `publish`/`unpublish`
 * failed against a real deployment while every package test stayed green.
 *
 * This file is the client-side mirror of that server rule: drive every tool
 * through the REAL SDK against a recording server, then assert that each query
 * parameter it sent is declared for that operation in
 * AgentDrive's `tests/openapi.golden.json` — the same reviewed snapshot the SDK
 * is generated from. The contract lives in this repository, so a `/v0` rename
 * fails this job on the PR that makes it, before any deploy.
 *
 * What it deliberately does NOT cover: response handling, authorization, and
 * live behaviour. Those need the MCP smoke gate against a running deployment.
 */
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  AgentDriveClient,
  StaticTokenProvider,
} from "@tokencanopy/agentdrive-sdk";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { defineAgentDriveMcpTools } from "../src/server.js";
import type { AgentDriveClientLike } from "../src/types.js";

/* ── the contract ─────────────────────────────────────────────────────── */

// AgentDrive's `tests/openapi.golden.json`. Two levels up from `mcp/server/`
// rather than four from `packages/agentdrive-mcp/`: the sidecar moved inside
// the app it speaks to, so the contract it is pinned against is a sibling now
// instead of something reached by leaving the package.
const GOLDEN = fileURLToPath(
  new URL("../../../tests/openapi.golden.json", import.meta.url),
);

interface OperationParameters {
  /** Declared `in: query` names. */
  query: Set<string>;
  /** Declared enum values per query parameter, where the schema pins them. */
  enums: Map<string, Set<string>>;
}

/** `{ "GET /v0/drives/{drive_id}/folders" -> parameters }` */
function loadContract(): Map<string, OperationParameters> {
  const document = JSON.parse(readFileSync(GOLDEN, "utf8")) as {
    paths: Record<string, Record<string, unknown>>;
  };
  const operations = new Map<string, OperationParameters>();
  for (const [path, item] of Object.entries(document.paths)) {
    for (const [method, operation] of Object.entries(item)) {
      if (!HTTP_METHODS.has(method)) continue;
      const declared = (operation as { parameters?: DeclaredParameter[] })
        .parameters;
      const query = new Set<string>();
      const enums = new Map<string, Set<string>>();
      for (const parameter of declared ?? []) {
        if (parameter.in !== "query") continue;
        query.add(parameter.name);
        const values = enumValues(parameter.schema);
        if (values) enums.set(parameter.name, values);
      }
      operations.set(`${method.toUpperCase()} ${path}`, { query, enums });
    }
  }
  return operations;
}

interface DeclaredParameter {
  name: string;
  in: string;
  schema?: unknown;
}

const HTTP_METHODS = new Set([
  "get",
  "put",
  "post",
  "delete",
  "patch",
  "head",
  "options",
  "trace",
]);

/**
 * Enum values, reached through the one level of indirection the generator
 * emits: a nullable/optional parameter becomes `anyOf: [{enum: …}, {type:
 * "null"}]` rather than carrying `enum` at the top.
 */
function enumValues(schema: unknown): Set<string> | undefined {
  if (!schema || typeof schema !== "object") return undefined;
  const node = schema as { enum?: unknown[]; anyOf?: unknown[] };
  if (Array.isArray(node.enum)) {
    return new Set(node.enum.filter((v): v is string => typeof v === "string"));
  }
  for (const branch of node.anyOf ?? []) {
    const nested = enumValues(branch);
    if (nested) return nested;
  }
  return undefined;
}

/**
 * A concrete request path back to the template that declares it.
 *
 * Segment-wise, because a template segment is either a literal or `{name}`
 * and ids are opaque — matching on length plus literals is exact here and
 * needs no knowledge of which segment holds which id.
 */
function matchTemplate(
  templates: Iterable<string>,
  method: string,
  path: string,
): string | undefined {
  const actual = path.split("/");
  for (const key of templates) {
    const [templateMethod, template] = key.split(" ");
    if (templateMethod !== method) continue;
    const expected = template.split("/");
    if (expected.length !== actual.length) continue;
    const matches = expected.every(
      (segment, index) =>
        (segment.startsWith("{") && segment.endsWith("}")) ||
        segment === actual[index],
    );
    if (matches) return key;
  }
  return undefined;
}

/* ── the recording server ─────────────────────────────────────────────── */

interface RecordedRequest {
  method: string;
  path: string;
  query: URLSearchParams;
}

const recorded: RecordedRequest[] = [];
let server: Server;
let origin: string;

/**
 * Responses are deliberately minimal and generic. The assertion is about the
 * REQUEST, which is recorded before a byte of the response is written, so a
 * body the SDK cannot parse costs coverage of any follow-up call in the same
 * handler but never a false pass. An empty `items` array is what keeps a list
 * tool from fanning out into per-item calls that would need richer fixtures.
 */
function respond(path: string): unknown {
  if (path.endsWith("/usage")) {
    return { storage_bytes: 0, retrieval_bytes: 0 };
  }
  return {
    items: [],
    entries: [],
    grants: [],
    next_cursor: null,
    id: "drv_0000000000000001",
    revision: "rev_0000000000000001",
    state: "active",
  };
}

beforeAll(async () => {
  server = createServer((request, response) => {
    const url = new URL(request.url ?? "/", "http://recorder.invalid");
    recorded.push({
      method: (request.method ?? "GET").toUpperCase(),
      path: url.pathname,
      query: url.searchParams,
    });
    request.resume();
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify(respond(url.pathname)));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  origin = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  await new Promise<void>((resolve, reject) =>
    server.close((error) => (error ? reject(error) : resolve())),
  );
});

/* ── the tools ────────────────────────────────────────────────────────── */

const DRIVE = "drv_0000000000000001";
const FOLDER = "fld_0000000000000001";
const ARTIFACT = "art_0000000000000001";
const VERSION = "ver_0000000000000001";
const UPLOAD = "upld_0000000000000001";
const REVISION = "rev_0000000000000001";

/**
 * One argument set per tool. Exhaustive BY ASSERTION below: a tool added
 * without an entry here fails the suite rather than silently escaping
 * coverage, which is the property that makes this file worth having.
 */
const TOOL_ARGUMENTS: Record<string, Record<string, unknown>> = {
  list_drives: {},
  create_drive: { name: "wire contract" },
  delete_drive: { drive_id: DRIVE, revision: REVISION },
  restore_drive: { drive_id: DRIVE, revision: REVISION },
  list_directory: { drive_id: DRIVE, parent_id: FOLDER },
  search_drive: { drive_id: DRIVE, query: "contract" },
  read_artifact: { drive_id: DRIVE, artifact_id: ARTIFACT },
  create_artifact: {
    drive_id: DRIVE,
    name: "note.md",
    parent_id: FOLDER,
    content: { encoding: "text", value: "hello" },
  },
  replace_artifact_content: {
    drive_id: DRIVE,
    artifact_id: ARTIFACT,
    revision: REVISION,
    content: { encoding: "text", value: "hello" },
  },
  update_artifact_metadata: {
    drive_id: DRIVE,
    artifact_id: ARTIFACT,
    revision: REVISION,
    name: "renamed.md",
  },
  create_folder: { drive_id: DRIVE, name: "folder", parent_id: FOLDER },
  move: {
    drive_id: DRIVE,
    type: "artifact",
    resource_id: ARTIFACT,
    revision: REVISION,
    parent_id: FOLDER,
  },
  delete: {
    drive_id: DRIVE,
    type: "artifact",
    resource_id: ARTIFACT,
    revision: REVISION,
  },
  restore: {
    drive_id: DRIVE,
    type: "artifact",
    resource_id: ARTIFACT,
    revision: REVISION,
  },
  list_artifact_versions: { drive_id: DRIVE, artifact_id: ARTIFACT },
  list_changes: { drive_id: DRIVE, start: "beginning" },
  list_access_grants: { drive_id: DRIVE },
  create_share_link: {
    drive_id: DRIVE,
    resource_type: "artifact",
    resource_id: ARTIFACT,
  },
  publish: {
    drive_id: DRIVE,
    resource_type: "artifact",
    resource_id: ARTIFACT,
  },
  unpublish: {
    drive_id: DRIVE,
    resource_type: "artifact",
    resource_id: ARTIFACT,
  },
  begin_file_upload: {
    drive_id: DRIVE,
    name: "upload.bin",
    parent_id: FOLDER,
    size_bytes: 1,
    content_type: "application/octet-stream",
  },
  get_file_upload: { drive_id: DRIVE, upload_id: UPLOAD },
  complete_file_upload: { drive_id: DRIVE, upload_id: UPLOAD },
  cancel_file_upload: {
    drive_id: DRIVE,
    upload_id: UPLOAD,
    revision: REVISION,
  },
};

void VERSION;

interface CollectedTool {
  name: string;
  handler: (args: Record<string, unknown>) => Promise<unknown>;
}

function collectTools(client: AgentDriveClientLike): CollectedTool[] {
  const tools: CollectedTool[] = [];
  defineAgentDriveMcpTools(
    client,
    ((name, _scopes, _config, handler) => {
      tools.push({
        name,
        handler: handler as unknown as CollectedTool["handler"],
      });
    }) as Parameters<typeof defineAgentDriveMcpTools>[1],
    {
      publicBaseUrl: "https://share.tokencanopy.test",
    },
  );
  return tools;
}

describe("the sidecar's wire shape matches AgentDrive's contract", () => {
  it("sends only query parameters the contract declares", async () => {
    const client = new AgentDriveClient({
      baseUrl: origin,
      tokenProvider: new StaticTokenProvider("wire-contract-token"),
    }) as unknown as AgentDriveClientLike;

    const tools = collectTools(client);
    expect(tools.length).toBeGreaterThan(0);

    // Every registered tool is driven, so a new one cannot opt out of this
    // check by simply not appearing in the fixture table.
    expect(tools.map((tool) => tool.name).sort()).toEqual(
      Object.keys(TOOL_ARGUMENTS).sort(),
    );

    const silent: string[] = [];
    for (const tool of tools) {
      const before = recorded.length;
      // A rejection is expected and irrelevant: the request is recorded
      // before the response is written, and the minimal bodies above do not
      // satisfy every model the SDK parses.
      await tool.handler(TOOL_ARGUMENTS[tool.name]).catch(() => undefined);
      if (recorded.length === before) silent.push(tool.name);
    }

    // A tool that issues no request is checked by nothing here, and it fails
    // SILENTLY -- `guarded()` turns a bad fixture into a returned result
    // rather than a throw, so a typo in TOOL_ARGUMENTS would quietly remove a
    // tool from coverage while the suite stayed green. Four of these were
    // wrong on the first draft of this file.
    expect(silent).toEqual([]);

    expect(recorded.length).toBeGreaterThan(0);

    const contract = loadContract();
    const undeclared: string[] = [];
    const unknownOperations: string[] = [];
    const badValues: string[] = [];

    for (const request of recorded) {
      const key = matchTemplate(contract.keys(), request.method, request.path);
      if (!key) {
        unknownOperations.push(`${request.method} ${request.path}`);
        continue;
      }
      const declared = contract.get(key)!;
      for (const [name, value] of request.query) {
        if (!declared.query.has(name)) {
          undeclared.push(`${key} sent undeclared query parameter '${name}'`);
          continue;
        }
        const allowed = declared.enums.get(name);
        if (allowed && !allowed.has(value)) {
          badValues.push(
            `${key} sent '${name}=${value}', not one of ${[...allowed].join("|")}`,
          );
        }
      }
    }

    expect(unknownOperations).toEqual([]);
    expect(undeclared).toEqual([]);
    expect(badValues).toEqual([]);
  });

  it("pins the filter this test was written for", () => {
    // A canary on the contract itself. If `/v0/drives` ever stops declaring
    // `state`, the assertion above would still pass for a sidecar that also
    // stopped sending it -- both sides can drift together and the check goes
    // quiet. This names the parameter whose rename caused the skew.
    const contract = loadContract();
    const drives = contract.get("GET /v0/drives");
    expect(drives?.query.has("state")).toBe(true);
    expect(drives?.query.has("lifecycle")).toBe(false);
  });
});
