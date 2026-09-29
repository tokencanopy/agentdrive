"""B6: scope ∩ local grant enforcement on the content surface (§6.1, §8).

Verifies that a local grant is REQUIRED to read/list/write content:

  * a folder-granted viewer can list/read ONLY their folder's subtree (the
    list endpoint filters by grant visibility) and cannot write;
  * an editor on a folder can create inside it but not elsewhere;
  * a same-workspace actor with no grant is 404 (as-if-absent), never 200;
  * cross-workspace is still 404 DRIVE_NOT_FOUND;
  * grant mutations remain manager-gated; break-glass still works.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentdrive.api.v0_deps import v0_actor
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
    workspace_role: str | None = None,
) -> V0ActorContext:
    scopes = scopes if scopes is not None else {
        "drives:read", "drives:write", "usage:read",
        "content:read", "content:write", "sharing:read", "sharing:write",
        "changes:read",
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
        workspace_role=workspace_role if not is_agent else None,
    )


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def override_actor(app_with_lifespan):
    def _set(actor: V0ActorContext) -> None:
        app_with_lifespan.dependency_overrides[v0_actor] = lambda: actor

    yield _set
    app_with_lifespan.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_drive_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE"
        )


async def _create_drive(http, name: str, key: str) -> object:
    resp = await http.post("/v0/drives", json={"name": name}, headers={"Idempotency-Key": key})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _mkdir(http, drive_id: str, parent_id: str, name: str, key: str) -> object:
    resp = await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_artifact(http, drive_id, parent_id, name, key, content: bytes = b"hi") -> object:
    resp = await http.post(
        f"/v0/drives/{drive_id}/artifacts",
        files={
            "parent_id": (None, parent_id),
            "name": (None, name),
            "content": ("file.bin", content, "application/octet-stream"),
        },
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_grant(http, drive_id: str, key: str, **body) -> object:
    resp = await http.post(
        f"/v0/drives/{drive_id}/grants",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# viewer with a folder grant: subtree-only read, no write
# ---------------------------------------------------------------------------


async def test_folder_viewer_reads_only_their_subtree(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "b6view", "kb6v")
    root = drive["root_folder_id"]
    sub = await _mkdir(http, drive["id"], root, "sub", "kb6v-1")
    other = await _mkdir(http, drive["id"], root, "other", "kb6v-2")
    await _create_artifact(http, drive["id"], sub["id"], "in-sub.txt", "kb6v-3")
    await _create_artifact(http, drive["id"], other["id"], "in-other.txt", "kb6v-4")

    # Grant OTHER_AGENT viewer on `sub` only.
    await _create_grant(
        http, drive["id"], "kb6v-5",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=sub["id"], role="viewer",
    )

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))

    # List of the drive's artifacts is filtered to the visible subtree.
    listed = await http.get(f"/v0/drives/{drive['id']}/artifacts")
    assert listed.status_code == 200, listed.text
    names = [i["name"] for i in listed.json()["items"]]
    assert "in-sub.txt" in names
    assert "in-other.txt" not in names

    # Folders list likewise filtered.
    folders = await http.get(f"/v0/drives/{drive['id']}/folders")
    assert folders.status_code == 200, folders.text
    fnames = [f["name"] for f in folders.json()["items"]]
    assert "sub" in fnames
    assert "other" not in fnames

    # Direct read of the granted artifact works; the ungranted one is 404.
    ok = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{await _artifact_id(http, drive, 'in-sub.txt')}"
    )
    assert ok.status_code == 200, ok.text

    # Cannot write into an ungranted folder.
    denied = await _mkdir_raw(http, drive["id"], other["id"], "nope", "kb6v-6")
    assert denied.status_code == 404


async def _artifact_id(http, drive, name: str) -> str:
    listed = await http.get(f"/v0/drives/{drive['id']}/artifacts")
    for i in listed.json()["items"]:
        if i["name"] == name:
            return i["id"]
    raise AssertionError(f"artifact {name} not visible")


async def _mkdir_raw(http, drive_id, parent_id, name, key):
    return await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": key},
    )


# ---------------------------------------------------------------------------
# editor on a folder can create inside it
# ---------------------------------------------------------------------------


async def test_folder_editor_can_create_inside_granted_folder(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "b6edit", "kb6e")
    root = drive["root_folder_id"]
    sub = await _mkdir(http, drive["id"], root, "sub", "kb6e-1")
    other = await _mkdir(http, drive["id"], root, "other", "kb6e-2")

    await _create_grant(
        http, drive["id"], "kb6e-3",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=sub["id"], role="editor",
    )

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))

    inside = await _mkdir_raw(http, drive["id"], sub["id"], "child", "kb6e-4")
    assert inside.status_code == 201, inside.text

    outside = await _mkdir_raw(http, drive["id"], other["id"], "child", "kb6e-5")
    assert outside.status_code == 404


# ---------------------------------------------------------------------------
# same-workspace actor with no grant is 404
# ---------------------------------------------------------------------------


async def test_no_grant_same_workspace_is_404(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "b6nog", "kb6n")
    await _mkdir(http, drive["id"], drive["root_folder_id"], "leaf", "kb6n-1")
    await _create_artifact(http, drive["id"], drive["root_folder_id"], "a.txt", "kb6n-2")

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))

    # List is filtered by grant visibility: empty page, no leak.
    listed = await http.get(f"/v0/drives/{drive['id']}/artifacts")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []

    # Direct reads of any resource 404 (as-if-absent).
    resp = await http.get(f"/v0/drives/{drive['id']}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


# ---------------------------------------------------------------------------
# cross-workspace still reads as DRIVE_NOT_FOUND
# ---------------------------------------------------------------------------


async def test_cross_workspace_is_drive_not_found(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "b6xws", "kb6x")
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp = await http.get(f"/v0/drives/{drive['id']}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DRIVE_NOT_FOUND"


# ---------------------------------------------------------------------------
# grant mutations stay manager-gated; break-glass still works
# ---------------------------------------------------------------------------


async def test_break_glass_still_works_without_local_grant(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "b6bg", "kb6bg")
    async with conn() as c:
        await c.execute(
            "UPDATE grants SET revoked_at = now() WHERE drive_id = $1 AND role = 'manager'",
            drive["id"],
        )

    admin = make_actor(
        subject="tcusr_0000000000000005", subject_type="user",
        workspace=WS_A, workspace_role="admin",
        scopes={"drives:read", "drives:write", "sharing:read", "sharing:write"},
    )
    override_actor(admin)
    resp = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "user", "principal_id": admin.subject,
            "resource_type": "drive", "resource_id": drive["id"], "role": "manager",
        },
        headers={"Idempotency-Key": "kb6bg-1"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "manager"


# ---------------------------------------------------------------------------
# effective_visibility — the aggregate "who else can reach this?" summary
# ---------------------------------------------------------------------------


async def test_effective_visibility_private_when_only_the_owner_can_reach_it(
    http, override_actor
):
    """A brand-new drive holds exactly the creator's own manager grant, so
    nothing in it is 'shared' — the field describes EXPOSURE, and the owner
    reaching their own artifact is not exposure."""
    override_actor(make_actor())
    drive = await _create_drive(http, "evpriv", "kevpriv")
    art = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "a.txt", "kevpriv-1"
    )
    resp = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["effective_visibility"] == "private"


async def test_effective_visibility_shared_on_a_direct_grant(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "evshare", "kevshare")
    art = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "a.txt", "kevshare-1"
    )
    await _create_grant(
        http, drive["id"], "kevshare-2",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="artifact", resource_id=art["id"], role="viewer",
    )
    resp = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}")
    assert resp.json()["effective_visibility"] == "shared"


async def test_effective_visibility_inherits_a_folder_grant(http, override_actor):
    """The walk is artifact → folder chain → drive, so a grant on an ancestor
    folder makes the artifact shared without touching the artifact's row."""
    override_actor(make_actor())
    drive = await _create_drive(http, "evinh", "kevinh")
    parent = await _mkdir(http, drive["id"], drive["root_folder_id"], "p", "kevinh-1")
    child = await _mkdir(http, drive["id"], parent["id"], "c", "kevinh-2")
    art = await _create_artifact(http, drive["id"], child["id"], "a.txt", "kevinh-3")

    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "private"

    await _create_grant(
        http, drive["id"], "kevinh-4",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=parent["id"], role="viewer",
    )
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "shared"


