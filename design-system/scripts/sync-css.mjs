// Regenerate the FastAPI app's stylesheet from the design-system source of truth.
//
// The canonical stylesheet lives at design-system/src/styles/agentdrive.css. The
// running app needs a copy at src/agentdrive/static/agentdrive.css, which is
// GENERATED (source + banner), committed, and guarded in CI. v1 is a literal
// copy; this script is the seam where future PostCSS (autoprefix, minify) can
// land without changing the contract.
//
// The committed app copy is `banner + source`, so it is intentionally NOT
// byte-identical to the source. The CI drift guard is regenerate-then-`git
// diff --exit-code`, never source-vs-copy.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", ".."); // design-system/scripts -> repo root
const SOURCE = resolve(repoRoot, "design-system/src/styles/agentdrive.css");
const DEST = resolve(repoRoot, "src/agentdrive/static/agentdrive.css");

const BANNER =
  "/* GENERATED from design-system/src/styles/agentdrive.css — do not edit. Run `make css`. */\n";

const css = readFileSync(SOURCE, "utf8");
writeFileSync(DEST, BANNER + css);

console.log(
  `[sync-css] ${relative(repoRoot, SOURCE)} -> ${relative(repoRoot, DEST)} ` +
    `(${css.length} bytes + banner)`,
);
