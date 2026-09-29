"""Canonical registry of every PUBLIC error code AgentDrive emits.

Every machine-matchable error code that can reach a client through the
top-level ``{"error": {"code": ...}}`` envelope (contract §6.3) must appear
here. Since the v0 contract reset, the public surface is exactly the mounted
``/v0`` routers, the central handlers in ``app.py``, and the ``/s/{share_key}``
redemption route — the legacy surfaces (web routes, MCP transport, compile,
query, billing, feedback, AgentTag) live unmounted in ``archive/`` and their
codes are no longer part of the public contract.

The contract (enforced by ``tests/test_error_code_registry.py`` in BOTH
directions):

  * **Adding a code is deliberate.** Add it here AND emit it; the drift
    guard fails if a new literal shows up in the source that isn't
    registered — or if a registered code is no longer emitted anywhere
    (dead entries rot the doc).
  * **Removing (or renaming) a code is a breaking API change.** Agents
    branch on these strings. Grep consumers — SDKs, the Skill, docs,
    fixtures — before touching an existing entry.

Grouping below is by family, comments only — the registry itself is one
flat frozenset.
"""

from __future__ import annotations

ERROR_CODES: frozenset[str] = frozenset(
    {
        # ── auth (401) — the /v0 actor boundary ──────────────────────────
        "AUTHENTICATION_REQUIRED",
        # ── scope (403) ──────────────────────────────────────────────────
        "PERMISSION_DENIED",
        # ── validation / malformed request (400 / 422) ───────────────────
        "VALIDATION_ERROR",
        "BAD_REQUEST",  # DataError/Overflow backstop in app.py
        "INVALID_ARGUMENT",
        "INVALID_QUERY",
        # a query parameter is individually well-formed but violates a
        # RELATIONSHIP between parameters — today: `resource_id` supplied
        # without the `resource_type` that disambiguates it (grants_list,
        # shares_list). Distinct from INVALID_ARGUMENT (a bad value) and
        # INVALID_QUERY (a parameter that does not exist at all), so a
        # client can tell "you sent junk" from "you sent half a filter".
        "INVALID_PARAMETER",
        "INVALID_REQUEST",  # changes: exactly one of start= / cursor=
        "INVALID_CURSOR",
        "SEARCH_MODE_UNAVAILABLE",
        # ── not-found / gone (404 / 405 / 410) ───────────────────────────
        "NOT_FOUND",
        "METHOD_NOT_ALLOWED",  # dynamic — see _DYNAMIC_ONLY in the test
        "DRIVE_NOT_FOUND",
        "FOLDER_NOT_FOUND",
        "ARTIFACT_NOT_FOUND",
        # a version reference that is missing or belongs to another artifact
        # — distinct from ARTIFACT_NOT_FOUND so a bad version_id on read/
        # content/restore/copy never claims the ARTIFACT itself is gone
        "VERSION_NOT_FOUND",
        "GRANT_NOT_FOUND",
        "SHARE_NOT_FOUND",
        # the uniform refusal for the private viewer surface: unknown,
        # expired, deleted-target, and revoked-grant viewer credentials all
        # collapse to this one code (anti-enumeration, §7.1)
        "VIEWER_SESSION_NOT_FOUND",
        # within-workspace unauthorized access — deliberately distinct from
        # *_NOT_FOUND (contract §6.1: in-workspace existence is not a secret)
        "NOT_AUTHORIZED",
        "CHANGE_CURSOR_EXPIRED",
        # ── media negotiation (406) ──────────────────────────────────────
        # the uploads surface's strict JSON negotiation (B3 §5.1): Accept
        # must include JSON
        "NOT_ACCEPTABLE",
        # ── conflict (409) ───────────────────────────────────────────────
        "CONFLICT",
        "ARTIFACT_PATH_CONFLICT",
        "FOLDER_PATH_CONFLICT",
        "FOLDER_RECURSIVE_REQUIRED",
        "GRANT_CONFLICT",
        # 409: the drive creator's own drive-level manager grant is
        # permanent. Revoking, demoting, or expiring it would leave a drive
        # nobody can administer, so the API refuses rather than relying on
        # the workspace-administrator break-glass to put it back.
        "GRANT_PERMANENT",
        "DRIVE_LIMIT_EXCEEDED",
        # a declared sha256 does not match the content bytes (contract §7)
        "CHECKSUM_MISMATCH",
        "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_IN_PROGRESS",
        "IDEMPOTENCY_KEY_REQUIRED",
        # ── B3 direct-upload sessions (2026-08-14 design §5.8) ───────────
        # 409: publication-time namespace collision (a rival took the name)
        "NAME_CONFLICT",
        # 409: cancel attempted after publication committed
        "UPLOAD_ALREADY_COMPLETED",
        # 409: another completion/cancel owns the transition fence
        "UPLOAD_BUSY",
        # 409: no finalized scratch object yet; session returned to active
        "UPLOAD_INCOMPLETE",
        # 409: terminal state cannot publish (rejected/expired/cancelled)
        "UPLOAD_NOT_COMPLETABLE",
        # 413: declared size exceeds the enabled direct-transfer ceiling
        "PAYLOAD_TOO_LARGE",
        # 422: observed GCS size differs from the declaration
        "OBJECT_SIZE_MISMATCH",
        # 422: mandatory finalized metadata absent or adoption identity
        # invariants do not match
        "OBJECT_METADATA_MISMATCH",
        # 422: publication deadline elapsed; publication is closed
        "UPLOAD_EXPIRED",
        # 422: reservation/session hard bound cannot be acquired
        "TRANSFER_LIMIT_EXCEEDED",
        # 503: direct transfer is not fully configured/enabled; no fallback
        "TRANSFER_DISABLED",
        # 503: provider stat/rewrite transient or ambiguous; fenced
        # reconciliation continues without duplicate work
        "TRANSFER_UNAVAILABLE",
        # 503: the direct download signer/configuration is unavailable —
        # fail closed, no stream/redirect/viewer fallback (packet 4 §5.7)
        "DOWNLOAD_SIGNING_UNAVAILABLE",
        # 503: the private viewer host is not bound on this deployment
        # (`viewer_base_url` unset), so a viewer-session credential would be
        # redeemable nowhere — the mint fails closed instead of issuing it.
        # Operator enablement, no fallback, no Retry-After (the
        # TRANSFER_DISABLED rule).
        "VIEWER_DISABLED",
        # ── sheets / edit sessions (2026-08-22 design §5.9) ─────────────
        # 409: formulas, unsupported features, macros, or an artifact that is
        # not a spreadsheet at all. NEVER 404 — the artifact exists and the
        # caller can see it in a listing.
        "WORKBOOK_NOT_EDITABLE",
        # 422: bytes are not a readable workbook of the declared type
        "WORKBOOK_UNPARSEABLE",
        # 404: a named sheet is absent — distinct from ARTIFACT_NOT_FOUND so a
        # bad sheet name never claims the ARTIFACT is gone
        "SHEET_NOT_FOUND",
        # 404: unknown, discarded, or another drive's session — ONE code for
        # every case, because distinguishing them is an enumeration oracle
        "SHEET_SESSION_NOT_FOUND",
        # 409: the lease elapsed; pending edits were not saved
        "SHEET_SESSION_EXPIRED",
        # 409: a terminal session cannot be written to or completed again
        "SHEET_SESSION_ALREADY_COMPLETED",
        # 409: the per-session edit or cell budget is exhausted
        "SHEET_EDIT_LIMIT_EXCEEDED",
        # 413: above the session cell cap. Distinct from ARTIFACT_TOO_LARGE
        # because the remedy differs — split the workbook, not use transfer
        "WORKBOOK_TOO_LARGE",
        # ── preconditions (412 / 428) ────────────────────────────────────
        "PRECONDITION_REQUIRED",
        "PRECONDITION_FAILED",
        # ── media types (415) ────────────────────────────────────────────
        "UNSUPPORTED_MEDIA_TYPE",
        # ── limits (413 / 429) ───────────────────────────────────────────
        "ARTIFACT_TOO_LARGE",
        "SUBTREE_TOO_LARGE",
        "RATE_LIMITED",
        "BANDWIDTH_LIMIT_EXCEEDED",
        # ── server (5xx) ─────────────────────────────────────────────────
        "INTERNAL_ERROR",
        # 503: the /v0 auth boundary itself is unavailable (Hub JWKS
        # unreachable) — OUR unavailability, not the caller's fault
        "AUTH_UNAVAILABLE",
    }
)
