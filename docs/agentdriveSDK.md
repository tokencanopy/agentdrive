# AgentDrive Python SDK Design

- **Status:** Accepted architecture for rebuilding the existing SDK pipeline
- **Date:** 2026-08-09 (revised 2026-09-13)
- **Distribution:** `agentdrive-sdk` (already ours on PyPI; see below)
- **Import:** `agentdrive_sdk`

> **Private-beta amendment (ratified 2026-09-07):** The published Python,
> TypeScript, and Go SDKs are preview clients, not supported private-beta
> surfaces. Every authenticated `/v0` operation they describe is beta. Do not
> present an SDK as supported until it is regenerated from the reviewed
> OpenAPI, published coherently, and its caller journey passes.

## Decision

The Python SDK will be delivered in two phases:

1. Generate a complete SDK core from AgentDrive's authoritative OpenAPI
   contract.
2. Add a smaller handwritten Python facade that composes the generated core.

The generated core is the API contract and the escape hatch for complete
coverage. The facade improves caller experience; it is not a second HTTP
implementation.

This follows the separation used by the `tokencanopy/e2a` SDK pipeline: a
pinned and committed generated base, deterministic drift and compatibility
gates, then handwritten workflow helpers over that base.

### This is a rebuild, not a greenfield package

`tokencanopy/agentdrive-sdk` already exists (public, formerly under
`Mnexa-AI`), already ships generated Python, TypeScript, and Go SDKs, and
already publishes `agentdrive-sdk` to PyPI (0.0.1, 2026-06-14). Its current
pipeline is the model this design replaces: it regenerates weekly from the
live endpoint's spec, has no drift or compatibility gates, and its committed
snapshot is stale — 175 operations from before the v0 contract reset.

This design therefore rebuilds that repository's pipeline in place. The PyPI
distribution name is already ours; releases under this design supersede the
0.0.1 placeholder. Until a release built under this design ships, the
published package must not be treated as a supported client — the shipped
REST API and served OpenAPI remain the way to integrate.

## Scope

The committed feature-on staging OpenAPI snapshot has 63 operations.
Production's feature-off document has 55 and is the canonical snapshot:

- 51 bearer-authenticated `/v0` operations in production, all beta — drives, folders, artifacts,
  versions, search, changes, grants, and shares, plus the two namespace
  navigation reads, the viewer-session mint, the four B3 direct-upload
  session controls, the B3 download-capability mint, and committed spreadsheet
  reads. Staging enables eight additional spreadsheet edit-session operations,
  making the catalog 59. This note deliberately does not name them: the
  generated documentation is the only exact API reference, which
  `test_python_sdk_markdown_does_not_handwrite_the_exact_api_reference`
  enforces by banning every operation id from this file;
- health;
- OAuth protected-resource discovery, at two paths — the drive resource and
  the hosted MCP's own; and
- anonymous share redemption.

Phase 1 covers all 55 production operations exactly as described by the
feature-off OpenAPI; the feature-on 63-operation staging snapshot is a preview
input until sheet sessions are promoted. Phase 2 groups the 51 production
authenticated operations into an ergonomic resource facade. The four public
operations remain available through the generated core without being added to
that facade.

These counts are asserted against `src/agentdrive/api/v0-operations.json` and
the golden snapshot by `test_documented_operation_counts_match_the_contract`;
they had drifted three times before that test existed, because a number
transcribed into prose has nothing holding it to the surface it describes.

## Phase 1: generated SDK core

### Source and reproducibility

- The input is the reviewed OpenAPI snapshot committed by the AgentDrive
  server contract tests.
- SDK provenance records the AgentDrive source commit, snapshot path, snapshot
  digest, and generator identity.
- OpenAPI Generator runs from the pinned
  `openapitools/openapi-generator-cli:v7.16.0` container image — the same pin
  the e2a pipeline uses (its Makefile and both `generate-oag.sh` scripts).
  Moving the pin is a reviewed change, never an implicit upgrade.
- One core is generated with `-g python`, `library=httpx`: a single
  asynchronous transport implementation. Following e2a, there is exactly one
  generated implementation of request construction, serialization, and
  response parsing; the synchronous entry point is a handwritten facade bridge
  over this one core (Phase 2), not a second generated transport. A second
  generated core would recreate, inside the SDK, the drift surface this
  design exists to eliminate.
- One documented command prepares the snapshot and regenerates the core with
  the committed options.
- Generated output is committed so reviewers and releases see the exact code.

### Spec preparation

The committed snapshot is OpenAPI 3.1, and OpenAPI Generator rejects its
nullable-`$ref` shapes. As in e2a (its `e2a-openapi-codegen-normalize` step),
generation is preceded by a deterministic preparation stage owned by this
pipeline:

- normalize the 3.1 snapshot to the 3.0 shapes the generator accepts;
- post-process generator input and output where required — strip enum
  constraints from additive server vocabularies so unknown members do not
  break older SDKs, and clean up generated imports;
- run the generator with full spec validation. `--skip-validate-spec` is not
  an acceptable substitute for normalization.

The preparation stage is part of the deterministic pipeline: its input,
transforms, and output are committed and reproducible like the generated code
itself.

### Isolation and ownership

Generated Python code lives in the clearly isolated `agentdrive_sdk.generated`
namespace and is never hand-edited. The handwritten package uses a separate
source tree, so regeneration replaces only generated output and cannot erase
the facade.

The generated core includes:

