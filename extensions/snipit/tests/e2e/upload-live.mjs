/**
 * Over-the-wire end-to-end for the extension's upload pipeline.
 *
 * The unit suite drives `uploadCapture` against a mocked `fetch` and then
 * inspects the `FormData` OBJECT it was handed. That proves the fields are
 * right; it proves nothing about the bytes. A real multipart body has to be
 * encoded, boundary-delimited, and parsed by a server — and encoding is
 * exactly the class of bug an in-process double hides.
 *
 * So this boots a real `node:http` server that speaks enough of the v0
 * contract to answer the pipeline, and runs the pipeline against it with
 * the platform `fetch`. What it proves:
 *
 *   - the multipart body parses server-side, with every field intact and
 *     the PNG bytes byte-identical;
 *   - the runtime sets its own `content-type` boundary (we must not);
 *   - the `Authorization` and `Idempotency-Key` headers arrive;
 *   - a unicode title survives the wire without a header ByteString error;
 *   - the 401 replay reuses the idempotency key across two REAL requests.
 *
 * Run: node tests/e2e/upload-live.mjs
 */

import { createServer } from "node:http";
import { once } from "node:events";
import assert from "node:assert/strict";

const WORKSPACE = "tcws_0000000000000001";
const DRIVE = "drv_00000000000000a1";
const FOLDER = "fld_00000000000000b2";
const ARTIFACT = "art_00000000000000d4";

let checks = 0;
let failures = 0;
function check(label, condition, detail) {
  checks += 1;
  if (condition) console.log(`  ok   ${label}`);
  else {
    failures += 1;
    console.log(
      `  FAIL ${label}${detail === undefined ? "" : ` — ${JSON.stringify(detail)}`}`,
    );
  }
}

/** The PNG bytes the "capture" produces — a real signature plus noise, so a
 *  truncation or a re-encode shows up as a byte mismatch. */
const PNG_BYTES = new Uint8Array([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
  ...Array.from({ length: 512 }, (_, i) => (i * 7 + 13) % 256),
]);

/* ── A v0-shaped server, enough for the pipeline ───────────────────── */

const received = [];
let artifactStatus = 201;
let artifactAttempts = 0;

const server = createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks);
  const url = new URL(req.url, `http://${req.headers.host}`);

  const record = {
    method: req.method,
    path: url.pathname,
    query: Object.fromEntries(url.searchParams),
    headers: req.headers,
    raw,
  };
  received.push(record);

  const json = (status, body) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  };

  if (url.pathname === "/v0/snipit/agentdrive-token") {
    const requested = JSON.parse(raw.toString("utf8") || "{}").access;
    record.requestedAccess = requested;
    return json(200, {
      schema_version: 1,
      access_token: `drive-token-${received.length}`,
      token_type: "Bearer",
      expires_in: 300,
      access: requested,
      scope:
        requested === "browse"
          ? "drives:read content:read"
          : "content:read content:write sharing:write",
      resource: base,
      api_base_url: base,
      workspace_id: WORKSPACE,
    });
  }
  if (url.pathname.endsWith("/artifacts") && req.method === "POST") {
    artifactAttempts += 1;
    // Parse the multipart body the way a server actually does.
    const parsed = await new Response(raw, {
      headers: { "content-type": req.headers["content-type"] },
    }).formData();
    record.fields = parsed;
    if (artifactStatus !== 201 && artifactAttempts === 1) {
      return json(artifactStatus, { error: { code: "UNAUTHENTICATED" } });
    }
    return json(201, { id: ARTIFACT, name: parsed.get("name") });
  }
  if (url.pathname.endsWith("/shares") && req.method === "POST") {
    return json(201, { id: "shr_1", url: `${base}/s/secret-key/` });
  }
  return json(404, { error: { code: "NOT_FOUND" } });
});

server.listen(0, "127.0.0.1");
await once(server, "listening");
const base = `http://127.0.0.1:${server.address().port}`;

