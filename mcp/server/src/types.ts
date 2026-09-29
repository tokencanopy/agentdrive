/**
 * The minimum surface consumed by the MCP layer. Production supplies an
 * AgentDriveClient from @tokencanopy/agentdrive-sdk; keeping this seam explicit
 * makes the MCP protocol testable without a second HTTP implementation.
 */

export interface PageLike<T = Record<string, unknown>> {
  items?: T[];
  nextCursor?: string | null;
  hasMore?: boolean;
}

export interface DriveLike {
  id: string;
  [key: string]: unknown;
}

export interface ArtifactLike {
  id?: string;
  contentType?: string;
  [key: string]: unknown;
}

export interface LookupLike {
  id: string;
  [key: string]: unknown;
}

/** A folder as the SDK returns it; `parentId` is null at the drive root. */
export interface FolderLike {
  id: string;
  parentId?: string | null;
  [key: string]: unknown;
}

/** An access grant as the SDK returns it (the model's camelCase names). */
export interface GrantLike {
  id: string;
  revision: string;
  resourceType: string;
  resourceId: string;
  principalType: string;
  [key: string]: unknown;
}

/**
 * The one grant shape the MCP layer creates: a public viewer grant.
 *
 * Spelled out for the same reason `UploadBeginRequest` is — the SDK
 * serializes a MODEL, so the wire names (`principal_type`) would read back
 * `undefined`. `principalId` is deliberately absent: AgentDrive rejects a
 * public grant that carries one, and a public grant is viewer-only.
 */
export interface PublicGrantCreate {
  principalType: "public";
  resourceType: "artifact" | "folder";
  resourceId: string;
  role: "viewer";
  expiresAt: Date | null;
}

export interface AgentDriveClientLike {
  drives: {
    list(options?: Record<string, unknown>): Promise<PageLike<DriveLike>>;
    usage(driveId: string): Promise<unknown>;
    create(
      name: string,
      options?: { metadata?: Record<string, unknown>; idempotencyKey?: string },
    ): Promise<unknown>;
    delete(
      driveId: string,
      revision: string,
      idempotencyKey?: string,
    ): Promise<unknown>;
    restore(
      driveId: string,
      revision: string,
      idempotencyKey?: string,
    ): Promise<unknown>;
  };
  entries: {
    list(
      driveId: string,
      options?: Record<string, unknown>,
    ): Promise<PageLike & { entries?: Record<string, unknown>[] }>;
    lookup(driveId: string, path: string, type?: string): Promise<LookupLike>;
  };
  folders: {
    get(driveId: string, folderId: string): Promise<FolderLike>;
    create(
      driveId: string,
      name: string,
      options?: Record<string, unknown>,
    ): Promise<unknown>;
    update(
      driveId: string,
      folderId: string,
      revision: string,
      options?: Record<string, unknown>,
    ): Promise<unknown>;
    delete(
      driveId: string,
      folderId: string,
      revision: string,
      options?: Record<string, unknown>,
    ): Promise<unknown>;
    restore(
      driveId: string,
      folderId: string,
      revision: string,
      idempotencyKey?: string,
    ): Promise<unknown>;
  };
  artifacts: {
    get(driveId: string, artifactId: string): Promise<ArtifactLike>;
    create(
      driveId: string,
      name: string,
      content: Blob | ArrayBuffer | Uint8Array | string,
      options?: Record<string, unknown>,
    ): Promise<unknown>;
    update(
      driveId: string,
      artifactId: string,
      revision: string,
      options?: Record<string, unknown>,
    ): Promise<unknown>;
    delete(
      driveId: string,
      artifactId: string,
      revision: string,
      idempotencyKey?: string,
    ): Promise<unknown>;
    restore(
      driveId: string,
      artifactId: string,
      revision: string,
      idempotencyKey?: string,
    ): Promise<unknown>;
    content(driveId: string, artifactId: string): Promise<Blob>;
    /** Optional streaming seam used by the hosted MCP read limit. */
    contentResponse?(driveId: string, artifactId: string): Promise<Response>;
  };
  versions: {
    list(
      driveId: string,
      artifactId: string,
      options?: Record<string, unknown>,
    ): Promise<PageLike>;
    append(
      driveId: string,
      artifactId: string,
      revision: string,
      content: Blob | ArrayBuffer | Uint8Array | string,
      options?: Record<string, unknown>,
    ): Promise<unknown>;
  };
  search: {
    find(
      driveId: string,
      query: string,
      options?: Record<string, unknown>,
    ): Promise<PageLike>;
  };
  changes: {
    list(driveId: string, options?: Record<string, unknown>): Promise<PageLike>;
  };
  grants: {
    list(
      driveId: string,
      options?: Record<string, unknown>,
    ): Promise<PageLike<GrantLike>>;
    create(
      driveId: string,
      input: PublicGrantCreate,
      idempotencyKey?: string,
    ): Promise<GrantLike>;
    revoke(
      driveId: string,
      grantId: string,
      revision: string,
      idempotencyKey?: string,
    ): Promise<GrantLike>;
  };
  shares: {
    create(
      driveId: string,
      input: Record<string, unknown>,
      idempotencyKey?: string,
    ): Promise<unknown>;
  };
  /** Resumable upload sessions.
   *
   * The reason these are exposed as tools at all: `create_artifact` carries
   * bytes INLINE, which means every byte crosses the model's context on its
   * way to the drive. That caps an upload at ~1 MiB, inflates it 4/3 through
   * base64, and puts file content in transcripts. A session hands the agent a
   * signed URL instead, so the bytes travel agent → object store and the model
   * never sees them.
   */
  uploads: {
    begin(
      driveId: string,
      request: UploadBeginRequest,
      options?: { revision?: string; idempotencyKey?: string },
    ): Promise<unknown>;
    read(driveId: string, uploadId: string): Promise<unknown>;
    complete(
      driveId: string,
      uploadId: string,
      idempotencyKey?: string,
    ): Promise<unknown>;
    cancel(
      driveId: string,
      uploadId: string,
      revision: string,
      idempotencyKey?: string,
    ): Promise<unknown>;
  };
}

/**
 * The upload-session request, in the shape the SDK's MODEL takes.
 *
 * Deliberately spelled out rather than left as `Record<string, unknown>`.
 * The generated client serializes a model, not a body: its
 * `...ToJSON` functions read `mediaType` and emit `media_type`, and the
 * target's `instanceOf` discriminator tests for `parentFolderId` before it
 * will match either `oneOf` branch. Passing the wire names is therefore
 * silently wrong — every field reads back `undefined`, the target matches no
 * branch and serializes to `{}`, and the API answers 400 describing nothing,
 * because the request it received really was empty.
 *
 * That shipped once behind a `Record<string, unknown>`, which accepts any
 * key and so accepted the wrong ones. Naming the fields makes the same
 * mistake a compile error.
 */
export type UploadBeginRequest = {
  target:
    | { kind: "artifact"; parentFolderId: string; name: string }
    | { kind: "version"; artifactId: string };
  content: {
    sizeBytes: number;
    mediaType: string;
    checksum: { algorithm: "crc32c"; value: string };
  };
};

export type AgentDriveClientFactory = (
  accessToken: string,
) => AgentDriveClientLike;
