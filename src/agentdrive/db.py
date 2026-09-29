import logging
from contextlib import asynccontextmanager
from time import perf_counter

import asyncpg

from .config import settings

# Helpers across the codebase accept an optional connection so they
# can participate in an outer transaction. `conn()` below yields a
# pool-acquired `PoolConnectionProxy` (NOT a bare `asyncpg.Connection`),
# but both expose the same query API. Use this alias for parameters
# typed as "any DB handle" so the type checker doesn't reject the
# common pool-yielded-c=c pattern.
type DBConn = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy

_pool: asyncpg.Pool | None = None
log = logging.getLogger(__name__)


async def init_pool(max_size: int | None = None) -> None:
    """Open the process's connection pool.

    `max_size` is a parameter because there is now more than one process per
    container: the public API and, when the hosted MCP is enabled, the private
    loopback ingress (`agentdrive.internal_ingress`). Two processes each
    holding the API's ceiling would double this instance's share of Cloud SQL
    connections for a surface that serves exactly one local client. The
    default is unchanged, so the public app is unaffected.
    """
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=max_size if max_size is not None else settings.database_pool_max,
    )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    assert _pool is not None, "DB pool not initialized — did the app lifespan run?"
    return _pool


@asynccontextmanager
async def conn():
    started = perf_counter()
    async with pool().acquire() as c:
        wait_ms = (perf_counter() - started) * 1000
        log.info("at=database_pool_wait wait_ms=%.3f", wait_ms)
        yield c
