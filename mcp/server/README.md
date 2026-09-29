# `@tokencanopy/agentdrive-mcp`

This package is the hosted AgentDrive MCP surface. It serves Streamable HTTP at
`https://drive.mcp.tokencanopy.com/mcp` — the transport's own single-purpose
origin (ADR-0002, `docs/adr/0002-mcp-transport-topology.md`). The legacy
`https://drive.tokencanopy.com/mcp` path is retired. The product API and MCP
remain separate RFC 8707 resources with separate audiences.

The MCP layer has no database or raw HTTP implementation. Each request extracts
the caller's Hub-issued bearer token and constructs one
`@tokencanopy/agentdrive-sdk` `AgentDriveClient` with a `StaticTokenProvider`.
The SDK owns the transport, typed errors, idempotency, revisions, path helpers,
and transfer safety. A client token is never placed in MCP tool output,
process-global state, or logs.

## Two audiences, and a private data plane

The MCP bearer is bound to `https://drive.mcp.tokencanopy.com/mcp`, NOT to the
public product audience `https://drive.tokencanopy.com` (2026-08-28 security
remediation). They used to be the same, which made an MCP session token a
fully valid `/v0` product token and reduced this reviewed tool surface to a
suggestion.

That split removes this server's way of calling AgentDrive over the public
API: public `/v0` rejects an MCP-audience token, and holding a product-audience
token instead would restore exactly the confusion the split removed.
Production calls a workload-identity-protected internal ingress instead:

- `MCP_AGENTDRIVE_INTERNAL_URL` is either a numeric loopback HTTP origin for
  local/transition use, or an exact HTTPS URL ending in `/_internal/mcp`.
- Remote mode requires `MCP_AGENTDRIVE_INTERNAL_AUDIENCE`. The client obtains
  a Google ID token for that audience and sends it only in the private service
  authorization headers. The API accepts exactly the dedicated MCP service
  account and an explicit operation-ID allowlist.
- The workload identity is not user authorization. AgentDrive separately
  verifies the forwarded MCP JWT against the MCP audience and intersects its
  scopes with live local grants.
- Loopback mode still requires the supervisor's per-boot
  `MCP_INTERNAL_PROOF`. Remote mode and loopback proof mode are mutually
  exclusive.
- Redirects strip internal credentials and the caller bearer. Only HTTPS
  GET/HEAD external redirects are followed, for signed object transfers.

`MCP_AGENTDRIVE_API_BASE_URL` is retired.

The package exposes two integration points:

- `createAgentDriveMcpServer(client, authorization)` for protocol tests or an
  embedding HTTP framework. `authorization.scopes` decides which tools exist.
- `createAgentDriveMcpHttpServer({ clientFactory, auth })` for the Node/Cloud
  Run handler. `src/index.ts` supplies the production factory and verifier
  configuration using the published TypeScript SDK package.

The server is stateless by design. It creates a fresh MCP transport and SDK
client per request, and accepts only `Authorization: Bearer <Hub token>`.
It serves the RFC 9728 protected-resource document at
`/.well-known/oauth-protected-resource/mcp` ONLY. The root form describes the
public `/v0` product resource — a different audience, whose scope list is
maintained separately even though the strings coincide today — and AgentDrive
itself owns it; every Bearer challenge here names the path-scoped form.

Before creating the SDK client or MCP transport it verifies the Hub JWT
signature through JWKS, exact issuer, the exact `/mcp` audience, expiry,
required platform claims, and scope syntax. Missing or invalid credentials
return `401` with a standards-compliant Bearer challenge; a token carrying no
scope of this resource returns `403` with an `insufficient_scope` challenge.
Health probes are `/health` and `/ready` and remain credential-free.

## Per-tool authorization

Each tool declares its required scopes beside its schema and handler in one
registration call. There is no separate authorization map to drift from the
registration map, and a tool cannot be added without declaring scopes — the
parameter is a non-empty tuple, so an empty one does not compile.

