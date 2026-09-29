# AgentDrive v0 API contract reset

**Status:** shipped (atomic cutover on the `v0` branch); body reconciled to the shipped surface  
**Date:** 2026-07-30  
**Review amendments:** 2026-07-31 (upstream spec commit `13c8ded`); 2026-08-06 (pre-cutover scope reduction + full-body reconciliation after the contract review); 2026-08-19 (folder sealing removed — inheritance is additive-only)  
**Owner:** AgentDrive data plane  
**Canonical operation inventory:** `src/agentdrive/api/v0-operations.json`  
**Shipped:** 39 public `/v0` operations  
**Compatibility posture:** deliberate clean break before launch

> **Changelog (2026-08-06):** this document originally targeted 46 operations.
> Before cutover, the **uploads and jobs verticals were cut** (4 + 3 ops) —
> inline multipart upload is the upload path, copies are same-drive-only, and
> cross-drive copy is rejected with `400 INVALID_ARGUMENT` rather than
> returning a job. The `/mcp` transport is a follow-on, not part of this
> cutover. The body below has been edited to describe the shipped
> 39-operation surface; sections covering the deferred verticals are marked
> **DEFERRED — NOT SHIPPED IN v0** and retained as the accepted design for a
> future release. Where the shipped wire behavior differs from the original
> prose (changes envelope, path-conflict codes, within-workspace 404 policy),
> the body now states the shipped behavior. The authoritative contract is the
> mounted `/v0` routers and the served OpenAPI, pinned to the manifest by
> `tests/conformance/`.

The product reasoning and the full legacy-operation reconciliation live in
Token Canopy's umbrella design for this contract, and the hosted product's
identity contract owns canonical agents, runtimes, workspaces, credentials,
and product-token issuance; neither is part of this repository. This
repository-local document is the implementation contract for the
AgentDrive data plane.

## 1. Outcome

AgentDrive becomes a small, explicitly multi-drive storage API for durable
artifacts, immutable versions, retrieval, local access, sharing, and a
durable per-drive change feed.

Every product resource is drive-scoped. Mutable resources are addressed by
opaque stable IDs, not paths. Token Canopy Hub establishes who the caller is
and which workspace/product scopes the caller may represent; AgentDrive still
decides whether that principal may access the requested drive, folder, or
artifact.

The authorization rule is:

```text
Hub product-token scope ∩ AgentDrive local resource capability
```

Neither side expands the other. Workspace membership and product eligibility
grant no default content access, including no default read access.

## 2. Boundaries

Token Canopy Hub owns:

- `tcagt_*` canonical agent identity;
- `tcusr_*` human identity;
- `tcrun_*` enrolled agent runtimes;
- workspaces and agent workspace memberships;
- `tccred_*` membership-bound OAuth client credentials;
- product entitlement;
- audience-bound AgentDrive token issuance.

AgentDrive owns:

- drive lifecycle and workspace association;
- folder and artifact namespaces;
- immutable artifact versions and bytes;
- local grants and read-only share links;
- storage/retrieval usage;
- asynchronous storage jobs (deferred vertical — not shipped in v0);
- the durable per-drive change feed.

The reset removes AgentDrive-local workspaces, members, invitations, machine
credentials, OAuth authorization-server behavior, billing, query, compile,
feedback, and AgentTag-specific APIs from the core public data-plane contract.
Those implementations may temporarily remain internal during migration, but
they are not launch API operations.

AgentDrive may retain protected-resource discovery that points clients to Hub.
It does not mint product access tokens.

## 3. Authentication context

Every authenticated `/v0` request receives a short-lived Hub-issued bearer
token with:

- `sub`: canonical `tcagt_*` or `tcusr_*`;
- one active workspace and membership context (`membership_id` on every
  token, so change-feed actor attribution is uniform);
- agent-only claims: `client_id` and `credential_id` (the same `tccred_*`
  value), `runtime_id`, and `sponsor_id` (the `tcusr_*` sponsoring
  controller at issuance, enabling the drive-creation bootstrap grant
  without a synchronous Hub lookup);
- human-only claim: `workspace_role` (`owner`, `admin`, or `member`,
  minted verbatim from the Hub workspace role), enabling the
  workspace-admin overlay (§8) and break-glass grant recovery — `owner`
  and `admin` are equivalent everywhere AgentDrive consults the claim;
- `aud=https://drive.tokencanopy.com`;
- explicit AgentDrive scopes;
- issuer, expiry, token ID, and normal JWT claims.

Agent-only claims on a human token, or `workspace_role` on an agent token,
fail validation.

Two Hub-issued bearer forms exist: agent product access tokens obtained
through OAuth client credentials (`sub` is `tcagt_*`), and human product
tokens derived from an authenticated Hub session (`sub` is `tcusr_*`), defined
in companion Hub §7.3. Both carry the same issuer, audience, expiry,
workspace, and explicit-scope contract, validate identically, and intersect
with AgentDrive-local grants. Human product tokens are how `user` grant
principals and `user` change actors are exercised; no other human
authentication path exists on `/v0`.

The default access-token lifetime is one hour. Raw HTTP clients repeat the Hub
client-credentials exchange when necessary; SDK and hosted MCP integrations
renew before expiry without an interactive WorkOS screen. The durable client
secret or private key is presented only to Hub.

AgentDrive validates tokens locally from Hub's published signing keys. A valid
token remains usable during a Hub outage until expiry. New issuance depends on
Hub.

Caller-controlled actor headers are never authoritative. Mutations and changes
record the verified subject, workspace, runtime/credential identifiers when
available, AgentDrive request ID, operation, outcome, timestamp, and W3C trace
ID when supplied.

## 4. Resource model

| Resource | Contract |
| --- | --- |
| Drive | Workspace-associated storage and authorization boundary; several drives per workspace are allowed. |
| Folder | Mutable identity with parent, name, metadata, lifecycle, and revision. |
| Artifact | Mutable identity with parent, name, metadata, lifecycle, head-version reference, and revision. |
| Version | Immutable bytes, checksum, media type, size, server-observed actor attribution, parent-version reference, and creation time. |
| Upload | *(deferred — not shipped in v0)* Expiring direct-upload session targeting either a new artifact or a new version. |
| Grant | Local capability assigned to a Hub principal over a drive, folder, or artifact. |
| Share | Expiring, revocable, read-only bearer link whose secret is revealed only at creation or rotation. |
| Change | Immutable drive-scoped content invalidation record with a durable cursor. |
| Job | *(deferred — not shipped in v0)* Readable/cancelable state for an asynchronous operation created by a domain endpoint. |

