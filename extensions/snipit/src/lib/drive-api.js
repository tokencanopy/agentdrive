// The AgentDrive `/v0` calls the extension makes.
//
// Thin on purpose: each function is one request plus its error mapping.
// Orchestration (which folder, which name, what to do about a conflict)
// lives in upload.js, so the retry rules are readable in one place instead
// of spread across four call sites.
//
// Every function takes an already-minted drive token; none of them knows
// how to get one.

/** AgentDrive's inline artifact-create ceiling. Checked client-side so an
 *  oversize capture fails instantly with a sentence, rather than after
 *  uploading 20 MiB. */
export const MAX_INLINE_BYTES = 20 * 1024 * 1024;

export class DriveApiError extends Error {
  constructor(code, message, { status, apiCode, retryAfterSeconds } = {}) {
    super(message);
    this.code = code;
    this.status = status;
    /** The v0 error code, e.g. `ARTIFACT_PATH_CONFLICT`. Load-bearing for
     *  409s, where the status alone does not say what to do. */
    this.apiCode = apiCode;
    /** Seconds from a `Retry-After` header, when the server sent one. */
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/** `Retry-After` in seconds, bounded so a hostile or broken value cannot
 *  park a capture for an hour. */
function retryAfterOf(response) {
  const raw = Number(response.headers?.get?.("Retry-After"));
  if (!Number.isFinite(raw) || raw <= 0) return null;
  return Math.min(raw, 15);
}

async function readError(response) {
  const body = await response.json().catch(() => null);
  // v0 errors are `{error: {code, message}}`; be forgiving about shape.
  const apiCode =
    typeof body?.error?.code === "string"
      ? body.error.code
      : typeof body?.error === "string"
        ? body.error
        : null;
  return { apiCode, message: body?.error?.message ?? null };
}

/**
 * Map a v0 failure onto something the popup can say.
 *
 * `403`/`404` on a resource the person chose earlier is the interesting
 * one: it almost always means the folder or drive was deleted or the grant
 * was revoked, so it becomes LOCATION_UNAVAILABLE and the caller marks the
 * saved location stale rather than retrying into the same wall.
 */
async function failure(response, { resource }) {
  const { apiCode, message } = await readError(response);
  if (response.status === 401) {
    return new DriveApiError("UNAUTHENTICATED", "The AgentDrive token was rejected.", {
      status: 401,
      apiCode,
    });
  }
  if (response.status === 403 || response.status === 404) {
    return new DriveApiError(
      "LOCATION_UNAVAILABLE",
      `The ${resource} SnipIt saves to is no longer available — choose a new location in settings.`,
      { status: response.status, apiCode },
    );
  }
  if (response.status === 409) {
    // AgentDrive answers 409 for FIVE different situations and the right
    // response differs per code, so the code is carried, not flattened:
    //   ARTIFACT_PATH_CONFLICT   — the name is taken. Rename.
    //   IDEMPOTENCY_IN_PROGRESS  — our own earlier request is still
    //                              running. Wait and replay the SAME key;
    //                              renaming under a new one would make a
    //                              SECOND artifact out of one capture.
    //   CHECKSUM_MISMATCH        — the bytes did not survive. Retry as-is.
    //   IDEMPOTENCY_CONFLICT     — the key was used for a different
    //                              request. Not retryable.
    //   CONFLICT                 — the parent folder is not live.
    return new DriveApiError("CONFLICT", message ?? "Conflict.", {
      status: 409,
      apiCode,
      retryAfterSeconds: retryAfterOf(response),
    });
  }
  if (response.status === 413) {
    return new DriveApiError(
      "CAPTURE_TOO_LARGE",
      "That capture is too large for AgentDrive to store inline.",
      { status: 413, apiCode },
    );
  }
  if (response.status === 429) {
    return new DriveApiError(
      "RATE_LIMITED",
      "AgentDrive is rate-limiting this drive — try again in a moment.",
      { status: 429, apiCode },
    );
  }
  return new DriveApiError(
    "DRIVE_UNAVAILABLE",
    `AgentDrive returned an error (${response.status}).`,
    { status: response.status, apiCode },
  );
}

async function request(token, path, init = {}, { resource = "folder" } = {}) {
  let response;
  try {
    response = await fetch(`${token.apiBase}${path}`, {
      ...init,
      credentials: "omit",
      headers: {
        Authorization: `Bearer ${token.accessToken}`,
        ...(init.headers ?? {}),
      },
    });
  } catch {
    throw new DriveApiError(
      "DRIVE_UNAVAILABLE",
      "Couldn't reach AgentDrive. Check your connection and try again.",
    );
  }
  if (!response.ok) throw await failure(response, { resource });
  return await response.json();
}

/** The drives this token's workspace member can read — the picker's second
 *  level. */
export async function listDrives(token, { limit = 100 } = {}) {
  const body = await request(
    token,
    `/v0/drives?limit=${encodeURIComponent(String(limit))}`,
    { method: "GET" },
    { resource: "workspace" },
  );
  return body.items ?? [];
}

/** One folder's direct child folders — the picker's third level. */
export async function listChildFolders(
  token,
  driveId,
  parentId,
  { limit = 100, cursor } = {},
) {
  const params = new URLSearchParams({
    parent_id: parentId,
    type: "folder",
    limit: String(limit),
  });
  if (cursor) params.set("cursor", cursor);
  const body = await request(
    token,
    `/v0/drives/${encodeURIComponent(driveId)}/entries?${params.toString()}`,
    { method: "GET" },
  );
  return { items: body.items ?? [], nextCursor: body.next_cursor ?? null };
}

/** A drive's root folder id, so the picker can start somewhere. */
export async function readDrive(token, driveId) {
  return await request(
    token,
    `/v0/drives/${encodeURIComponent(driveId)}`,
    { method: "GET" },
    { resource: "drive" },
  );
}

/** Exact-name lookup among a parent's child folders. Cheaper and more
 *  precise than listing: `folders_list` filters on `name` server-side. */
export async function findFolderByName(token, driveId, parentId, name) {
  const params = new URLSearchParams({ parent_id: parentId, name, limit: "1" });
  const body = await request(
    token,
    `/v0/drives/${encodeURIComponent(driveId)}/folders?${params.toString()}`,
    { method: "GET" },
  );
  return (body.items ?? [])[0] ?? null;
}

export async function createFolder(token, driveId, { parentId, name }) {
  return await request(
    token,
    `/v0/drives/${encodeURIComponent(driveId)}/folders`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_id: parentId, name }),
    },
  );
}