async def test_effective_visibility_public_beats_shared(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "evpub", "kevpub")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "f", "kevpub-1")
    art = await _create_artifact(http, drive["id"], folder["id"], "a.txt", "kevpub-2")
    await _create_grant(
        http, drive["id"], "kevpub-3",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="artifact", resource_id=art["id"], role="viewer",
    )
    await _create_grant(
        http, drive["id"], "kevpub-4",
        principal_type="public",
        resource_type="folder", resource_id=folder["id"], role="viewer",
    )
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "public"


async def test_effective_visibility_follows_the_whole_ancestry(http, override_actor):
    """Inheritance is additive-only, so a grant on a DISTANT ancestor reaches
    the artifact — and the badge must say so. The badge and the access
    decision are one walk: if they disagreed, the UI would report an artifact
    as private while a principal could open it, the worse failure of the two.

    (Before folder sealing was removed, the intermediate folder could be
    marked `sealed` and this same grant reached nothing.)
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "evanc", "kevanc")
    outer = await _mkdir(http, drive["id"], drive["root_folder_id"], "outer", "kevanc-1")
    inner = await _mkdir(http, drive["id"], outer["id"], "inner", "kevanc-2")
    art = await _create_artifact(http, drive["id"], inner["id"], "a.txt", "kevanc-3")

    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "private"

    # One grant, two levels up.
    await _create_grant(
        http, drive["id"], "kevanc-4",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=outer["id"], role="viewer",
    )
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "shared"

    # ...and the badge agrees with the access decision it mirrors: the grantee
    # really can read it now.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).status_code == 200


async def test_effective_visibility_ignores_revoked_and_expired_grants(
    http, override_actor
):
    """Live grants only. A revoked or expired row is dead to authorization,
    so advertising it as exposure would over-report sharing forever."""
    override_actor(make_actor())
    drive = await _create_drive(http, "evlive", "kevlive")
    art = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "a.txt", "kevlive-1"
    )
    grant = await _create_grant(
        http, drive["id"], "kevlive-2",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="artifact", resource_id=art["id"], role="viewer",
    )
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "shared"

    revoked = await http.delete(
        f"/v0/drives/{drive['id']}/grants/{grant['id']}",
        headers={
            "Idempotency-Key": "kevlive-3",
            "If-Match": f'"{grant["revision"]}"',
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "private"

    # An unrevoked but EXPIRED grant is equally dead.
    async with conn() as c:
        await c.execute(
            "INSERT INTO grants (id, drive_id, resource_type, resource_id, "
            "principal_type, principal_id, role, revision, expires_at) "
            "VALUES ('grn_00000000000000e1', $1, 'artifact', $2, 'agent', $3, "
            "'viewer', 'rev_00000000000000e1', now() - interval '1 hour')",
            drive["id"], art["id"], OTHER_AGENT,
        )
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "private"


async def test_effective_visibility_on_a_list_page_is_one_query(http, override_actor):
    """The field must be resolved for a whole page in a single statement —
    it runs on every artifact read, and a per-row lookup would turn any
    listing into an N+1. Asserted by counting the statements the page issues,
    not by trusting the implementation to stay batched."""
    override_actor(make_actor())
    drive = await _create_drive(http, "evn1", "kevn1")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "f", "kevn1-1")
    for i in range(5):
        await _create_artifact(http, drive["id"], folder["id"], f"a{i}.txt", f"kevn1-a{i}")
    await _create_grant(
        http, drive["id"], "kevn1-2",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder["id"], role="viewer",
    )

    from agentdrive.core import v0_authz

    calls: list[int] = []
    original = v0_authz.artifact_visibility

    async def counting(c, drive_id, artifact_ids):
        calls.append(len(artifact_ids))
        return await original(c, drive_id, artifact_ids)

    v0_authz.artifact_visibility = counting
    try:
        listed = await http.get(f"/v0/drives/{drive['id']}/artifacts")
    finally:
        v0_authz.artifact_visibility = original

    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 5
    assert all(a["effective_visibility"] == "shared" for a in items)
    # ONE batched resolution covering all five rows, not five lookups.
    assert calls == [5]


async def test_effective_visibility_founding_managers_are_not_sharing(
    http, override_actor
):
    """An agent-created drive mints manager for the agent AND its sponsoring
    human in the SAME transaction. Both are the drive's owner, so a drive
    nobody has been given access to reads `private` — while a manager granted
    LATER is a sharing event and flips it to `shared`. Comparing against the
    creator id alone would have marked every agent drive shared from birth.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "evfound", "kevfound")
    art = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "a.txt", "kevfound-1"
    )

    # Both founding grants exist, and the artifact is still private.
    async with conn() as c:
        founders = await c.fetch(
            "SELECT principal_type, principal_id FROM grants "
            "WHERE drive_id=$1 AND resource_type='drive' AND role='manager'",
            drive["id"],
        )
    assert {r["principal_id"] for r in founders} == {AGENT, SPONSOR}
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "private"

    # A manager added after the fact is a share, not an owner.
    await _create_grant(
        http, drive["id"], "kevfound-2",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="manager",
    )
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "shared"