### 4.1 IDs, revisions, and namespace

- Existing `drv_*`, `art_*`, and `fld_*` IDs remain.
- Every reset-v0 drive has one server-created root folder. Drive
  representations expose its `root_folder_id`; the root has no parent (a
  null `parent_id`), a null `name`, and canonical path `/`.
- Immutable versions use `ver_*`.
- Mutable representations use opaque `rev_*` revisions.
- `ETag` is the quoted current revision.
- Immutable version resources are the exception: their `ETag` is the quoted
  `ver_*` id. A version-creating 201 (append, restore) additionally returns
  `artifact_revision` in the body — the artifact's post-mutation revision,
  usable as the next `If-Match`.
- Every user-visible mutation of mutable state produces a new revision.
- `name` is one path segment; clients never mutate by full path. An item name
  is NFC-normalized and contains 1–255 Unicode code points. It may contain
  spaces and ordinary Unicode, but may not have leading/trailing Unicode
  whitespace, `/` or `\\`, Cc/Cs/Zl/Zp characters, U+FEFF, bidi controls
  U+202A–U+202E or U+2066–U+2069, or consist only of dots. Folder and artifact
  siblings share one case-sensitive namespace over the canonical value.
- Sibling artifacts and folders share one collision domain.
- Rename and same-drive move update `name` and/or `parent_id` through `PATCH`.
- Cross-drive move is unsupported. Cross-drive copy authorizes the source and
  destination independently.
- Folder and artifact paths are derived conveniences. Responses return IDs.
- Folder and artifact list operations accept exact-match `parent_id` plus
  `name` filters. Because sibling names share one collision domain, parent
  plus name matches at most one item, so clients resolve a path to a stable
  ID segment-by-segment with one bounded query per level. This is the
  supported replacement for the removed path-addressed routes.

Artifact history records a parent version so the initial linear history can
become a DAG later without changing the API. Restoring a historical version
creates a new immutable version; it never rewinds or mutates history.

Generic artifacts are not CRDT documents. Concurrent mutation uses revisions
and HTTP preconditions.

## 5. Exact HTTP surface

Paths are relative to the deployment's canonical API origin.
`https://drive.tokencanopy.com` is the canonical API origin for
production. `https://api.agentdrive.run` is a temporary retirement alias
during the bounded compatibility window; it is not a supported-client default.
The served OpenAPI's `servers[0]` names the environment's Token Canopy
canonical resource, including when the request arrives through the alias, so a
generated SDK targets the environment whose audience-bound tokens it holds.

`src/agentdrive/api/v0-operations.json` is the machine-readable, test-pinned inventory.
It contains exactly these 39 operations:

| Domain | Count | Operations |
| --- | ---: | --- |
| Drives | 7 | list, create, read, update, soft-delete, restore, usage |
| Folders | 7 | list, create, read, update/move, soft-delete, copy, restore |
| Artifacts | 8 | list, create, read, update/move, soft-delete, content, copy, restore |
| Versions | 5 | list, append, read, content, restore-as-new-head |
| Search | 1 | drive-scoped passage retrieval |
| Changes | 1 | cursor-resumable drive changes |
| Grants | 5 | list, create, read, update, revoke |
| Shares | 5 | list, create, read, revoke, rotate |

Two verticals were designed and deferred before cutover (their sections below
are marked accordingly): **Uploads** (4 ops — create session, read state,
cancel, complete) and **Jobs** (3 ops — list, read, cancel). Until they ship,
inline multipart upload is the only upload path and every shipped operation
completes synchronously.

There are no mutable path routes, `/meta`, `/move`, `/download-url`, numeric
version routes, `/drives/me`, `/trash`, `/events`, `/find`, generic job
creation, or raw worker logs.

The single search operation supports three retrieval modes — `lexical`,
`hybrid`, and `semantic` — but a deployment may enable only a subset, minimum
`lexical`; the OpenAPI snapshot declares the enabled subset in the
operation's description (this deployment enables `lexical` only, defaulting
to it when `mode` is omitted). Requesting a disabled mode fails with the
catalogued `400 SEARCH_MODE_UNAVAILABLE` error; an unrecognized mode value
is a validation error. Every result includes the artifact id, the artifact's
current head version id (nullable in the schema for a headless artifact — a
state only the deferred uploads vertical can create; every shipped write path
produces an artifact with a head version), a rank score, and a bounded
snippet.

The snippet is **HTML-safe by contract**: it is a highlighted excerpt of
artifact content, and the only markup it may contain is the server's own
`<mark>`/`</mark>` highlight pair. Artifact content is entity-escaped, so a
client may render the snippet as HTML. This is a server obligation, not a
client one: the underlying `ts_headline` output interpolates the highlight
markers into attacker-controlled bytes, and it is tag-aware without being
sanitizing — it drops `<script>` but passes `<img src=x onerror=…>` through
untouched — so the escape is applied once, on the server, for every consumer.

Outside `/v0`, the shipped non-API surface is `/health`,
`/.well-known/oauth-protected-resource` (RFC 9728 discovery), and the **public
read surface**: four server-rendered routes, anonymous, bound to the share
host and served nowhere else.

| Route | Resolves via | Cache-Control |
|---|---|---|
| `/s/{share_key}/` | possession of the key | `private, no-store` |
| `/a/{art_id}/` | a live `public` grant; follows the head | `public, max-age=60, must-revalidate` |
| `/v/{art_id}/{ver_id}/` | a live, revocable `public` grant; one frozen version | `public, max-age=0, must-revalidate` |
| `/f/{fld_id}/` | a live `public` grant; lists immediate children | `public, max-age=60, must-revalidate` |

Each has a `/content` sub-route serving the raw bytes. Page URLs end in a
slash so a page can link its own bytes relatively; the un-slashed forms 308 to
them, unconditionally and before any lookup, so the redirect cannot become an
existence oracle.

Every refusal on every route is one byte-identical 404 — unknown id,
unpublished, revoked, expired, soft-deleted artifact, soft-deleted drive and
unknown version are indistinguishable, and `/f/` returns the same body as
`/a/`, so the response does not leak which kind of id was guessed.