/**
 * Create one artifact with inline content.
 *
 * Multipart is the only shape `artifacts_create` accepts. The
 * `Content-Type` header is deliberately NOT set: the runtime must add the
 * multipart boundary, and setting it by hand produces a body the server
 * cannot parse.
 */
export async function createArtifact(
  token,
  driveId,
  { parentId, name, metadata, blob, contentType, sha256, idempotencyKey },
) {
  const form = new FormData();
  form.set("parent_id", parentId);
  form.set("name", name);
  form.set("metadata", JSON.stringify(metadata));
  form.set("content_type", contentType);
  // Optional to AgentDrive, sent anyway: it is the end-to-end check that
  // the bytes that left here are the bytes that landed, and a capture is
  // the one thing we cannot ask the user to reproduce.
  if (sha256) form.set("sha256", sha256);
  form.set("content", blob, name);
  return await request(
    token,
    `/v0/drives/${encodeURIComponent(driveId)}/artifacts`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: form,
    },
    { resource: "folder" },
  );
}

/**
 * Mint a share link for one artifact.
 *
 * The returned `url` carries the secret and is returned ONLY here — it is
 * never readable again, which is why the caller puts it straight on the
 * clipboard and stores only the share's id.
 */
export async function createShare(
  token,
  driveId,
  { artifactId, expiresAt = null },
) {
  return await request(
    token,
    `/v0/drives/${encodeURIComponent(driveId)}/shares`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        resource_type: "artifact",
        resource_id: artifactId,
        ...(expiresAt ? { expires_at: expiresAt } : {}),
      }),
    },
    { resource: "artifact" },
  );
}
