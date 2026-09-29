"""The workspace-admin manager overlay (§8, ratified 2026-08-28).

A human actor whose verified token carries ``workspace_role`` owner or admin
holds implicit ``manager`` on every drive in their own workspace — member- and
agent-created drives included — for visibility, and to close the orphan-drive
gap (a departed creator's permanent grant no longer strands a drive). The
boundaries that must NOT move:

  * members stay grant-only — one member's drive is invisible to another;
  * agents never receive the overlay, whatever claims their actor carries;
  * the overlay never crosses workspaces;
  * token scope still intersects — an owner with read-only scopes reads but
    cannot write;
  * listing parity: a drive an owner/admin can open by id appears in their
    ``list_drives``, and folder/artifact/search/grant listings inside it show
    every row, exactly as their by-id reads would.

Fixtures follow ``tests/test_v0_grants.py`` (local ``http`` /
``override_actor`` / ``_clean_tables``); the break-glass and viewer-session
sections drive the core modules directly.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.core import v0_authz as authz
from agentdrive.core import v0_grants as grants_core
from agentdrive.core import v0_viewer_sessions as viewer_core
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
MEMBER = "tcusr_0000000000000011"
OTHER_MEMBER = "tcusr_0000000000000012"
OWNER = "tcusr_0000000000000013"
ADMIN = "tcusr_0000000000000014"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"
WS_B = "tcws_0000000000000002"

_ALL_SCOPES = {
    "drives:read", "drives:write", "usage:read",
    "content:read", "content:write", "changes:read",
    "sharing:read", "sharing:write",
}


def make_actor(
    *,
    subject: str = AGENT,
    subject_type: str = "agent",
    workspace: str = WS_A,
    scopes: set[str] | None = None,
    sponsor: str | None = SPONSOR,
    workspace_role: str | None = None,
) -> V0ActorContext:
    scopes = scopes if scopes is not None else set(_ALL_SCOPES)
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
        workspace_role=workspace_role if not is_agent else None,
    )


def owner_actor(**over) -> V0ActorContext:
    over.setdefault("subject", OWNER)
    over.setdefault("workspace_role", "owner")
    return make_actor(subject_type="user", **over)


def admin_actor(**over) -> V0ActorContext:
    over.setdefault("subject", ADMIN)
    over.setdefault("workspace_role", "admin")
    return make_actor(subject_type="user", **over)


def member_actor(subject: str = MEMBER, **over) -> V0ActorContext:
    over.setdefault("workspace_role", "member")
    return make_actor(subject=subject, subject_type="user", **over)


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def override_actor(app_with_lifespan):
    def _set(actor: V0ActorContext) -> None:
        app.dependency_overrides[v0_actor] = lambda: actor

    yield _set
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


async def _create_drive(http, override_actor, creator: V0ActorContext, name: str, key: str) -> dict:
    override_actor(creator)
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_artifact(
    http, drive: dict, *, name: str, body: bytes, key: str,
    content_type: str = "text/plain",
) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b'--b\r\nContent-Disposition: form-data; name="parent_id"\r\n\r\n'
            + drive["root_folder_id"].encode()
            + b'\r\n--b\r\nContent-Disposition: form-data; name="name"\r\n\r\n'
            + name.encode()
            + b'\r\n--b\r\nContent-Disposition: form-data; name="content"; filename="f"\r\n'
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n"
            + body + b"\r\n--b--\r\n"
        ),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": key,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _orphan(drive_id: str) -> None:
    """Simulate a fully departed founding set: revoke EVERY grant, so no
    principal that can still obtain a token holds anything on the drive."""
    async with conn() as c:
        await c.execute(
            "UPDATE grants SET revoked_at = now() WHERE drive_id = $1", drive_id
        )


# ── owner/admin: full access to drives they hold no grant in ────────────────


async def test_owner_opens_and_manages_a_member_created_drive(http, override_actor):
    drive = await _create_drive(http, override_actor, member_actor(), "m-drive", "k-od1")

    override_actor(owner_actor())
    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 200, read.text

    # Manager-level: administer the access graph with no grant row of their own.
    granted = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "user", "principal_id": OTHER_MEMBER,
            "resource_type": "drive", "resource_id": drive["id"],
            "role": "viewer",
        },
        headers={"Idempotency-Key": "k-od1-g"},
    )
    assert granted.status_code == 201, granted.text


async def test_admin_opens_and_manages_an_agent_created_drive(http, override_actor):
    drive = await _create_drive(http, override_actor, make_actor(), "a-drive", "k-ad1")

    override_actor(admin_actor())
    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 200, read.text

    updated = await http.patch(
        f"/v0/drives/{drive['id']}",
        json={"name": "renamed-by-admin"},
        headers={
            "If-Match": f'"{drive["revision"]}"',
            "Idempotency-Key": "k-ad1-p",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "renamed-by-admin"


async def test_member_still_cannot_see_another_members_drive(http, override_actor):
    drive = await _create_drive(http, override_actor, member_actor(), "priv", "k-mm1")

    override_actor(member_actor(subject=OTHER_MEMBER))
    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "NOT_AUTHORIZED"

    listed = await http.get("/v0/drives")
    assert listed.status_code == 200
    assert drive["id"] not in [d["id"] for d in listed.json()["items"]]


async def test_agent_without_grant_is_still_denied(http, override_actor):
    drive = await _create_drive(http, override_actor, member_actor(), "nag", "k-ag1")

    override_actor(make_actor(subject="tcagt_0000000000000002"))
    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_forged_agent_workspace_role_confers_nothing(http, override_actor):
    """Defense in depth: the verifier rejects `workspace_role` on an agent
    token, and even a hand-built agent actor carrying one gets no overlay —
    `is_workspace_admin` is False for any non-user subject."""
    drive = await _create_drive(http, override_actor, member_actor(), "forge", "k-fg1")

    forged = V0ActorContext(
        subject="tcagt_0000000000000003",
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(_ALL_SCOPES),
        workspace_role="owner",  # would never survive verification
    )
    assert forged.is_workspace_admin is False
    override_actor(forged)
    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 404


async def test_overlay_never_crosses_workspaces(http, override_actor):
    drive = await _create_drive(http, override_actor, member_actor(), "xws", "k-xw1")

    override_actor(owner_actor(workspace=WS_B))
    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "DRIVE_NOT_FOUND"


async def test_overlay_respects_token_scopes(http, override_actor):
    """The overlay grants LOCAL capability only — §7.1's intersection still
    applies, so an owner with read-only scopes cannot write."""
    drive = await _create_drive(http, override_actor, member_actor(), "ro", "k-ro1")

    override_actor(owner_actor(scopes={"drives:read", "content:read", "sharing:read"}))
    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 200, read.text

    write = await http.patch(
        f"/v0/drives/{drive['id']}",
        json={"name": "nope"},
        headers={
            "If-Match": f'"{drive["revision"]}"',
            "Idempotency-Key": "k-ro1-p",
        },
    )
    assert write.status_code == 403
    assert write.json()["error"]["code"] == "PERMISSION_DENIED"


# ── listing parity ───────────────────────────────────────────────────────────


async def test_owner_lists_every_drive_in_the_workspace(http, override_actor):
    mine = await _create_drive(http, override_actor, owner_actor(), "own", "k-lp0")
    member_drive = await _create_drive(http, override_actor, member_actor(), "mem", "k-lp1")
    agent_drive = await _create_drive(http, override_actor, make_actor(), "agt", "k-lp2")
    foreign = await _create_drive(
        http, override_actor,
        member_actor(subject="tcusr_0000000000000015", workspace=WS_B),
        "other-ws", "k-lp3",
    )

    override_actor(owner_actor())
    listed = await http.get("/v0/drives")
    assert listed.status_code == 200
    ids = [d["id"] for d in listed.json()["items"]]
    assert mine["id"] in ids
    assert member_drive["id"] in ids
    assert agent_drive["id"] in ids
    assert foreign["id"] not in ids

    # A member's list stays grant-scoped: only their own drive shows.
    override_actor(member_actor())
    listed = await http.get("/v0/drives")
    assert [d["id"] for d in listed.json()["items"]] == [member_drive["id"]]


async def test_admin_sees_folder_artifact_and_search_rows(http, override_actor):
    drive = await _create_drive(http, override_actor, member_actor(), "deep", "k-ls1")
    folder = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        json={"parent_id": drive["root_folder_id"], "name": "docs"},
        headers={"Idempotency-Key": "k-ls1-f"},
    )
    assert folder.status_code == 201, folder.text
    artifact = await _create_artifact(
        http, drive, name="report.txt",
        body=b"quarterly zebrafish report", key="k-ls1-a",
    )

    override_actor(admin_actor())
    folders = await http.get(f"/v0/drives/{drive['id']}/folders")
    assert folders.status_code == 200, folders.text
    assert folder.json()["id"] in [f["id"] for f in folders.json()["items"]]

    artifacts = await http.get(f"/v0/drives/{drive['id']}/artifacts")
    assert artifacts.status_code == 200, artifacts.text
    assert artifact["id"] in [a["id"] for a in artifacts.json()["items"]]

    hits = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "zebrafish"}
    )
    assert hits.status_code == 200, hits.text
    assert artifact["id"] in [h["id"] for h in hits.json()["items"]]

    entries = await http.get(
        f"/v0/drives/{drive['id']}/entries",
        params={"parent_id": drive["root_folder_id"]},
    )
    assert entries.status_code == 200, entries.text

    feed = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    assert feed.status_code == 200, feed.text


async def test_owner_reads_the_whole_access_graph(http, override_actor):
    """`list_grants` enumerates every row for a drive manager — which the
    overlay makes the owner, with no grant row of their own to admit them."""
    drive = await _create_drive(http, override_actor, member_actor(), "graph", "k-gr1")

    override_actor(owner_actor())
    listed = await http.get(f"/v0/drives/{drive['id']}/grants")
    assert listed.status_code == 200, listed.text
    principals = {g["principal_id"] for g in listed.json()["items"]}
    assert MEMBER in principals  # a row naming someone else — full enumeration

    # A member with no grant anywhere in the drive still gets the uniform 404.
    override_actor(member_actor(subject=OTHER_MEMBER))
    refused = await http.get(f"/v0/drives/{drive['id']}/grants")
    assert refused.status_code == 404


async def test_overlay_mutations_attribute_the_real_actor(http, override_actor):
    """Change-feed attribution: an overlay-based mutation records the acting
    owner/admin as the change actor — there is no grant row behind the
    action, but the actor block comes from the verified token as always."""
    drive = await _create_drive(http, override_actor, member_actor(), "attr", "k-at1")

    override_actor(admin_actor())
    granted = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "user", "principal_id": OTHER_MEMBER,
            "resource_type": "drive", "resource_id": drive["id"],
            "role": "viewer",
        },
        headers={"Idempotency-Key": "k-at2"},
    )
    assert granted.status_code == 201, granted.text

    feed = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    assert feed.status_code == 200, feed.text
    created = [
        e for e in feed.json()["items"] if e["type"] == "grant.created"
        and e["data"]["principal_id"] == OTHER_MEMBER
    ]
    assert created, feed.json()["items"]
    assert created[0]["actor"] == {"type": "user", "id": ADMIN}


# ── the orphan-drive gap ─────────────────────────────────────────────────────


async def test_orphaned_drive_stays_fully_accessible_to_owner_and_admin(
    http, override_actor
):
    """A drive whose every grant is dead — the departed-creator case the old
    break-glass docstring overclaimed about — is still fully reachable and
    administrable through the overlay."""
    drive = await _create_drive(http, override_actor, member_actor(), "orphan", "k-or1")
    artifact = await _create_artifact(
        http, drive, name="left-behind.txt", body=b"still here", key="k-or2",
    )
    await _orphan(drive["id"])

    for actor in (owner_actor(), admin_actor()):
        override_actor(actor)
        read = await http.get(f"/v0/drives/{drive['id']}")
        assert read.status_code == 200, read.text
        got = await http.get(
            f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}"
        )
        assert got.status_code == 200, got.text

    # And they can re-establish explicit access for a successor.
    override_actor(owner_actor())
    regrant = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "user", "principal_id": OTHER_MEMBER,
            "resource_type": "drive", "resource_id": drive["id"],
            "role": "manager",
        },
        headers={"Idempotency-Key": "k-or3"},
    )
    assert regrant.status_code == 201, regrant.text


# ── break-glass: residual, and owner-correct (the workspace_role bug) ───────


async def test_owner_is_workspace_admin():
    """The regression the overlay work uncovered: Hub mints `owner` as a
    distinct role, and testing `== "admin"` locked owners out of break-glass."""
    assert owner_actor().is_workspace_admin is True
    assert admin_actor().is_workspace_admin is True
    assert member_actor().is_workspace_admin is False
    assert make_actor().is_workspace_admin is False  # agent


async def test_break_glass_admits_an_owner_at_zero_managers(http, override_actor):
    """Drive the residual path directly: with the overlay bypassed (it now
    satisfies the ordinary manager check first), an OWNER at zero active
    managers may still mint exactly their own drive-manager grant."""
    drive = await _create_drive(http, override_actor, member_actor(), "bg", "k-bg1")
    await _orphan(drive["id"])

    owner = owner_actor()
    async with conn() as c, c.transaction():
        recovered = await grants_core._try_break_glass(
            c, owner, drive["id"],
            principal_type="user", principal_id=owner.subject,
            resource_type="drive", resource_id=drive["id"],
            role="manager", expires_at=None,
        )
    assert recovered is not None
    assert recovered["role"] == "manager"
    assert recovered["principal_id"] == OWNER


async def test_break_glass_still_refuses_while_a_manager_lives(http, override_actor):
    drive = await _create_drive(http, override_actor, member_actor(), "bg2", "k-bg2")

    owner = owner_actor()
    async with conn() as c, c.transaction():
        recovered = await grants_core._try_break_glass(
            c, owner, drive["id"],
            principal_type="user", principal_id=owner.subject,
            resource_type="drive", resource_id=drive["id"],
            role="manager", expires_at=None,
        )
    assert recovered is None


async def test_break_glass_still_refuses_a_member(http, override_actor):
    drive = await _create_drive(http, override_actor, member_actor(), "bg3", "k-bg3")
    await _orphan(drive["id"])

    member = member_actor(subject=OTHER_MEMBER)
    async with conn() as c, c.transaction():
        recovered = await grants_core._try_break_glass(
            c, member, drive["id"],
            principal_type="user", principal_id=member.subject,
            resource_type="drive", resource_id=drive["id"],
            role="manager", expires_at=None,
        )
    assert recovered is None


# ── overlay-minted credentials survive their token-less re-check ────────────


async def test_owner_minted_viewer_session_resolves(
    http, override_actor, monkeypatch
):
    """A viewer session minted through the overlay (no grant row) must still
    resolve: the mint snapshots `workspace_role`, and the stored principal
    carries it into the re-check — otherwise the overlay would mint
    credentials that can never redeem (the public-grant trap)."""
    monkeypatch.setattr(settings, "viewer_base_url", "https://viewer.example.test")
    drive = await _create_drive(http, override_actor, member_actor(), "view", "k-vs1")
    artifact = await _create_artifact(
        http, drive, name="doc.md", body=b"# hello\n", key="k-vs2",
        content_type="text/markdown",
    )

    override_actor(owner_actor())
    minted = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/viewer-sessions",
        json={},
        headers={"Idempotency-Key": "k-vs3"},
    )
    assert minted.status_code == 200, minted.text
    credential = minted.json()["credential"]

    async with conn() as c:
        resolved = await viewer_core.resolve_credential(c, credential)
    assert resolved is not None
    assert resolved["artifact_id"] == artifact["id"]


async def test_effective_role_is_manager_everywhere_for_an_owner(
    http, override_actor
):
    """The single choke point: `effective_role` answers manager on the drive,
    a folder, and an artifact, with zero grant rows for the asking owner."""
    drive = await _create_drive(http, override_actor, member_actor(), "core", "k-cr1")
    artifact = await _create_artifact(
        http, drive, name="a.txt", body=b"x", key="k-cr2",
    )

    owner = owner_actor()
    async with conn() as c:
        for resource_type, resource_id in (
            ("drive", drive["id"]),
            ("folder", drive["root_folder_id"]),
            ("artifact", artifact["id"]),
        ):
            role = await authz.effective_role(
                c, actor=owner, drive_id=drive["id"],
                resource_type=resource_type, resource_id=resource_id,
            )
            assert role == "manager", (resource_type, role)

        # include_public=False surfaces (change feed, viewer re-checks) get
        # the overlay too — it is the actor's own standing, not a public one.
        role = await authz.effective_role(
            c, actor=owner, drive_id=drive["id"],
            resource_type="drive", resource_id=drive["id"],
            include_public=False,
        )
        assert role == "manager"