This supersedes the earlier JSON-only redemption route, which required a JSON
`Accept` and so 404'd every browser. Its byte-serving behaviour is preserved
unchanged for non-HTML clients at the same URL; only its `shares_redeem`
OpenAPI entry is gone, and that was never one of the 39 contract operations.
The browser-session viewer remains unmounted; it returns with the workspace UI
follow-on.

## 6. Cross-cutting HTTP contract

### 6.1 Authentication and authorization

- `/v0` is authenticated by default; public exceptions are explicit.
- A token that fails verification — wrong issuer, audience, expiry,
  signature, or malformed workspace claims — returns `401` with a Bearer
  challenge. A valid token addressing a resource outside its workspace is an
  object-level miss: the anti-enumeration `404` rule below wins, never a
  cross-workspace `401` or `403`.
- Unauthorized object access is always a `404` status. **Across workspaces**,
  the response is byte-equivalent to absence (the resource's `*_NOT_FOUND`
  code): a cross-workspace probe reveals nothing. **Within the caller's own
  workspace**, the 404 deliberately carries the distinct code
  `NOT_AUTHORIZED`: existence of a resource inside your own workspace is not
  treated as a secret — its content, metadata, and name are — and the
  distinct code tells an agent whose grant is missing or revoked to request
  access rather than misdiagnosing deletion and taking wrong recovery
  actions. IDs are unguessable (16 hex chars), so this discloses existence
  only for identifiers the caller already possesses. Preconditions never
  extend this: authorization is evaluated before `If-Match`, so a caller
  without capability sees the 404 — never a 428/412 that would confirm a
  revision.
- Scope checks and local-capability checks both run. The local half is a
  grant row OR the workspace-admin overlay (§8): a human token whose
  `workspace_role` is `owner` or `admin` holds implicit `manager` on every
  drive in its own workspace. The overlay is local capability only — it
  never adds scope, never crosses workspaces, and never applies to an
  agent (`workspace_role` fails validation on an agent token).
- Drive creation is the only operation without a pre-existing local grant. It
  requires `drives:write`, active workspace membership, entitlement, and Hub
  policy, then atomically grants the creating principal local `manager`. When
  the creator is an agent, it also grants `manager` to the human sponsoring
  controller of the agent's workspace membership, so every drive begins with
  a human manager. Neither grant creates ambient access for any other
  workspace MEMBER; workspace owners and admins hold manager on the new
  drive already, through the overlay rather than a grant row.

Initial scope vocabulary:

```text
drives:read       drives:write
content:read      content:write
changes:read
sharing:read      sharing:write
usage:read
```

An omitted scope grants nothing. (The deferred uploads vertical is covered by
`content:*` when it ships; the deferred jobs vertical adds
`jobs:read`/`jobs:write` — neither scope exists on the shipped surface, and
the discovery document's `scopes_supported` lists exactly the eight above.)

The scope-to-operation mapping is: `drives:read`/`drives:write` cover the
drive resource operations; `content:read`/`content:write` cover folders,
artifacts, versions, content, and search; `changes:read` covers the changes
feed; `sharing:read`/`sharing:write` cover both grants and share links;
`usage:read` covers drive usage.

### 6.2 Mutation safety

- Every mutation requires `Idempotency-Key`; omitting it returns
  `400 IDEMPOTENCY_KEY_REQUIRED`.
- The same principal/method/path/body under one key returns the original
  result. Replay re-checks authorization first: a principal whose access was
  revoked after the original request receives the normal authorization
  failure, not the stored result. Replay returns the stored result without
  re-evaluating preconditions; `If-Match` is judged only on first execution.
- Key reuse for a different request returns
  `409 IDEMPOTENCY_CONFLICT`.
- Requests rejected before execution — missing `Idempotency-Key`, `428`,
  `412`, or authorization failures — record nothing; the same key remains
  usable for the corrected retry.
- Existing-state mutation requires `If-Match`.
- Missing precondition returns `428 PRECONDITION_REQUIRED`.
- Stale revision returns `412 PRECONDITION_FAILED`.
- Create operations do not require a parent revision. Copy (folder and
  artifact) is a creation operation: `If-Match` is optional, and when
  supplied it is validated against the source's current revision. (When the
  deferred uploads vertical ships, upload completion is likewise exempt from
  a request-time `428`: the precondition is captured at session creation and
  a stale head surfaces as `412` at completion.)
- Same-drive folder copy is synchronous for at most 5,000 live resources,
  counting the source root, descendant folders, and artifacts. The count is
  bounded at 5,001 under the drive namespace lock before recursive rows are
  materialized or locked; an oversized subtree returns
  `409 SUBTREE_TOO_LARGE` without creating a partial copy.
- Names are never an upsert key: creating over an existing sibling name
  returns `409` with code `ARTIFACT_PATH_CONFLICT` or
  `FOLDER_PATH_CONFLICT` (one collision domain, two codes naming which kind
  of sibling holds the name); renames happen only through explicit `PATCH`.
- Writes reject unknown fields.

### 6.3 Responses, collections, and errors

- Creation returns `201 Created` and `Location`.
- `Location` is an absolute URL under the deployment's canonical API origin
  (`API_BASE_URL`, falling back to `PUBLIC_BASE_URL`) — the same origin as
  the served spec's `servers[0]` — independent of the internal service
  origin.
- (Deferred with the jobs vertical: asynchronous acceptance returns
  `202 Accepted` and a job location. Every shipped operation completes
  synchronously.)
- Soft-delete returns `200 OK` with the updated deleted representation and
  revision. It never returns an empty `204`, because clients need the
  lifecycle state and ETag for restore. The soft-delete `200` body's
  post-delete revision is a sanctioned `If-Match` source for the subsequent
  restore, equally valid to the revision from a `state`-filtered list row.
  Folder delete and restore wrap the representation to carry the recursive
  outcome: `{"folder": <representation>, "cascade": {"folders": n,
  "artifacts": m}}` — the revision lives at `folder.revision`. Drive and
  artifact lifecycle responses return the bare representation.
- Mutable singleton reads emit `ETag` and honor `If-None-Match`.
- Private JSON declares a non-public cache policy.
- Normal collections return `{ "items": [], "next_cursor": null }`.
- Collection limit defaults to 50 and is capped at 100.
- Cursors are opaque and bound to the collection, normalized filters, and
  stable keyset position.
