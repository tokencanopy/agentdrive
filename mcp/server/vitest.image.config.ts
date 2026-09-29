import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

/**
 * The wire-contract suite, run against the SDK **the image installs**.
 *
 * `wire-contract.test.ts` already asserts that the sidecar sends `state` and
 * never the pre-rename `lifecycle`. That assertion once passed while the
 * shipped image was wrong, because it resolves
 * `@tokencanopy/agentdrive-sdk` from the root workspace — 0.0.4, correct —
 * while the shipped image installed 0.0.3 from `deploy/mcp/`,
 * whose facade defaults that parameter rather than omitting it. Same
 * assertion, right answer, wrong tree.
 *
 * The sidecar's SOURCE cannot drift: the Dockerfile copies
 * `mcp/server/src` verbatim. Only its installed dependencies
 * can. So this config changes exactly one thing — where the SDK resolves
 * from — and reuses the assertions unaltered. Run it after installing that
 * tree the way the image does:
 *
 *     (cd deploy/mcp && npm ci --ignore-scripts)       # from the AgentDrive root
 *     npm run test:image --workspace @tokencanopy/agentdrive-mcp   # from the npm workspace root
 *
 * The second command must run from the npm workspace root: inside
 * `deploy/mcp`, `--workspace @tokencanopy/agentdrive-mcp` names
 * the image's own `server/` workspace, which has no `test:image` script.
 *
 * It fails with a clear message rather than silently testing the workspace
 * copy twice if that install has not happened.
 */
const IMAGE_SDK = fileURLToPath(
  new URL(
    "../../deploy/mcp/node_modules/@tokencanopy/agentdrive-sdk",
    import.meta.url,
  ),
);

export default defineConfig({
  resolve: {
    alias: [
      { find: /^@tokencanopy\/agentdrive-sdk$/u, replacement: IMAGE_SDK },
    ],
  },
  test: {
    include: ["test/wire-contract.test.ts"],
    globalSetup: ["./test/image-sdk-setup.ts"],
  },
});
