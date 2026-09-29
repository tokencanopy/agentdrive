# AgentDrive SnipIt — Chrome Extension

The Manifest V3 Chrome extension that captures a screenshot straight into
your AgentDrive, recording the page it came from.

**Sign-in is Token Canopy Hub, not AgentDrive.** The original flow redeemed
a ticket at AgentDrive's own `/auth/extension/*` endpoints; AgentDrive
stopped being an authorization server at the v0 contract reset, and those
routes are archived. The extension is now an OAuth public client of Hub
(PKCE S256 via `chrome.identity.launchWebAuthFlow`) and exchanges that Hub
session for a short-lived, audience-bound AgentDrive token at
`POST /v0/snipit/agentdrive-token`. Design:
`docs/superpowers/specs/2026-09-05-snipit-hub-auth-and-capture-provenance-design.md`
in the tokencanopy repo.

## Run locally

1. **Load the extension unpacked.** In Chrome:
   * Navigate to `chrome://extensions`.
   * Toggle **Developer mode** (top right).
   * Click **Load unpacked** and select `extensions/snipit/`.
   * Confirm the ID reads `kpdpkhkhinihhehlakjbdlloagcmpkok`. Both manifests
     carry the store item's public key precisely so it does; a different ID
     means the key is missing and sign-in will refuse before it starts.
2. **Sign in.** An unpacked build talks to **staging** by default, so no
   local stack is needed. Click the SnipIt icon → **Sign in**. If you are
   already signed in to Token Canopy in that browser, Hub's silent-SSO
   branch means you only confirm consent.
3. **Choose where captures go.** The popup asks on first run. SnipIt
   proposes a `Screenshots` folder only when you have exactly one workspace
   containing exactly one drive — with more than one of either there is no
   safe guess, so it asks instead.
4. **Capture.** **Capture region** or **Capture tab**. The PNG uploads to
   the folder you chose, the configured link lands on your clipboard, and
   the page's address and title are recorded on the artifact.

### Pointing a dev build at a local stack

Settings → **Developer** (unpacked builds only) takes a Hub, AgentDrive,
console and share origin, and every request follows them. A **packaged**
build ignores the override entirely and always uses production — extension
storage is not a trust boundary, so a store install must not be
re-pointable by anything that can write to it.

The local ports the override may name are fixed by the dev manifest's host
permissions (`localhost:8080` Hub, `localhost:8765` AgentDrive,
`localhost:3000` console), because hosts are the one thing Chrome will not
let a build change at runtime. Run AgentDrive on 8765 with
`PORT=8765 make dev`. Whatever Hub you point at must register the same
`snipit-chrome-…` OAuth client and mint for the audience your local
AgentDrive verifies.

## Layout

```
extensions/snipit/
├── manifest.json            (MV3, dev — staging + localhost origins)
├── manifest.prod.json       (MV3, store — production origins only)
├── icons/                   (16/48/128 PNGs)
└── src/
    ├── background/          (service worker — capture, upload, clipboard)
    ├── popup/               (UI shell: signed-out / no-location / busy / idle)
    ├── options/             (settings: location picker, preferences, dev endpoints)
    ├── overlay/             (region-select content script)
    ├── offscreen/           (clipboard + SW keep-alive)
    └── lib/
        ├── config.js        (every origin, and the dev override)
        ├── hub-auth.js      (Hub PKCE sign-in, refresh, sign-out)
        ├── drive-token.js   (the short-lived AgentDrive token, cached)
        ├── drive-api.js     (the v0 calls)
        ├── upload.js        (capture → artifact → link, and the retry rules)
        ├── provenance.js    (what gets recorded about the source page)
        ├── settings.js      (the saved location and preferences)
        ├── first-run.js     (when a location proposal is safe)
        ├── path.js          (naming)
        └── pkce.js
```

### Two credentials, and two access levels

The drive token comes in two flavours the extension asks for by name, and
they are cached separately because neither is a superset of the other:

| | `browse` | `capture` |
|---|---|---|
| used by | the settings picker | the capture pipeline |
| scopes | `drives:read content:read` | `content:read content:write sharing:write` |

So a capture cannot enumerate the workspace's drives, and browsing cannot
write or mint a public link. The picker asks for the write token only at the
one point it creates a folder.