- Artifact listing additionally filters on exact `label` membership (the
  legacy lowercase-normalized labels column), exact `content_type`, and
  inclusive `updated_after`/`updated_before` RFC 3339 bounds; all filters
  participate in the cursor's filter fingerprint.
- Every collection that can hold a non-live row takes the same `state`
  filter: `active|deleted|all` on drives, folders, artifacts and entries,
  `active|revoked|all` on grants and shares. Drive listing's `state=deleted`
  is how a manager discovers a deleted drive and its current ETag before
  restore. The parameter was spelled `lifecycle` on five of those six until
  the prelaunch rename; there is no alias — the old spelling is rejected as
  an unknown query parameter.
- Unknown query parameters are rejected.
- Timestamps are fixed-width RFC 3339 UTC with `Z`.
- Every response returns an AgentDrive request ID (`X-Request-Id`).
- *(Planned, not yet implemented:)* accepted W3C `traceparent` context will
  be propagated into server-observed attribution, with invalid trace context
  ignored rather than trusted. The shipped surface does not yet read
  `traceparent`.

The only JSON error envelope is:

```json
{
  "error": {
    "code": "PRECONDITION_FAILED",
    "message": "The resource changed after it was read.",
    "details": {}
  }
}
```

Clients branch on `code`, never `message`. `details` is optional
supplementary data and may be absent or empty; the committed detail keys are
`{"recovery": "full_sync"}` on `410 CHANGE_CURSOR_EXPIRED` and
`{"current_revision": ...}` on `412 PRECONDITION_FAILED` (the resource's
live `rev_*`, the If-Match value for a corrected retry).

JSON uses `application/json; charset=utf-8`. Unsupported request media types
return `415` (enforced today on the multipart operations — a JSON POST to
artifact create or version append is a `415`; a content-negotiation `406`
for unacceptable `Accept` values is not implemented in shipped v0, which
ignores `Accept` on `/v0`). The edge owns HSTS and compression.
Application/edge policy emits `nosniff`. The workspace UI origin is separate
from the API origin, so browser CORS uses an explicit allowlist of
first-party origins with enumerated methods and headers; credentialed
requests never use `Access-Control-Allow-Origin: *`.
Rate limits return `429` and a usable `Retry-After` value. (Steady-state
`RateLimit-*` headers are an edge-deployment follow-up; the shipped app does
not emit them.) Requests whose
contract has no body, including drive delete and restore, reject non-empty
bodies rather than silently omitting those bytes from idempotency identity.

## 7. Direct uploads — DEFERRED, NOT SHIPPED IN v0

> **This entire section describes the deferred uploads vertical.** It is the
> accepted design for a future release; none of it is mounted. In shipped
> v0, the only upload path is inline multipart on artifact create and
> version append. The `v0_uploads` table remains as unused substrate.

`POST /v0/drives/{drive_id}/uploads` accepts a discriminated target:

- a new artifact from `parent_id` and `name`; or
- a new version for an existing `artifact_id`.

The request supplies media type, byte size, and optionally a checksum — the
contract's direct-upload example is CRC32C (`crc32c`, base64), the integrity
value the object store itself observes on every landed object; a declared
`sha256` is also accepted. The response
supplies an expiring URL, exact HTTP method, and exact required headers.
Callers upload directly with ordinary HTTP, including `curl`, rather than
proxying large bytes through Cloud Run, an SDK, or MCP.

For a version target, upload creation captures the artifact's required
`If-Match`. Completion returns `412` and creates no visible version if the head
changed while bytes transferred. Completion is idempotent and rechecks quota,
checksum, target state, namespace collision, and preconditions.

The persisted integrity hash on artifacts and versions is always
server-observed. A declared `crc32c` is verified against the store's observed
value; a declared `sha256` that the implementation does not re-hash is stored
only as explicitly client-attested metadata, never as the integrity hash. A
checksum mismatch fails completion with `409 CHECKSUM_MISMATCH` and no visible
artifact or version. `checksum` is optional; when omitted, the server records
only its own observed integrity value.

The raw API has single-object sessions only. High-level SDK convenience may
compose:

```python
client.artifacts.upload(...)
client.artifacts.upload_batch(...)
client.folders.upload(...)
```

Batch and folder orchestration use bounded parallel single-file sessions and
return per-item outcomes. They are not atomic in v0.

## 8. Grants and shares

Grant principals are stable discriminated references:

- `agent` / `tcagt_*`;
- `user` / `tcusr_*`;
- `workspace` / Hub workspace ID, only when a manager deliberately wants all
  active members of that workspace — both human members and agent
  memberships — to receive the role;
- `public`, only with role `viewer`.

Grant resources are drive, folder, or artifact references. Roles are `viewer`,
`editor`, and `manager`. No dormant `commenter` role exists.

**Inheritance is additive-only (amended 2026-08-19).** If a principal can see
a folder, they can see everything under it: a folder grant applies down that
folder's whole subtree at any depth, and an explicit drive-level grant is
authoritative throughout the drive. Nothing subtracts reach on the way down.
This keeps the recovery invariant meaningful — break-glass produces a
drive-level manager grant, which must be able to administer every subtree —
and it matches the model users already hold from Google Drive. Restricting a
subtree is expressed by not granting the ancestor, never by a per-node
opt-out. The reset's `grant_inheritance=inherit|sealed` folder policy was
REMOVED before launch (it could be set — and the boundary it drew moved —
with `editor`, while every other access-graph mutation requires `manager`;
and gating the field would not have closed the outcome, since the same
principal can move, copy, or re-parent resources across the boundary). The
legacy artifact `inherit_grants` column is likewise not part of the reset
artifact contract and must not create a second, hidden inheritance model.

**The workspace-admin overlay (amended 2026-08-28).** A human actor whose
verified token carries `workspace_role` `owner` or `admin` holds implicit
`manager` on EVERY drive whose `workspace_id` is the token's own workspace
— member-created and agent-created drives included — and transitively on
every folder and artifact in them, since `manager` is the ceiling role and
inheritance is additive-only. No grant row exists or is created for this
access; it is resolved at the single authorization choke point
(`effective_role`), so every surface — by-id reads, mutations, grant
administration, listings, search, the change feed (permission events
included), and capability mints — answers consistently: a drive an
owner/admin can open by id appears in their `drives_list`, and vice versa.

