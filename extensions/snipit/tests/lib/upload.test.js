// The capture upload pipeline, end to end over a mocked network.
//
// Replaces the pre-v0 suite, which tested a header builder for
// `X-AgentDrive-Source` on a `PUT /v0/artifacts/<path>` endpoint that no
// longer exists. v0 creates artifacts by `parent_id` under a drive, as
// multipart, and provenance rides the `metadata` field.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

import {
  installChromeMock,
  installFetchMock,
  jsonResponse,
} from "../support/chrome-mock.js";

installChromeMock();

const { ENVIRONMENTS } = await import("../../src/lib/config.js");
const { HUB_SESSION_KEY } = await import("../../src/lib/hub-auth.js");
const { uploadCapture } = await import("../../src/lib/upload.js");

const HUB = ENVIRONMENTS.staging.hubBase;
const API = "https://drive.staging.tokencanopy.test";
const DRIVE = "drv_00000000000000a1";
const ROOT_FOLDER = "fld_00000000000000b2";
const DATE_FOLDER = "fld_00000000000000c3";
const ARTIFACT = "art_00000000000000d4";

const LOCATION = {
  workspace_id: "tcws_0000000000000001",
  workspace_name: "Acme",
  drive_id: DRIVE,
  drive_name: "Design drive",
  folder_id: ROOT_FOLDER,
  folder_path: ["Screenshots"],
  stale: false,
};

const CAPTURE = {
  blob: new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], {
    type: "image/png",
  }),
  title: "Example Page",
  pageUrl: "https://example.test/docs?tab=2",
  mode: "region",
  devicePixelRatio: 2,
  now: new Date("2026-09-05T18:22:41.000Z"),
};

let state;
let calls;
let handlers;

function defaultHandlers() {
  return {
    listFolders: () => jsonResponse({ items: [], next_cursor: null }),
    createFolder: () => jsonResponse({ id: DATE_FOLDER, name: "2026-09-05" }, 201),
    createArtifact: () =>
      jsonResponse(
        { id: ARTIFACT, name: "example-page-182241.png", revision: "rev_1" },
        201,
      ),
    createShare: () =>
      jsonResponse({ id: "shr_1", url: `${API}/s/secret-key/` }, 201),
  };
}

function install() {
  calls = installFetchMock([
    {
      match: "/.well-known/openid-configuration",
      respond: async () =>
        jsonResponse({
          issuer: `${HUB}/oidc`,
          authorization_endpoint: `${HUB}/oidc/auth`,
          token_endpoint: `${HUB}/oidc/token`,
          revocation_endpoint: `${HUB}/oidc/token/revocation`,
        }),
    },
    {
      match: "/v0/snipit/agentdrive-token",
      method: "POST",
      respond: async (_url, init) =>
        jsonResponse({
          schema_version: 1,
          access_token: `drive-token-${calls.length}`,
          token_type: "Bearer",
          expires_in: 300,
          access: JSON.parse(String(init.body ?? "{}")).access,
          scope: "content:read content:write sharing:write",
          resource: API,
          api_base_url: API,
          workspace_id: LOCATION.workspace_id,
        }),
    },
    {
      match: (url) => url.includes("/folders?") || url.endsWith("/folders"),
      method: "GET",
      respond: async (url) => handlers.listFolders(url),
    },
    {
      match: (url) => url.endsWith("/folders"),
      method: "POST",
      respond: async (url, init) => handlers.createFolder(url, init),
    },
    {
      match: "/artifacts",
      method: "POST",
      respond: async (url, init) => handlers.createArtifact(url, init),
    },
    {
      match: "/shares",
      method: "POST",
      respond: async (url, init) => handlers.createShare(url, init),
    },
  ]);
}

function callsTo(fragment, method) {
  return calls.filter(
    (call) => call.url.includes(fragment) && (!method || call.method === method),
  );
}

async function fieldsOf(call) {
  const form = call.init.body;
  const out = {};
  for (const [key, value] of form.entries()) out[key] = value;
  return out;
}

beforeEach(() => {
  state = installChromeMock({ installType: "development" });
  state.local[HUB_SESSION_KEY] = {
    accessToken: "hub-access-1",
    refreshToken: "hub-refresh-1",
    expiresAtMs: Date.now() + 3_600_000,
    hubBase: HUB,
  };
  handlers = defaultHandlers();
  install();
});

