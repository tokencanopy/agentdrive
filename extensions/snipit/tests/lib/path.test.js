// Run with: node --test tests/lib/path.test.js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  slugifyTitle,
  captureName,
  dateFolderName,
} from "../../src/lib/path.js";

test("slugifyTitle basic", () => {
  assert.equal(slugifyTitle("Hello World"), "hello-world");
  assert.equal(slugifyTitle("Foo - Bar - Baz"), "foo-bar-baz");
});

test("slugifyTitle handles empty / whitespace", () => {
  assert.equal(slugifyTitle(""), "untitled");
  assert.equal(slugifyTitle("   "), "untitled");
  assert.equal(slugifyTitle(undefined), "untitled");
  assert.equal(slugifyTitle(null), "untitled");
});

test("slugifyTitle strips URL-unsafe + collapses dashes", () => {
  assert.equal(slugifyTitle("Foo/Bar? Baz!"), "foobar-baz");
  assert.equal(slugifyTitle("--leading-and-trailing--"), "leading-and-trailing");
  assert.equal(slugifyTitle("multi    spaces"), "multi-spaces");
});

test("slugifyTitle truncates long input", () => {
  const long = "a".repeat(200);
  const slug = slugifyTitle(long);
  assert.ok(slug.length <= 60);
  assert.ok(slug.startsWith("aaaa"));
});

test("captureName is a slug plus a UTC time suffix", () => {
  const now = new Date(Date.UTC(2026, 5, 12, 9, 5, 3));
  assert.equal(captureName({ title: "Dashboard Bug", now }), "dashboard-bug-090503.png");
});

test("captureName falls back to untitled", () => {
  const now = new Date(Date.UTC(2026, 5, 12, 0, 0, 0));
  assert.equal(captureName({ title: "", now }), "untitled-000000.png");
});

test("captureName never carries a path separator", () => {
  // v0 names an artifact INSIDE a parent_id; a slash in the name would be
  // a path the API does not have.
  for (const t of ["a/b", "../etc/passwd", "x\\y", "  ", "\u00e9\u00e9\u00e9"]) {
    const name = captureName({ title: t, now: new Date(Date.UTC(2026, 5, 1)) });
    assert.ok(!name.includes("/"), `no slash in ${name}`);
    assert.ok(!name.includes("\\"), `no backslash in ${name}`);
    assert.match(name, /^[a-z0-9-]+-\d{6}\.png$/);
  }
});

test("dateFolderName is the UTC calendar day", () => {
  // UTC so the folder does not change when someone travels, and so names
  // sort lexicographically by day.
  assert.equal(dateFolderName(new Date("2026-09-05T23:59:59.000Z")), "2026-09-05");
  assert.equal(dateFolderName(new Date("2026-09-06T00:00:01.000Z")), "2026-09-06");
});
