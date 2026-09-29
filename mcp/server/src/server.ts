import { createHash } from "node:crypto";

import {
  McpServer,
  type ToolCallback,
} from "@modelcontextprotocol/sdk/server/mcp.js";
import type {
  AnySchema,
  ZodRawShapeCompat,
} from "@modelcontextprotocol/sdk/server/zod-compat.js";
import type { ToolAnnotations } from "@modelcontextprotocol/sdk/types.js";
import { z, ZodError } from "zod";
import { looksLikeCredentialFile } from "@tokencanopy/agentdrive-sdk";
import { AGENTDRIVE_MCP_SCOPES } from "@tokencanopy/mcp-auth";

import type {
  AgentDriveClientLike,
  DriveLike,
  GrantLike,
  UploadBeginRequest,
} from "./types.js";

export type AgentDriveMcpScope = (typeof AGENTDRIVE_MCP_SCOPES)[number];

/**
 * A tool's required scopes. A NON-EMPTY tuple on purpose: a future tool
 * cannot be added without declaring what it needs, because the empty array
 * does not type-check.
 */
export type RequiredToolScopes = readonly [
  AgentDriveMcpScope,
  ...AgentDriveMcpScope[],
];

type ToolInput = undefined | ZodRawShapeCompat | AnySchema;

interface ScopedToolConfig<Input extends ToolInput> {
  title?: string;
  description?: string;
  inputSchema?: Input;
  annotations?: ToolAnnotations;
}

/**
 * The ONLY way a tool enters the AgentDrive MCP surface.
 *
 * Scopes are declared HERE, beside the schema and the handler, not in a
 * second authorization table that can drift from the registration table.
 * `defineAgentDriveMcpTools` below is the single authoritative definition;
 * both the live server and the scope registry read it.
 */
export type DefineAgentDriveTool = <Input extends ToolInput>(
  name: string,
  requiredScopes: RequiredToolScopes,
  config: ScopedToolConfig<Input>,
  handler: ToolCallback<Input>,
) => void;

/** What a verified bearer authorizes. Read at CALL time, not captured. */
export interface AgentDriveMcpAuthorization {
  scopes: readonly string[];
}

/** Deployment facts the tools need that are not the client's business. */
export interface AgentDriveMcpToolOptions {
  /**
   * The public shell's origin — where a published resource's permalink
   * lives (`share.tokencanopy.com`; `/a/{artifact_id}/`, `/f/{folder_id}/`).
   * An exact https origin, already validated by the runtime configuration
   * (`MCP_PUBLIC_BASE_URL`). Undefined when the deployment has not declared
   * one: `publish` then omits `public_url` and says so in `warnings`, because
   * a guessed host is worse than none.
   */
  publicBaseUrl?: string;
}

function authorizes(
  granted: readonly string[],
  requiredScopes: RequiredToolScopes,
): boolean {
  return requiredScopes.every((scope) => granted.includes(scope));
}

/** The tool-level analogue of RFC 6750's `insufficient_scope`. */
function scopeDenied(requiredScopes: RequiredToolScopes) {
  const body = {
    error: {
      code: "insufficient_scope",
      message: `this tool requires the ${requiredScopes.join(" ")} scope(s), which this authorization does not grant`,
    },
  };
  return {
    isError: true,
    content: [{ type: "text" as const, text: JSON.stringify(body) }],
  };
}

const MAX_PAGE = 100;
const MAX_INLINE_BYTES = 1024 * 1024;
const MAX_NAME_LENGTH = 255;
const MAX_BASE64_CHARS = 4 * Math.ceil(MAX_INLINE_BYTES / 3);
const MAX_METADATA_BYTES = 16 * 1024;
const MAX_METADATA_DEPTH = 8;
const MAX_METADATA_KEYS = 100;
const MAX_METADATA_ITEMS = 100;
const MAX_METADATA_STRING_BYTES = 4096;

class InvalidInputError extends Error {
  readonly code = "invalid_input";
  readonly statusCode = 400;

  constructor(message: string) {
    super(message);
    this.name = "InvalidInputError";
  }
}

class ContentTooLargeError extends Error {
  readonly code = "content_too_large";
  readonly statusCode = 413;

  constructor() {
    super("artifact content exceeds the MCP inline read limit");
    this.name = "ContentTooLargeError";
  }
}

/**
 * A refusal the MCP layer decided on ITS OWN, before or instead of calling
 * AgentDrive, whose message and details are therefore safe to return as-is.
 *
 * `failed()` deliberately replaces every upstream message with a fixed one
 * per status code, because an SDK error can carry a response body. That is
 * right for upstream errors and wrong for this one: an agent told only
 * "AgentDrive conflict" cannot learn which ancestor grant makes revoking
 * here pointless, and the point of the refusal is to name it.
 */
class ToolRefusedError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ToolRefusedError";
  }
}

function failed(error: unknown) {
  if (error instanceof ToolRefusedError) {
    return {
      isError: true,
      content: [
        {
          type: "text" as const,
          text: JSON.stringify({
            error: {
              code: error.code,
              message: error.message,
              ...error.details,
            },
          }),
        },
      ],
    };
  }
  const record = asRecord(error);
  const statusCode =
    typeof record?.statusCode === "number" ? record.statusCode : undefined;
  const candidateCode =
    typeof record?.code === "string" &&
    /^[A-Za-z0-9_.:-]{1,100}$/.test(record.code)
      ? record.code
      : undefined;
  const code =
    candidateCode ??
    statusCodeCode(statusCode) ??
    (error instanceof ZodError ? "invalid_input" : "agentdrive_error");
  const message = safeMessage(statusCode, error instanceof ZodError);
  return {
    isError: true,
    content: [
      {
        type: "text" as const,
        text: JSON.stringify({ error: { code, message } }),
      },
    ],
  };
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : undefined;
}

function statusCodeCode(statusCode: number | undefined): string | undefined {
  return statusCode == null
    ? undefined
    : {
        400: "invalid_request",
        401: "unauthorized",
        403: "permission_denied",
        404: "not_found",
        409: "conflict",
        412: "precondition_failed",
        428: "precondition_required",
        429: "rate_limited",
        413: "content_too_large",
        503: "service_unavailable",
      }[statusCode];
}

function safeMessage(
  statusCode: number | undefined,
  invalidInput: boolean,
): string {
  if (invalidInput || statusCode === 400 || statusCode === 422)
    return "Invalid AgentDrive input";
  if (statusCode === 401) return "AgentDrive authentication failed";
  if (statusCode === 403) return "AgentDrive permission denied";
  if (statusCode === 404) return "AgentDrive resource not found";
  if (statusCode === 409) return "AgentDrive conflict";
  if (statusCode === 412 || statusCode === 428)
    return "AgentDrive revision precondition failed";
  if (statusCode === 429) return "AgentDrive rate limit exceeded";
  if (statusCode === 413)
    return "Artifact content exceeds the MCP inline read limit";
  if (statusCode != null && statusCode >= 500)
    return "AgentDrive service unavailable";
  return "AgentDrive operation failed";
}

function guarded<T extends Record<string, unknown>>(handler: () => Promise<T>) {
  return handler().then(result, failed);
}

/**
 * AgentDrive mints every id as `<prefix>_` plus exactly 16 lowercase hex
 * characters (`core/ids.py`), and rejects anything else with
 * `INVALID_ARGUMENT` before the resource is ever looked up.
 *
 * These schemas used to accept `[A-Za-z0-9_-]+`, which is strictly looser. The
 * consequence was not a security hole but a diagnostic one: `art_doesnotexist`
 * satisfied the declared pattern, reached the server, and came back
 * `INVALID_ARGUMENT` -- indistinguishable to a caller from a well-formed id
 * that simply does not exist (`ARTIFACT_NOT_FOUND`). An agent reading the
 * schema had no way to tell "you typed it wrong" from "it is gone". Matching
 * the real shape rejects a malformed id at the tool boundary with a precise
 * message, so anything reaching AgentDrive is well-formed and NOT_FOUND means
 * what it says.
 */
const ID_HEX = 16;
const idPattern = (prefix: string): RegExp =>
  new RegExp(`^${prefix}_[a-f0-9]{${ID_HEX}}$`, "u");