test("creates the artifact under a date folder with provenance metadata", async () => {
  const result = await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: true, link: "share", strip_query: false },
  });

  const [create] = callsTo("/artifacts", "POST");
  assert.equal(create.url, `${API}/v0/drives/${DRIVE}/artifacts`);
  // The DRIVE token, never the Hub session — they are different
  // credentials for different audiences and must not be interchanged.
  assert.match(create.init.headers.Authorization, /^Bearer drive-token-\d+$/);
  assert.ok(!create.init.headers.Authorization.includes("hub-access"));

  const fields = await fieldsOf(create);
  assert.equal(fields.parent_id, DATE_FOLDER);
  assert.equal(fields.content_type, "image/png");
  assert.match(fields.name, /^example-page-\d{6}\.png$/);
  // The field carrying the PNG. Renaming it would otherwise pass every
  // in-process test and fail against the real server.
  assert.ok(fields.content instanceof Blob, "content field is the blob");
  assert.equal(fields.content.size, CAPTURE.blob.size);
  // The end-to-end integrity check AgentDrive verifies when present.
  assert.match(fields.sha256, /^[0-9a-f]{64}$/);

  const metadata = JSON.parse(fields.metadata);
  assert.equal(metadata.source.url, "https://example.test/docs?tab=2");
  assert.equal(metadata.source.title, "Example Page");
  assert.equal(metadata.source.captured_at, "2026-09-05T18:22:41.000Z");
  assert.equal(metadata.capture.mode, "region");

  assert.equal(result.artifactId, ARTIFACT);
  assert.equal(result.link, `${API}/s/secret-key/`);
});

test("sends an Idempotency-Key so a retried create cannot duplicate", async () => {
  await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "none" },
  });

  const [create] = callsTo("/artifacts", "POST");
  assert.ok(create.init.headers["Idempotency-Key"]);
  assert.ok(create.init.headers["Idempotency-Key"].length >= 16);
});

test("reuses an existing date folder instead of creating a second one", async () => {
  handlers.listFolders = () =>
    jsonResponse({
      items: [{ id: DATE_FOLDER, name: "2026-09-05" }],
      next_cursor: null,
    });
  let created = 0;
  handlers.createFolder = () => {
    created += 1;
    return jsonResponse({ id: "fld_should_not_happen" }, 201);
  };

  await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: true, link: "none" },
  });

  assert.equal(created, 0);
  const fields = await fieldsOf(callsTo("/artifacts", "POST")[0]);
  assert.equal(fields.parent_id, DATE_FOLDER);
});

test("a folder-creation race resolves by re-reading, not by failing", async () => {
  // Two captures in the same second both find no date folder; the loser's
  // POST answers 409 and must then find the winner's folder.
  let listCalls = 0;
  handlers.listFolders = () => {
    listCalls += 1;
    return listCalls === 1
      ? jsonResponse({ items: [], next_cursor: null })
      : jsonResponse({
          items: [{ id: DATE_FOLDER, name: "2026-09-05" }],
          next_cursor: null,
        });
  };
  handlers.createFolder = () =>
    jsonResponse({ error: { code: "FOLDER_PATH_CONFLICT" } }, 409);

  await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: true, link: "none" },
  });

  assert.equal(listCalls, 2);
  const fields = await fieldsOf(callsTo("/artifacts", "POST")[0]);
  assert.equal(fields.parent_id, DATE_FOLDER);
});

test("group_by_date off uploads straight into the chosen folder", async () => {
  await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "none" },
  });

  assert.equal(callsTo("/folders").length, 0);
  const fields = await fieldsOf(callsTo("/artifacts", "POST")[0]);
  assert.equal(fields.parent_id, ROOT_FOLDER);
});

test("replays once on 401 with a fresh token and the SAME idempotency key", async () => {
  // The key must not change: a replay under a new key could create a second
  // artifact if the first request actually landed.
  let attempts = 0;
  handlers.createArtifact = () => {
    attempts += 1;
    return attempts === 1
      ? jsonResponse({ error: { code: "UNAUTHENTICATED" } }, 401)
      : jsonResponse({ id: ARTIFACT, name: "x.png" }, 201);
  };

  const result = await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "none" },
  });

  const creates = callsTo("/artifacts", "POST");
  assert.equal(creates.length, 2);
  assert.equal(
    creates[0].init.headers["Idempotency-Key"],
    creates[1].init.headers["Idempotency-Key"],
  );
  assert.notEqual(
    creates[0].init.headers.Authorization,
    creates[1].init.headers.Authorization,
  );
  assert.equal(result.artifactId, ARTIFACT);
});

test("gives up after a second 401 rather than looping", async () => {
  handlers.createArtifact = () =>
    jsonResponse({ error: { code: "UNAUTHENTICATED" } }, 401);

  await assert.rejects(
    () =>
      uploadCapture({
        ...CAPTURE,
        location: LOCATION,
        preferences: { group_by_date: false, link: "none" },
      }),
    (error) => {
      assert.equal(error.code, "NOT_SIGNED_IN");
      return true;
    },
  );
  assert.equal(callsTo("/artifacts", "POST").length, 2);
});

