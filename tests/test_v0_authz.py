"""Local-capability authorization (`core.v0_authz`): the single authority
primitive.

Exercises `effective_role` / `has_role` / `require` over real Postgres:
drive grants authoritative throughout the drive, folder grants reaching the
whole subtree below them (additive-only inheritance — no boundary subtracts
reach), direct artifact grants, and workspace/public principal matching.
Tests drive the core module directly (no HTTP), so the grant fixtures are
inserted via the grants vertical's core or raw SQL.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentdrive.core import v0_authz as authz
from agentdrive.core.v0_drives import create_drive
from agentdrive.core.v0_grants import create_grant, revoke_grant
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
OTHER_AGENT = "tcagt_0000000000000002"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"
WS_B = "tcws_0000000000000002"


def make_actor(
    *,
    subject: str = AGENT,
    subject_type: str = "agent",
    workspace: str = WS_A,
    scopes: set[str] | None = None,
    sponsor: str | None = SPONSOR,
) -> V0ActorContext:
    scopes = scopes if scopes is not None else {
        "drives:read", "drives:write", "usage:read",
        "content:read", "content:write", "sharing:read", "sharing:write",
    }
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
        sponsor_id=sponsor if is_agent else None,
        workspace_role=None if is_agent else "admin",
    )


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


async def _drive(c, actor) -> dict:
    async with c.transaction():
        payload = await create_drive(c, actor, name="d", metadata={})
    return payload


async def _folder(c, drive_id: str, parent_id: str, name: str) -> dict:
    from agentdrive.core.ids import new_id

    folder_id = new_id("fld")
    async with c.transaction():
        row = await c.fetchrow(
            "INSERT INTO folders "
            "(id, drive_id, parent_id, name, revision) "
            "VALUES ($1, $2, $3, $4, $5) "
            "RETURNING id, drive_id, parent_id, name",
            folder_id, drive_id, parent_id, name, "rev_00000000000000aa",
        )
    return dict(row)


async def _grant(c, actor, drive_id: str, **body) -> dict:
    body.setdefault("principal_id", None)
    async with c.transaction():
        payload = await create_grant(c, actor, drive_id, expires_at=None, **body)
    return payload


async def _role(c, actor, drive_id: str, resource_type: str, resource_id: str) -> str | None:
    return await authz.effective_role(
        c, actor=actor, drive_id=drive_id,
        resource_type=resource_type, resource_id=resource_id,
    )


# ── drive-level authority ────────────────────────────────────────────────────


async def test_drive_manager_is_authoritative_everywhere(app_with_lifespan):
    override = make_actor()
    async with conn() as c:
        drive = await _drive(c, override)
        sub = await _folder(c, drive["id"], drive["root_folder_id"], "sub")
        deep = await _folder(c, drive["id"], sub["id"], "deep")

        assert await _role(c, override, drive["id"], "drive", drive["id"]) == "manager"
        assert await _role(c, override, drive["id"], "folder", sub["id"]) == "manager"
        assert await _role(c, override, drive["id"], "folder", deep["id"]) == "manager"

        assert await authz.has_role(
            c, actor=override, drive_id=drive["id"],
            resource_type="folder", resource_id=deep["id"], minimum="manager",
        )


async def test_other_principal_without_grant_has_no_role(app_with_lifespan):
    creator = make_actor()
    outsider = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        assert await _role(c, outsider, drive["id"], "drive", drive["id"]) is None


async def test_require_raises_when_no_local_capability(app_with_lifespan):
    creator = make_actor()
    outsider = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        with pytest.raises(authz.NotAuthorizedError):
            await authz.require(
                c, actor=outsider, drive_id=drive["id"],
                resource_type="drive", resource_id=drive["id"], minimum="viewer",
            )
        # The creator (drive manager) passes.
        await authz.require(
            c, actor=creator, drive_id=drive["id"],
            resource_type="drive", resource_id=drive["id"], minimum="manager",
        )


# ── folder grants (additive-only inheritance) ────────────────────────────────


async def test_folder_grant_applies_down_ancestry(app_with_lifespan):
    creator = make_actor()
    other = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        sub = await _folder(c, drive["id"], drive["root_folder_id"], "sub")
        deep = await _folder(c, drive["id"], sub["id"], "deep")

        await _grant(c, creator, drive["id"],
                     principal_type="agent", principal_id=OTHER_AGENT,
                     resource_type="folder", resource_id=sub["id"], role="editor")

        assert await _role(c, other, drive["id"], "folder", sub["id"]) == "editor"
        # Inherited down to the descendant folder.
        assert await _role(c, other, drive["id"], "folder", deep["id"]) == "editor"


async def test_folder_grant_reaches_every_descendant(app_with_lifespan):
    """Additive-only inheritance: seeing a folder means seeing everything
    under it, at any depth. This is the case the removed `sealed` boundary
    used to cut — a grant two levels above the target now reaches it, and no
    folder in between can subtract that reach."""
    creator = make_actor()
    other = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        sub = await _folder(c, drive["id"], drive["root_folder_id"], "sub")
        mid = await _folder(c, drive["id"], sub["id"], "mid")
        below = await _folder(c, drive["id"], mid["id"], "below")

        # One grant, on `sub` — the whole subtree under it follows.
        await _grant(c, creator, drive["id"],
                     principal_type="agent", principal_id=OTHER_AGENT,
                     resource_type="folder", resource_id=sub["id"], role="editor")

        assert await _role(c, other, drive["id"], "folder", sub["id"]) == "editor"
        assert await _role(c, other, drive["id"], "folder", mid["id"]) == "editor"
        assert await _role(c, other, drive["id"], "folder", below["id"]) == "editor"

        # A grant deeper down can only ADD; it never lowers what an ancestor
        # grant already confers.
        await _grant(c, creator, drive["id"],
                     principal_type="agent", principal_id=OTHER_AGENT,
                     resource_type="folder", resource_id=mid["id"], role="viewer")
        assert await _role(c, other, drive["id"], "folder", below["id"]) == "editor"


async def test_drive_grant_authoritative_on_every_folder(app_with_lifespan):
    # A drive-level grant to ANOTHER agent applies everywhere in the drive.
    creator = make_actor()
    other = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        deep = await _folder(c, drive["id"], drive["root_folder_id"], "deep")

        await _grant(c, creator, drive["id"],
                     principal_type="agent", principal_id=OTHER_AGENT,
                     resource_type="drive", resource_id=drive["id"], role="viewer")

        assert await _role(c, other, drive["id"], "folder", deep["id"]) == "viewer"


# ── artifact grants ──────────────────────────────────────────────────────────


async def test_artifact_direct_grant_and_parent_inheritance(app_with_lifespan):
    creator = make_actor()
    other = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        sub = await _folder(c, drive["id"], drive["root_folder_id"], "sub")
        art_id = "art_00000000000000aa"
        async with c.transaction():
            await c.execute(
                "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
                "VALUES ($1, $2, $3, $4, $5)",
                art_id, drive["id"], sub["id"], "a.bin", "rev_00000000000000bb",
            )

        # Parent folder grant flows to the artifact.
        await _grant(c, creator, drive["id"],
                     principal_type="agent", principal_id=OTHER_AGENT,
                     resource_type="folder", resource_id=sub["id"], role="editor")
        assert await _role(c, other, drive["id"], "artifact", art_id) == "editor"

        # A direct artifact grant raises it.
        await _grant(c, creator, drive["id"],
                     principal_type="agent", principal_id=OTHER_AGENT,
                     resource_type="artifact", resource_id=art_id, role="manager")
        assert await _role(c, other, drive["id"], "artifact", art_id) == "manager"


# ── workspace + public principals ────────────────────────────────────────────


async def test_workspace_grant_covers_members(app_with_lifespan):
    creator = make_actor()
    member = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        await _grant(c, creator, drive["id"],
                     principal_type="workspace", principal_id=WS_A,
                     resource_type="drive", resource_id=drive["id"], role="viewer")
        assert await _role(c, member, drive["id"], "drive", drive["id"]) == "viewer"


async def test_public_grant_covers_anyone_as_viewer(app_with_lifespan):
    creator = make_actor()
    stranger = make_actor(subject=OTHER_AGENT, workspace=WS_B)
    async with conn() as c:
        drive = await _drive(c, creator)
        await _grant(c, creator, drive["id"],
                     principal_type="public",
                     resource_type="drive", resource_id=drive["id"], role="viewer")
        assert await _role(c, stranger, drive["id"], "drive", drive["id"]) == "viewer"


async def test_revoked_grant_removes_authority(app_with_lifespan):
    creator = make_actor()
    other = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        grant = await _grant(c, creator, drive["id"],
                             principal_type="agent", principal_id=OTHER_AGENT,
                             resource_type="drive", resource_id=drive["id"], role="viewer")
        assert await _role(c, other, drive["id"], "drive", drive["id"]) == "viewer"

        async with c.transaction():
            await revoke_grant(
                c, creator, drive["id"], grant["id"],
                if_match=f'"{grant["revision"]}"',
            )
        assert await _role(c, other, drive["id"], "drive", drive["id"]) is None


async def test_effective_role_404_for_missing_drive(app_with_lifespan):
    actor = make_actor()
    async with conn() as c:
        with pytest.raises(authz.DriveNotFoundError):
            await authz.effective_role(
                c, actor=actor, drive_id="drv_00000000000000ff",
                resource_type="drive", resource_id="drv_00000000000000ff",
            )


async def test_effective_role_rejects_bad_resource_type(app_with_lifespan):
    actor = make_actor()
    async with conn() as c:
        drive = await _drive(c, actor)
        with pytest.raises(ValueError):
            await authz.effective_role(
                c, actor=actor, drive_id=drive["id"],
                resource_type="garbage", resource_id=drive["id"],
            )


async def test_artifact_inherits_a_grant_from_a_distant_ancestor(app_with_lifespan):
    """An artifact is reached by a folder grant anywhere above it, not only by
    one on its immediate parent — the artifact-side half of additive-only
    inheritance, and the case the removed seal used to cut."""
    creator = make_actor()
    other = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        sub = await _folder(c, drive["id"], drive["root_folder_id"], "sub")
        mid = await _folder(c, drive["id"], sub["id"], "mid")
        art_id = "art_00000000000000ab"
        async with c.transaction():
            await c.execute(
                "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
                "VALUES ($1, $2, $3, $4, $5)",
                art_id, drive["id"], mid["id"], "a.bin", "rev_00000000000000bb",
            )

        assert await _role(c, other, drive["id"], "artifact", art_id) is None

        # A grant on the GRANDparent folder reaches the artifact.
        await _grant(c, creator, drive["id"],
                     principal_type="agent", principal_id=OTHER_AGENT,
                     resource_type="folder", resource_id=sub["id"], role="editor")
        assert await _role(c, other, drive["id"], "artifact", art_id) == "editor"


async def test_deepest_ancestor_grant_reaches_the_leaf(app_with_lifespan):
    """Three levels down, one grant at the top. Every intermediate folder is
    transparent: there is no node that can stop the walk."""
    creator = make_actor()
    other = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    async with conn() as c:
        drive = await _drive(c, creator)
        outer = await _folder(c, drive["id"], drive["root_folder_id"], "outer")
        inner = await _folder(c, drive["id"], outer["id"], "inner")
        leaf = await _folder(c, drive["id"], inner["id"], "leaf")

        await _grant(c, creator, drive["id"],
                     principal_type="agent", principal_id=OTHER_AGENT,
                     resource_type="folder", resource_id=outer["id"], role="editor")

        assert await _role(c, other, drive["id"], "folder", inner["id"]) == "editor"
        assert await _role(c, other, drive["id"], "folder", leaf["id"]) == "editor"

        # A sibling subtree with no grant above it stays unreachable — reach is
        # additive, not global.
        elsewhere = await _folder(c, drive["id"], drive["root_folder_id"], "elsewhere")
        assert await _role(c, other, drive["id"], "folder", elsewhere["id"]) is None