const driveId = z.string().regex(idPattern("drv"));
const folderId = z.string().regex(idPattern("fld"));
const artifactId = z.string().regex(idPattern("art"));
const versionId = z.string().regex(idPattern("ver"));
const uploadId = z.string().regex(idPattern("upld"));
const entryId = z.union([folderId, artifactId]);
const grantResourceId = z.union([driveId, folderId, artifactId]);
const revision = z.string().min(1).max(300);
/** The v0 collection filter, one spelling and one wildcard across every
 * collection since the 2026-09-11 rename (#681). `entries` gave up `any`
 * for `all` in that change, so the drive and entry listings take the same
 * three values and a tool must not offer a fourth. */
const collectionState = z.enum(["active", "deleted", "all"]);
const page = z.number().int().min(1).max(MAX_PAGE).optional();
const cursor = z.string().min(1).max(1000).optional();

function metadataValidationIssue(
  value: Record<string, unknown>,
): string | undefined {
  let keyCount = 0;
  const visit = (current: unknown, depth: number): string | undefined => {
    if (depth > MAX_METADATA_DEPTH)
      return `metadata nesting is limited to ${MAX_METADATA_DEPTH} levels`;
    if (typeof current === "string") {
      if (
        new TextEncoder().encode(current).byteLength > MAX_METADATA_STRING_BYTES
      )
        return `metadata string values are limited to ${MAX_METADATA_STRING_BYTES} bytes`;
      return undefined;
    }
    if (Array.isArray(current)) {
      if (current.length > MAX_METADATA_ITEMS)
        return `metadata arrays are limited to ${MAX_METADATA_ITEMS} items`;
      for (const item of current) {
        const issue = visit(item, depth + 1);
        if (issue) return issue;
      }
      return undefined;
    }
    if (typeof current !== "object" || current === null) return undefined;
    for (const [key, child] of Object.entries(current)) {
      keyCount += 1;
      if (keyCount > MAX_METADATA_KEYS)
        return `metadata is limited to ${MAX_METADATA_KEYS} keys`;
      if (new TextEncoder().encode(key).byteLength > MAX_METADATA_STRING_BYTES)
        return `metadata keys are limited to ${MAX_METADATA_STRING_BYTES} bytes`;
      const issue = visit(child, depth + 1);
      if (issue) return issue;
    }
    return undefined;
  };

  let serializedBytes: number;
  try {
    serializedBytes = new TextEncoder().encode(
      JSON.stringify(value),
    ).byteLength;
  } catch {
    return "metadata must be JSON serializable";
  }
  if (serializedBytes > MAX_METADATA_BYTES)
    return `metadata is limited to ${MAX_METADATA_BYTES} serialized bytes`;
  return visit(value, 0);
}

const metadataValue = z
  .record(z.string(), z.unknown())
  .superRefine((value, context) => {
    const issue = metadataValidationIssue(value);
    if (issue) context.addIssue({ code: "custom", message: issue });
  });
const metadata = metadataValue.optional();
const labelsValue = z.array(z.string().min(1).max(200)).max(100);
const artifactPath = z.string().min(1).max(2000);

function hasForbiddenNameCharacter(value: string): boolean {
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0;
    if (
      code <= 0x1f ||
      code === 0x7f ||
      (code >= 0x202a && code <= 0x202e) ||
      (code >= 0x2066 && code <= 0x2069) ||
      code === 0x2028 ||
      code === 0x2029
    )
      return true;
  }
  return false;
}

const artifactName = z
  .string()
  .min(1)
  .max(MAX_NAME_LENGTH * 2)
  .transform((value) => value.normalize("NFC"))
  .refine(
    (value) =>
      [...value].length <= MAX_NAME_LENGTH &&
      !value.includes("/") &&
      !value.includes("\\") &&
      !/^\.{1,}$/u.test(value) &&
      !/^\s|\s$/u.test(value) &&
      !hasForbiddenNameCharacter(value) &&
      !looksLikeCredentialFile(value),
    { message: "name must be one safe, non-credential path segment" },
  );
/** A drive's display name. Not a path segment — "/" is legal — but control
 * and bidi-override characters stay out, same as entry names. */
const driveName = z
  .string()
  .min(1)
  .max(MAX_NAME_LENGTH * 2)
  .transform((value) => value.normalize("NFC"))
  .refine(
    (value) =>
      [...value].length <= MAX_NAME_LENGTH &&
      !/^\s|\s$/u.test(value) &&
      !hasForbiddenNameCharacter(value),
    { message: "name must be a bounded, printable display name" },
  );
const entryNameFilter = z
  .string()
  .min(1)
  .max(MAX_NAME_LENGTH * 2)
  .transform((value) => value.normalize("NFC"))
  .refine((value) => [...value].length <= MAX_NAME_LENGTH, {
    message: `name must contain at most ${MAX_NAME_LENGTH} Unicode characters`,
  });
const inlineContent = z.discriminatedUnion("encoding", [
  z.strictObject({
    encoding: z.literal("text"),
    value: z.string().min(1).max(MAX_INLINE_BYTES),
  }),
  z.strictObject({
    encoding: z.literal("base64"),
    value: z.string().min(1).max(MAX_BASE64_CHARS),
  }),
]);

type InlineContent = z.infer<typeof inlineContent>;
const artifactMetadataInput = z
  .strictObject({
    drive_id: driveId,
    artifact_id: artifactId,
    revision,
    metadata,
    labels: labelsValue.optional(),
    idempotency_key: z.string().min(1).max(300).optional(),
  })
  .refine(
    (value) => value.metadata !== undefined || value.labels !== undefined,
    { message: "metadata or labels is required" },
  );

const readArtifactInput = z
  .strictObject({
    drive_id: driveId,
    artifact_id: artifactId.optional(),
    path: artifactPath.optional(),
    include_content: z.boolean().optional(),
    max_bytes: z.number().int().min(1).max(MAX_INLINE_BYTES).optional(),
  })
  .refine(
    (value) => (value.artifact_id !== undefined) !== (value.path !== undefined),
    {
      message: "provide exactly one of artifact_id or path",
    },
  );

const moveInput = z
  .strictObject({
    drive_id: driveId,
    type: z.enum(["folder", "artifact"]),
    resource_id: entryId,
    revision,
    parent_id: folderId.optional(),
    parent_path: artifactPath.optional(),
    name: artifactName.optional(),
    idempotency_key: z.string().min(1).max(300).optional(),
  })
  .refine(
    (value) =>
      value.parent_id !== undefined ||
      value.parent_path !== undefined ||
      value.name !== undefined,
    { message: "parent_id, parent_path, or name is required" },
  )
  .refine(
    (value) => value.parent_id === undefined || value.parent_path === undefined,
    { message: "provide parent_id or parent_path, not both" },
  )
  .refine(
    (value) =>
      value.type === "folder"
        ? value.resource_id.startsWith("fld_")
        : value.resource_id.startsWith("art_"),
    { message: "resource_id must match type" },
  );

const listDirectoryInput = z
  .strictObject({
    drive_id: driveId,
    parent_id: folderId.optional(),
    path: artifactPath.optional(),
    type: z.enum(["folder", "artifact"]).optional(),
    name: entryNameFilter.optional(),
    /** `/v0/drives/{id}/entries` spells this `state`, and every entry it
     * returns carries its own `state`, `deleted_at` and `revision` — which
     * is what makes `restore` usable: a soft-deleted entry is invisible by
     * default and 404s on read, so this listing is the only way to recover
     * the post-delete revision a restore needs for `If-Match`.
     *
     * The wildcard is `all`, NOT `any` — see `collectionState`. */
    state: collectionState.optional(),
    limit: page,
    cursor,
  })
  .refine(
    (value) => value.parent_id === undefined || value.path === undefined,
    { message: "provide parent_id or path, not both" },
  );

const createArtifactInput = z
  .strictObject({
    drive_id: driveId,
    name: artifactName,
    parent_id: folderId.optional(),
    parent_path: artifactPath.optional(),
    content: inlineContent,
    content_type: z.string().min(1).max(200).optional(),
    metadata,
    idempotency_key: z.string().min(1).max(300).optional(),
  })
  .refine(
    (value) => value.parent_id === undefined || value.parent_path === undefined,
    { message: "provide parent_id or parent_path, not both" },
  );