test("renames once on a name conflict, with a NEW idempotency key", async () => {
  // The opposite rule from the 401 replay: this is a genuinely different
  // request, so reusing the key would replay the failure.
  let attempts = 0;
  const names = [];
  handlers.createArtifact = async (_url, init) => {
    attempts += 1;
    names.push(init.body.get("name"));
    return attempts === 1
      ? jsonResponse({ error: { code: "ARTIFACT_PATH_CONFLICT" } }, 409)
      : jsonResponse({ id: ARTIFACT, name: names[1] }, 201);
  };

  const result = await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "none" },
  });

  assert.equal(attempts, 2);
  assert.notEqual(names[0], names[1]);
  assert.match(names[1], /-[0-9a-f]{4}\.png$/);
  const creates = callsTo("/artifacts", "POST");
  assert.notEqual(
    creates[0].init.headers["Idempotency-Key"],
    creates[1].init.headers["Idempotency-Key"],
  );
  assert.equal(result.artifactId, ARTIFACT);
});

test("an in-flight idempotency conflict waits and replays the SAME key — never duplicates", async () => {
  // AgentDrive answers 409 IDEMPOTENCY_IN_PROGRESS when our OWN earlier
  // request is still running. Renaming under a fresh key here would make a
  // SECOND artifact out of one capture — the one duplication this pipeline
  // has to prevent.
  let attempts = 0;
  const names = [];
  handlers.createArtifact = async (_url, init) => {
    attempts += 1;
    names.push(init.body.get("name"));
    return attempts === 1
      ? {
          ok: false,
          status: 409,
          headers: new Headers({ "Retry-After": "0" }),
          json: async () => ({ error: { code: "IDEMPOTENCY_IN_PROGRESS" } }),
          text: async () => "{}",
        }
      : jsonResponse({ id: ARTIFACT, name: names[1] }, 201);
  };

  const result = await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "none" },
  });

  assert.equal(result.artifactId, ARTIFACT);
  const creates = callsTo("/artifacts", "POST");
  assert.equal(creates.length, 2);
  assert.equal(
    creates[0].init.headers["Idempotency-Key"],
    creates[1].init.headers["Idempotency-Key"],
    "the key is replayed, so only one artifact can result",
  );
  assert.equal(names[0], names[1], "and the name is unchanged");
});

test("a checksum mismatch replays unchanged, then reports corruption", async () => {
  // Reachable only because the pipeline sends `sha256`. Renaming a
  // corrupted upload would file it under a new name and call it success.
  let attempts = 0;
  handlers.createArtifact = async () => {
    attempts += 1;
    return {
      ok: false,
      status: 409,
      headers: new Headers(),
      json: async () => ({ error: { code: "CHECKSUM_MISMATCH" } }),
      text: async () => "{}",
    };
  };

  await assert.rejects(
    () =>
      uploadCapture({
        ...CAPTURE,
        location: LOCATION,
        preferences: { group_by_date: false, link: "none" },
      }),
    (error) => {
      assert.equal(error.code, "CAPTURE_CORRUPTED");
      assert.match(error.message, /intact|again/i);
      return true;
    },
  );

  assert.equal(attempts, 2, "one replay, then give up");
  const creates = callsTo("/artifacts", "POST");
  assert.equal(
    creates[0].init.headers["Idempotency-Key"],
    creates[1].init.headers["Idempotency-Key"],
    "a replay, not a new request",
  );
});

test("an idempotency-key conflict is reported, not retried", async () => {
  // The key was used for a DIFFERENT request. Retrying cannot help.
  let attempts = 0;
  handlers.createArtifact = async () => {
    attempts += 1;
    return {
      ok: false,
      status: 409,
      headers: new Headers(),
      json: async () => ({
        error: { code: "IDEMPOTENCY_CONFLICT", message: "key already used" },
      }),
      text: async () => "{}",
    };
  };

  await assert.rejects(() =>
    uploadCapture({
      ...CAPTURE,
      location: LOCATION,
      preferences: { group_by_date: false, link: "none" },
    }),
  );
  assert.equal(attempts, 1);
});

test("a 403 or 404 on the target folder reports a stale location", async () => {
  for (const status of [403, 404]) {
    handlers = defaultHandlers();
    handlers.createArtifact = () =>
      jsonResponse({ error: { code: "NOT_AUTHORIZED" } }, status);
    install();

    await assert.rejects(
      () =>
        uploadCapture({
          ...CAPTURE,
          location: LOCATION,
          preferences: { group_by_date: false, link: "none" },
        }),
      (error) => {
        assert.equal(error.code, "LOCATION_UNAVAILABLE");
        assert.match(error.message, /location|folder/i);
        return true;
      },
    );
  }
});