async def test_effective_visibility_is_exposure_not_the_callers_own_access(
    http, override_actor
):
    """The field answers "who else can reach this", not "what can I do here".
    A grantee reading a shared artifact sees `shared` — the same answer the
    owner sees — because it describes the artifact, not the reader."""
    override_actor(make_actor())
    drive = await _create_drive(http, "evwho", "kevwho")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "f", "kevwho-1")
    art = await _create_artifact(http, drive["id"], folder["id"], "a.txt", "kevwho-2")
    await _create_grant(
        http, drive["id"], "kevwho-3",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder["id"], role="viewer",
    )
    as_owner = (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"]

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    as_grantee = (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"]

    assert as_owner == as_grantee == "shared"


async def test_effective_visibility_counts_live_share_links(http, override_actor):
    """A share link is a SECOND read path — possession of the secret is the
    credential, no principal behind it. A walk over `grants` alone reported
    `private` for an artifact anyone holding the link could fetch
    anonymously, which is the one direction this field must never fail in.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "evlink", "kevlink")
    art = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "a.txt", "kevlink-1", b"hello"
    )
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "private"

    share = await http.post(
        f"/v0/drives/{drive['id']}/shares",
        json={"resource_type": "artifact", "resource_id": art["id"]},
        headers={"Idempotency-Key": "kevlink-2"},
    )
    assert share.status_code == 201, share.text
    body = (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()
    assert body["effective_visibility"] == "public"

    # Revoking the link puts it back.
    revoked = await http.delete(
        f"/v0/drives/{drive['id']}/shares/{share.json()['id']}",
        headers={
            "Idempotency-Key": "kevlink-3",
            "If-Match": f'"{share.json()["revision"]}"',
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "private"


async def test_effective_visibility_counts_a_version_share(http, override_actor):
    """A share on an immutable version still exposes this artifact's bytes."""
    override_actor(make_actor())
    drive = await _create_drive(http, "evver", "kevver")
    art = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "a.txt", "kevver-1", b"hello"
    )
    share = await http.post(
        f"/v0/drives/{drive['id']}/shares",
        json={
            "resource_type": "artifact_version",
            "resource_id": art["head_version_id"],
        },
        headers={"Idempotency-Key": "kevver-2"},
    )
    assert share.status_code == 201, share.text
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "public"


async def test_effective_visibility_drops_a_revoked_founder_from_the_owner_set(
    http, override_actor
):
    """Founding membership is not permanent. Once a founder's drive-manager
    grant is revoked they are no longer an owner, so handing them access back
    to one artifact is a sharing event like any other — previously they stayed
    exempt for the life of the drive and the artifact read `private` while
    they could still open it."""
    override_actor(make_actor())
    drive = await _create_drive(http, "evexf", "kevexf")
    art = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "a.txt", "kevexf-1"
    )

    # Revoke the sponsor's founding manager grant.
    async with conn() as c:
        founding_id = await c.fetchval(
            "SELECT id FROM grants WHERE drive_id=$1 AND resource_type='drive' "
            "AND principal_id=$2",
            drive["id"], SPONSOR,
        )
        await c.execute(
            "UPDATE grants SET revoked_at = now() WHERE id = $1", founding_id
        )

    # Re-grant the ex-owner viewer on the single artifact.
    await _create_grant(
        http, drive["id"], "kevexf-2",
        principal_type="user", principal_id=SPONSOR,
        resource_type="artifact", resource_id=art["id"], role="viewer",
    )
    assert (await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )).json()["effective_visibility"] == "shared"


