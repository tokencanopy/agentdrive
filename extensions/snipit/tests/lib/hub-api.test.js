// The workspace listing behind the picker's first level.
//
// This decides which workspaces somebody can choose to save screenshots
// into, so a malformed response must not produce a half-rendered choice —
// and it must not fail the whole page over one bad row either.

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
const { listWorkspaces } = await import("../../src/lib/hub-api.js");

const HUB = ENVIRONMENTS.staging.hubBase;

let state;
let calls;
let responder;

function install() {
  calls = installFetchMock([
    {
      match: "/v0/snipit/workspaces",
      method: "GET",
      respond: async (url) => responder(url),
    },
  ]);
}

beforeEach(() => {
  state = installChromeMock({ installType: "development" });
  state.local[HUB_SESSION_KEY] = {
    accessToken: "hub-access-1",
    refreshToken: "hub-refresh-1",
    expiresAtMs: Date.now() + 3_600_000,
    hubBase: HUB,
  };
  responder = async () =>
    jsonResponse({
      items: [
        { id: "tcws_1", display_name: "Acme", role: "owner" },
        { id: "tcws_2", display_name: "Shared", role: "member" },
      ],
      next_cursor: null,
    });
  install();
});

test("lists workspaces with the Hub bearer", async () => {
  const page = await listWorkspaces();

  assert.deepEqual(page.items, [
    { id: "tcws_1", display_name: "Acme", role: "owner" },
    { id: "tcws_2", display_name: "Shared", role: "member" },
  ]);
  assert.equal(page.nextCursor, null);
  assert.equal(calls[0].init.headers.Authorization, "Bearer hub-access-1");
  assert.ok(calls[0].url.startsWith(`${HUB}/v0/snipit/workspaces?`));
});

test("percent-encodes a cursor rather than pasting it into the query", async () => {
  responder = async () => jsonResponse({ items: [], next_cursor: null });

  await listWorkspaces({ limit: 1, cursor: "a b+c/d=" });

  const query = new URL(calls[0].url).searchParams;
  assert.equal(query.get("cursor"), "a b+c/d=");
  assert.equal(query.get("limit"), "1");
});

test("drops a malformed row instead of failing the whole picker", async () => {
  // A picker showing three of four workspaces is usable; one showing an
  // error because a display name came back as a number is not.
  responder = async () =>
    jsonResponse({
      items: [
        { id: "tcws_1", display_name: "Acme", role: "owner" },
        { id: "tcws_2", display_name: 42, role: "member" },
        { id: "", display_name: "Empty id", role: "member" },
        { display_name: "No id", role: "member" },
        null,
        "not an object",
        [],
      ],
      next_cursor: null,
    });

  const page = await listWorkspaces();

  assert.deepEqual(page.items, [
    { id: "tcws_1", display_name: "Acme", role: "owner" },
  ]);
});

test("keeps only the three fields the picker reads", async () => {
  // `kind` is a migration artifact; anything hub adds later has no reader
  // here and must not silently become part of what the picker branches on.
  responder = async () =>
    jsonResponse({
      items: [
        {
          id: "tcws_1",
          display_name: "Acme",
          role: "owner",
          kind: "personal",
          secret: "should not survive",
        },
      ],
      next_cursor: null,
    });

  const [item] = (await listWorkspaces()).items;

  assert.deepEqual(Object.keys(item).sort(), ["display_name", "id", "role"]);
});

test("treats a non-array items field as an empty page", async () => {
  for (const items of [null, undefined, "nope", {}, 42]) {
    responder = async () => jsonResponse({ items, next_cursor: null });
    assert.deepEqual((await listWorkspaces()).items, []);
  }
});

test("reports a lost sign-in distinctly from an outage", async () => {
  responder = async () => jsonResponse({ error: "unauthenticated" }, 401);
  await assert.rejects(() => listWorkspaces(), (error) => {
    assert.equal(error.code, "NOT_SIGNED_IN");
    return true;
  });

  responder = async () => jsonResponse({ error: "boom" }, 503);
  await assert.rejects(() => listWorkspaces(), (error) => {
    assert.equal(error.code, "HUB_UNAVAILABLE");
    assert.equal(error.status, 503);
    return true;
  });
});

test("a dropped connection is an outage, not a lost sign-in", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };

  await assert.rejects(() => listWorkspaces(), (error) => {
    assert.equal(error.code, "HUB_UNAVAILABLE");
    return true;
  });
});

test("a body that is not JSON is an empty page, not a crash", async () => {
  responder = async () => ({
    ok: true,
    status: 200,
    json: async () => {
      throw new SyntaxError("not json");
    },
    text: async () => "<html>",
  });

  assert.deepEqual((await listWorkspaces()).items, []);
});