- the low-level asynchronous operation client;
- generated request and response models;
- authentication and transport plumbing;
- multipart, binary response, conditional response, and redirect behavior; and
- the original OpenAPI operation names without ergonomic renaming.

### Required gates

Phase 1 is complete only when all of these gates are automated and green:

- **Snapshot drift:** the SDK input matches the reviewed server snapshot and
  its recorded provenance.
- **Deterministic regeneration:** rerunning the pinned preparation and
  generator stages produces no tracked or untracked diff.
- **Full contract shape:** machine checks cover operations, parameters,
  requiredness, nullability, request schemas, response schemas, content types,
  statuses, headers, multipart bodies, downloads, and redirects. Checking only
  operation-name membership is insufficient.
- **Compatibility:** an OpenAPI backwards-compatibility gate rejects breaking
  changes to stable non-v0 operations unless the compatibility policy
  explicitly permits them. Every `/v0` operation is beta during private beta;
  its snapshot diff still requires coordinated contract and SDK review.
- **Model evolution:** generated-model tests define and verify behavior for
  additive fields and unknown enum values.
- **Live conformance:** shared scenarios exercise authentication, pagination,
  multipart uploads, conditional reads, idempotency, structured failures, and
  redirect credential stripping through the generated core.

The generated core must pass these gates before implementing the complete
Phase 2 facade.

## Phase 2: ergonomic Python facade

The facade is intentionally smaller and handwritten. It may provide:

- resource grouping for drives, folders, artifacts, versions, search, changes,
  grants, shares, and viewer sessions;
- asynchronous and synchronous client entry points — the synchronous client is
  the e2a-style bridge over the single generated core, so resources, retries,
  errors, and pagination have exactly one implementation, and a mechanical
  parity check pins the two entry points to the same surface;
- static tokens and refresh-capable token providers;
- cursor iterators and checkpoint-oriented change consumption;
- path, bytes, and stream conveniences for inline uploads;
- safe streaming and file conveniences for downloads;
- idempotency-key creation and bounded retry policy;
- revision and conditional-read helpers; and
- typed domain errors that retain generated response context.

Every facade wrapper must map explicitly to a generated operation and compose
generated request and response models. It must not duplicate endpoint paths,
HTTP serialization, wire models, response parsing, status tables, or error
envelopes.

### Caller-experience principles

- **Idempotency remains visible.** The facade may create a key for one logical
  mutation, but it must reuse that key and the same generated request across a
  safe retry.
- **Revisions remain explicit.** The caller decides how to reconcile stale
  state; the SDK never fetches a newer revision and silently overwrites.
- **Cursors remain opaque.** Iterators preserve the original collection and
  filters, and change consumers persist a checkpoint only after processing a
  page successfully.
- **Transfers preserve safety.** Inline uploads enforce the server's bounded
  content rules. Download redirects never forward the AgentDrive bearer token
  to storage, while retaining useful AgentDrive response metadata.
- **One-time secrets remain one-time.** Share helpers do not assume a replay can
  recover plaintext and do not derive a share URL from the API origin.
- **Deployment prefixes are preserved.** A configured API base may include a
  path prefix and remains authoritative.

## Documentation strategy

The exact SDK API reference will be generated in the `agentdrive-sdk`
repository from the same OpenAPI input and the generated Python surface and
docstrings as the SDK core. It will include callable signatures, models,
parameters, operation mappings, responses, headers, and error contracts.
Deterministic regeneration and a documentation drift guard will keep that
reference synchronized with code.

Handwritten documentation adds context rather than restating the contract:

- installation, authentication, and quickstarts;
- end-to-end AgentDrive workflows and recipes;
- explanations of idempotency, revisions, pagination, transfers, and sharing;
- facade examples;
- migration guidance; and
- troubleshooting.

This document is the concise architecture record. It is not the production SDK
API reference.

## Implementation sequence

1. Import the reviewed 63-operation OpenAPI snapshot with provenance into
   `tokencanopy/agentdrive-sdk`, replacing the stale pre-reset snapshot and
   the live-endpoint regeneration flow.
2. Land the deterministic preparation stage (3.1→3.0 normalization and
   post-processing), pin the generator image and configuration, and commit
   the isolated async core.
3. Add snapshot, deterministic regeneration, full-shape, compatibility,
   model-evolution, and generated documentation gates.
4. Run shared live conformance scenarios through the generated core.
5. Implement Phase 2 facade primitives — including the synchronous bridge —
   and one vertical resource slice using only generated operations and models.
6. Expand facade coverage with explicit mappings and facade sync/async parity
   tests.
7. Publish only after generated-core and facade acceptance gates pass.
   `agentdrive-sdk` 0.0.3 is on PyPI as of 2026-08-28, superseding the 0.0.1
   placeholder; it is the version the TypeScript facade ships under too.

## Acceptance criteria

- The generated core covers all 63 operations and is reproducible from one
  prepared contract, isolated, committed, and never hand-edited.
- The synchronous facade bridge exposes the same surface as the asynchronous
  entry point, verified mechanically, with one underlying implementation.
- Breaking OpenAPI changes and stale generated code or documentation fail CI.
- Generated models have tested forward-compatibility behavior.
- Every Phase 2 wrapper has one explicit generated-operation mapping and live
  conformance coverage.
- No handwritten facade module duplicates wire request construction, response
  parsing, models, or contract tables.
- Generated documentation is the only exact API reference.
- This Markdown file remains the single reviewable architecture source in the
  AgentDrive server repository.
