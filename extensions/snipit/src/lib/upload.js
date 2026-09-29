// Capture → artifact → link.
//
// The orchestration the pipeline actually needs, in one place: resolve the
// target folder, create the artifact, and (optionally) mint the link that
// goes on the clipboard. Every retry rule lives here rather than in
// drive-api.js, because the rules differ per failure and reading them side
// by side is the only way to keep them straight:
//
//   * 401 → the token went stale mid-flight. Re-mint and replay with the
//     SAME idempotency key, so a request that actually landed is replayed,
//     not duplicated. Once only.
//   * 409 ARTIFACT_PATH_CONFLICT → a genuinely different request. New
//     name, NEW key, once only.
//   * 409 IDEMPOTENCY_IN_PROGRESS → our OWN earlier request is still
//     running. Wait the server's Retry-After and replay the SAME key. A
//     rename under a new key here makes a SECOND artifact out of one
//     capture, which is the one duplication this design has to prevent.
//   * 409 CHECKSUM_MISMATCH → the bytes did not survive the wire. Replay
//     unchanged: same key, same name. A rename would file a corrupted
//     upload under a new name and call it success.
//   * 409 on the date folder → someone else created it between our read
//     and our write. Re-read; do not create.
//   * 403/404 → the saved location is gone. Say so; do not retry.

import { getEndpoints } from "./config.js";
import { driveToken } from "./drive-token.js";
import {
  createArtifact,
  createFolder,
  createShare,
  DriveApiError,
  findFolderByName,
  MAX_INLINE_BYTES,
} from "./drive-api.js";
import { buildProvenance } from "./provenance.js";
import { captureName, dateFolderName } from "./path.js";