const replaceArtifactContentInput = z.strictObject({
  drive_id: driveId,
  artifact_id: artifactId,
  revision,
  content: inlineContent,
  content_type: z.string().min(1).max(200).optional(),
  idempotency_key: z.string().min(1).max(300).optional(),
});

const createFolderInput = z
  .strictObject({
    drive_id: driveId,
    name: artifactName,
    parent_id: folderId.optional(),
    parent_path: artifactPath.optional(),
    metadata,
    idempotency_key: z.string().min(1).max(300).optional(),
  })
  .refine(
    (value) => value.parent_id === undefined || value.parent_path === undefined,
    { message: "provide parent_id or parent_path, not both" },
  );

const deleteInput = z
  .strictObject({
    drive_id: driveId,
    type: z.enum(["folder", "artifact"]),
    resource_id: entryId,
    revision,
    recursive: z.boolean().optional(),
    idempotency_key: z.string().min(1).max(300).optional(),
  })
  .refine((value) => value.type === "folder" || value.recursive === undefined, {
    message: "recursive is only valid when type is folder",
  })
  .refine(
    (value) =>
      value.type === "folder"
        ? value.resource_id.startsWith("fld_")
        : value.resource_id.startsWith("art_"),
    { message: "resource_id must match type" },
  );

const restoreInput = z
  .strictObject({
    drive_id: driveId,
    type: z.enum(["folder", "artifact"]),
    resource_id: entryId,
    revision,
    idempotency_key: z.string().min(1).max(300).optional(),
  })
  .refine(
    (value) =>
      value.type === "folder"
        ? value.resource_id.startsWith("fld_")
        : value.resource_id.startsWith("art_"),
    { message: "resource_id must match type" },
  );

const listAccessGrantsInput = z
  .strictObject({
    drive_id: driveId,
    resource_id: grantResourceId.optional(),
    resource_type: z.enum(["drive", "folder", "artifact"]).optional(),
    limit: page,
    cursor,
  })
  .refine(
    (value) =>
      value.resource_id === undefined || value.resource_type !== undefined,
    { message: "resource_id requires resource_type" },
  )
  .refine(
    (value) => {
      if (value.resource_id === undefined || value.resource_type === undefined)
        return true;
      const prefix = {
        drive: "drv_",
        folder: "fld_",
        artifact: "art_",
      }[value.resource_type];
      return value.resource_id.startsWith(prefix);
    },
    { message: "resource_id must match resource_type" },
  );

const listChangesInput = z
  .strictObject({
    drive_id: driveId,
    limit: page,
    cursor,
    start: z.enum(["now", "beginning"]).optional(),
    type: z.string().min(1).max(100).optional(),
  })
  .refine(
    (value) => (value.cursor !== undefined) !== (value.start !== undefined),
    { message: "provide exactly one of cursor or start" },
  );

const createShareLinkInput = z
  .strictObject({
    drive_id: driveId,
    resource_id: z.union([artifactId, versionId, folderId]),
    resource_type: z.enum(["artifact", "artifact_version", "folder"]),
    expires_at: z.string().datetime().optional(),
    idempotency_key: z.string().min(1).max(300).optional(),
  })
  .refine(
    (value) => {
      if (value.resource_type === "artifact")
        return value.resource_id.startsWith("art_");
      if (value.resource_type === "artifact_version")
        return value.resource_id.startsWith("ver_");
      return value.resource_id.startsWith("fld_");
    },
    { message: "resource_id must match resource_type" },
  );

// --- Publish / unpublish ------------------------------------------------
//
// "Published" in AgentDrive is not a flag on the resource: it is a live access
// grant with `principal_type: "public"`, always `viewer`, on an artifact,
// folder, or drive, and a resource is published when ANY such grant covers
// it directly or through its ancestry (CONTEXT.md § Published). These two
// tools are the narrow verbs over that model — artifact and folder only. A
// drive-level public grant makes everything now and later public and stays
// console-only by decision (Josh, 2026-09-10;
// docs/superpowers/specs/2026-09-10-agentdrive-mcp-public-access-design.md).
const publicResourceType = z.enum(["artifact", "folder"]);
const publicResourceFields = {
  drive_id: driveId,
  resource_type: publicResourceType,
  resource_id: entryId,
  idempotency_key: z.string().min(1).max(300).optional(),
};
const resourceIdMatchesType = (value: {
  resource_type: "artifact" | "folder";
  resource_id: string;
}) =>
  value.resource_type === "folder"
    ? value.resource_id.startsWith("fld_")
    : value.resource_id.startsWith("art_");

const publishInput = z
  .strictObject({
    ...publicResourceFields,
    // RFC 3339 with any offset, as the server accepts. The server does NOT
    // reject a past expiry — it stores it and reports `state: "expired"` —
    // so a public grant that was born dead is refused here instead.
    expires_at: z.iso.datetime({ offset: true }).optional(),
  })
  .refine(resourceIdMatchesType, {
    message: "resource_id must match resource_type",
  })
  .refine(
    (value) =>
      value.expires_at === undefined ||
      Date.parse(value.expires_at) > Date.now(),
    { message: "expires_at must be in the future" },
  );

const unpublishInput = z
  .strictObject(publicResourceFields)
  .refine(resourceIdMatchesType, {
    message: "resource_id must match resource_type",
  });

/** Bounds on the two walks the tools perform, so a pathological drive
 * (thousands of public grants, a folder chain that loops) is refused rather
 * than paged forever. */
const MAX_PUBLIC_GRANT_PAGES = 10;
const MAX_FOLDER_DEPTH = 256;

type PublicResourceType = z.infer<typeof publicResourceType>;
type GrantResourceType = "drive" | PublicResourceType;
type AncestorGrant = {
  grant_id: string;
  resource_type: GrantResourceType;
  resource_id: string;
};

const grantKey = (resourceType: string, resourceId: string) =>
  `${resourceType}:${resourceId}`;

/** Every live public grant in the drive, keyed by the resource it covers.
 * One listing serves both the direct check and the ancestry check. */
async function livePublicGrants(
  client: AgentDriveClientLike,
  driveId: string,
): Promise<Map<string, GrantLike[]>> {
  const byResource = new Map<string, GrantLike[]>();
  let cursor: string | undefined;
  for (let pageIndex = 0; ; pageIndex += 1) {
    if (pageIndex >= MAX_PUBLIC_GRANT_PAGES) {
      throw new ToolRefusedError(
        "public_grants_unbounded",
        `the drive has more than ${MAX_PUBLIC_GRANT_PAGES * MAX_PAGE} live public grants; manage its public access from the console`,
      );
    }
    const pageResult = await client.grants.list(driveId, {
      state: "active",
      principalType: "public",
      limit: MAX_PAGE,
      cursor,
    });
    for (const grant of pageResult.items ?? []) {
      if (grant.principalType !== "public") continue;
      const key = grantKey(grant.resourceType, grant.resourceId);
      byResource.set(key, [...(byResource.get(key) ?? []), grant]);
    }
    if (!pageResult.nextCursor) return byResource;
    cursor = pageResult.nextCursor;
  }
}

/** The folder ids above a resource, nearest first; the drive root's parent
 * (null) ends the walk. `firstFolder` is the resource's own parent for an
 * artifact, or the folder's parent for a folder.
 *
 * `unreadable` is set instead of throwing when an ancestor answers 403 or
 * 404: `folders_read` requires viewer on THAT folder and grants inherit
 * downward only, so a caller whose manager grant sits on the resource or a
 * subfolder cannot read above it — while `grants_create` on the resource
 * would still succeed. The tool must not be stricter than the server. */
async function folderAncestors(
  client: AgentDriveClientLike,
  driveId: string,
  firstFolder: string | null | undefined,
): Promise<{ ancestors: string[]; unreadable: string | null }> {
  const ancestors: string[] = [];
  let current = firstFolder ?? null;
  while (current) {
    if (ancestors.length >= MAX_FOLDER_DEPTH || ancestors.includes(current)) {
      throw new ToolRefusedError(
        "folder_ancestry_unbounded",
        `the folder chain above the resource exceeds ${MAX_FOLDER_DEPTH} levels or loops; manage its public access from the console`,
      );
    }
    ancestors.push(current);
    let folder;
    try {
      folder = await client.folders.get(driveId, current);
    } catch (error) {
      const status = asRecord(error)?.statusCode;
      if (status === 403 || status === 404) {
        return { ancestors, unreadable: current };
      }
      throw error;
    }
    current = folder.parentId ?? null;
  }
  return { ancestors, unreadable: null };
}

