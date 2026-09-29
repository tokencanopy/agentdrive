"""`_principal_matches` — the ONE grant-resolution rule (schema.sql).

Every authorization query in the product calls this function, which is the
point: search, navigation, content, changes, and sharing cannot drift apart if
they all ask the same question. That also makes it the one place a mistake is
total, so it gets tested directly rather than only through the routes.

The Service exclusion is the case that needs stating. A `workspace` grant
covers "everyone in this workspace", which for humans and agents means their
workspace membership. A Service Account has NO workspace membership (service
account design §7.1) — it belongs to a workspace but is not a member of it —
so a workspace grant must never reach it. Its access is exactly the explicit
`service` grants it holds, plus drives it created.

Written as a truth table rather than as route tests because the failure mode
is a missing row, not a wrong answer on a row somebody thought of.
"""

from __future__ import annotations

import os
import socket

import pytest
import pytest_asyncio

_DB_URL = os.environ.get("DATABASE_URL")
_WS = "tcws_0000000000000001"
_OTHER_WS = "tcws_0000000000000002"


def _postgres_reachable() -> bool:
    host = os.environ.get("POSTGRES_TEST_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_TEST_PORT", "5432"))
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="local Postgres is not reachable"
)


@pytest_asyncio.fixture
async def conn(app_with_lifespan):
    """Depends on `app_with_lifespan` only for its side effect: that session
    fixture is what creates and migrates the per-pid test database this
    connects to."""
    import asyncpg

    connection = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        yield connection
    finally:
        await connection.close()


async def _matches(
    conn, actor_type: str, actor_subject: str, actor_workspace: str,
    principal_type: str, principal_id: str | None,
) -> bool:
    return await conn.fetchval(
        "SELECT _principal_matches($1, $2, $3, $4, $5)",
        actor_type, actor_subject, actor_workspace, principal_type, principal_id,
    )


@pytest.mark.parametrize(
    "actor_type,actor_subject",
    [("user", "tcusr_0000000000000001"), ("agent", "tcagt_0000000000000001")],
)
async def test_a_workspace_grant_still_covers_humans_and_agents(
    conn, actor_type, actor_subject
):
    assert await _matches(
        conn, actor_type, actor_subject, _WS, "workspace", _WS
    ) is True


async def test_a_workspace_grant_never_covers_a_service(conn):
    """The exclusion, stated once here so no call site can forget it.

    Without it, adding the Service branch to the token verifier would have
    silently handed every Service Account viewer/editor/manager access to
    every drive shared with its workspace — access nobody granted it.
    """
    assert await _matches(
        conn, "service", "tcsvc_0000000000000001", _WS, "workspace", _WS
    ) is False


async def test_a_workspace_grant_for_another_workspace_covers_nobody(conn):
    for actor_type, subject in [
        ("user", "tcusr_0000000000000001"),
        ("agent", "tcagt_0000000000000001"),
        ("service", "tcsvc_0000000000000001"),
    ]:
        assert await _matches(
            conn, actor_type, subject, _WS, "workspace", _OTHER_WS
        ) is False


async def test_an_explicit_grant_matches_its_own_principal(conn):
    for actor_type, subject in [
        ("user", "tcusr_0000000000000001"),
        ("agent", "tcagt_0000000000000001"),
        ("service", "tcsvc_0000000000000001"),
    ]:
        assert await _matches(
            conn, actor_type, subject, _WS, actor_type, subject
        ) is True


async def test_an_explicit_grant_does_not_match_a_different_principal(conn):
    assert await _matches(
        conn, "service", "tcsvc_0000000000000001", _WS,
        "service", "tcsvc_0000000000000002",
    ) is False
    assert await _matches(
        conn, "service", "tcsvc_0000000000000001", _WS,
        "agent", "tcagt_0000000000000001",
    ) is False
    assert await _matches(
        conn, "agent", "tcagt_0000000000000001", _WS,
        "service", "tcsvc_0000000000000001",
    ) is False


async def test_a_public_grant_still_covers_every_actor_including_a_service(conn):
    """`public` is the publication mechanism, viewer-only by constraint. It
    covers anonymous readers, so excluding a Service from it would be
    incoherent: the artifact is already readable by anyone with the link."""
    for actor_type, subject in [
        ("user", "tcusr_0000000000000001"),
        ("agent", "tcagt_0000000000000001"),
        ("service", "tcsvc_0000000000000001"),
        ("public", ""),
    ]:
        assert await _matches(conn, actor_type, subject, _WS, "public", None) is True