/* ── The extension's own environment ───────────────────────────────── */

const store = {
  local: {
    hub_session: {
      accessToken: "hub-access-token",
      refreshToken: "hub-refresh-token",
      expiresAtMs: Date.now() + 3_600_000,
      hubBase: base,
    },
    // The developer override is what points an unpacked build at a local
    // stack — exercised here rather than described.
    endpoint_override: {
      hubBase: base,
      apiBase: base,
      consoleBase: base,
      shareBase: base,
    },
  },
  session: {},
};

function area(name) {
  return {
    get: async (keys) => {
      const list = typeof keys === "string" ? [keys] : keys;
      const out = {};
      for (const key of list) if (key in store[name]) out[key] = store[name][key];
      return out;
    },
    set: async (obj) => Object.assign(store[name], obj),
    remove: async (keys) => {
      for (const key of [].concat(keys)) delete store[name][key];
    },
  };
}

globalThis.chrome = {
  runtime: { id: "kpdpkhkhinihhehlakjbdlloagcmpkok" },
  storage: { local: area("local"), session: area("session") },
  management: { getSelf: async () => ({ installType: "development" }) },
};
Object.defineProperty(globalThis, "navigator", {
  value: { locks: { request: async (_n, _o, cb) => await cb() } },
  configurable: true,
  writable: true,
});

const { uploadCapture } = await import("../../src/lib/upload.js");

const LOCATION = {
  workspace_id: WORKSPACE,
  workspace_name: "Acme",
  drive_id: DRIVE,
  drive_name: "Design drive",
  folder_id: FOLDER,
  folder_path: ["Screenshots"],
  stale: false,
};