`tools/list` exposes only the tools the presented token authorizes, and every
handler re-checks its own scopes at call time before touching the SDK, so a
hand-built JSON-RPC `tools/call` naming an unlisted tool is refused rather
than executed.

| Tool                                                                                                                    | Required scopes                                   |
| ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `list_drives`                                                                                                           | `drives:read` + `usage:read`                      |
| `list_directory`, `search_drive`, `read_artifact`, `list_artifact_versions`                                             | `content:read`                                    |
| `list_changes`                                                                                                          | `changes:read`                                    |
| `list_access_grants`                                                                                                    | `sharing:read`                                    |
| `create_drive`, `delete_drive`, `restore_drive`                                                                         | `drives:write`                                    |
| `create_artifact`, `replace_artifact_content`, `update_artifact_metadata`, `create_folder`, `move`, `delete`, `restore` | `content:write`                                   |
| `begin_file_upload`, `get_file_upload`, `complete_file_upload`, `cancel_file_upload`                                    | `content:write`                                   |
| `create_share_link`                                                                                                     | `sharing:write`                                   |
| `publish`, `unpublish`                                                                                                  | `sharing:read` + `sharing:write` + `content:read` |

`list_drives` reads `/v0/drives/{id}/usage` per drive, but **only for an
active one**. That endpoint fetches with `include_deleted=False`, so a
soft-deleted drive is a 404 there — and a fan-out that ignored this made
`state: "deleted"` fail outright and let one deleted drive fail a
`state: "all"` page including its active drives. A non-active drive reports
`usage: null` and still carries `storage_bytes` and `retrieval_bytes`, which
`DriveOut` holds itself; only the limit-aware detail (`meters`,
`effective_limits`) is absent. The same 404 is caught rather than raised,
because `state` is not the only way to reach one: a drive can be deleted
between the listing and the usage read, and one null beats a failed page.

Verified against AgentDrive's own `_SCOPE_*` constants, not against SDK method
names: `v0_uploads` requires `content:write` on all four routes including its
GET, so `get_file_upload` is a write tool. A read-only grant therefore
initializes successfully and exposes seven tools; a full grant exposes all
twenty-four. `publish` and `unpublish` need `content:read` as well as the two
sharing scopes because they read the artifact or folder for its parent chain and
`effective_visibility`, and `/v0` gates those reads on `content:read`.
`create_drive` needs no pre-existing local grant: AgentDrive's
`POST /v0/drives` requires only the `drives:write` scope and bootstraps
manager grants for the creating agent and its sponsor. `delete_drive` does
need one — `DELETE /v0/drives/{id}` is gated on a local manager grant on top
of the scope — and it is the only destructive tool whose blast radius is a
whole drive, which is why it lives beside `create_drive` rather than inside
`delete`: the two verbs answer to different scopes. That split is the
surface's naming rule — a bare verb (`move`, `delete`, `restore`) acts on
something inside a drive under `content:write`, and a `_drive` suffix acts on
the drive itself under `drives:write`. Merging them is not a cosmetic choice:
`authorizes()` is an AND, so one tool declaring both scopes would vanish from
`tools/list` for an agent holding only `content:write`.
`agentDriveMcpToolScopes()` projects the same definitions for tests
and release checks.

Production wiring defaults to the production Hub issuer, the `/mcp` audience,
its path-scoped protected-resource metadata URL, and the production JWKS URL.
`MCP_AGENTDRIVE_INTERNAL_URL` and `MCP_INTERNAL_PROOF` are always required and
have no defaults. Staging and other environments must additionally set
`MCP_ENVIRONMENT` plus the complete matching tuple: `MCP_AUTH_ISSUER`,
`MCP_AUTH_AUDIENCE`, `MCP_AUTH_METADATA_URL`, and `MCP_AUTH_JWKS_URL`. The
audience must carry the exact `/mcp` path — a bare origin is refused, because
that spelling IS the product resource — and all four are required together, so
a non-production verifier cannot accept a production token.