export class UploadError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function newIdempotencyKey() {
  return crypto.randomUUID();
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/** Hex SHA-256 of the capture, so AgentDrive can verify the bytes it
 *  received are the bytes we sent. */
async function digestOf(blob) {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * The date folder for this capture, creating it if it does not exist.
 *
 * `null` parent means the caller turned date grouping off.
 */
async function resolveParentFolder(token, location, preferences, now) {
  if (!preferences.group_by_date) return location.folder_id;

  const name = dateFolderName(now);
  const existing = await findFolderByName(
    token,
    location.drive_id,
    location.folder_id,
    name,
  );
  if (existing) return existing.id;

  try {
    const created = await createFolder(token, location.drive_id, {
      parentId: location.folder_id,
      name,
    });
    return created.id;
  } catch (error) {
    if (error instanceof DriveApiError && error.code === "CONFLICT") {
      // Another capture won the race. Re-read rather than fail: the folder
      // we wanted now exists, it just is not ours.
      const found = await findFolderByName(
        token,
        location.drive_id,
        location.folder_id,
        name,
      );
      if (found) return found.id;
    }
    throw error;
  }
}

/** A short suffix that makes a colliding name unique without making it
 *  ugly — `example-page-182241-4f2a.png`. */
function disambiguate(name) {
  const suffix = Array.from(crypto.getRandomValues(new Uint8Array(2)))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return name.replace(/\.png$/, `-${suffix}.png`);
}

/**
 * Upload one capture.
 *
 * @param {object} args
 * @param {Blob} args.blob PNG bytes
 * @param {string} args.title source page title
 * @param {string} args.pageUrl source page URL
 * @param {"region" | "viewport"} args.mode
 * @param {number} [args.devicePixelRatio]
 * @param {object} args.location the saved capture location
 * @param {object} args.preferences the saved preferences
 * @param {Date} [args.now] injected by tests
 * @returns {Promise<{artifactId: string, name: string, link: string | null,
 *   linkError: string | null}>}
 */
export async function uploadCapture({
  blob,
  title,
  pageUrl,
  mode,
  devicePixelRatio,
  location,
  preferences,
  now = new Date(),
}) {
  if (blob.size > MAX_INLINE_BYTES) {
    throw new UploadError(
      "CAPTURE_TOO_LARGE",
      `That capture is ${Math.round(blob.size / (1024 * 1024))} MB — larger than the ${MAX_INLINE_BYTES / (1024 * 1024)} MB SnipIt can upload in one piece.`,
    );
  }

  let token = await driveToken(location.workspace_id, "capture");
  const parentId = await resolveParentFolder(token, location, preferences, now);
  const metadata = buildProvenance({
    pageUrl,
    title,
    mode,
    devicePixelRatio,
    stripQuery: preferences.strip_query === true,
    now,
  });

  const sha256 = await digestOf(blob);
  let name = captureName({ title, now });
  let idempotencyKey = newIdempotencyKey();
  let replayedUnauthenticated = false;
  let renamed = false;
  let waitedForInFlight = false;
  let replayedChecksum = false;
  let artifact;

  // Every retry is gated by its own one-shot flag, so no failure mode can
  // loop — at most one attempt per distinct cause.
  for (;;) {
    try {
      artifact = await createArtifact(token, location.drive_id, {
        parentId,
        name,
        metadata,
        blob,
        contentType: "image/png",
        sha256,
        idempotencyKey,
      });
      break;
    } catch (error) {
      if (!(error instanceof DriveApiError)) throw error;

      if (error.code === "UNAUTHENTICATED" && !replayedUnauthenticated) {
        replayedUnauthenticated = true;
        // The cache believes in a token AgentDrive just rejected, so
        // bypass it. Same key: this is a replay, not a new request.
        token = await driveToken(location.workspace_id, "capture", {
          forceRefresh: true,
        });
        continue;
      }
      if (error.code === "UNAUTHENTICATED") {
        throw new UploadError(
          "NOT_SIGNED_IN",
          "AgentDrive rejected the sign-in — sign out and back in from the SnipIt popup.",
        );
      }
      if (error.code === "CONFLICT") {
        // The status says "conflict"; only the code says which one, and
        // they do NOT share a remedy.
        if (error.apiCode === "IDEMPOTENCY_IN_PROGRESS" && !waitedForInFlight) {
          waitedForInFlight = true;
          // Our own earlier attempt is still in flight. Wait for the
          // server's own estimate, then replay the SAME key so whichever
          // request lands first is the only artifact created.
          await sleep((error.retryAfterSeconds ?? 5) * 1000);
          continue;
        }
        if (error.apiCode === "CHECKSUM_MISMATCH" && !replayedChecksum) {
          replayedChecksum = true;
          // The bytes were corrupted in transit. Same key, same name —
          // this is a replay of the same request, not a new one.
          continue;
        }
        if (error.apiCode === "CHECKSUM_MISMATCH") {
          throw new UploadError(
            "CAPTURE_CORRUPTED",
            "The capture didn't arrive intact at AgentDrive. Try capturing again.",
          );
        }
        if (error.apiCode === "ARTIFACT_PATH_CONFLICT" && !renamed) {
          renamed = true;
          name = disambiguate(name);
          // A genuinely different request deserves a different key;
          // reusing it would replay the conflict instead of trying the
          // new name.
          idempotencyKey = newIdempotencyKey();
          continue;
        }
        throw new UploadError(
          "CONFLICT",
          error.apiCode === "ARTIFACT_PATH_CONFLICT"
            ? "A capture with that name already exists here."
            : error.message,
        );
      }
      if (error.code === "LOCATION_UNAVAILABLE") {
        throw new UploadError("LOCATION_UNAVAILABLE", error.message);
      }
      throw new UploadError(error.code, error.message);
    }
  }

  const link = await resolveLink({
    token,
    location,
    preferences,
    artifactId: artifact.id,
    now,
  });

  return {
    artifactId: artifact.id,
    name: artifact.name ?? name,
    ...link,
  };
}

/**
 * Why a share link could not be minted, in words the reader can act on.
 *
 * `shares_create` is gated on a drive-level MANAGER grant, not merely the
 * `sharing:write` scope the token carries — so a workspace member with only
 * an editor grant on the capture folder can upload perfectly well and still
 * never mint a link. The generic 403 message for that case talks about the
 * saved location, which is not the problem and which changing will not fix.
 */
function shareFailureMessage(error) {
  if (error instanceof DriveApiError && error.status === 403) {
    return (
      "SnipIt can't create share links in this drive. Switch \u201cCopy a link\u201d " +
      "to a console link in settings, or ask a drive manager for access."
    );
  }
  return error instanceof DriveApiError
    ? error.message
    : "Couldn't create a share link for this capture.";
}

/**
 * What goes on the clipboard.
 *
 * A failure here never fails the capture: the artifact is already saved,
 * and telling someone their screenshot was lost because the share call
 * 500ed would be a lie.
 */
async function resolveLink({
  token,
  location,
  preferences,
  artifactId,
  now,
}) {
  if (preferences.link === "none") {
    return { link: null, linkError: null };
  }
  if (preferences.link === "console") {
    const { consoleBase } = await getEndpoints();
    return {
      link:
        `${consoleBase}/drive/${encodeURIComponent(location.drive_id)}` +
        `/a/${encodeURIComponent(artifactId)}/`,
      linkError: null,
    };
  }

  const days = preferences.link_expiry_days;
  const expiresAt =
    typeof days === "number"
      ? new Date(now.getTime() + days * 86_400_000).toISOString()
      : null;
  try {
    const share = await createShare(token, location.drive_id, {
      artifactId,
      expiresAt,
    });
    return { link: share.url, linkError: null };
  } catch (error) {
    return {
      link: null,
      linkError: shareFailureMessage(error),
    };
  }
}