### Two credentials, deliberately

The **Hub session** (`chrome.storage.local`) is an identity: long-lived,
survives a browser restart, never sent to AgentDrive. The **drive token**
(`chrome.storage.session`) is product authority: five minutes, one
workspace, four scopes, and gone when the browser closes. Neither is ever
presented where the other belongs.

## Production build

For v0, the unpacked directory IS the build — there's no Vite/webpack
step. Two manifests live in the directory:

* `manifest.json` — the **dev** manifest used by Chrome's *Load unpacked*
  flow. Includes `http://localhost:8765/*` in `host_permissions` and the
  CSP `connect-src` so the extension can talk to the backend running on
  your machine.
* `manifest.prod.json` — the **publish-ready** manifest. Identical to the
  dev one except for which origins it may reach: production only, with the
  staging hosts and the three localhost ports removed from both
  `host_permissions` and the CSP `connect-src`. `tests/manifest.test.js`
  asserts that every other field matches, and that production mentions no
  staging or localhost origin at all.
  This is what Chrome Web Store reviewers see; the privacy policy at
  `https://tokencanopy.com/privacy` describes only the hosts in
  `manifest.prod.json`.

To publish to the Chrome Web Store, swap the manifests at zip time:

```
cd extensions/snipit
cp manifest.prod.json /tmp/manifest.json
zip -r ../snipit-v0.4.0.zip . \
    -x "tests/*" "README.md" "manifest.json" "manifest.prod.json" "*.DS_Store"
zip -j ../snipit-v0.4.0.zip /tmp/manifest.json
```

(The `-j` flag drops directory info so `/tmp/manifest.json` lands as
`manifest.json` at the zip root, where Chrome expects it.)

## Tests

Node-based tests live in `tests/`: module tests for `src/lib/` in
`tests/lib/`, service-worker state-machine tests (the real
`background.js` driven through a mocked `chrome.*` + `fetch`) in
`tests/background/`, popup render/poll tests in `tests/popup/`, and
manifest-parity tests in `tests/manifest.test.js`.

What they pin, beyond the happy path: the BUSY/PHASE capture-pipeline
lifecycle; the storage.session quota regression ("Capture tab stopped
working"); a unicode title surviving into the artifact's metadata; that the
PKCE verifier never rides the authorize URL; that a rotating refresh token
is spent exactly once under concurrency; that a packaged build ignores the
endpoint override; and that the retry rules hold — a 401 replays under the
SAME idempotency key, a name conflict retries under a NEW one. Run with:

```
node --test "tests/**/*.test.js"
```

CI runs the same suite in `_drive-suite.yml`.

End-to-end verification happens manually against staging (see "Run
locally" above) — the suites mock `chrome.*` and `fetch`, so nothing in
them exercises a real browser, a real Hub, or a real upload.

## Permissions

What each permission in `manifest.prod.json` is for.

| Permission | Why |
|---|---|
| `activeTab` | Capture the tab the user clicked SnipIt on. Granted per click; no host permission for arbitrary pages. |
| `identity` | The Hub OAuth sign-in flow (`launchWebAuthFlow`). |
| `notifications` | Report a capture's success or failure when the popup is closed. |
| `offscreen` | Write the link to the clipboard; a service worker cannot. |
| `scripting` | Inject the region-select overlay on the active tab. |
| `storage` | Remember the sign-in and the chosen save location. |
| `auth.tokencanopy.com` | Sign in and mint the AgentDrive token. |
| `drive.tokencanopy.com` | Upload the capture. |
| `app` / `share.tokencanopy.com` | Open or link to the saved capture. |


### Over-the-wire verification

`tests/e2e/upload-live.mjs` runs the real upload pipeline against a real
local HTTP server that parses the multipart body — proving the encoding, the
PNG bytes, the headers, and the 401 replay's idempotency key, none of which
a mocked `fetch` can prove. It needs no Docker and no credentials:

```
node tests/e2e/upload-live.mjs
```

The Hub half has its own live pass in the tokencanopy repo:
`apps/hub/test/e2e/snipit-live.ts` boots hub on a real port and drives the
whole OAuth flow plus both `/v0/snipit/*` routes with `fetch`.