Rationale, both halves ratified together: **visibility** — owners and
admins administer and pay for the workspace, and a workspace surface where
the people accountable for it cannot see what exists in it invites shadow
storage; and **orphan-prevention** — the permanent creator grant keeps a
live manager ROW on every drive, but a departed creator's row is a manager
no token will ever exercise again, which break-glass (below) could not
repair because the row still counts as active. With the overlay the
corrected orphan invariant is: **no drive in a workspace with a live
owner or admin is ever unreachable.**

The boundaries that deliberately do NOT move:

- **members stay grant-only** — one member's drive remains invisible to
  another member without an explicit grant;
- **agents never receive the overlay** — `workspace_role` is human-only
  and fails validation on an agent token, whatever the agent's sponsor's
  role is;
- **token scope still intersects** (§6.1) — an owner holding read-only
  scopes reads everything and writes nothing;
- **the overlay never crosses workspaces** — it is pinned to the drive's
  `workspace_id` equalling the token's;
- the two token-less re-authorization surfaces (viewer-session resolution
  and the upload reconciler) honor the overlay from a `workspace_role`
  snapshot recorded at mint, bounded by the session's own lifetime — the
  same treatment token scope already receives there.

Because overlay access has no grant row, `grants_list` does not describe
it: clients rendering an access roster should treat workspace owners/admins
as implicit managers.

**PLANNED NARROWING — NOT SHIPPED.** A design exists, not yet ratified.
In outline: the overlay
narrows to a drive-level INVENTORY capability — `drives_list`,
`drives_read`, `drives_usage`, and the full `grants_list` roster, never
folders, artifacts, bytes, search, the content change feed, capability
mints, or any mutation; content access becomes an explicit, recorded,
expiring break-glass grant, which `_try_break_glass` already nearly
implements; and the overlay's orphan-prevention half moves to a Hub
membership-departure signal plus a computable "zero live managers" queue.
Until that ships, the behavior described above is the shipped behavior, and
this paragraph is the only forward-looking text in this section.

**The creator's drive-level manager grant is permanent (amended
2026-08-22).** A drive's access is grants and nothing else, so the row
`drives_create` mints for `created_by_principal_id` is the only thing making
the drive's creator its administrator. `grants_revoke` refuses it, and
`grants_update` refuses any change that would demote it below `manager` or
give it an `expires_at`; all three answer `409 GRANT_PERMANENT`. A no-op
re-PATCH of `manager` with no expiry still succeeds.

The rule is about the drive, not about who is asking — a second manager
cannot revoke the creator either, because a self-only rule still permits
"B revokes A, then B leaves". With it, **every live drive has at least one
live manager grant** — and with the workspace-admin overlay above, at
least one REACHABLE manager whenever the workspace has a live owner or
admin, which is the invariant that actually matters (a departed creator's
permanent row satisfies the grant-level statement while administering
nothing). Break-glass below is now a residual backstop behind both.
Deleting the drive remains the way to be rid of it.

Scope is exact: only the creator's DRIVE-level manager grant. Their grants
on folders and artifacts inside the drive are ordinary sharing and revoke
normally, as does the sponsor's founding manager row for an agent-created
drive. A drive with a null `created_by_principal_id` has no creator to pin
and behaves as it did before.

**Reading grants is narrower than reading the drive.** Listing or reading
grants requires the `sharing:read` scope, but what it returns depends on the
caller's role: a caller holding `manager` on the drive sees every grant in
it; every other caller sees only the grants that name them — their own
`agent`/`user` rows, a `workspace` grant covering them, and any `public`
grant (which already exposes the resource to them). The operations are never
refused for lack of `manager`: a principal must always be able to see the
access they hold. The drive's roster of principals, roles and expiries is
manager-only, and reading a single hidden grant by id returns the surface's
uniform `404 GRANT_NOT_FOUND` rather than confirming it exists.

Both `grants` and `shares` listings accept exact-match `resource_type` +
`resource_id` filters — "what access, or what links, exist on this
resource". `resource_id` requires `resource_type`, because a bare resource
id is ambiguous across the resource kinds; supplying one without the other
is `400 INVALID_PARAMETER`.

Reading grants is refused outright only for a caller holding no live grant
anywhere in the drive; a folder-scoped principal can always see the access
they themselves hold.

Artifact reads carry a server-computed `effective_visibility` of `public`,
`shared`, or `private`. It resolves over **both** read paths, because either
alone understates exposure:

- **grants** — the artifact's own grants, its whole folder ancestry (exactly
  as authorization resolves it), and the drive;
- **share links** — a live share on the artifact head, on any of its
  versions, or on an ancestor folder. A share consults no grant at all.

It is `public` when a live share link exists, or any reachable live grant has
principal type `public` — in both cases reach is not limited to any named
principal. It is `shared` when a reachable live grant names a principal
outside the drive's founding set (the creator plus the drive-manager grants
minted with the drive, for as long as those stay live — a founder whose grant
is revoked leaves the set). Otherwise it is `private`.

It describes the artifact's exposure, not the calling principal's own access:
two callers with different roles see the same value for the same artifact.

Lifecycle asymmetry is deliberate: drive and folder soft-delete/restore are
`manager` operations (container lifecycle is structural, and folder deletion
cascades); artifact soft-delete/restore are `editor` operations (file
lifecycle is ordinary content work, reversible and single-item).

Recovery invariant: every drive remains administrable by a human. While a
drive has zero active `manager` grants, a workspace administrator (role
`owner` or `admin`) of the drive's workspace may create a `manager` grant
for themselves through the normal grant-creation operation; AgentDrive
verifies the role from the Hub-issued token and records the recovery as an
audited grant creation (the durable grant row, with server-observed
attribution; a dedicated `drive.grant_recovered` change-feed entry is a
planned follow-up and is not yet emitted). This break-glass authorization
applies only while no active manager exists. Under the workspace-admin
overlay it is RESIDUAL: the same owner/admin already passes the ordinary
manager check, so the branch is unreachable unless the overlay is one day
narrowed — it is retained, and kept correct, precisely for that day.

Share links are separate read-only bearer capabilities over:

- an immutable `artifact_version` snapshot;
- a live artifact head; or
- a live folder subtree.

SDK artifact sharing defaults to an immutable snapshot. Live targets are
explicit. Secrets are returned only at create/rotate time, stored as hashes,
excluded from logs, and never present in list/get responses.