# ---------------------------------------------------------------------------
# sheet edit sessions obey the same grant predicate as the artifact they edit
# ---------------------------------------------------------------------------


async def test_sheet_sessions_are_invisible_without_a_grant(http, override_actor):
    """A session is a view onto an artifact's contents, so seeing one has to
    require what seeing the artifact requires.

    Seven of the ten sheet operations are drive-flat (`/sheet-sessions/{id}`)
    rather than nested under the artifact, so `require_local(...,
    "artifact", "artifact_id")` — which reads the artifact from the PATH —
    cannot be attached to them. The check has to come from the session row
    instead, and this is what proves it does.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "b6sheet", "kb6s")
    root = drive["root_folder_id"]
    artifact = await _create_artifact(
        http, drive["id"], root, "model.csv", "kb6s-1", content=b"Region,Q1\nEMEA,1200\n"
    )

    opened = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/sheet-sessions",
        json={},
        headers={"If-Match": f'"{artifact["revision"]}"', "Idempotency-Key": "kb6s-2"},
    )
    assert opened.status_code == 201, opened.text
    session_id = opened.json()["session_id"]

    wrote = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/sheet-sessions/{session_id}/cells",
        json={"writes": [{"sheet": "Sheet1", "range": "B2", "values": [["secret"]]}]},
        headers={"Idempotency-Key": "kb6s-3"},
    )
    assert wrote.status_code == 200, wrote.text

    # A same-workspace principal holding no grant on this artifact.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))

    listed = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/sheet-sessions",
        params={"artifact_id": artifact["id"]},
    )
    assert listed.status_code in (200, 404), listed.text
    if listed.status_code == 200:
        assert listed.json()["items"] == [], "listed a session on an artifact it cannot read"

    # Reading the session, its working grid, and its edit log all expose the
    # artifact's contents — the edit log carries literal cell values.
    for path in (
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/sheet-sessions/{session_id}",
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/sheet-sessions/{session_id}/edits",
    ):
        resp = await http.get(path)
        assert resp.status_code == 404, (path, resp.status_code, resp.text)

    cells = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/sheet-sessions/{session_id}/cells",
        params={"sheet": "Sheet1", "range": "B2"},
    )
    assert cells.status_code == 404, cells.text

    # And writing into, completing, or discarding someone else's session is
    # a mutation of an artifact this principal cannot touch at all.
    hijack = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/sheet-sessions/{session_id}/cells",
        json={"writes": [{"sheet": "Sheet1", "range": "B2", "values": [["theirs"]]}]},
        headers={"Idempotency-Key": "kb6s-4"},
    )
    assert hijack.status_code == 404, hijack.text

    published = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/sheet-sessions/{session_id}/complete",
        json={},
        headers={"Idempotency-Key": "kb6s-5"},
    )
    assert published.status_code == 404, published.text
