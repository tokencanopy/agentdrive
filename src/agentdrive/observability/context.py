"""ContextVars carrying the bracket-prefix fields.

These are read by the `ContextFilter` in `logging_setup` so module code
never has to pass identifiers through function signatures. Each boundary
(REST middleware, MCP transport wrapper, task_context, auth dep) sets
the contextvars it knows about; everything inside that scope inherits
them across `await` boundaries.

Naming is verbose on purpose — `drive_id` reads better in tools.py than
`d` and matches the bracket-prefix field name an agent will grep for.
"""

from contextvars import ContextVar, Token

# Foreground request identifiers.
request_id: ContextVar[str | None] = ContextVar("ad_request_id", default=None)
operation: ContextVar[str | None] = ContextVar("ad_operation", default=None)

# Background task identifiers.
task_name: ContextVar[str | None] = ContextVar("ad_task_name", default=None)
task_run_id: ContextVar[str | None] = ContextVar("ad_task_run_id", default=None)

# Shared across both — present whenever a tenant is in scope.
drive_id: ContextVar[str | None] = ContextVar("ad_drive_id", default=None)

# WorkOS integration (PR 2 / S2): user + organization scope. Set by the
# auth path when bearer-token resolution or session resolution can name
# the human and the org behind the request. Optional — bearer-token
# callers without an associated user (rare; only legacy fixtures) leave
# `user_id` empty, and any request scope without an active org leaves
# `organization_id` empty.
user_id: ContextVar[str | None] = ContextVar("ad_user_id", default=None)
organization_id: ContextVar[str | None] = ContextVar("ad_organization_id", default=None)

# Optional artifact-scoped marker (set by handlers that operate on a
# specific artifact, e.g. the indexer worker per-row, the upload route
# after persistence). Pinpoints log spelunking when one of many artifacts
# in the same request misbehaves.
artifact_id: ContextVar[str | None] = ContextVar("ad_artifact_id", default=None)


def set_drive_context(value: str | None) -> Token:
    """Stamp `drive=` on every subsequent log line in this context.

    Returns a Token. Two valid cleanup strategies:

    1. **Hold the token, reset at scope exit.** Standard pattern for
       any caller running outside a `RequestContextMiddleware` /
       `task_context` scope (e.g., a one-off script, a test helper).
    2. **Discard the token; rely on an upstream stake.** Both
       `RequestContextMiddleware` and `task_context` stake a `drive_id`
       token at their boundary and reset it on exit — so any inner
       `set_drive_context(...)` call inside that scope is rolled back
       automatically. `auth.authed_drive` uses this strategy because
       FastAPI deps don't have a natural "request done" hook to reset
       on.
    """
    return drive_id.set(value)


def set_operation(value: str | None) -> Token:
    """Stamp `[<operation>]` (e.g. `GET /v0/artifacts`, `mcp.search`)
    on every subsequent log line in this context."""
    return operation.set(value)


def set_artifact_context(value: str | None) -> Token:
    """Stamp `[artifact=art_…]` on every subsequent log line. Use inside
    a per-artifact loop in an indexer worker, or inside a REST handler
    once the artifact ID is known."""
    return artifact_id.set(value)


def set_user_context(value: str | None) -> Token:
    """Stamp `[user=usr_…]` on every subsequent log line. Set by the
    auth path once the request's user identity is known.

    Same cleanup model as `set_drive_context` — `RequestContextMiddleware`
    and `task_context` stake `user_id.set(None)` at boundary and reset
    on exit, so inner sets inside that scope are rolled back."""
    return user_id.set(value)


def set_organization_context(value: str | None) -> Token:
    """Stamp `[org=org_…]` on every subsequent log line. Set by the
    auth path when the request operates against an organization
    (bearer-token: the drive's owning org; session: the active_org).

    Same cleanup model as `set_drive_context`."""
    return organization_id.set(value)