Authenticated agent collaboration uses grants, not bearer links.

## 9. Changes and jobs

`GET /v0/drives/{drive_id}/changes` accepts exactly one of:

```text
start=now
start=beginning
cursor=cur_...
```

It is a finite, cursor-resumable pull feed. It guarantees total order within a
drive, a captured high-water mark for each pagination cycle, an opaque resume
cursor even on empty pages, transactional state/change writes, and at-least-once
client processing deduplicated by `chg_*`.

The response uses the generic collection envelope extended with `has_more`:

```json
{
  "items": [
    {
      "id": "chg_...",
      "change_set_id": "cset_...",
      "type": "artifact.version.created",
      "drive_id": "drv_...",
      "actor": {
        "type": "agent",
        "id": "tcagt_..."
      },
      "resource": {
        "type": "artifact",
        "id": "art_..."
      },
      "previous_revision": "rev_...",
      "revision": "rev_...",
      "occurred_at": "2026-07-30T00:00:00.000000Z",
      "data": {}
    }
  ],
  "next_cursor": "cur_...",
  "has_more": false
}
```

The change document is exactly this field list. `actor` is the nested
authenticated principal (`agent` with a Hub `tcagt_*` id or `user` with a
`tcusr_*` id); the richer internal attribution (membership, runtime,
credential, request, trace, operation) is never exposed. `revision` is the
resource's head revision after the change; `previous_revision` is its
immediately prior revision (`null` when the resource had none, e.g. on
creation). `occurred_at` is the commit timestamp. The internal feed position
is never exposed — the opaque cursor alone carries it, so ordering and
contiguity are observed through cursor traversal, not a position field.

`has_more` distinguishes a short page from a drained cycle: it is `true`
while the successor cursor still sits below the cycle's captured high-water
mark (more committed rows remain to be read) and `false` once a page reaches
the high-water mark. Re-presenting a drained cursor follows changes committed
after the capture.

`order` selects the direction, and the two walks are for different jobs.
`order=oldest` is the default and the only resumable one: the forward sync
walk described above, whose drained cursor re-presented later picks up
whatever committed since. `order=newest` is a browse walk for a human reading
a history screen — it captures the head and descends toward the retention
floor, newest row first. Because new events land *above* a captured head, a
drained descending cursor stays drained forever; checking for new activity
means capturing the head again, not re-presenting the cursor. For the same
reason `order=newest` accepts only `start=now`, and `order` accompanies
`start` and never a `cursor` — the direction rides in the sealed cursor
(ascending carries `{a, h}`, descending `{b, f}`), so a caller never repeats
it and cannot flip a cursor into the other walk. Cursors minted before
`order` existed carry `{a, h}` and keep working unchanged. The manager-only
permission filter and the `limit + 1` page probe are the same predicates in
both directions.

The caller needs both Hub `changes:read` scope and a live local non-public
drive grant on every cursor capture and page read. Revoking the grant makes an
existing cursor unusable; a public share/grant does not confer authenticated
change-feed access. Cursors are stateless sealed tokens — nothing server-side
records a position, and cursors do not expire on time. A client that lost a
response re-presents the same cursor and receives the same page; the only
`410` is a cursor whose position has fallen behind the drive's change
retention floor.

The feed contains references and invalidation data, never bytes, presigned
URLs, share secrets, or full metadata documents. Resource `GET` remains
authoritative. Content changes include:

```text
drive.updated | drive.deleted | drive.restored
folder.created | folder.updated | folder.deleted | folder.restored
artifact.created | artifact.updated | artifact.deleted | artifact.restored
artifact.version.created
```

Recursive operations may share a `change_set_id`. (When the deferred jobs
vertical ships: an async job preserves the authenticated initiator as actor
and may add an executor; it does not replace the actor with a generic
worker.)

Expired cursors return `410 CHANGE_CURSOR_EXPIRED` with recovery
`full_sync`. Recovery captures `start=now`, enumerates current resources, then
replays from the captured cursor.

The change feed is not audit administration, CDC, SSE, a blockchain, Nostr, or
AT Protocol. Those may transport or verify the canonical envelope later.

**Jobs — DEFERRED, NOT SHIPPED IN v0.** The accepted design for the future
jobs vertical: jobs are only returned by domain operations that need
asynchronous execution. A job is created in the drive whose state it mutates,
and the returned `Location` is the authoritative place to poll. A job is
always readable and cancelable by its creating principal, independent of
drive-level grants: creator pollability waives the local-grant side of the
authorization intersection only, never the `jobs:read`/`jobs:write` scope.
The public API lists, reads, and requests cancellation. It exposes structured
outcomes, not raw worker logs. None of this is mounted in shipped v0 — every
shipped operation completes synchronously and cross-drive copy is rejected
with `400 INVALID_ARGUMENT`.

## 10. Derived SDK and MCP — FOLLOW-ON, NOT PART OF THIS CUTOVER

> The `/mcp` transport and the handwritten SDK ship after the REST surface
> and discovery freeze. Nothing in this section is mounted today.

Derivation order is fixed:

```text
raw HTTP → generated low-level transport → handwritten SDK → curated MCP
```

Neither SDK nor MCP invents another auth, error, pagination, concurrency, or
upload model.

The working MCP inventory is:

```text
overview, browse, find, read, create, update, create_folder,
move, delete, history, changes, access, share_link, upload
```

`upload` is the accepted canonical singular name. The dedicated MCP server
provides the namespace, so tool names do not carry an `agentdrive_` prefix.
MCP calls the handwritten SDK rather than duplicating business logic.

The MCP candidate is submitted to Anthropic's Claude directory only after the
deployed endpoint's tool names, descriptions, input schemas, and auth metadata
are smoke-tested and frozen. The current directory form introspects that live
surface.

## 11. Implementation slices

All slices land on one dedicated implementation branch as coherent commits.
The runtime/OpenAPI cutover remains atomic from a beta caller's perspective.

1. **Contract transfer and guardrails**
   - this repository-local design;
   - the exact 46-operation target manifest;
   - tests for count, uniqueness, domain counts, canonical origin, and legacy
     exclusions.
2. **Hub principal boundary**
   - Hub issuer/JWKS validation;
   - canonical principal/workspace/runtime context;
   - audience and scope enforcement;
   - removal of caller-asserted actor identity.
