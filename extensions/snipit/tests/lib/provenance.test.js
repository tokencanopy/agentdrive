// Capture provenance: what gets recorded about the page a clip came from.
//
// This metadata is written by the extension and READ by the console, which
// renders the URL as a link. So the sanitisation here is the first half of
// an XSS boundary (the console's reader is the second), and the redaction
// rules are a privacy promise made in the settings copy.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildProvenance,
  sanitizeSourceUrl,
  PRODUCER,
} from "../../src/lib/provenance.js";

const AT = new Date("2026-09-05T18:22:41.000Z");

test("records the page URL, title and capture time", () => {
  const metadata = buildProvenance({
    pageUrl: "https://example.test/docs/page?tab=2#section",
    title: "Some page",
    mode: "region",
    devicePixelRatio: 2,
    now: AT,
  });

  assert.deepEqual(metadata, {
    source: {
      kind: "web_page",
      url: "https://example.test/docs/page?tab=2#section",
      title: "Some page",
      captured_at: "2026-09-05T18:22:41.000Z",
    },
    capture: {
      producer: PRODUCER,
      mode: "region",
      device_pixel_ratio: 2,
    },
  });
});

test("keeps the fragment — SPA routes live there", () => {
  const { source } = buildProvenance({
    pageUrl: "https://example.test/app#/inbox/42",
    title: "Inbox",
    mode: "viewport",
    now: AT,
  });

  assert.equal(source.url, "https://example.test/app#/inbox/42");
});

test("strips credentials from the URL", () => {
  // Never record a password, whatever the setting says.
  const { source } = buildProvenance({
    pageUrl: "https://user:hunter2@example.test/private",
    title: "Private",
    mode: "viewport",
    now: AT,
  });

  assert.equal(source.url, "https://example.test/private");
  assert.ok(!source.url.includes("hunter2"));
});

test("stripQuery drops the query and fragment and marks the redaction", () => {
  const { source } = buildProvenance({
    pageUrl: "https://example.test/dashboard?token=secret#tab",
    title: "Dashboard",
    mode: "viewport",
    stripQuery: true,
    now: AT,
  });

  assert.equal(source.url, "https://example.test/dashboard");
  assert.equal(source.url_redacted, true);
  assert.ok(!JSON.stringify(source).includes("secret"));
});

test("the redaction marker is absent, not false, when nothing was stripped", () => {
  // Absent means "captured as it was"; `false` would be a claim the reader
  // has to interpret.
  const { source } = buildProvenance({
    pageUrl: "https://example.test/page",
    title: "Page",
    mode: "viewport",
    now: AT,
  });

  assert.ok(!("url_redacted" in source));
});

test("a non-http(s) page records no URL and no kind, but keeps the title", () => {
  for (const pageUrl of [
    "file:///Users/someone/secret.pdf",
    "chrome://settings",
    "data:text/html,<h1>hi</h1>",
    "javascript:alert(1)",
    "about:blank",
    "",
    undefined,
  ]) {
    const { source } = buildProvenance({
      pageUrl,
      title: "Local thing",
      mode: "viewport",
      now: AT,
    });

    assert.ok(!("url" in source), `no url for ${pageUrl}`);
    assert.ok(!("kind" in source), `no kind for ${pageUrl}`);
    assert.equal(source.title, "Local thing");
    assert.equal(source.captured_at, "2026-09-05T18:22:41.000Z");
  }
});

test("truncates a very long title and drops control characters", () => {
  const { source } = buildProvenance({
    pageUrl: "https://example.test/",
    title: `a\u0000b\u001Fc\u007F${"x".repeat(400)}`,
    mode: "viewport",
    now: AT,
  });

  assert.ok(source.title.length <= 256);
  assert.ok(!/[\u0000-\u001F\u007F]/.test(source.title));
  assert.ok(source.title.startsWith("abc"));
});

test("a missing title records null rather than a made-up one", () => {
  const { source } = buildProvenance({
    pageUrl: "https://example.test/",
    title: "",
    mode: "viewport",
    now: AT,
  });

  assert.equal(source.title, null);
});

test("refuses an absurdly long URL rather than recording it", () => {
  const { source } = buildProvenance({
    pageUrl: `https://example.test/${"a".repeat(4000)}`,
    title: "Long",
    mode: "viewport",
    now: AT,
  });

  assert.ok(!("url" in source));
});

test("omits device_pixel_ratio when it is not a sane number", () => {
  for (const devicePixelRatio of [undefined, 0, -1, NaN, Infinity, "2"]) {
    const { capture } = buildProvenance({
      pageUrl: "https://example.test/",
      title: "t",
      mode: "viewport",
      devicePixelRatio,
      now: AT,
    });
    assert.ok(!("device_pixel_ratio" in capture));
  }
});

test("the whole metadata object stays small", () => {
  // It rides a multipart field and is indexed for search; an unbounded
  // title or URL would be the only way it could grow.
  const metadata = buildProvenance({
    pageUrl: `https://example.test/${"p".repeat(1800)}`,
    title: "T".repeat(1000),
    mode: "region",
    devicePixelRatio: 3,
    now: AT,
  });

  assert.ok(JSON.stringify(metadata).length < 2600);
});

test("sanitizeSourceUrl is the single rule both callers share", () => {
  assert.equal(
    sanitizeSourceUrl("https://example.test/a?b=c#d"),
    "https://example.test/a?b=c#d",
  );
  assert.equal(sanitizeSourceUrl("javascript:alert(1)"), null);
  assert.equal(sanitizeSourceUrl("  https://example.test/  "), null);
  assert.equal(sanitizeSourceUrl(null), null);
});