/** A grant the server reports as live. The SDK's `GrantOut.state` is the
 * server's computed word (`active` / `expired` / `revoked`); a fake or an
 * older model without it is taken at face value. */
function grantIsLive(grant: GrantLike): boolean {
  return grant.state === undefined || grant.state === "active";
}

/** The nearest live public grant an ancestor contributes: the closest
 * folder first, then the drive itself. Null when nothing above is public. */
function inheritedPublicGrant(
  publicGrants: Map<string, GrantLike[]>,
  driveId: string,
  ancestors: readonly string[],
): AncestorGrant | null {
  for (const folderId of ancestors) {
    const grant = publicGrants.get(grantKey("folder", folderId))?.[0];
    if (grant)
      return {
        grant_id: grant.id,
        resource_type: "folder",
        resource_id: folderId,
      };
  }
  const driveGrant = publicGrants.get(grantKey("drive", driveId))?.[0];
  return driveGrant
    ? { grant_id: driveGrant.id, resource_type: "drive", resource_id: driveId }
    : null;
}

/** Everything both verbs need to know before they touch a grant. Reads the
 * resource first — a missing one is NOT_FOUND before any mutation — then its
 * ancestry, then the drive's live public grants in one listing. */
async function publicState(
  client: AgentDriveClientLike,
  driveId: string,
  resourceType: PublicResourceType,
  resourceId: string,
) {
  const warnings: string[] = [];
  const parentId =
    resourceType === "artifact"
      ? (await client.artifacts.get(driveId, resourceId)).parentId
      : (await client.folders.get(driveId, resourceId)).parentId;
  const { ancestors, unreadable } = await folderAncestors(
    client,
    driveId,
    typeof parentId === "string" ? parentId : null,
  );
  if (unreadable) {
    warnings.push(
      `ancestry not fully checked: folder ${unreadable} is not readable by this authorization, so inherited_from covers only the folders below it and the drive`,
    );
  }
  const publicGrants = await livePublicGrants(client, driveId);
  const direct = (
    publicGrants.get(grantKey(resourceType, resourceId)) ?? []
  ).filter(grantIsLive);
  const inheritedFrom = inheritedPublicGrant(publicGrants, driveId, ancestors);
  return { warnings, direct, inheritedFrom };
}

/** The server's computed word after the change, for an artifact.
 * `FolderOut` carries no such field, so a folder reports null. The mutation
 * has landed by now, so a failed re-read must not turn a success into an
 * error the caller would act on as a failure. */
async function effectiveVisibilityAfter(
  client: AgentDriveClientLike,
  driveId: string,
  resourceType: PublicResourceType,
  resourceId: string,
  warnings: string[],
): Promise<unknown> {
  if (resourceType !== "artifact") return null;
  try {
    const after = await client.artifacts.get(driveId, resourceId);
    return after.effectiveVisibility ?? null;
  } catch {
    warnings.push(
      "effective_visibility unavailable: the change was applied but re-reading the artifact failed; read it again to confirm",
    );
    return null;
  }
}

// --- Resumable upload sessions ------------------------------------------
//
// `create_artifact` carries bytes inline, so every byte crosses the model's
// context to reach the drive: capped at ~1 MiB, inflated 4/3 by base64, and
// written into transcripts. These tools hand back a signed URL instead, and
// the agent transfers the file itself. The model never sees the bytes.
//
// CRC32C is required by the API before the target is disclosed, so the agent
// commits to the exact object it is about to send. It is not in Python's
// stdlib; `begin_file_upload`'s description carries a dependency-free way to
// compute it, because a required parameter an agent cannot produce is the
// same as an unusable tool.
const uploadChecksum = z.strictObject({
  algorithm: z.literal("crc32c"),
  value: z
    .string()
    .regex(
      /^[A-Za-z0-9+/]{6}==$/u,
      "padded standard-base64 CRC32C of exactly four bytes",
    ),
});

