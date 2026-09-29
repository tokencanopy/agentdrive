// Endpoint resolution.
//
// One resolver decides every origin the extension talks to, so a developer
// can point an unpacked build at a local Hub + Drive stack without editing
// a file — and so a PACKAGED build cannot be re-pointed by anything that
// can write extension storage.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

import { installChromeMock } from "../support/chrome-mock.js";

installChromeMock();

const {
  ENVIRONMENTS,
  getEndpoints,
  readEndpointOverride,
  setEndpointOverride,
  clearEndpointOverride,
  validateEndpoints,
  ENDPOINT_OVERRIDE_KEY,
} = await import("../../src/lib/config.js");

let state;
beforeEach(() => {
  state = installChromeMock();
});

test("a packaged install resolves production", async () => {
  state.installType = "normal";

  const endpoints = await getEndpoints();

  assert.deepEqual(endpoints, {
    ...ENVIRONMENTS.production,
    env: "production",
  });
  assert.equal(endpoints.hubBase, "https://auth.tokencanopy.com");
  assert.equal(endpoints.apiBase, "https://drive.tokencanopy.com");
});

test("an unpacked install resolves staging", async () => {
  state.installType = "development";

  const endpoints = await getEndpoints();

  assert.equal(endpoints.env, "staging");
  assert.deepEqual(endpoints, { ...ENVIRONMENTS.staging, env: "staging" });
});

test("an unpacked install honours a stored override", async () => {
  state.installType = "development";
  const local = {
    hubBase: "http://localhost:8080",
    apiBase: "http://localhost:8765",
    consoleBase: "http://localhost:3000",
    shareBase: "http://localhost:8765",
  };
  await setEndpointOverride(local);

  const endpoints = await getEndpoints();

  assert.deepEqual(endpoints, { ...local, env: "custom" });
});

test("a PACKAGED install ignores an override entirely", async () => {
  // The load-bearing case: extension storage is not a trust boundary, so a
  // store build must resolve production no matter what is written there.
  state.installType = "normal";
  state.local[ENDPOINT_OVERRIDE_KEY] = {
    hubBase: "https://attacker.example.test",
    apiBase: "https://attacker.example.test",
    consoleBase: "https://attacker.example.test",
    shareBase: "https://attacker.example.test",
  };

  const endpoints = await getEndpoints();

  assert.equal(endpoints.env, "production");
  assert.equal(endpoints.hubBase, ENVIRONMENTS.production.hubBase);
});

test("setEndpointOverride refuses to write from a packaged install", async () => {
  state.installType = "normal";

  await assert.rejects(
    () =>
      setEndpointOverride({
        hubBase: "http://localhost:8080",
        apiBase: "http://localhost:8765",
        consoleBase: "http://localhost:3000",
        shareBase: "http://localhost:8765",
      }),
    /packaged/i,
  );
  assert.equal(state.local[ENDPOINT_OVERRIDE_KEY], undefined);
});

test("clearEndpointOverride returns an unpacked install to staging", async () => {
  state.installType = "development";
  await setEndpointOverride({
    hubBase: "http://localhost:8080",
    apiBase: "http://localhost:8765",
    consoleBase: "http://localhost:3000",
    shareBase: "http://localhost:8765",
  });

  await clearEndpointOverride();

  assert.equal((await getEndpoints()).env, "staging");
  assert.equal(await readEndpointOverride(), null);
});

test("a stored override that no longer validates is ignored, not obeyed", async () => {
  // Storage can be edited by hand or left over from an older schema. A bad
  // value must fall back to the environment default rather than produce a
  // half-configured client that posts a token somewhere unintended.
  state.installType = "development";
  state.local[ENDPOINT_OVERRIDE_KEY] = {
    hubBase: "http://localhost:8080",
    apiBase: "not a url",
    consoleBase: "http://localhost:3000",
    shareBase: "http://localhost:8765",
  };

  assert.equal((await getEndpoints()).env, "staging");
});

test("validateEndpoints accepts only absolute http(s) origins", () => {
  const good = {
    hubBase: "http://localhost:8080",
    apiBase: "https://drive.example.test",
    consoleBase: "http://127.0.0.1:3000",
    shareBase: "https://share.example.test",
  };
  assert.deepEqual(validateEndpoints(good), good);

  const rejected = [
    { ...good, hubBase: "ftp://localhost:8080" },
    { ...good, hubBase: "javascript:alert(1)" },
    { ...good, hubBase: "http://user:pass@localhost:8080" },
    { ...good, hubBase: "http://localhost:8080/oidc" },
    { ...good, hubBase: "http://localhost:8080#frag" },
    { ...good, hubBase: "" },
    { ...good, hubBase: null },
    { ...good, hubBase: undefined },
    { hubBase: good.hubBase },
    null,
    "http://localhost:8080",
  ];
  for (const value of rejected) {
    assert.equal(
      validateEndpoints(value),
      null,
      `should reject ${JSON.stringify(value)}`,
    );
  }
});

test("validateEndpoints normalises a trailing slash away", () => {
  // A base joined as `${base}/v0/...` would otherwise produce a double
  // slash, which some routers treat as a different path.
  const validated = validateEndpoints({
    hubBase: "http://localhost:8080/",
    apiBase: "http://localhost:8765/",
    consoleBase: "http://localhost:3000/",
    shareBase: "http://localhost:8765/",
  });

  assert.equal(validated.hubBase, "http://localhost:8080");
  assert.equal(validated.apiBase, "http://localhost:8765");
});

test("every shipped environment is itself valid", () => {
  // Guards the constants against a typo that only a real sign-in would
  // otherwise reveal.
  for (const [name, endpoints] of Object.entries(ENVIRONMENTS)) {
    assert.ok(validateEndpoints(endpoints), `${name} must validate`);
  }
});
