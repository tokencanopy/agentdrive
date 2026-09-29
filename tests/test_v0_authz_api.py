"""Route-layer authorization dependencies (`api.v0_authz`): the two halves.

`require_scope` enforces the token half (403 without the scope);
`require_local` enforces the local half (404 as-if-absent without a grant),
composing with an optional scope. Exercises both as FastAPI dependencies on
a minimal probe route, driven over the composed app.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from agentdrive.api.v0_authz import require_local, require_scope
from agentdrive.api.v0_deps import v0_actor
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
OTHER_AGENT = "tcagt_0000000000000002"
WS_A = "tcws_0000000000000001"


def make_actor(
    *,
    subject: str = AGENT,
    subject_type: str = "agent",
    workspace: str = WS_A,
    scopes: set[str] | None = None,
) -> V0ActorContext:
    scopes = scopes if scopes is not None else {"content:read", "content:write"}
    is_agent = subject_type == "agent"
    return V0ActorContext(
        subject=subject,
        subject_type=subject_type,
        workspace_id=workspace,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(scopes),
        credential_id="tccred_0000000000000001" if is_agent else None,
        runtime_id="tcrun_0000000000000001" if is_agent else None,
        sponsor_id=SPONSOR if is_agent else None,
        workspace_role=None if is_agent else "admin",
    )


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


async def _probe_app(dep) -> FastAPI:
    """A minimal route gated by a dependency, driven in-process."""
    from agentdrive.api.v0_errors import V0ApiError, v0_api_error_handler

    probe = FastAPI()
    probe.add_exception_handler(V0ApiError, v0_api_error_handler)

    @probe.get("/v0/drives/{drive_id}/folders/{folder_id}", dependencies=[Depends(dep)])
    async def handler(drive_id: str, folder_id: str):
        return {"ok": True}

    return probe


async def _create_drive_via_sql(actor) -> dict:
    from agentdrive.core.v0_drives import create_drive

    async with conn() as c, c.transaction():
        payload = await create_drive(c, actor, name="d", metadata={})
    return payload


async def _create_drive_grant_via_sql(actor, drive_id: str) -> None:
    from agentdrive.core.v0_grants import create_grant

    async with conn() as c, c.transaction():
            await create_grant(
                c, actor, drive_id,
                principal_type="agent", principal_id=actor.subject,
                resource_type="drive", resource_id=drive_id, role="manager",
                expires_at=None,
            )


async def _req(probe: FastAPI, actor: V0ActorContext, drive_id: str, folder_id: str):
    probe.dependency_overrides[v0_actor] = lambda: actor
    try:
        transport = ASGITransport(app=probe)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.get(f"/v0/drives/{drive_id}/folders/{folder_id}")
    finally:
        probe.dependency_overrides.pop(v0_actor, None)


async def test_require_scope_denies_without_scope(app_with_lifespan):
    creator = make_actor(scopes={"content:read"})
    probe = await _probe_app(require_scope("content:write"))
    resp = await _req(probe, creator, "drv_00000000000000aa", "fld_00000000000000aa")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_require_scope_allows_with_scope(app_with_lifespan):
    creator = make_actor(scopes={"content:write"})
    probe = await _probe_app(require_scope("content:write"))
    resp = await _req(probe, creator, "drv_00000000000000aa", "fld_00000000000000aa")
    assert resp.status_code == 200


async def test_require_local_allows_drive_manager(app_with_lifespan):
    creator = make_actor(scopes={"content:read", "content:write"})
    drive = await _create_drive_via_sql(creator)
    # The creator is already a drive manager (create_drive mints it).
    probe = await _probe_app(require_local("manager", "folder", "folder_id"))
    resp = await _req(probe, creator, drive["id"], drive["root_folder_id"])
    assert resp.status_code == 200


async def test_require_local_denies_without_grant(app_with_lifespan):
    creator = make_actor(scopes={"content:read", "content:write"})
    drive = await _create_drive_via_sql(creator)
    outsider = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    probe = await _probe_app(require_local("manager", "folder", "folder_id"))
    resp = await _req(probe, outsider, drive["id"], drive["root_folder_id"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_require_local_with_scope_composes(app_with_lifespan):
    creator = make_actor(scopes={"content:read"})  # has the grant, lacks write scope
    drive = await _create_drive_via_sql(creator)
    probe = await _probe_app(
        require_local("manager", "folder", "folder_id", scope="content:write")
    )
    resp = await _req(probe, creator, drive["id"], drive["root_folder_id"])
    assert resp.status_code == 403  # scope half fails first


async def test_require_local_404_for_missing_drive(app_with_lifespan):
    creator = make_actor(scopes={"content:read", "content:write"})
    probe = await _probe_app(require_local("manager", "folder", "folder_id"))
    resp = await _req(probe, creator, "drv_00000000000000ff", "fld_00000000000000aa")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DRIVE_NOT_FOUND"
