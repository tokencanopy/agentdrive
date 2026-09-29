"""Background-task context manager.

Wrap a worker tick / scheduled job body in `task_context(name, drive_id=…)`
so every log line emitted inside it carries `[task=<name>] [run=<id>]
[drive=<id>?]`. Each entry mints a fresh `run` so successive ticks are
greppable as distinct invocations.

    async with task_context("indexer", drive_id=row["drive_id"]):
        await _process(row)

The context manager logs `at=task.start` / `at=task.end` (or
`at=task.exception`) with elapsed milliseconds — gives the indexer's
"how long did each tick take" telemetry for free.
"""

import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from . import context as ctx

log = logging.getLogger("agentdrive.task")

# Task names appear as `[task=<name>]` in log lines, which downstream
# tooling parses as `key=value`. Disallow embedded spaces, brackets,
# `=`, or anything that would break that contract. Lowercase
# snake_case is the documented convention.
_VALID_TASK_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _mint_run_id() -> str:
    return secrets.token_hex(4)


@asynccontextmanager
async def task_context(
    name: str,
    *,
    drive_id: str | None = None,
) -> AsyncIterator[str]:
    """Set `task_name` + `task_run_id` (+ optional `drive_id`) ContextVars
    for the body. Yields the minted `run_id` so the caller can pass it
    to downstream systems that want their own correlation token.

    `name` must be lowercase snake_case so the bracket prefix stays
    machine-parseable (`[task=indexer]`, not `[task=foo bar]`)."""
    if not _VALID_TASK_NAME.fullmatch(name):
        raise ValueError(
            f"invalid task name {name!r}: must match {_VALID_TASK_NAME.pattern}"
        )
    run_id = _mint_run_id()
    name_token = ctx.task_name.set(name)
    run_token = ctx.task_run_id.set(run_id)
    drive_token = ctx.drive_id.set(drive_id) if drive_id is not None else None

    start = time.perf_counter()
    log.info("at=task.start")
    try:
        yield run_id
    except Exception:
        elapsed = round((time.perf_counter() - start) * 1000, 1)
        log.exception("at=task.exception ms=%s", elapsed)
        raise
    else:
        elapsed = round((time.perf_counter() - start) * 1000, 1)
        log.info("at=task.end ms=%s", elapsed)
    finally:
        ctx.task_name.reset(name_token)
        ctx.task_run_id.reset(run_token)
        if drive_token is not None:
            ctx.drive_id.reset(drive_token)
