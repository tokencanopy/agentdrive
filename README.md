# AgentDrive (v0 / beta)

The artifact handoff layer for AI agents. Upload an artifact via API, get a public URL with a styled rendered view.

A **drive** is a workspace's space for artifacts, identified by a `drv_…` id, with folders (`fld_…`) and artifacts (`art_…`). Access is governed by **grants** (viewer/editor/manager) per principal, and artifacts can be shared by **possession-based share links** (`/s/{share_key}`).

This repo implements the **v0 API surface**: 51 production-beta operations
over `/v0`, plus eight sheet edit-session operations enabled in staging —
drives, folders, artifacts (multipart inline upload), namespace entries/path
lookup, immutable versions, grants, share links, drive-scoped search, and a
change feed — against local Postgres + GCS emulator.

## Self-host in ten minutes

One machine with Docker, and nothing else: Postgres and the AgentDrive image,
artifact bytes on a local volume, API keys minted by the CLI. This is the
`compose.selfhost.yml` the smoke script (`scripts/selfhost-smoke.sh`) runs
verbatim.

```bash
git clone https://github.com/tokencanopy/agentdrive && cd agentdrive
grep -q '^AGENTDRIVE_SESSION_SECRET=' .env 2>/dev/null || echo "AGENTDRIVE_SESSION_SECRET=$(openssl rand -hex 32)" >> .env
docker compose -f compose.selfhost.yml up -d
docker compose -f compose.selfhost.yml exec api python -m agentdrive.keys init
docker compose -f compose.selfhost.yml exec api \
  python -m agentdrive.keys create --subject-type agent --name claude-code --scopes all
# paste the printed adk_… key as the Bearer of the MCP server at http://localhost:8080/mcp
```

The first `up` builds the image (a few minutes; Node and Python stages), then
starts Postgres, runs the migrations once, and starts the API on
`http://localhost:8080`. `init` mints the workspace's owner subject once;
`create` prints the key exactly once — treat it as a password, and prefer
`read -rs KEY` over pasting it into a command line you keep in shell history
(`list` and `revoke ID` manage what was issued; scopes are fixed at creation,
so changing them is revoke and reissue).

Knobs, all in `.env`, which every `docker compose` command in the directory
reads:

- `AGENTDRIVE_SESSION_SECRET` (required, at least 32 characters) seals the
  paginated list cursors. Rotating it invalidates in-flight cursors, nothing
  else.
- `AGENTDRIVE_PORT` (default 8080). The share links and the MCP discovery
  document follow it automatically.
- `AGENTDRIVE_BIND` (default `127.0.0.1`): the API listens on loopback only.
  Set `0.0.0.0` to reach it from other machines, set
  `AGENTDRIVE_PUBLIC_BASE_URL` to the URL they will use, and put TLS in
  front — the API key crosses in the clear otherwise.
- `AGENTDRIVE_DB_PASSWORD` (default `agentdrive`). Postgres is not published
  on the host, so the default is safe until you attach other containers to
  the project's network; change it then.
- `COMPOSE_PROJECT_NAME` for a second install on the same machine; each
  project gets its own containers and volumes.

Data lives in the `agentdrive_pgdata` and `agentdrive_data` volumes.
**Upgrade** with `git pull && docker compose -f compose.selfhost.yml up -d --build`
(without `--build`, `up` keeps running the old image). `docker compose -f
compose.selfhost.yml down -v` deletes everything.

The same key on the REST API — create a drive, upload a file, share it:

```bash
read -rs KEY   # paste the adk_… key from `create` above
curl -s http://localhost:8080/v0/drives -H "Authorization: Bearer $KEY" \
  -H "Idempotency-Key: $(openssl rand -hex 16)" -H "Content-Type: application/json" \
  -d '{"name":"My drive"}'
# → {"id":"drv_…","root_folder_id":"fld_…",…}
echo '# Hello from a self-hosted AgentDrive' > hello.md
curl -s http://localhost:8080/v0/drives/drv_…/artifacts -H "Authorization: Bearer $KEY" \
  -H "Idempotency-Key: $(openssl rand -hex 16)" \
  -F parent_id=fld_… -F name=hello.md -F "content=@hello.md;type=text/markdown"
# → {"id":"art_…",…}
curl -s http://localhost:8080/v0/drives/drv_…/shares -H "Authorization: Bearer $KEY" \
  -H "Idempotency-Key: $(openssl rand -hex 16)" -H "Content-Type: application/json" \
  -d '{"resource_type":"artifact","resource_id":"art_…"}'
# → {"id":"shr_…","secret":"…","url":"http://localhost:8080/s/…/"}
```