3. **Storage concurrency foundation**
   - `rev_*`, `ver_*`, idempotency records, uniform error envelope;
   - revision/ETag and precondition behavior;
   - stable-ID version model and attribution seams.
4. **Drive-scoped storage resources**
   - drives, folders, artifacts, versions, content, and direct uploads;
   - transactional namespace and soft-delete semantics;
   - cross-drive copy authorization and async-job handoff.
5. **Local access and collaboration**
   - grant resolution and additive-only inheritance;
   - snapshot/live shares and one-time secrets;
   - drive-scoped search.
6. **Changes and generic jobs**
   - transactional change log, cursor lifecycle, full-sync recovery;
   - job list/read/cancel without worker logs.
7. **Cutover and derived clients**
   - remove legacy `/v0` routes and local credential/OAuth surfaces;
   - regenerate the handler OpenAPI snapshot and prove it exactly matches the
     target manifest;
   - migrate the handwritten SDK and curate MCP;
   - switch all first-party callers.
8. **Release proof**
   - full suite plus cross-workspace/drive isolation tests;
   - real local HTTP/MCP smoke tests;
   - staging deployment and schema freeze;
   - Claude directory resubmission.

### 11.1 Current implementation checkpoint

**The atomic cutover has shipped.** `agentdrive.app` mounts exactly the
39-operation reset surface (the eight shipped verticals across
`api/v0_*.py`), the Hub-pointing protected-resource discovery
(`api/v0_discovery.py`), and the retained non-`/v0` surfaces: `/health`,
`/static`, and `/s/{share_key}` redemption. Nothing else is mounted — no
`/mcp` (follow-on), no uploads or jobs routers (deferred), no browser
sign-in, permalink, or render routes (they return with the workspace UI
follow-on). The legacy public surface — the path-addressed API, the
tokens/drives/members/workspaces control planes, legacy uploads, compile,
query, billing, feedback, AgentTag, the local OP with its JWKS/discovery,
MCP OAuth, the claim ceremony, the Jinja web routers, and the legacy MCP
registry — was moved wholesale to `archive/` (excluded from packaging and
test discovery; `tests/test_archive_is_unwired.py` pins that nothing
imports it). The OpenAPI golden describes 42 operations across 27 paths —
the 39 `/v0` operations plus `/health`, discovery, and redemption — and the
conformance gate (`tests/conformance/test_generated_contract.py`) pins the
REAL app's `/v0` surface to `src/agentdrive/api/v0-operations.json` by method,
path, and operationId, with `tests/test_openapi_snapshot.py` holding the
golden byte-fresh against the live app.

Checkpoint facts for the shipped surface:

- Hub product-token verification (issuer/audience/signature/claim-split) and
  scope enforcement are live; every `401` carries the RFC 6750 Bearer
  challenge with `resource_metadata` pointing at the RFC 9728 discovery
  document.
- **The schema is a day-0 baseline.** This branch ships `schema.sql` as the
  complete fresh-database schema and deletes the legacy migration chain
  (`0001`–`0043`; see `migrations/README.md`). The reset substrate that the
  implementation branch developed as migrations `0044`–`0052` was folded
  into the baseline before cutover — a reset deployment starts from
  `schema.sql`, replays nothing, and there is no legacy content for the
  reset to stay compatible with. The `v0_uploads` and jobs tables remain in
  the baseline as unused substrate for the deferred verticals.
- Drives, folders, artifacts, and versions implement the 27 storage
  operations exactly as §4–§6 describe: multipart-only create (JSON POST is
  a `415`), the shared sibling collision domain, root immutability,
  additive-only grant inheritance, manager-only deleted visibility,
  per-resource revisions, and
  transactional change rows. Artifact/folder lists bind every filter into
  the sealed cursor fingerprint. Content reads stream at/under the 16 MiB
  signed-download bound and `307` to a short-lived signed URL above it
  (streaming fallback when signing is unavailable), with
  ETag/`If-None-Match` → `304` intact. Copies are same-drive-only and
  synchronous; an optional `version_id` selects which immutable version's
  content an artifact copy references.
- Grants implement §8, including the workspace-admin overlay (resolved in
  `core.v0_authz.effective_role`, the single choke point, and mirrored as a
  flag parameter into every grant-filtered list/search query so listing
  parity holds), the permanent creator grant (`409
  GRANT_PERMANENT` from both `grants_revoke` and `grants_update`, resolved
  against `drives.created_by_principal_id` inside the same transaction as
  the `FOR UPDATE` row read) and the residual break-glass recovery
  invariant: the
  zero-manager count and the recovery insert run in one transaction under a
  drive-row `FOR UPDATE` lock, so concurrent administrators serialize to a
  single winner. Recovery is durably recorded as the grant row itself; the
  dedicated `drive.grant_recovered` change-feed entry is a planned
  follow-up. Routine grant mutations emit no change-feed entries.
- Shares implement §8 with drive-manager-only administration **and
  visibility**: list and read are manager-gated in v0 (share metadata is an
  admin surface). This may widen in a later release (creator-scoped or
  resource-scoped); the preferred future vehicle for collaborator awareness
  is a per-resource indicator, not the drive-wide list. The plaintext
  secret is returned exactly once — on first execution of create or rotate;
  idempotent replay returns the stored result with `"secret": null`, and
  rotate is the documented recovery.
- Pagination across all six resource lists and search/changes uses one
  sealed HMAC cursor system bound to collection kind, drive (or workspace),
  and the normalized filter fingerprint; any malformed, tampered,
  cross-collection, cross-drive, or filter-changed cursor is the single
  `400 INVALID_CURSOR`.
- The reset intentionally does not preserve the legacy "last live drive"
  guard: a Hub workspace may have zero drives, then create one later. Hub
  owns workspace lifecycle; AgentDrive owns optional drive lifecycle.
- Module layout: HTTP routers use the `v0_` prefix; cross-cutting revision,
  idempotency, cursor, and change primitives are unprefixed because every
  storage domain shares them. Core mutation results retain status, headers,
  and exact bytes because those are the idempotent replay record. The
  per-router `_run_mutation` copies are acknowledged duplication — a shared
  mutation runner is a cleanup candidate now that five shapes exist.