One process can serve several MCP resources, one per single-purpose origin
(ADR-0002, `docs/adr/0002-mcp-transport-topology.md`).
`MCP_AUTH_ADDITIONAL_AUDIENCES` is a comma-separated list of further exact
`/mcp` resources — `https://drive.mcp.tokencanopy.com/mcp` — each on an origin
distinct from the primary's; the metadata URL is derived from each. The
FastAPI edge labels every request it forwards from such an origin with
`x-agentdrive-mcp-origin: <origin>` (never copied from the client), and the
server answers with THAT resource's document, challenge, and audience. A
bearer is verified against the labelled resource only: the origins are
separate resources with separate audiences, and neither accepts the other's
token. No label means the primary resource, so a one-origin deployment is
unchanged; an unknown label is a 404.

This is a hosted monorepo service, not a public npm package. The MCP package
and its private reusable auth seam are deployed together from the reviewed
workspace build.

The package does not receive refresh tokens. The MCP client renews through Hub
and presents a new short-lived AgentDrive access token on the next request.
No manually provisioned client id is required for supported coding agents:

```bash
codex mcp add agentdrive --url https://drive.mcp.tokencanopy.com/mcp
codex mcp login agentdrive

claude mcp add --transport http --scope user agentdrive \
  https://drive.mcp.tokencanopy.com/mcp
claude mcp login agentdrive
```

Codex currently may print a scope-rejection message and two authorization
URLs. Its first request carries only global OIDC identity scopes, so Hub
correctly refuses it; Codex then retries with the AgentDrive scopes it
discovered. Open the second URL. Do not weaken the grant boundary to make the
identity-only attempt succeed.

Codex prefers its published Client ID Metadata Document and can fall back to
DCR. Claude Code uses constrained DCR. Both use authorization code with PKCE,
an ephemeral loopback callback, the exact AgentDrive resource indicator, and
automatic refresh. A pre-registered client id remains available for managed
clients with a fixed callback.

Cursor Desktop uses the same no-setup DCR path. Cursor may submit its desktop,
web/Agents, and legacy callbacks together; Hub recognizes that exact vendor
callback bundle but persists only Cursor's current fixed desktop callback,
`http://localhost:8787/callback`, so the OAuth provider receives a valid
native loopback client. Cursor's hosted web/Agents surface still uses the
deployment-managed client lane.