try {
  /* ── 1. A capture, end to end over real HTTP ────────────────────── */
  console.log("upload over the wire");
  const result = await uploadCapture({
    blob: new Blob([PNG_BYTES], { type: "image/png" }),
    title: "Café — naïve ünïcode 🚀",
    pageUrl: "https://example.test/docs?tab=2#frag",
    mode: "region",
    devicePixelRatio: 2,
    location: LOCATION,
    preferences: {
      group_by_date: false,
      link: "share",
      strip_query: false,
      link_expiry_days: null,
    },
    now: new Date("2026-09-05T18:22:41.000Z"),
  });

  check("the artifact was created", result.artifactId === ARTIFACT, result);
  check("a share link came back", result.link === `${base}/s/secret-key/`);

  const create = received.find((r) => r.path.endsWith("/artifacts"));
  check("the create reached the right path", create !== undefined);
  check(
    "it carried the drive token, not the hub session",
    create.headers.authorization?.startsWith("Bearer drive-token-") &&
      !create.headers.authorization.includes("hub-access"),
    create.headers.authorization,
  );
  check(
    "an Idempotency-Key header arrived",
    typeof create.headers["idempotency-key"] === "string",
  );

  // The capture path asks for the WRITE token and never the browse one, so
  // a capture cannot enumerate the workspace's drives.
  const mints = received.filter((r) => r.path.endsWith("/agentdrive-token"));
  check(
    "the capture asked for a capture token, not a browse one",
    mints.length > 0 && mints.every((m) => m.requestedAccess === "capture"),
    mints.map((m) => m.requestedAccess),
  );
  check(
    "the runtime set its own multipart boundary",
    /^multipart\/form-data; boundary=/.test(create.headers["content-type"]),
    create.headers["content-type"],
  );

  /* ── 2. The body actually parsed, and the bytes survived ────────── */
  console.log("\nmultipart body");
  const fields = create.fields;
  check("parent_id", fields.get("parent_id") === FOLDER);
  check("content_type", fields.get("content_type") === "image/png");
  check(
    "name is a slug plus a UTC time",
    /^cafe-naive-unicode-\d{6}\.png$/.test(fields.get("name")),
    fields.get("name"),
  );

  const uploaded = new Uint8Array(await fields.get("content").arrayBuffer());
  check(
    "the PNG bytes are byte-identical after the round trip",
    uploaded.length === PNG_BYTES.length &&
      uploaded.every((byte, i) => byte === PNG_BYTES[i]),
    { sent: PNG_BYTES.length, got: uploaded.length },
  );

  const metadata = JSON.parse(fields.get("metadata"));
  check(
    "the unicode title survived the wire intact",
    metadata.source.title === "Café — naïve ünïcode 🚀",
    metadata.source.title,
  );
  check(
    "the source URL is recorded whole, fragment included",
    metadata.source.url === "https://example.test/docs?tab=2#frag",
    metadata.source.url,
  );
  check("the capture mode is recorded", metadata.capture.mode === "region");
  check(
    "no redaction marker when nothing was stripped",
    !("url_redacted" in metadata.source),
  );

  /* ── 3. The 401 replay, across two REAL requests ────────────────── */
  console.log("\n401 replay");
  received.length = 0;
  artifactAttempts = 0;
  artifactStatus = 401;
  const replayed = await uploadCapture({
    blob: new Blob([PNG_BYTES], { type: "image/png" }),
    title: "Retry me",
    pageUrl: "https://example.test/retry",
    mode: "viewport",
    location: LOCATION,
    preferences: { group_by_date: false, link: "none" },
  });
  check("the replay succeeded", replayed.artifactId === ARTIFACT);
  const creates = received.filter((r) => r.path.endsWith("/artifacts"));
  check("exactly two create attempts", creates.length === 2, creates.length);
  check(
    "the idempotency key is IDENTICAL across the replay",
    creates[0].headers["idempotency-key"] ===
      creates[1].headers["idempotency-key"],
  );
  check(
    "the second attempt used a freshly minted token",
    creates[0].headers.authorization !== creates[1].headers.authorization,
  );

  /* ── 4. The strip-query setting, over the wire ──────────────────── */
  console.log("\nstrip-query setting");
  received.length = 0;
  artifactAttempts = 0;
  artifactStatus = 201;
  await uploadCapture({
    blob: new Blob([PNG_BYTES], { type: "image/png" }),
    title: "Dashboard",
    pageUrl: "https://example.test/dash?token=SUPERSECRET#tab",
    mode: "viewport",
    location: LOCATION,
    preferences: { group_by_date: false, link: "none", strip_query: true },
  });
  const stripped = JSON.parse(
    received.find((r) => r.path.endsWith("/artifacts")).fields.get("metadata"),
  );
  check(
    "the query string never reached the wire",
    !received
      .find((r) => r.path.endsWith("/artifacts"))
      .raw.toString("utf8")
      .includes("SUPERSECRET"),
  );
  check("the address was shortened", stripped.source.url === "https://example.test/dash");
  check("and the redaction is recorded", stripped.source.url_redacted === true);

  /* ── 5. Date grouping creates and reuses one folder ─────────────── */
  console.log("\ndate folders");
  received.length = 0;
  artifactAttempts = 0;
  // The stub 404s unknown paths, so `findFolderByName` fails closed — which
  // is itself worth proving: an unreachable folder must not silently drop
  // the capture into the parent.
  let groupedFailed = false;
  try {
    await uploadCapture({
      blob: new Blob([PNG_BYTES], { type: "image/png" }),
      title: "Grouped",
      pageUrl: "https://example.test/g",
      mode: "viewport",
      location: LOCATION,
      preferences: { group_by_date: true, link: "none" },
    });
  } catch (error) {
    groupedFailed = true;
    check(
      "an unreachable date folder reports a stale location, not a silent fallback",
      error.code === "LOCATION_UNAVAILABLE",
      error.code,
    );
  }
  check("the capture did not land in the parent folder", groupedFailed);
} finally {
  server.close();
}

console.log(`\n${checks - failures}/${checks} checks passed`);
if (failures > 0) process.exitCode = 1;