const beginFileUploadInput = z
  .strictObject({
    drive_id: driveId,
    parent_id: folderId.optional(),
    name: z.string().min(1).max(510).optional(),
    artifact_id: artifactId.optional(),
    size_bytes: z.number().int().min(0),
    media_type: z
      .string()
      .min(1)
      .max(200)
      .regex(
        /^[A-Za-z0-9!#$&^_.+-]+\/[A-Za-z0-9!#$&^_.+-]+$/u,
        "bare IANA type/subtype, no parameters",
      ),
    checksum: uploadChecksum,
    idempotency_key: z.string().min(1).max(300).optional(),
  })
  .refine(
    (value) =>
      // Exactly one destination: a NEW artifact in a folder, or a NEW version
      // of an existing one. The API models these as a union, and accepting
      // both here would silently pick one.
      (value.artifact_id !== undefined) !==
      (value.parent_id !== undefined && value.name !== undefined),
    {
      message:
        "provide either artifact_id (new version) or both parent_id and name (new artifact)",
    },
  );

const uploadSessionInput = z.strictObject({
  drive_id: driveId,
  upload_id: uploadId,
});

const completeFileUploadInput = z.strictObject({
  drive_id: driveId,
  upload_id: uploadId,
  idempotency_key: z.string().min(1).max(300).optional(),
});

const cancelFileUploadInput = z.strictObject({
  drive_id: driveId,
  upload_id: uploadId,
  revision: z.string().min(1).max(300),
  idempotency_key: z.string().min(1).max(300).optional(),
});

/** Exactly one upload destination, narrowed for the type checker.
 * `beginFileUploadInput`'s refinement already rejects the ambiguous and empty
 * cases, so the throw is unreachable — it exists so this returns a complete
 * target rather than one with `undefined` fields the SDK would drop. */
function uploadTarget(args: {
  artifact_id?: string;
  parent_id?: string;
  name?: string;
}): UploadBeginRequest["target"] {
  if (args.artifact_id !== undefined) {
    return { kind: "version", artifactId: args.artifact_id };
  }
  if (args.parent_id !== undefined && args.name !== undefined) {
    return {
      kind: "artifact",
      parentFolderId: args.parent_id,
      name: args.name,
    };
  }
  throw new InvalidInputError(
    "provide either artifact_id or both parent_id and name",
  );
}

function semanticIdempotencyKey(toolName: string, args: unknown): string {
  const digest = createHash("sha256")
    .update(stableJson({ tool: toolName, args }))
    .digest("hex");
  return `mcp-${digest}`;
}

function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object")
    return JSON.stringify(value) ?? "null";
  if (Array.isArray(value))
    return `[${value.map((item) => stableJson(item)).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`)
    .join(",")}}`;
}

function result(value: Record<string, unknown>) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(value) }],
    structuredContent: value,
  };
}

/**
 * Build the AgentDrive MCP server for one verified request.
 *
 * `tools/list` exposes only what `authorization.scopes` covers, and every
 * handler re-checks its own scopes before touching the SDK — so a hand-built
 * JSON-RPC `tools/call` naming an unlisted tool is refused rather than
 * executed. The re-check reads `authorization.scopes` at CALL time rather
 * than the set captured during registration, so it is a genuinely independent
 * second gate and not a restatement of the first.
 */
export function createAgentDriveMcpServer(
  client: AgentDriveClientLike,
  // REQUIRED, with no default. A default would grant the whole surface to any
  // caller that forgot the argument, which is the wrong direction for the one
  // parameter that decides what a token can do.
  authorization: AgentDriveMcpAuthorization,
  options: AgentDriveMcpToolOptions = {},
): McpServer {
  const server = new McpServer({
    name: "tokencanopy-agentdrive",
    version: "0.1.0",
  });
  defineAgentDriveMcpTools(
    client,
    (name, requiredScopes, config, handler) => {
      if (!authorizes(authorization.scopes, requiredScopes)) return;
      const guardedHandler = ((...callArgs: never[]) =>
        authorizes(authorization.scopes, requiredScopes)
          ? (handler as (...args: never[]) => unknown)(...callArgs)
          : scopeDenied(requiredScopes)) as typeof handler;
      server.registerTool(name, config, guardedHandler);
    },
    options,
  );
  return server;
}

/**
 * The tool name → required scopes map, derived from the SAME definitions the
 * server registers. Exported for tests and for the release smoke's frozen
 * surface check; it is a projection of the definitions, never a second
 * source.
 */
export function agentDriveMcpToolScopes(): ReadonlyMap<
  string,
  RequiredToolScopes
> {
  const scopes = new Map<string, RequiredToolScopes>();
  // Handlers close over the client but are never invoked here, so a client
  // that throws on any access proves the definition pass touches nothing.
  const unusable = new Proxy(
    {},
    {
      get() {
        throw new Error(
          "agentDriveMcpToolScopes must not touch the AgentDrive client",
        );
      },
    },
  ) as AgentDriveClientLike;
  defineAgentDriveMcpTools(unusable, (name, requiredScopes) => {
    scopes.set(name, requiredScopes);
  });
  return scopes;
}

/** The permalink of a published resource on the public shell, in the shapes
 * `apps/drive/src/agentdrive/public/routes.py` serves and
 * `apps/app/lib/publicUrls.ts` links. */
function publicPermalink(
  base: string,
  resourceType: PublicResourceType,
  resourceId: string,
): string {
  const prefix = resourceType === "artifact" ? "a" : "f";
  return `${base}/${prefix}/${encodeURIComponent(resourceId)}/`;
}

/**
 * One drive's usage summary, or null when there cannot be one.
 *
 * `/v0/drives/{id}/usage` is an ACTIVE-drive read: core `drive_usage` fetches
 * with `include_deleted=False`. Calling it for every row of a listing was
 * therefore fatal the moment `state` could admit a soft-deleted drive — one
 * 404 rejected the `Promise.all` and failed the whole page, active drives
 * included. Skipping it for a non-active drive is what lets `state: "deleted"`
 * and `state: "all"` work at all; the drive row still carries
 * `storage_bytes` and `retrieval_bytes`, so only the limit-aware detail
 * (`meters`, `effective_limits`) is absent.
 *
 * The 404 is ALSO caught, because `state` is not the only way to reach one:
 * another actor can delete a drive between the listing and this call, and a
 * page that fails because someone else deleted something is worse than a
 * page with one null in it.
 */
async function driveUsageOrNull(
  client: AgentDriveClientLike,
  drive: DriveLike,
): Promise<unknown> {
  if (drive.state !== "active") {
    return null;
  }
  try {
    return await client.drives.usage(drive.id);
  } catch (error) {
    const statusCode = asRecord(error)?.statusCode;
    if (statusCode === 404) {
      return null;
    }
    throw error;
  }
}

export function defineAgentDriveMcpTools(
  client: AgentDriveClientLike,
  tool: DefineAgentDriveTool,
  options: AgentDriveMcpToolOptions = {},
): void {
  const mutationKey = (
    toolName: string,
    provided: string | undefined,
    args: unknown,
  ) => provided ?? semanticIdempotencyKey(toolName, args);

  tool(
    "list_drives",
    ["drives:read", "usage:read"],
    {
      title: "List drives",
      description:
        "List the AgentDrive drives and bounded usage summaries. A " +
        "soft-deleted drive reports usage null — usage is an active-drive " +
        "read — but still carries its byte counters. state defaults to " +
        "active; pass deleted or all to see soft-deleted drives, whose " +
        "revision is what restore_drive needs. Repeat the same state " +
        "alongside cursor when paging: the cursor is sealed against the " +
        "filter it was issued under.",
      inputSchema: z.strictObject({
        /** Same filter `list_directory` takes, on the collection above it.
         * A soft-deleted drive is reachable no other way — reads 404 — so
         * without this the revision `restore_drive` needs lives only in the
         * response to the `delete_drive` that produced it. */
        state: collectionState.optional(),
        limit: page,
        cursor,
      }),
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    (args) =>
      guarded(async () => {
        const drives = await client.drives.list({
          state: args.state ?? "active",
          limit: args.limit ?? MAX_PAGE,
          cursor: args.cursor,
        });
        const items = await Promise.all(
          (drives.items ?? []).map(async (drive: DriveLike) => ({
            drive,
            usage: await driveUsageOrNull(client, drive),
          })),
        );
        return { drives: items, next_cursor: drives.nextCursor ?? null };
      }),
  );

  tool(
    "create_drive",
    ["drives:write"],
    {
      title: "Create drive",
      description:
        "Create a new AgentDrive drive owned by this workspace. The " +
        "creating agent and its sponsor both receive manager grants on the " +
        "new drive, so it is immediately usable.",
      inputSchema: z.strictObject({
        name: driveName,
        metadata,
        idempotency_key: z.string().min(1).max(300).optional(),
      }),
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => ({
        drive: await client.drives.create(args.name, {
          metadata: args.metadata,
          idempotencyKey: mutationKey(
            "create_drive",
            args.idempotency_key,
            args,
          ),
        }),
      })),
  );

  tool(
    "delete_drive",
    ["drives:write"],
    {
      title: "Delete drive",
      description:
        "Delete one drive and everything in it, under the drive's current " +
        "revision — `list_drives` carries it. AgentDrive soft-deletes the " +
        "drive: it leaves the default listing and its contents stop " +
        "resolving. `restore_drive` undoes it, using the revision this " +
        "call returns or the one `list_drives` shows for state deleted. " +
        "Requires a manager grant on the drive.",
      inputSchema: z.strictObject({
        drive_id: driveId,
        revision,
        idempotency_key: z.string().min(1).max(300).optional(),
      }),
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => ({
        drive: await client.drives.delete(
          args.drive_id,
          args.revision,
          mutationKey("delete_drive", args.idempotency_key, args),
        ),
      })),
  );

  tool(
    "restore_drive",
    ["drives:write"],
    {
      title: "Restore drive",
      description:
        "Bring back a soft-deleted drive, under the revision it carries " +
        "AFTER the delete — `delete_drive` returns it. Restoring a drive " +
        "that is already active is a conflict, not a no-op. Requires a " +
        "manager grant on the drive.",
      inputSchema: z.strictObject({
        drive_id: driveId,
        revision,
        idempotency_key: z.string().min(1).max(300).optional(),
      }),
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => ({
        drive: await client.drives.restore(
          args.drive_id,
          args.revision,
          mutationKey("restore_drive", args.idempotency_key, args),
        ),
      })),
  );

  tool(
    "list_directory",
    ["content:read"],
    {
      title: "List folder contents",
      description:
        "List the immediate folders and artifacts under one folder or path. " +
        "Provide parent_id or path, or omit both for the drive root. " +
        "state defaults to active; pass deleted or all to see soft-deleted " +
        "entries, whose revision is what restore needs. Repeat the same " +
        "state alongside cursor when paging: the cursor is sealed against " +
        "the filter it was issued under.",
      inputSchema: listDirectoryInput,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    (args) =>
      guarded(async () => {
        const parentId =
          args.parent_id ??
          (args.path
            ? (await client.entries.lookup(args.drive_id, args.path, "folder"))
                .id
            : undefined);
        const entries = await client.entries.list(args.drive_id, {
          parentId,
          type: args.type,
          name: args.name,
          state: args.state,
          limit: args.limit ?? MAX_PAGE,
          cursor: args.cursor,
        });
        return {
          drive_id: args.drive_id,
          parent_id: parentId ?? null,
          entries: entries.entries ?? [],
          next_cursor: entries.nextCursor ?? null,
        };
      }),
  );

  tool(
    "search_drive",
    ["content:read"],
    {
      title: "Search a drive",
      description:
        "Search one drive using AgentDrive's drive-scoped search index.",
      inputSchema: z.strictObject({
        drive_id: driveId,
        query: z.string().min(1).max(1000),
        parent_id: folderId.optional(),
        limit: page,
        cursor,
      }),
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    (args) =>
      guarded(async () => {
        const found = await client.search.find(args.drive_id, args.query, {
          parentId: args.parent_id,
          limit: args.limit ?? MAX_PAGE,
          cursor: args.cursor,
        });
        return {
          drive_id: args.drive_id,
          query: args.query,
          items: found.items ?? [],
          next_cursor: found.nextCursor ?? null,
        };
      }),
  );

  tool(
    "read_artifact",
    ["content:read"],
    {
      title: "Read a file",
      description:
        "Read artifact metadata and, when requested, bounded artifact bytes. Provide exactly one of artifact_id or path.",
      inputSchema: readArtifactInput,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    (args) =>
      guarded(async () => {
        if (!!args.artifact_id === !!args.path)
          throw new InvalidInputError(
            "provide exactly one of artifact_id or path",
          );
        const artifactId =
          args.artifact_id ??
          (await client.entries.lookup(args.drive_id, args.path!, "artifact"))
            .id;
        const artifact = await client.artifacts.get(args.drive_id, artifactId);
        const value: Record<string, unknown> = {
          drive_id: args.drive_id,
          artifact,
        };
        if (args.include_content) {
          const maxBytes = args.max_bytes ?? MAX_INLINE_BYTES;
          if (client.artifacts.contentResponse) {
            const response = await client.artifacts.contentResponse(
              args.drive_id,
              artifactId,
            );
            const body = await readResponseWithinLimit(response, maxBytes);
            value.content_type =
              body.contentType ||
              artifact.contentType ||
              "application/octet-stream";
            value.content_base64 = encodeBase64(body.bytes);
          } else {
            const blob = await client.artifacts.content(
              args.drive_id,
              artifactId,
            );
            if (blob.size > maxBytes) throw new ContentTooLargeError();
            const bytes = new Uint8Array(await blob.arrayBuffer());
            value.content_type =
              blob.type || artifact.contentType || "application/octet-stream";
            value.content_base64 = encodeBase64(bytes);
          }
        }
        return value;
      }),
  );

  tool(
    "create_artifact",
    ["content:write"],
    {
      title: "Create a file",
      description:
        "Create a new artifact from inline content. It never overwrites an " +
        "existing artifact; inline text or base64 content is capped at 1 MiB. " +
        "Use this for small text you are generating anyway (notes, JSON, a " +
        "short script). For an actual FILE — anything binary, anything you " +
        "read off disk, anything near or above 1 MiB — use begin_file_upload " +
        "instead: inline content travels through the model, so it costs " +
        "context, inflates 4/3 as base64, and lands in the transcript.",
      inputSchema: createArtifactInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => ({
        artifact: await client.artifacts.create(
          args.drive_id,
          args.name,
          inlineValue(args.content),
          {
            parentId: args.parent_id,
            parentPath: args.parent_path,
            contentType:
              args.content_type ??
              (args.content.encoding === "text"
                ? "text/plain"
                : "application/octet-stream"),
            metadata: args.metadata,
            idempotencyKey: mutationKey(
              "create_artifact",
              args.idempotency_key,
              args,
            ),
          },
        ),
      })),
  );

  tool(
    "replace_artifact_content",
    ["content:write"],
    {
      title: "Replace file contents",
      description:
        "Replace the content of an existing artifact by appending a new version. It never creates an artifact and requires the current revision.",
      inputSchema: replaceArtifactContentInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => ({
        version: await client.versions.append(
          args.drive_id,
          args.artifact_id,
          args.revision,
          inlineValue(args.content),
          {
            contentType:
              args.content_type ??
              (args.content.encoding === "text"
                ? "text/plain"
                : "application/octet-stream"),
            idempotencyKey: mutationKey(
              "replace_artifact_content",
              args.idempotency_key,
              args,
            ),
          },
        ),
      })),
  );

  tool(
    "update_artifact_metadata",
    ["content:write"],
    {
      title: "Update file metadata",
      description:
        "Update metadata or labels on an existing artifact. Provide metadata or labels. It never changes content, name, or placement and requires the current revision.",
      inputSchema: artifactMetadataInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => ({
        artifact: await client.artifacts.update(
          args.drive_id,
          args.artifact_id,
          args.revision,
          {
            metadata: args.metadata,
            labels: args.labels,
            idempotencyKey: mutationKey(
              "update_artifact_metadata",
              args.idempotency_key,
              args,
            ),
          },
        ),
      })),
  );

  tool(
    "create_folder",
    ["content:write"],
    {
      title: "Create folder",
      description: "Create one folder under a parent folder or path.",
      inputSchema: createFolderInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => ({
        folder: await client.folders.create(args.drive_id, args.name, {
          parentId: args.parent_id,
          parentPath: args.parent_path,
          metadata: args.metadata,
          idempotencyKey: mutationKey(
            "create_folder",
            args.idempotency_key,
            args,
          ),
        }),
      })),
  );

  tool(
    "move",
    ["content:write"],
    {
      title: "Move or rename",
      description:
        "Move or rename one folder or artifact with a required current revision. Provide at least one of parent_id, parent_path, or name.",
      inputSchema: moveInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => {
        const value =
          args.type === "folder"
            ? await client.folders.update(
                args.drive_id,
                args.resource_id,
                args.revision,
                {
                  parentId: args.parent_id,
                  parentPath: args.parent_path,
                  name: args.name,
                  idempotencyKey: mutationKey(
                    "move",
                    args.idempotency_key,
                    args,
                  ),
                },
              )
            : await client.artifacts.update(
                args.drive_id,
                args.resource_id,
                args.revision,
                {
                  parentId: args.parent_id,
                  parentPath: args.parent_path,
                  name: args.name,
                  idempotencyKey: mutationKey(
                    "move",
                    args.idempotency_key,
                    args,
                  ),
                },
              );
        return { type: args.type, resource: value };
      }),
  );

  tool(
    "delete",
    ["content:write"],
    {
      title: "Delete file or folder",
      description:
        "Delete one folder or artifact with a required current revision. recursive applies only to folders.",
      inputSchema: deleteInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => {
        const value =
          args.type === "folder"
            ? await client.folders.delete(
                args.drive_id,
                args.resource_id,
                args.revision,
                {
                  recursive: args.recursive ?? false,
                  idempotencyKey: mutationKey(
                    "delete",
                    args.idempotency_key,
                    args,
                  ),
                },
              )
            : await client.artifacts.delete(
                args.drive_id,
                args.resource_id,
                args.revision,
                mutationKey("delete", args.idempotency_key, args),
              );
        return { type: args.type, resource: value };
      }),
  );

  tool(
    "restore",
    ["content:write"],
    {
      title: "Restore file or folder",
      description:
        "Bring back one soft-deleted folder or artifact, under the revision " +
        "it carries AFTER the delete. `list_directory` with state deleted " +
        "is where to find it. A folder comes back with the subtree that was " +
        "deleted with it, atomically — there is no recursive option. " +
        "Restoring an artifact fails while its parent folder is still " +
        "deleted (restore the folder first), and fails if a live sibling " +
        "has taken its name since.",
      inputSchema: restoreInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => {
        const key = mutationKey("restore", args.idempotency_key, args);
        const value =
          args.type === "folder"
            ? await client.folders.restore(
                args.drive_id,
                args.resource_id,
                args.revision,
                key,
              )
            : await client.artifacts.restore(
                args.drive_id,
                args.resource_id,
                args.revision,
                key,
              );
        return { type: args.type, resource: value };
      }),
  );

  tool(
    "list_artifact_versions",
    ["content:read"],
    {
      title: "List file versions",
      description:
        "List immutable versions for an artifact, bounded by a cursor and page size.",
      inputSchema: z.strictObject({
        drive_id: driveId,
        artifact_id: artifactId,
        limit: page,
        cursor,
      }),
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    (args) =>
      guarded(async () => {
        const versions = await client.versions.list(
          args.drive_id,
          args.artifact_id,
          { limit: args.limit ?? MAX_PAGE, cursor: args.cursor },
        );
        return {
          drive_id: args.drive_id,
          artifact_id: args.artifact_id,
          items: versions.items ?? [],
          next_cursor: versions.nextCursor ?? null,
        };
      }),
  );

  tool(
    "list_changes",
    ["changes:read"],
    {
      title: "List recent changes",
      description:
        "Read a bounded page from the drive change feed. Provide exactly one of cursor or start (now/beginning); persist next_cursor to resume.",
      inputSchema: listChangesInput,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    (args) =>
      guarded(async () => {
        const changes = await client.changes.list(args.drive_id, {
          limit: args.limit ?? MAX_PAGE,
          cursor: args.cursor,
          start: args.start,
          type: args.type,
        });
        return {
          drive_id: args.drive_id,
          items: changes.items ?? [],
          next_cursor: changes.nextCursor ?? null,
          has_more: changes.hasMore ?? Boolean(changes.nextCursor),
        };
      }),
  );

  tool(
    "list_access_grants",
    ["sharing:read"],
    {
      title: "List access grants",
      description: "List active grants for a drive or one resource.",
      inputSchema: listAccessGrantsInput,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    (args) =>
      guarded(async () => {
        const grants = await client.grants.list(args.drive_id, {
          state: "active",
          resourceId: args.resource_id,
          resourceType: args.resource_type,
          limit: args.limit ?? MAX_PAGE,
          cursor: args.cursor,
        });
        return {
          drive_id: args.drive_id,
          items: grants.items ?? [],
          next_cursor: grants.nextCursor ?? null,
        };
      }),
  );

  tool(
    "create_share_link",
    ["sharing:write"],
    {
      title: "Create share link",
      description:
        "Create a possession-based share link. The plaintext secret is returned only in this response.",
      inputSchema: createShareLinkInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => ({
        share: await client.shares.create(
          args.drive_id,
          {
            resourceId: args.resource_id,
            resourceType: args.resource_type,
            expiresAt: args.expires_at ? new Date(args.expires_at) : undefined,
          },
          mutationKey("create_share_link", args.idempotency_key, args),
        ),
      })),
  );

  tool(
    "publish",
    // Lists grants to learn the current state, reads the artifact or folder
    // for its parent chain and `effective_visibility` (both `/v0` reads
    // require content:read), then creates the public grant.
    ["sharing:read", "sharing:write", "content:read"],
    {
      title: "Publish",
      description: [
        "Make one artifact or folder readable by ANYONE WITH THE LINK, no",
        "account needed, by creating a viewer-only public access grant. This",
        "is NOT a versioned release: publishing an artifact exposes its head",
        "AND every version permalink, and later versions are published as",
        "they are written. It is NOT a share link either — a share link is a",
        "secret that expires; a published resource has a permanent public",
        "address. A public grant can never edit.",
        "",
        "If a live direct public grant already exists this is a no-op that",
        "returns it UNCHANGED, its expiry included (a warning says so).",
        "expires_at (RFC 3339, in the future) bounds a NEW grant; omitting it",
        "keeps the resource published until unpublish. Drives cannot be",
        "published with this tool: a public drive exposes everything in it",
        "now and later, so that stays in the console.",
        "",
        "The result reports `public_url` — the permanent public address, the",
        "thing to hand to a person — `published` (a live DIRECT public grant",
        "exists), `inherited_from` (the nearest ancestor public grant, or",
        "null), for an artifact the server's `effective_visibility` after the",
        "change (folders have none: null), and `warnings` for anything the",
        "call could not do or check, including a deployment that has no",
        "public origin configured, in which case public_url is absent.",
      ].join(" "),
      inputSchema: publishInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    (args) =>
      guarded(async () => {
        const { drive_id: driveId, resource_type: resourceType } = args;
        const resourceId = args.resource_id;
        const { warnings, direct, inheritedFrom } = await publicState(
          client,
          driveId,
          resourceType,
          resourceId,
        );
        let grant: GrantLike | null = direct[0] ?? null;
        if (grant) {
          if (args.expires_at !== undefined) {
            warnings.push(
              `expires_at ignored: a live direct public grant ${grant.id} already exists and is returned unchanged`,
            );
          }
        } else {
          // Deliberately NOT the semantic key the other write tools derive.
          // The listing above already makes a same-arguments retry a safe
          // no-op, and a derived key here is a hazard: AgentDrive replays a
          // stored 201 for 24 h, so publish → unpublish → publish would hand
          // back the REVOKED grant and report it published. A caller-supplied
          // key is still honoured; otherwise the SDK mints a fresh one.
          grant = await client.grants.create(
            driveId,
            {
              principalType: "public",
              resourceType,
              resourceId,
              role: "viewer",
              expiresAt: args.expires_at ? new Date(args.expires_at) : null,
            },
            args.idempotency_key,
          );
        }
        const effectiveVisibility = await effectiveVisibilityAfter(
          client,
          driveId,
          resourceType,
          resourceId,
          warnings,
        );
        // Omit rather than guess: a permalink on the wrong host is a link
        // that does not work, handed to a person as if it did.
        if (options.publicBaseUrl === undefined) {
          warnings.push(
            "public_url unavailable: this deployment has no public origin configured (MCP_PUBLIC_BASE_URL), so the permalink is not returned",
          );
        }
        return {
          drive_id: driveId,
          resource_type: resourceType,
          resource_id: resourceId,
          published: grantIsLive(grant),
          ...(options.publicBaseUrl === undefined
            ? {}
            : {
                public_url: publicPermalink(
                  options.publicBaseUrl,
                  resourceType,
                  resourceId,
                ),
              }),
          grant,
          inherited_from: inheritedFrom,
          effective_visibility: effectiveVisibility,
          warnings,
        };
      }),
  );

  tool(
    "unpublish",
    ["sharing:read", "sharing:write", "content:read"],
    {
      title: "Unpublish",
      description: [
        "Stop one artifact or folder being readable by anyone with the link,",
        "by revoking every live direct public access grant on it (retry on",
        "precondition_failed). If there is none but the resource is published",
        "through the drive or an ancestor folder, the call is refused with",
        "public_inherited naming that grant, because revoking here would",
        "change nothing. Share links are separate and are not touched.",
        "",
        "The result reports `published: false`, `inherited_from` (an ancestor",
        "public grant that STILL publishes it, or null), for an artifact the",
        "server's `effective_visibility` after the change — check it, because",
        "an artifact stays public through an ancestor grant OR a live share",
        "link after its own grant is revoked (folders have none: null) — and",
        "`warnings` for anything the call could not do or check.",
      ].join(" "),
      inputSchema: unpublishInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () => {
        const { drive_id: driveId, resource_type: resourceType } = args;
        const resourceId = args.resource_id;
        const { warnings, direct, inheritedFrom } = await publicState(
          client,
          driveId,
          resourceType,
          resourceId,
        );
        if (direct.length === 0 && inheritedFrom) {
          throw new ToolRefusedError(
            "public_inherited",
            `the ${resourceType} has no public grant of its own; it is published through grant ${inheritedFrom.grant_id} on the ${inheritedFrom.resource_type} ${inheritedFrom.resource_id}, so revoking here would change nothing`,
            { inherited_from: inheritedFrom },
          );
        }
        for (const candidate of direct) {
          // Revoke is If-Match on the grant's revision, so the derived key
          // is per grant and per revision: a replay after success is a
          // stale precondition, never a second revoke of something else.
          await client.grants.revoke(
            driveId,
            candidate.id,
            candidate.revision,
            semanticIdempotencyKey("unpublish.revoke", {
              drive_id: driveId,
              grant_id: candidate.id,
              revision: candidate.revision,
              idempotency_key: args.idempotency_key ?? null,
            }),
          );
        }
        const effectiveVisibility = await effectiveVisibilityAfter(
          client,
          driveId,
          resourceType,
          resourceId,
          warnings,
        );
        // `published: false` beside `effective_visibility: "public"` is a
        // correct answer that reads as a contradiction: the artifact's OWN
        // public grant is gone, and the server still counts it public
        // because something else reaches it. Say which, in the channel the
        // result already has for "what this call could not do", so a caller
        // does not have to know the server's visibility rule to act on it.
        // Two things can keep it public here: an ancestor grant we found and
        // left alone (the refusal above fires only when there was nothing
        // of the artifact's own to revoke), or a live share link, which the
        // server counts as public because possession of the link is the
        // credential and no principal bounds it.
        if (resourceType === "artifact" && effectiveVisibility === "public") {
          warnings.push(
            inheritedFrom
              ? `still published: grant ${inheritedFrom.grant_id} on the ${inheritedFrom.resource_type} ${inheritedFrom.resource_id} covers it; unpublish that ${inheritedFrom.resource_type} to close it`
              : "still publicly reachable after its own public grant was revoked: a live share link serves it (possession of the link is the credential; see create_share_link). Revoke the share link to make the artifact private.",
          );
        }
        return {
          drive_id: driveId,
          resource_type: resourceType,
          resource_id: resourceId,
          published: false,
          grant: null,
          inherited_from: inheritedFrom,
          effective_visibility: effectiveVisibility,
          warnings,
        };
      }),
  );

  tool(
    "begin_file_upload",
    ["content:write"],
    {
      title: "Begin file upload",
      description: [
        "Start a direct upload and get a one-time target to send the file to.",
        "PREFER THIS over create_artifact for any real file. create_artifact",
        "carries content inline, so the bytes pass through the model: that",
        "caps a file at ~1 MiB, inflates it 4/3 as base64, and writes the",
        "content into the conversation. Here the bytes go straight from you",
        "to storage and the model never sees them, so size is bounded by the",
        "drive, not by a context window.",
        "",
        "The transfer is GCS XML resumable, so it is POST-then-PUT, not one",
        "PUT. `upload.target` is the DESTINATION in the drive, never a URL --",
        "the URL is `upload.transfer.initiation.url`, and it is disclosed",
        "exactly once, in this response. Save it before doing anything else:",
        "re-reading the session cannot return it, it only sets",
        "`restart_required`, and a lost target means starting over.",
        "",
        "  1. begin_file_upload",
        "",
        "  2. POST the initiation URL with an EMPTY body and EXACTLY the",
        "     headers in `upload.transfer.initiation.requiredHeaders` --",
        "     signed values, so adding, dropping, or reordering one breaks the",
        "     signature. The 201 answers with a `Location` header: that is the",
        "     resumable session URI.",
        '     curl -i -X POST "$INITIATION_URL" -d "" \\',
        '       -H "<each required header>"',
        "",
        "  3. PUT the bytes at that session URI with the headers in",
        "     `upload.transfer.chunks.requiredHeaders`. One PUT carries a",
        "     whole small file; larger ones chunk with Content-Range and",
        "     resume from the 308 response's Range.",
        '     curl -X PUT --upload-file FILE "$SESSION_URI"',
        "",
        "  4. complete_file_upload -> the artifact/version becomes readable",
        "",
        "size_bytes, media_type and checksum must describe the exact bytes you",
        "are about to send; the upload is rejected if they disagree.",
        "The checksum is CRC32C (not CRC32, not MD5), base64 of four bytes.",
        "Python has no stdlib CRC32C, so compute it with:",
        "  python3 -c 'import sys,base64",
        "P=0x82F63B78; t=[]",
        "for i in range(256):",
        "    c=i",
        "    for _ in range(8): c=(c>>1)^(P if c&1 else 0)",
        "    t.append(c)",
        "c=0xFFFFFFFF",
        'f=open(sys.argv[1],"rb")',
        "while (k:=f.read(1<<20)):",
        "    for b in k: c=t[(c^b)&0xFF]^(c>>8)",
        'print(base64.b64encode((c^0xFFFFFFFF).to_bytes(4,"big")).decode())\' FILE',
        "(`gcloud storage hash --crc32c FILE` works too and is much faster on",
        "large files.)",
        "",
        "Destination is exactly one of: parent_id + name for a NEW artifact,",
        "or artifact_id for a NEW VERSION of an existing one.",
      ].join("\n"),
      inputSchema: beginFileUploadInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () =>
        uploadResult(
          await client.uploads.begin(
            args.drive_id,
            // camelCase, NOT the snake_case wire names. The generated client
            // serializes a MODEL: `UploadsCreateRequestContentToJSON` reads
            // `value['mediaType']` and emits `media_type`, and the target's
            // `instanceOf` discriminator requires `parentFolderId` before it
            // will match either `oneOf` branch.
            //
            // Passing wire names looks right and fails silently: every field
            // reads back `undefined`, the target matches no branch and
            // serializes to `{}`, and AgentDrive rejects the body with 400 —
            // naming nothing, because from its side the request genuinely was
            // empty. Shipped exactly that way; see the typed seam below, which
            // is what turns this into a compile error instead of a 400.
            {
              // Narrowed on the fields themselves, not on `artifact_id`'s
              // absence. The schema's `.refine` already guarantees exactly one
              // destination, but a refinement is invisible to the type checker,
              // so branching the other way leaves `parent_id`/`name` as
              // `string | undefined` and quietly admits an incomplete target.
              target: uploadTarget(args),
              content: {
                sizeBytes: args.size_bytes,
                mediaType: args.media_type,
                checksum: args.checksum,
              },
            },
            {
              idempotencyKey: mutationKey(
                "begin_file_upload",
                args.idempotency_key,
                args,
              ),
            },
          ),
        ),
      ),
  );

  tool(
    "get_file_upload",
    ["content:write"],
    {
      title: "Check upload status",
      description:
        "Read an upload session's state. Use it to check whether a transfer " +
        "was received before completing, or to see why one failed. The signed " +
        "target is disclosed only once, at begin_file_upload; if you lost it, " +
        "start a new session rather than expecting it here.",
      inputSchema: uploadSessionInput,
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () =>
        uploadResult(await client.uploads.read(args.drive_id, args.upload_id)),
      ),
  );

  tool(
    "complete_file_upload",
    ["content:write"],
    {
      title: "Complete file upload",
      description:
        "Finalize an upload after you have sent the bytes to the signed " +
        "target. This is what makes the artifact or version readable — an " +
        "uploaded object that is never completed does not appear in the " +
        "drive. Fails if the transferred bytes do not match the size and " +
        "checksum declared at begin_file_upload.",
      inputSchema: completeFileUploadInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () =>
        uploadResult(
          await client.uploads.complete(
            args.drive_id,
            args.upload_id,
            mutationKey("complete_file_upload", args.idempotency_key, args),
          ),
        ),
      ),
  );

  tool(
    "cancel_file_upload",
    ["content:write"],
    {
      title: "Cancel file upload",
      description:
        "Abandon an upload session and release its reserved target. Use it " +
        "when a transfer failed and you do not intend to retry, so the " +
        "session does not sit open until it expires. Requires the session's " +
        "current revision.",
      inputSchema: cancelFileUploadInput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    (args) =>
      guarded(async () =>
        uploadResult(
          await client.uploads.cancel(
            args.drive_id,
            args.upload_id,
            args.revision,
            mutationKey("cancel_file_upload", args.idempotency_key, args),
          ),
        ),
      ),
  );
}

/** The generated SDK already returns the wire envelope `{ upload: ... }`.
 * Test doubles and older client seams may return only the inner resource, so
 * normalize once without ever producing the broken `{upload:{upload:...}}`.
 */
function uploadResult(result: unknown): { upload: unknown } {
  if (
    typeof result === "object" &&
    result !== null &&
    Object.hasOwn(result, "upload")
  ) {
    return result as { upload: unknown };
  }
  return { upload: result };
}

function inlineValue(content: InlineContent): string | Uint8Array {
  if (content.encoding === "text") {
    const bytes = new TextEncoder().encode(content.value);
    if (bytes.byteLength > MAX_INLINE_BYTES) throw new ContentTooLargeError();
    return content.value;
  }
  const bytes = decodeBase64(content.value);
  if (bytes.byteLength > MAX_INLINE_BYTES) throw new ContentTooLargeError();
  return bytes;
}

async function readResponseWithinLimit(
  response: Response,
  maxBytes: number,
): Promise<{ bytes: Uint8Array; contentType: string }> {
  if (!response.ok)
    throw new Error(`content response failed: ${response.status}`);
  const declaredLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    await response.body?.cancel();
    throw new ContentTooLargeError();
  }

  if (!response.body) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > maxBytes) throw new ContentTooLargeError();
    return {
      bytes,
      contentType: response.headers.get("content-type") ?? "",
    };
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        throw new ContentTooLargeError();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return {
    bytes,
    contentType: response.headers.get("content-type") ?? "",
  };
}

function encodeBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function decodeBase64(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 === 1)
    throw new InvalidInputError("content_base64 is not valid base64");
  let binary: string;
  try {
    binary = atob(value);
  } catch {
    throw new InvalidInputError("content_base64 is not valid base64");
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1)
    bytes[index] = binary.charCodeAt(index);
  return bytes;
}
