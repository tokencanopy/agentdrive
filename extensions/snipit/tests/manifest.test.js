// The two manifests must stay in lockstep except where they deliberately
// differ.
//
// `manifest.json` is the DEV manifest (Chrome's "Load unpacked");
// `manifest.prod.json` is what ships to the Web Store, and the release
// workflow swaps it in at zip time. Every field that is not about which
// origins the build may reach has to be identical, or the store build
// behaves differently from the one anybody tested.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const dev = JSON.parse(readFileSync(new URL("../manifest.json", import.meta.url)));
const prod = JSON.parse(
  readFileSync(new URL("../manifest.prod.json", import.meta.url)),
);

/** The only fields allowed to differ, and why. */
const ENVIRONMENT_FIELDS = ["host_permissions", "content_security_policy"];

test("dev and prod manifests differ only in which origins they may reach", () => {
  const strip = (m) =>
    Object.fromEntries(
      Object.entries(m).filter(([key]) => !ENVIRONMENT_FIELDS.includes(key)),
    );

  assert.deepEqual(strip(dev), strip(prod));
});

test("both manifests carry the store item's key, so the extension id is stable", () => {
  // Without this the unpacked build gets a different id, its OAuth
  // redirect URI changes, and sign-in fails with an opaque provider
  // error. It is also what lets ONE registered callback serve both.
  assert.equal(typeof dev.key, "string");
  assert.equal(dev.key, prod.key);
  assert.ok(dev.key.length > 300);
});

test("the versions match — the release workflow hard-fails otherwise", () => {
  assert.equal(dev.version, prod.version);
  assert.match(dev.version, /^\d+\.\d+\.\d+$/);
});

test("the production manifest reaches no staging or localhost origin", () => {
  const declared = JSON.stringify({
    hosts: prod.host_permissions,
    csp: prod.content_security_policy,
  });
  for (const forbidden of ["localhost", "127.0.0.1", "staging", "http://"]) {
    assert.ok(
      !declared.includes(forbidden),
      `production manifest must not mention ${forbidden}`,
    );
  }
});

test("the dev manifest is a superset of production's origins", () => {
  // A dev build that cannot reach production is a build nobody can use to
  // reproduce a production report.
  for (const host of prod.host_permissions) {
    assert.ok(
      dev.host_permissions.includes(host),
      `dev manifest missing ${host}`,
    );
  }
});

test("permissions stay minimal and justified", () => {
  // Every entry here has to be defensible to a Web Store reviewer, and
  // each addition needs a line in the README's permission justifications.
  assert.deepEqual([...prod.permissions].sort(), [
    "activeTab",
    "identity",
    "notifications",
    "offscreen",
    "scripting",
    "storage",
  ]);
  // Deliberately absent: `tabs` (activeTab is enough), `<all_urls>`, and
  // any host permission for pages being captured — `activeTab` grants the
  // capture on the user's click.
  assert.ok(!prod.permissions.includes("tabs"));
  assert.ok(!(prod.host_permissions ?? []).includes("<all_urls>"));
});

test("no web-accessible resources — the auth-handoff page is gone", () => {
  // The old flow exposed `src/auth/auth-complete.html` to the share
  // origin. The Hub flow uses chrome.identity, which needs no page of
  // ours to be reachable by a web origin at all.
  assert.equal(dev.web_accessible_resources, undefined);
  assert.equal(prod.web_accessible_resources, undefined);
});

test("the description does not promise annotation, which the extension no longer does", () => {
  for (const m of [dev, prod]) {
    assert.ok(!/annotate/i.test(m.description), m.description);
  }
});

test("the provenance producer string tracks the manifest version", async () => {
  // `capture.producer` exists so support can tell builds apart. A version
  // that silently lags the manifest makes it worse than useless.
  const { PRODUCER } = await import("../src/lib/provenance.js");

  assert.equal(PRODUCER, `snipit-chrome/${dev.version}`);
});