test("refuses a capture larger than the inline ceiling before any request", async () => {
  const huge = new Blob([new Uint8Array(21 * 1024 * 1024)], {
    type: "image/png",
  });

  await assert.rejects(
    () =>
      uploadCapture({
        ...CAPTURE,
        blob: huge,
        location: LOCATION,
        preferences: { group_by_date: false, link: "none" },
      }),
    (error) => {
      assert.equal(error.code, "CAPTURE_TOO_LARGE");
      return true;
    },
  );
  assert.equal(calls.length, 0, "nothing is sent");
});

test("mints a share link and returns it", async () => {
  const result = await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "share" },
  });

  const [share] = callsTo("/shares", "POST");
  assert.equal(share.url, `${API}/v0/drives/${DRIVE}/shares`);
  assert.deepEqual(JSON.parse(share.init.body), {
    resource_type: "artifact",
    resource_id: ARTIFACT,
  });
  assert.equal(result.link, `${API}/s/secret-key/`);
  // No share id comes back: the extension owns no revoke control, so
  // carrying one would be a field with no reader.
  assert.ok(!("shareId" in result), "no shareId in the result");
});

test("sends expires_at only when a link expiry is configured", async () => {
  await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "share", link_expiry_days: 30 },
  });

  const body = JSON.parse(callsTo("/shares", "POST")[0].init.body);
  assert.ok(body.expires_at);
  const expires = new Date(body.expires_at);
  const days = (expires.getTime() - CAPTURE.now.getTime()) / 86_400_000;
  assert.ok(Math.abs(days - 30) < 0.01, `expected ~30 days, got ${days}`);
});

test("link=console returns the console URL and mints no share", async () => {
  const result = await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "console" },
  });

  assert.equal(callsTo("/shares").length, 0);
  assert.equal(
    result.link,
    `${ENVIRONMENTS.staging.consoleBase}/drive/${DRIVE}/a/${ARTIFACT}/`,
  );
});

test("link=none returns no link and mints no share", async () => {
  const result = await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "none" },
  });

  assert.equal(callsTo("/shares").length, 0);
  assert.equal(result.link, null);
});

test("sends the sha256 of exactly the bytes uploaded", async () => {
  // Wrong bytes here would make AgentDrive reject every capture, so the
  // digest is checked against an independent computation, not the code's.
  const expected = Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", await CAPTURE.blob.arrayBuffer()),
    ),
  )
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");

  await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "none" },
  });

  const fields = await fieldsOf(callsTo("/artifacts", "POST")[0]);
  assert.equal(fields.sha256, expected);
});

test("a share refused for lack of a drive grant says what to do about it", async () => {
  // `shares_create` needs a drive-level MANAGER grant, not just the
  // `sharing:write` scope the token carries. The generic 403 message talks
  // about the saved location, which is neither the cause nor the fix.
  handlers.createShare = () =>
    jsonResponse({ error: { code: "NOT_AUTHORIZED" } }, 403);

  const result = await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "share" },
  });

  assert.equal(result.artifactId, ARTIFACT, "the capture is still saved");
  assert.equal(result.link, null);
  assert.match(result.linkError, /share links/i);
  assert.match(result.linkError, /console link|drive manager/i);
  assert.ok(
    !/choose a new location/i.test(result.linkError),
    "must not send the reader to change a location that is fine",
  );
});

test("a failed share does not lose the capture", async () => {
  // The artifact is already saved; failing the whole capture over the
  // clipboard step would tell the person their screenshot was lost.
  handlers.createShare = () => jsonResponse({ error: "nope" }, 500);

  const result = await uploadCapture({
    ...CAPTURE,
    location: LOCATION,
    preferences: { group_by_date: false, link: "share" },
  });

  assert.equal(result.artifactId, ARTIFACT);
  assert.equal(result.link, null);
  assert.ok(result.linkError);
});

test("strip_query is honoured end to end", async () => {
  await uploadCapture({
    ...CAPTURE,
    pageUrl: "https://example.test/dashboard?token=secret",
    location: LOCATION,
    preferences: { group_by_date: false, link: "none", strip_query: true },
  });

  const fields = await fieldsOf(callsTo("/artifacts", "POST")[0]);
  assert.ok(!fields.metadata.includes("secret"));
  assert.equal(JSON.parse(fields.metadata).source.url_redacted, true);
});