**Why the `drive.mcp` origin, and why the legacy URL fails on Claude Code
2.1.x.** Claude Code derives the OAuth `resource` parameter from the server
URL's origin instead of reading it from the protected-resource metadata its
own challenge points at
([anthropics/claude-code#52871](https://github.com/anthropics/claude-code/issues/52871)).
On `https://drive.tokencanopy.com/mcp` that origin is the `/v0` product
audience, which Hub correctly refuses to issue to an interactive client, so
`claude mcp login agentdrive` fails with `invalid_target` there. On
`https://drive.mcp.tokencanopy.com/mcp` nothing else is served, so Hub accepts
the bare origin as an alias for the MCP resource and the same client connects;
the token it receives still carries the canonical `/mcp` audience. Codex sends
the correct resource and works on either URL. Grants are audience-bound, so a
client moving between the two URLs re-consents.

For mutation calls that omit `idempotency_key`, the adapter derives a stable
key from the validated tool name and normalized arguments before invoking the
SDK. The same semantic mutation therefore replays safely across changed
JSON-RPC ids, ignored envelope fields, and batch retries; an explicit key can
be supplied when an intentionally distinct operation has identical arguments.
**`publish` is the one exception:** its create sends only a
caller-supplied key, never a derived one. AgentDrive replays a completed
mutation's stored response for 24 hours by key, so a key derived from the
arguments alone would hand back the first, since-revoked grant on a
publish → unpublish → publish cycle and report it published. The tool lists live grants before
it writes, which is what makes a same-arguments retry a safe no-op instead.

The beta MCP surface has exactly twenty-four explicit tools:

This is the current beta contract. Earlier launch notes called the surface
“nineteen tools”; that was the pre-`publish`/`unpublish` and drive-lifecycle
amendment. Acceptance scripts and release gates must use the 24-name list below,
not the historical 19-tool count.

- `list_drives`, `list_directory`, `search_drive`, `read_artifact`,
  `list_artifact_versions`, `list_changes`, `list_access_grants`
- `create_drive`, `delete_drive`, `restore_drive`
- `create_artifact`, `replace_artifact_content`, `update_artifact_metadata`
- `create_folder`, `move`, `delete`, `restore`
- `begin_file_upload`, `get_file_upload`, `complete_file_upload`,
  `cancel_file_upload`
- `create_share_link`, `publish`, `unpublish`

Creates never overwrite. `delete` takes one folder or artifact and
`delete_drive` takes the whole drive; both require the resource's current
revision, which is AgentDrive's `If-Match`, so a delete cannot land on a
resource that moved since it was read. Both are soft deletes, and `restore` and `restore_drive`
are their counterparts — taking the revision the resource carries AFTER the
delete, which the delete response returns and `list_directory` with
`state: "deleted"` recovers later. `list_drives` takes the same
filter for the collection above it, which is what makes `restore_drive`
usable from a cold start. The wildcard is `all`, not `any` — the 2026-09-11
rename (#681) gave every v0 collection filter one spelling and one wildcard,
and `entries`, which already said `state`, is the one that gave up `any`. Restoring a folder brings back the subtree
that went down with it, atomically, so there is no `recursive` option;
restoring an artifact fails while its parent folder is still deleted, and
fails if a live sibling has taken its name since.
`replace_artifact_content` writes a new version of an
existing artifact, `update_artifact_metadata` changes metadata or labels only,
and `move` owns rename and relocation. Inline content is limited to 1 MiB; the
four upload-session tools expose the reviewed direct-transfer workflow for
larger files without sending their bytes through MCP. Mutation schemas reject
unknown fields, ambiguous selectors, unsafe credential-like names, and
artifact names over 255 characters; streamed reads stop before the 1 MiB
response limit.

`publish` and `unpublish` are the two verbs over the public grant of one
artifact or folder (CONTEXT.md § Published): `publish` creates a viewer-only
`principal_type: "public"` grant (a no-op when a live direct one exists) —
not a versioned release (the head AND every version permalink become
readable) and not a share link (a share link is a secret that expires; a
published resource has a permanent address); `unpublish` revokes every live
direct one and refuses with `public_inherited` — naming the grant — when the
resource is published only through the drive or an ancestor folder. Drives are
deliberately not accepted. The result carries `inherited_from` and, for an
artifact, the server's `effective_visibility` after the change; a folder
reports `null` because `FolderOut` carries no such field; note that the
server counts a live share link as `public` too, so an artifact can read
`public` with no public grant anywhere. `warnings` lists what the call could
not do or check: an ancestor folder the authorization cannot read (a manager
grant on a subfolder does not reach upward), an `expires_at` ignored because
a live direct grant already existed, or a failed re-read after the change
landed. `publish` also returns `public_url`, the permanent permalink
(`/a/{artifact_id}/` or `/f/{folder_id}/`) on the public shell origin the
sidecar is configured with — `MCP_PUBLIC_BASE_URL`, exactly an https origin,
which a deployment sets to the same origin as the API's `SHARE_BASE_URL`. When that variable is unset the field is omitted, never
guessed, and a warning says so. Design note:
`docs/superpowers/specs/2026-09-10-agentdrive-mcp-public-access-design.md` (Token Canopy design history).

The original frozen tool set and its later amendments are documented in
`docs/superpowers/specs/2026-08-23-agentdrive-b7-hosted-mcp-design.md` (Token Canopy design history).