Open that `url` in a browser: the Markdown renders, styled, with no login.
(`secret` and `url` are returned only on first creation; the link stays
valid until you revoke the share.)

And as an MCP server, with the key as a static bearer (a self-hosted install
has no OAuth server to discover, and both its discovery documents say so):

```bash
# Claude Code (verified with 2.1)
claude mcp add --transport http --scope user agentdrive http://localhost:8080/mcp \
  --header "Authorization: Bearer $KEY"

# Codex (0.155 accepts this form; the key is read from an environment variable)
export AGENTDRIVE_API_KEY="$KEY"
codex mcp add agentdrive --url http://localhost:8080/mcp --bearer-token-env-var AGENTDRIVE_API_KEY
```

```json
// Cursor — .cursor/mcp.json (per Cursor's MCP documentation; not verified here)
{ "mcpServers": { "agentdrive": {
    "url": "http://localhost:8080/mcp",
    "headers": { "Authorization": "Bearer adk_…" } } } }
```

An MCP client needs a key with at least `drives:read` and `usage:read`;
`--scopes all` is the simple choice. Each tool declares the scopes it needs,
so `tools/list` offers only the tools the key can use, and a key with too few
scopes yields a server that offers no tools at all. Every call is authorized
again by the API. Object storage is the local volume in this file; the
hosted product runs the same image on Google Cloud Storage
(`STORAGE_BACKEND=gcs`), and an S3 backend is the first follow-on after the
public release.

## The hosted product

The canonical machine API origin is **`https://drive.tokencanopy.com`**;
the served OpenAPI and protected-resource metadata advertise that product-
scoped origin. `api.agentdrive.run` and the rest of `agentdrive.run` are
compatibility-only aliases during their retirement window, not defaults for
new clients. The human console at `app.tokencanopy.com/drive` is Token
Canopy's static app served by Hub: browser control calls are named same-origin
Hub BFF operations, not an AgentDrive-served UI or `/drive*` edge route.

Public permalinks use a trusted share-host metadata shell; artifact-authored
output renders on an isolated content origin on a separate registrable domain.
The private viewer implementation exists but is disabled in the hosted
deployment until its own review and evidence gates pass.

## Local dev

