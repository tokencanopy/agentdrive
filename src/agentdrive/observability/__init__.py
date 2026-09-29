"""LLM-friendly structured logging for AgentDrive.

Every log line carries a self-describing bracket prefix so an agent (or
human) can grep one identifier and reconstruct the trace:

    [req=ab12cd34] [drive=drv_xyz] [GET /v0/artifacts] msg
    [task=indexer] [run=ef56gh78] [drive=drv_xyz] msg

Identifiers come from ContextVars set at the boundaries — REST middleware,
MCP transport, background-task wrappers — and the `ContextFilter`
attaches them to every `LogRecord` automatically. Modules log normally
via `logging.getLogger(__name__)`; the prefix is applied at format time.

Public API:
  * `setup_logging()`                         — install filter + formatter
  * `RequestContextMiddleware`                — REST request prefix
  * `set_drive_context(drive_id) / set_operation(op)` — boundary helpers
  * `task_context(name, drive_id=?)`          — background-task wrapper
"""

from .context import (
    artifact_id,
    drive_id,
    operation,
    organization_id,
    request_id,
    set_artifact_context,
    set_drive_context,
    set_operation,
    set_organization_context,
    set_user_context,
    task_name,
    task_run_id,
    user_id,
)
from .logging_setup import setup_logging
from .middleware import RequestContextMiddleware
from .tasks import task_context

__all__ = [
    "RequestContextMiddleware",
    "artifact_id",
    "drive_id",
    "operation",
    "organization_id",
    "request_id",
    "set_artifact_context",
    "set_drive_context",
    "set_operation",
    "set_organization_context",
    "set_user_context",
    "setup_logging",
    "task_context",
    "task_name",
    "task_run_id",
    "user_id",
]
