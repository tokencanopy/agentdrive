"""Bracket-prefix formatter and ContextFilter installation.

The formatter pulls fields off `LogRecord` (placed there by
`ContextFilter`) and assembles a prefix in a stable order so an agent
can grep deterministically:

    [req=<id>] [task=<name>] [run=<id>] [user=<id>] [org=<id>]
    [drive=<id>] [<operation>] [artifact=<id>]

Only fields that are present render — a background task line drops
`req=`, a REST line drops `task=`/`run=`, a free-tier user without an
active organization drops `org=`, etc. The fields themselves are
always lowercase, always `key=value`, with no embedded spaces or
brackets in values (IDs come from `secrets.token_hex` or our own
`drv_*`/`usr_*`/`org_*`/`art_*` prefixes, which are safe).

The format string is fixed (no `LOG_FORMAT=json` toggle yet) — Cloud
Logging happily ingests the bracket form as `textPayload` and lets you
filter on a substring. A JSON formatter can be slotted in later via the
same `ContextFilter` without touching call sites.
"""

import logging

from ..config import settings
from . import context as ctx
from .redaction import redact_share_path

_PREFIX_ORDER = (
    ("req_id", "req"),
    ("task_name", "task"),
    ("task_run_id", "run"),
    ("user_id_ctx", "user"),
    ("organization_id_ctx", "org"),
    ("drive_id_ctx", "drive"),
    ("operation", None),  # rendered as bare `[<value>]`
    ("artifact_id_ctx", "artifact"),
)


class ContextFilter(logging.Filter):
    """Copy the observability ContextVars onto every LogRecord.

    `LogRecord` doesn't natively have these fields, so we attach them
    here. Some attribute names collide with `LogRecord`'s built-ins
    (e.g. `record.name` is the logger name) — we suffix the colliding
    ones with `_ctx` so the formatter doesn't shadow logging internals."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.req_id = ctx.request_id.get()
        record.task_name = ctx.task_name.get()
        record.task_run_id = ctx.task_run_id.get()
        # `record.drive_id` would collide with no-one in practice, but we
        # keep the `_ctx` suffix to be defensive against future stdlib
        # changes and to make the formatter's name space explicit.
        record.user_id_ctx = ctx.user_id.get()
        record.organization_id_ctx = ctx.organization_id.get()
        record.drive_id_ctx = ctx.drive_id.get()
        record.operation = ctx.operation.get()
        record.artifact_id_ctx = ctx.artifact_id.get()
        return True


class BracketFormatter(logging.Formatter):
    """`asctime LEVEL name: [req=…] [drive=…] [operation] message`.

    Order is fixed by `_PREFIX_ORDER` so identical contexts render
    identical prefixes — important for both grep ergonomics and snapshot
    testing."""

    def __init__(self, *, base_fmt: str = "%(asctime)s [%(levelname)s] %(name)s:"):
        super().__init__()
        self._base = logging.Formatter(base_fmt)

    def format(self, record: logging.LogRecord) -> str:
        # Render the message *without* the bracket prefix first, then
        # splice the prefix between the base header and the message body
        # so structured fields stay before the human text.
        base_head = self._base.format(record)
        prefix = _render_prefix(record)
        msg = record.getMessage()
        if record.exc_info:
            # Append the traceback like the stdlib formatter does.
            msg = msg + "\n" + self.formatException(record.exc_info)
        if prefix:
            return f"{base_head} {prefix} {msg}"
        return f"{base_head} {msg}"


def _render_prefix(record: logging.LogRecord) -> str:
    parts: list[str] = []
    for attr, key in _PREFIX_ORDER:
        value = getattr(record, attr, None)
        if value is None:
            continue
        if key is None:
            parts.append(f"[{value}]")
        else:
            parts.append(f"[{key}={value}]")
    return " ".join(parts)


_OUR_HANDLER_ATTR = "_agentdrive_observability"


class RedactSharePathFilter(logging.Filter):
    """Scrub bare or mounted share credentials from Uvicorn access args."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        args = record.args
        if isinstance(args, tuple):
            redacted = tuple(
                redact_share_path(a, settings.mount_prefix) if isinstance(a, str) else a
                for a in args
            )
            if redacted != args:
                record.args = redacted
        elif isinstance(args, str):
            record.args = redact_share_path(args, settings.mount_prefix)
        return True


def _install_share_redaction() -> None:
    """Attach the share-key redaction filter to `uvicorn.access` (idempotent)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSharePathFilter) for f in access.filters):
        access.addFilter(RedactSharePathFilter())


def setup_logging(level: int = logging.INFO) -> None:
    """Install the ContextFilter + BracketFormatter on the root logger.

    Idempotent — calling twice doesn't double up filters or handlers,
    so test fixtures that reset logging between tests stay clean.

    Replaces the previous `logging.basicConfig(...)` call in `app.py`
    because basicConfig is a no-op if `logging.root` already has
    handlers (it does, once anything imports `agentdrive`).

    Only removes OUR previously-installed handler on re-call; other
    handlers (notably pytest's `caplog` LogCaptureHandler) are left
    alone. Without this guard, a future fixture that calls
    `setup_logging()` during tests would silently break log assertions
    by stripping caplog's capture handler."""
    root = logging.getLogger()
    # Remove only our own previously-installed handler. Anything else
    # — pytest caplog, uvicorn's handler, a user-attached stream —
    # stays put.
    for h in list(root.handlers):
        if getattr(h, _OUR_HANDLER_ATTR, False):
            root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setFormatter(BracketFormatter())
    setattr(handler, _OUR_HANDLER_ATTR, True)
    # Attach the filter to the handler (not the root logger) so child
    # loggers don't have to opt in — every record that reaches a handler
    # gets the contextvars copied on. Filters at the logger level only
    # fire for records emitted *by that logger*; at the handler level
    # they fire for everything the handler sees.
    handler.addFilter(ContextFilter())
    root.addHandler(handler)
    root.setLevel(level)
    # Belt-and-braces for §4.5 H4: redact the share_key from uvicorn's own
    # access logger too (it formats the raw request path independently of our
    # request middleware). Works regardless of `--access-log` flags / env.
    _install_share_redaction()