Artifact bytes live in an object store chosen by `STORAGE_BACKEND`: `gcs`
(the hosted product's; locally the fake-gcs emulator below stands in) or
`fs`, a directory on this host set by `STORAGE_FS_ROOT` — no emulator, no
cloud credential. Both satisfy the same `agentdrive.storage` contract
(`tests/storage/test_contract.py` runs it against both); the
browser-initiated direct-transfer surface is GCS-only.

What a filesystem root is, so it can be operated safely: its identity is a
marker file inside it (`.agentdrive-store`), so the directory can be renamed
or moved and every version row still reads; the per-key locks under
`.locks/` serialize the API and the GC job on one host, and a network
filesystem that does not honour `flock` is refused at boot; one host per
root, and no two roots should share a marker. Run the suite against it with
`STORAGE_BACKEND=fs STORAGE_FS_ROOT=/tmp/agentdrive-store uv run pytest`
(each worker gets its own root); the GCS-only modules skip by backend.


Prereqs: Docker, Python 3.12+, [`uv`](https://docs.astral.sh/uv/).

**One command** brings up the whole stack — Postgres + GCS emulator, schema, and the app:

```bash
make dev                 # → http://localhost:8000  (PORT=8765 make dev to change)
```

Ctrl-C stops the app; the containers keep running (`docker
compose down` to stop them, add `-v` to wipe the dev DB).

<details><summary>…or run the pieces by hand</summary>

```bash
# 1. Start local Postgres + GCS emulator
docker compose up -d
# Older dev DB from before a schema change? reset: docker compose down -v && docker compose up -d
# (the schema is applied by step 2's apply_schema, not by `compose up`)

# 2. Deps + env + schema
uv sync
cp .env.example .env
python -c 'import secrets; print("SESSION_SECRET=" + secrets.token_urlsafe(48))' >> .env
uv run python -m agentdrive.scripts.apply_schema

# 3. The app
uv run uvicorn agentdrive.app:app --reload --port 8000
```

</details>

## Auth: getting a bearer token

Every `/v0` request needs a Hub-issued bearer token (Token Canopy Hub is the
only authorization server; this app only validates). The discovery document at
`/.well-known/oauth-protected-resource` names the Hub issuer, and every 401
carries a `WWW-Authenticate` challenge pointing at it.

- **Against a deployed environment:** agents and other non-cookie clients
  obtain an audience-bound product access token from Hub with OAuth client
  credentials and send it as `Authorization: Bearer …`. The human console
  does not receive this general token: its browser calls named Hub BFF
  operations with the Hub session cookie, and Hub delegates upstream
  server-side.
- **Self-hosted / local (`AUTH_MODE=local`):** the installation mints its own
  **opaque API keys** — there is no issuer, no signing key and no OAuth flow.
  Set `AUTH_MODE=local` and run

  ```bash
  python -m agentdrive.keys init                      # the workspace's owner subject, once
  python -m agentdrive.keys create --subject-type agent --name claude-code
  ```

  `create` prints `adk_…` **exactly once** — treat it as a password; only its
  sha256 is stored, so a lost key is reissued, never recovered. **One key
  works on every surface:** the `/v0` REST API, the SDKs and the MCP
  transport all take the same `Authorization: Bearer adk_…`. (The hosted
  product's `/v0`-versus-`/mcp` audience split exists because an MCP session
  token comes from an OAuth consent flow with its own resource; an operator
  who minted a key on their own box *is* the principal, so there is nothing
  to keep apart.)

  `--scopes` takes `all` or a subset (`--scopes drives:read,content:read`),
  and **a key's scopes are fixed at creation** — there is no command to widen
  one, so a leaked key cannot be escalated by whoever leaked it. Changing
  what a client may do is `revoke` plus `create`. `--expires 90d` is optional
  and keys do not expire by default. `python -m agentdrive.keys list` shows
  each key's display id (`adk_k7Qm2xZp…`), name, subject, scopes, expiry and
  revocation — never the key — and `revoke <id>` refuses it from the next
  request. `--subject-type user [--role owner|admin|member]` mints a person's
  key; an agent's carries the `init` owner as its sponsor, which is what lets
  an agent-created drive grant its sponsor and gives an agent-only install a
  human principal that can administer every drive.

  **Rotating a key keeps the identity.** `create` mints a new principal by
  default. To replace a leaked key — or to change what a client may do, which
  is always revoke-plus-create because scopes are fixed — pass
  `--subject <the subject `list` shows>`: the key is new, the subject is the
  same, and every per-drive grant naming it survives. Minting a fresh subject
  instead would quietly orphan those grants.

  The discovery document lists no authorization server in this mode (there is
  no OAuth flow to discover) and there is no `/jwks`: nothing here signs
  anything. Resolution is a row lookup per request, so a Postgres outage is a
  `503 AUTH_UNAVAILABLE` with `Retry-After`, never a fail-open; and a missing
  `local_api_keys` table — migrations never run — is a boot error rather than
  a process that answers 503 forever with a green `/health`.

  Local mode needs no origin for authentication, but **`PUBLIC_BASE_URL` still
  decides what a share link says**. It defaults to `http://localhost:8000`,
  which is right on a laptop and wrong on anything other people reach: set it
  to the origin clients will actually use before handing out a public link.
- **Local dev against Hub:** the test
  suite mints Hub-shaped RS256 tokens against a fake JWKS
  (`tests/conftest.py`, `_FakeJwks` — sign with a local RSA key, point
  `HUB_ISSUER`/JWKS at it); the in-process conformance tests
  (`tests/conformance/test_v0_smoke.py`) are the working end-to-end example
  and the fastest way to exercise the full flow locally.

## Try it

The spec is served at `/openapi.json` (Swagger UI at `/docs`).
Every mutation requires an `Idempotency-Key`; mutations of existing state also
require `If-Match` with the resource's current `ETag` (a quoted `rev_…`).

```bash
export TOKEN="<hub bearer>"

# Create a drive (your workspace's artifact store)
curl -X POST http://localhost:8000/v0/drives \
     -H "Authorization: Bearer $TOKEN" \
     -H "Idempotency-Key: create-drive-1" \
     -H "Content-Type: application/json" \
     -d '{"name": "My Drive"}'
# → 201, returns the drive with its root_folder_id (a fld_… id)

# Upload a markdown artifact into the root folder (multipart inline upload)
curl -X POST http://localhost:8000/v0/drives/DRV_ID/artifacts \
     -H "Authorization: Bearer $TOKEN" \
     -H "Idempotency-Key: create-art-1" \
     -F parent_id=ROOT_FOLDER_ID \
     -F name=hello.md \
     -F content=@- <<'EOF'
# Hello AgentDrive

Some **markdown** content.
EOF
# → 201, returns the artifact (art_… id, revision, head_version_id)

# Read it back
curl http://localhost:8000/v0/drives/DRV_ID/artifacts/ART_ID \
     -H "Authorization: Bearer $TOKEN"

# Download its bytes
curl http://localhost:8000/v0/drives/DRV_ID/artifacts/ART_ID/content \
     -H "Authorization: Bearer $TOKEN" -o hello.md

# Search the drive (lexical, grant-filtered)
curl 'http://localhost:8000/v0/drives/DRV_ID/search?q=markdown' \
     -H "Authorization: Bearer $TOKEN"

# Pull the change feed from the beginning
curl 'http://localhost:8000/v0/drives/DRV_ID/changes?start=beginning' \
     -H "Authorization: Bearer $TOKEN"
```

OpenAPI docs at <http://localhost:8000/docs>.

The Python SDK architecture is documented at
<http://localhost:8000/sdk/python> and in `docs/agentdriveSDK.md`. It is a
rebuild of the existing `tokencanopy/agentdrive-sdk` pipeline (which already
holds the `agentdrive-sdk` name on PyPI): Phase 1 generates a single async
core from the committed OpenAPI snapshot; Phase 2 adds an ergonomic facade —
including the sync bridge — over the active authenticated `/v0` operations.
Production serves 51; staging serves the 59-operation catalog while sheet
sessions remain under evaluation. The exact API reference will be generated with
the package in that repository. `agentdrive-sdk` 0.0.3 is published on PyPI
and npm as of 2026-08-28; the TypeScript facade is the client boundary the
hosted MCP runs on, and the Python facade is not yet a supported release, so
integrate from Python against the REST API and served OpenAPI directly.

## API surface

The complete catalog is the 59-op manifest in
`src/agentdrive/api/v0-operations.json`. `SHEET_SESSIONS_ENABLED=false` removes
its eight explicitly gated sheet-session entries from routing, the active
manifest, and served OpenAPI, leaving the 51-operation production contract.
Staging sets the flag true. Operation groups include:

- **Drives** (7): list, create, read, patch (rename/metadata), soft-delete,
  restore, usage.
- **Folders** (7): list, create, read, patch (rename/move/inheritance),
  recursive soft-delete, restore, subtree copy (same-drive).
- **Artifacts** (8): list, create (multipart inline upload), read, patch
  (name/move/metadata/labels), soft-delete, restore, content (stream or 307
  signed URL), copy (same-drive).
- **Versions** (5): list, append (multipart), read, content, restore-as-head.
- **Grants** (5): list, create, read, patch (role/expiry), revoke — manager-
  gated, with break-glass recovery while a drive has no active manager.
- **Shares** (5): list, create (returns the plaintext secret once), read,
  rotate, revoke; redemption at `/s/{share_key}/` is possession-based and
  server-rendered (see the public read surface below).
- **Search** (1): drive-scoped lexical full-text over `search_tsv`.
- **Changes** (1): dense per-drive change feed (sealed cursors, `start=now|
  beginning`, 410 on expired cursors).

Conventions: opaque prefixed ids (`drv_/fld_/art_/ver_/grn_/shr_/rev_/chg_/
cset_`), RFC3339-UTC timestamps, top-level `{"error":{code,message,details}}`
error envelope, sealed cursor pagination on every list, ETag/If-Match for
optimistic concurrency, Idempotency-Key on every mutation.

## Authorization model

Every operation is the intersection of the **token scope** and a **local
grant** on the target resource. Drive creation mints a manager grant for the
creator; folder grants apply down the folder's whole subtree (inheritance is
additive-only — if you can see a folder you can see everything under it); a
direct artifact grant covers that artifact. A same-workspace principal without
a grant reads as absent (404), and list endpoints are filtered by grant
visibility.

## What works in this slice

- The 51-operation production v0 REST surface above, plus the staging-only
  eight-operation sheet-session preview, conformance-pinned to the active
  manifest and served OpenAPI.
- Bearer auth against a Hub-issued token (token scope + local grant, §auth).
- Multipart inline artifact upload (15 MB cap), direct-to-GCS upload sessions
  for larger files, and immutable version appends with sha256 verification.
- A **public read surface** with four canonical routes on the branded share
  host: `/s/{share_key}/` (possession-based, dies with a soft-deleted target)
  plus `/a/{art_id}/`, `/v/{art_id}/{ver_id}/` and `/f/{fld_id}/`, which
  resolve through a live `public` grant. With `PUBLIC_CONTENT_BASE_URL` set,
  the share host returns only a trusted first-response metadata shell and
  exact-origin iframe; anonymous artifact-authored output and raw bytes stay on
  the isolated content origin. Removing that setting restores
  the direct share renderer for bounded B1 rollback. Every refusal remains one
  byte-identical 404, so neither origin reveals whether an id exists.
- A **private viewer implementation** under `/view/`, kept disabled in
  the hosted deployment. When enabled it lives only on its own isolated viewer
  origin; it never shares an origin or cookie
  domain with the public renderer or the Token Canopy session.
- RFC 9728 OAuth-protected-resource discovery at `/.well-known/oauth-protected-resource`.
- Runtime response validation: every route declares a `response_model`, so a
  payload that drifts from the contract fails loudly instead of shipping.
- Spec-driven conformance (Schemathesis) over the served OpenAPI.

## Hosted MCP

The curated TypeScript MCP is served at `https://drive.mcp.tokencanopy.com/mcp`
— the transport's own single-purpose origin (ADR-0002, `MCP_ORIGIN_BASE_URL`,
live since 2026-09-03). The legacy `https://drive.tokencanopy.com/mcp` path is
retired. The Node transport runs in its own Cloud Run service and dedicated
identity with no database, bucket, or secret access. It reaches FastAPI's exact
`/_internal/mcp` mount with a Google-signed workload token while forwarding
the caller's MCP bearer; FastAPI verifies both, enforces an exact operation
allowlist, and then runs the ordinary scope and local-grant checks. The
`drive.mcp` host answers
`/mcp` and its path-scoped RFC 9728 document and nothing else, which is what
lets Hub accept its bare origin as an alias for the MCP resource — the shim
for Claude Code 2.1.x, which derives the OAuth `resource` from the server
origin and is refused `invalid_target` on the legacy URL
([anthropics/claude-code#52871](https://github.com/anthropics/claude-code/issues/52871)).
The root protected-resource metadata stays on the canonical host at
`/.well-known/oauth-protected-resource`.

The beta MCP inventory has 24 tools: `list_drives`, `list_directory`,
`search_drive`, `read_artifact`, `list_artifact_versions`, `list_changes`,
`list_access_grants`, `create_drive`, `delete_drive`, `restore_drive`,
`create_artifact`,
`replace_artifact_content`, `update_artifact_metadata`, `create_folder`, `move`,
`delete`, `restore`, `begin_file_upload`, `get_file_upload`, `complete_file_upload`,
`cancel_file_upload`, `create_share_link`, `publish`, and `unpublish`. The
hosted release lane runs an anonymous challenge, authenticated initialize,
exact tool-list, and read-only `list_drives` smoke before any traffic moves.

## What's not in this slice (will follow)

- Cross-drive copy is deferred.
- The LLM wiki indexer (`_wiki/`), the LaTeX compile worker, and the legacy
  path-based viewer surfaces are retired on this branch.

## Search semantics

`GET /v0/drives/{drive_id}/search?q=...` — drive-scoped lexical full-text over
the artifact `search_tsv` (name + content preview + metadata/labels), filtered
by grant visibility (a caller only sees rows their local grants cover).

- **Supported syntax:** words (`kangaroo`), phrases (`"exact phrase"`),
  negation (`kangaroo -secret`), implicit AND (`kangaroo secret`), `OR`.
- **Not supported in v0:** semantic / embedding similarity; binary content
  (only name/metadata/labels match); non-English stemming; fuzzy; regex.
- **Filters:** `parent_id`, `content_type`, `label`, `updated_after`,
  `updated_before`, `limit`, `cursor`.

## Limits

- Max inline artifact size: **15 MB** per request → `413 ARTIFACT_TOO_LARGE`.
- List endpoints default `limit=50`, capped at 100.
- Rate limit: 600 requests/minute per principal → `429 RATE_LIMITED`
  (kill-switch: `V0_RATE_LIMIT_ENABLED=false`).

## API stability

The `/v0` prefix is the version, and the entire active REST inventory is
explicitly **beta** (`x-stability-level: beta` in the served OpenAPI): 51
operations in production and 59 in staging while sheet sessions are gated.
There is no stable `/v0` subset yet. The served OpenAPI is the
contract; a golden snapshot, the manifest, and the Schemathesis conformance
suite pin it against accidental drift. Promotion to stable is a later explicit
owner decision with a contract audit and coordinated SDK release.
