import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * Refuse to run if the image's dependency tree is not installed.
 *
 * Without this the alias would silently fall back to the workspace copy and
 * the job would pass while proving nothing — the precise failure mode this
 * suite exists to close.
 */
// `deploy/mcp` beside `mcp/`. Three levels up from `mcp/server/test/` rather
// than out of `packages/`: the sidecar moved inside the app, so the image's
// build tree is a sibling now.
const IMAGE_ROOT = fileURLToPath(
  new URL("../../../deploy/mcp", import.meta.url),
);
const IMAGE_SDK = `${IMAGE_ROOT}/node_modules/@tokencanopy/agentdrive-sdk`;

export function setup(): void {
  if (!existsSync(`${IMAGE_SDK}/package.json`)) {
    throw new Error(
      `the image's dependency tree is not installed at ${IMAGE_ROOT}.\n` +
        "Install it the way the Dockerfile does, then re-run:\n" +
        `  cd ${IMAGE_ROOT} && npm ci --ignore-scripts`,
    );
  }

  // Say which version is under test. When this suite fails, the first
  // question is always "which SDK was that?", and the answer belongs in the
  // log rather than in someone's next twenty minutes.
  const installed = JSON.parse(
    readFileSync(`${IMAGE_SDK}/package.json`, "utf8"),
  ) as { version?: string };
  console.log(
    `wire contract: testing the SDK the image installs — agentdrive-sdk ${installed.version}`,
  );
}
