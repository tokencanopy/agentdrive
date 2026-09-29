// Lay the stylesheet out for the npm package with every file it references.
//
// `./styles.css` is the package's CSS entry point, and the canonical sheet
// names fonts and images by relative `url()` — `fonts/*.woff2`, `img/*.png`,
// `img/agents/*.svg` — which the FastAPI app serves from
// src/agentdrive/static/. A package that shipped the sheet alone would hand a
// consumer's bundler URLs that resolve to nothing, and an npm version cannot
// be republished once it is wrong. So this copies the sheet to dist/styles/
// and, for each relative url() in it, the referenced file from the app's
// static directory to the same relative path beside it. A reference with no
// file behind it fails the build.
//
//     node scripts/package-styles.mjs            # after `vite build` (which empties dist/)
//     node scripts/package-styles.mjs --check    # also assert `npm pack` would ship them
import { copyFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pkg = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SOURCE = resolve(pkg, "src/styles/agentdrive.css");
const STATIC = resolve(pkg, "../src/agentdrive/static");
const OUT = resolve(pkg, "dist/styles");

export function relativeUrls(css) {
  const urls = new Set();
  for (const match of css.matchAll(/url\(\s*(["']?)([^"')]+)\1\s*\)/g)) {
    const url = match[2].trim();
    if (/^(data:|https?:|\/\/|\/|#)/.test(url)) continue;
    urls.add(url.split(/[?#]/)[0]);
  }
  return [...urls].sort();
}

const css = readFileSync(SOURCE, "utf8");
const urls = relativeUrls(css);
mkdirSync(OUT, { recursive: true });
copyFileSync(SOURCE, resolve(OUT, "agentdrive.css"));
const missing = [];
for (const url of urls) {
  const from = resolve(STATIC, url);
  if (!existsSync(from)) {
    missing.push(url);
    continue;
  }
  mkdirSync(dirname(resolve(OUT, url)), { recursive: true });
  copyFileSync(from, resolve(OUT, url));
}
if (missing.length) {
  console.error(`[package-styles] no file behind: ${missing.join(", ")}`);
  process.exit(1);
}
console.log(`[package-styles] dist/styles/agentdrive.css + ${urls.length} referenced files`);

if (process.argv.includes("--check")) {
  const [report] = JSON.parse(
    execFileSync("npm", ["pack", "--dry-run", "--json"], { cwd: pkg, encoding: "utf8" }),
  );
  const packed = new Set(report.files.map((f) => f.path));
  const wanted = ["dist/styles/agentdrive.css", ...urls.map((u) => `dist/styles/${u}`)];
  const absent = wanted.filter((p) => !packed.has(p));
  if (absent.length) {
    console.error(`[package-styles] npm pack would not ship: ${absent.join(", ")}`);
    process.exit(1);
  }
  console.log(`[package-styles] npm pack ships all ${wanted.length} files`);
}