The eight shipped verticals — drives, folders, artifacts, versions, grants,
shares, search, and changes — landed together in the atomic cutover. The
public OpenAPI golden describes the reset runtime contract exactly, including
the generic failure set (401/403/429/400), mandatory
`Idempotency-Key`/`If-Match` headers, and per-operation multipart schemas.

### 11.2 OpenAPI transition

`tests/openapi.golden.json` must always remain byte-for-byte fresh with the
currently running FastAPI handlers
(`tests/test_openapi_snapshot.py::test_openapi_snapshot_matches_golden_file`);
replacing it with an unimplemented document would defeat that gate. The
migration sequence this section originally described has completed: the
cutover removed the legacy route registration, registered the reset routers,
regenerated the golden, and `tests/conformance/test_generated_contract.py`
holds exact `(method, path, operationId)` equality between the live `/v0`
surface and `src/agentdrive/api/v0-operations.json`. An intentional contract change
is made by changing the routers, regenerating the golden, and reviewing the
golden diff in the same commit.

### 11.3 OpenAPI authoring checklist

Originally the list of prose-level gaps the executable snapshot had to
close. Status after cutover and the spec-truthfulness pass:

- **Closed in the served spec:** the `/copy` request body and its `201`
  synchronous status (same-drive-only); grant revisions/`ETag`; search
  parameter names, mode enum, and defaults; multipart part names and
  per-operation required fields for create and version append; the generic
  failure responses (401/403/429/400) and mandatory
  `Idempotency-Key`/`If-Match` headers.
- **Moot until the deferred verticals ship:** job and upload state
  enumerations, job visibility, expired-upload `GET` behavior, `202`
  status contracts.
- **Still open:** the complete machine-readable error-code catalogue
  (`api/error_codes.py` is the registry; serving it as a spec appendix is
  pending); exact `RateLimit-*` field names (edge follow-up); per-collection
  default sort keys in operation descriptions; duplicate-grant and
  out-of-workspace-principal semantics in the grant operation descriptions;
  whether the frozen launch contract ships as `v0` or is re-badged, and its
  post-freeze deprecation posture.

### 11.4 Cutover checklist pins

Two invariants the atomic cutover must enforce explicitly:

- **Error-envelope registration.** The public app must register
  `v0_validation_error_handler` (`api/v0_errors.py`) for
  `RequestValidationError` at cutover, alongside the `V0ApiError` handler.
  Without it, FastAPI's default 422 shape leaks onto `/v0` and the single
  error-envelope contract (§6.3) breaks.
- **Legacy writers cannot touch reset-created drives.** Under the day-0
  baseline this pin is satisfied structurally: the legacy handlers live in
  `archive/` and are neither mounted nor importable
  (`tests/test_archive_is_unwired.py`), and a reset deployment has no
  legacy content. The originally-planned database guard (reject
  revision-unchanged visible UPDATEs on folders/artifacts) remains a
  worthwhile defense-in-depth candidate for the baseline, but nothing
  mounted today can violate the invariant.

## 12. Verification gates

Contract:

- exactly 39 target operations and no duplicate method/path pair
  (`tests/conformance/test_manifest_contract.py`,
  `test_generated_contract.py`);
- the served `servers[0]` is this deployment's canonical API origin;
  `https://drive.tokencanopy.com` is the canonical API origin in
  production, while `https://api.agentdrive.run` is a temporary retirement
  alias and never a supported-client default;
- no mutable path routes or old control-plane/product-experiment routes;
- every collection paginates;
- every create documents `201` and `Location`;
- every existing-resource mutation documents `428` and `412`, except the
  creation-flavored `/copy` POSTs the contract exempts (optional validated
  `If-Match`, no `428`);
- one error envelope and catalogued error codes.

Authorization:

- wrong issuer, audience, signature, expiry, and workspace fail closed;
- token scope cannot expand a local grant and a local grant cannot expand
  token scope;
- cross-drive/cross-workspace IDOR attempts reveal nothing;
- membership or a token without content scope/local grant confers no read;
- drive creation atomically creates only the bootstrap manager grants — the
  creating principal and, for an agent creator, its sponsoring controller;
- a human product token (`sub` is `tcusr_*`) exercises user grants and
  produces `user` change actors; no other human path reaches `/v0`;
- the workspace-admin overlay: an owner/admin (human token) reads, lists,
  and administers every drive in their own workspace with no grant row; a
  member still cannot reach another member's drive; an agent without a
  grant is denied regardless of any claim it carries; overlay access never
  crosses workspaces and never exceeds token scope;
- workspace-administrator (owner or admin) break-glass grant creation
  succeeds only while a drive has zero active managers and is recorded as
  an audited grant — residual behind the overlay;
- actor attribution always comes from the verified token.

Data integrity:

- mutation and change record commit or roll back together;
- concurrent metadata/content/delete/restore races are pinned;
- idempotent replay returns the original IDs and result;
- version restore creates immutable history;
- cross-drive copy is all-or-nothing at its destination.

Content and retrieval:

- checksum mismatch on multipart create/append, quota races, stale version
  preconditions (uploads-session cases — interruption, expiry, duplicate
  completion, orphan cleanup — move to the deferred vertical);
- search never returns content outside capability;
- change replay has no gaps across disconnects/page boundaries;
- expired cursors follow the documented full-sync recovery.

Release:

- focused tests after every slice;
- full unit/integration suite;
- real over-the-wire HTTP exercise (MCP exercise moves to the MCP
  follow-on);
- independent and adversarial review;
- staging smoke tests against the deployed release candidate.

## 13. Non-goals and future seams

V0 does not expose CRDT documents, public Merkle proofs, AT Protocol
federation, Nostr transport, realtime chat/presence/tasks, a generic query
platform, or full conversation/model/tool provenance. There is no public
purge operation: purge of soft-deleted resources is automatic after the
plan's retention window.

The implementation preserves forward-compatible seams:

- internal MVCC behind `rev_*`;
- parent-version relationships for future merge DAGs;
- immutable ordered changes for optional signed checkpoints;
- opaque cursors independent of a database LSN;
- Hub canonical identity independent of an external identity provider;
- server-observed attribution and W3C trace correlation for a later shared
  Token Canopy provenance catalog;
- a distinct transfer-only drive `owner` role above `manager`, if drives
  become directly human-owned assets.

No AgentDrive HTTP product decision remains open. Operational retention,
session lifetime, object-size, and indexing SLO values must be set before
production without changing these wire semantics.
