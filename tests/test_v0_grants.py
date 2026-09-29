"""Grants vertical (slice 8): the 5 grant operations over real Postgres.

Drive creation mints a drive-level manager grant for the creator (and its
sponsor), so the default actor is already a drive manager and can administer
grants on the drive and any sub-resource. Mirrors the drives/folders test
shape: mutations require ``Idempotency-Key``; update/revoke require
``If-Match``; reads carry ETag and honor ``If-None-Match`` → 304; workspace
scoping 404s; ``sharing:read``/``sharing:write`` gate the token scope.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
OTHER_AGENT = "tcagt_0000000000000002"
INTRUDER = "tcagt_000000000000000a"
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


async def _create_drive(http, name: str, key: str) -> dict:
    prev = app.dependency_overrides.get(v0_actor)
    app.dependency_overrides[v0_actor] = lambda: make_actor(
        scopes={"drives:read", "drives:write", "usage:read"}
    )
    try:
        resp = await http.post(
            "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
        )
    finally:
        app.dependency_overrides.clear()
        if prev is not None:
            app.dependency_overrides[v0_actor] = prev
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _mkdir(http, drive_id: str, parent_id: str, name: str, key: str) -> object:
    return await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": key},
    )


async def _create_grant(http, drive_id: str, key: str, **body) -> object:
    return await http.post(
        f"/v0/drives/{drive_id}/grants",
        json=body,
        headers={"Idempotency-Key": key},
    )


# ── auth / scope ─────────────────────────────────────────────────────────────


async def test_grant_ops_require_auth(http):
    resp = await http.get("/v0/drives/drv_00000000000000a1/grants")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_grant_ops_enforce_token_scope(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gsc", "kgsc")
    override_actor(make_actor(scopes={"drives:read", "drives:write", "usage:read"}))
    resp = await _create_grant(
        http, drive["id"], "kgsc-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# ── create / read / list ─────────────────────────────────────────────────────


async def test_create_and_read_grant(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gc", "kgc")
    resp = await _create_grant(
        http, drive["id"], "kgc-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="editor",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("grn_")
    assert body["principal_id"] == OTHER_AGENT
    assert body["role"] == "editor"
    assert body["state"] == "active"
    assert body["resource_type"] == "drive"
    assert body["resource_id"] == drive["id"]
    assert body["revision"].startswith("rev_")
    assert resp.headers["etag"] == f'"{body["revision"]}"'
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    assert resp.headers["location"] == (
        f"{origin}/v0/drives/{drive['id']}/grants/{body['id']}"
    )

    read = await http.get(f"/v0/drives/{drive['id']}/grants/{body['id']}")
    assert read.status_code == 200
    assert read.json()["role"] == "editor"


async def test_grant_read_304(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "g304", "kg304")
    resp = await _create_grant(
        http, drive["id"], "kg304-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    gid = resp.json()["id"]
    etag = resp.headers["etag"]
    not_modified = await http.get(
        f"/v0/drives/{drive['id']}/grants/{gid}", headers={"If-None-Match": etag}
    )
    assert not_modified.status_code == 304


async def test_public_grant_is_viewer_only(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gpub", "kgpub")
    ok = await _create_grant(
        http, drive["id"], "kgpub-1",
        principal_type="public", resource_type="drive", resource_id=drive["id"],
        role="viewer",
    )
    assert ok.status_code == 201
    assert ok.json()["principal_type"] == "public"
    assert ok.json()["principal_id"] is None

    bad = await _create_grant(
        http, drive["id"], "kgpub-2",
        principal_type="public", resource_type="drive", resource_id=drive["id"],
        role="manager",
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_create_duplicate_live_grant_is_conflict(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gdup", "kgdup")
    body = dict(
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    first = await _create_grant(http, drive["id"], "kgdup-1", **body)
    assert first.status_code == 201
    second = await _create_grant(http, drive["id"], "kgdup-2", **body)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "GRANT_CONFLICT"


async def test_expired_grant_does_not_block_regrant(http, override_actor):
    """Fix 3: an EXPIRED grant (revoked_at IS NULL but expires_at in the
    past) is dead for authorization, so a re-grant of the same
    (resource, principal, role) must SUCCEED — the create transaction revokes
    the stale row (freeing the partial-unique-index slot) and inserts fresh."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gexp", "kgexp")
    body = dict(
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
        expires_at="2020-01-01T00:00:00Z",
    )
    first = await _create_grant(http, drive["id"], "kgexp-1", **body)
    assert first.status_code == 201, first.text
    assert first.json()["state"] == "expired"

    second = await _create_grant(http, drive["id"], "kgexp-2", **body)
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first.json()["id"]
    assert second.json()["state"] == "expired"

    async with conn() as c:
        old = await c.fetchrow(
            "SELECT revoked_at FROM grants WHERE id = $1", first.json()["id"]
        )
        assert old["revoked_at"] is not None, "the stale expired grant must be revoked"


async def test_list_grants_filters_and_paginates(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gls", "kgls")
    for i, role in enumerate(("viewer", "editor", "manager")):
        resp = await _create_grant(
            http, drive["id"], f"kgls-{i}",
            principal_type="agent", principal_id=f"tcagt_00000000000000{i+3}",
            resource_type="drive", resource_id=drive["id"], role=role,
        )
        assert resp.status_code == 201

    listed = await http.get(f"/v0/drives/{drive['id']}/grants", params={"limit": 2})
    assert listed.status_code == 200
    assert listed.json()["next_cursor"]

    page2 = await http.get(
        f"/v0/drives/{drive['id']}/grants",
        params={"limit": 2, "cursor": listed.json()["next_cursor"]},
    )
    assert page2.status_code == 200
    assert page2.json()["next_cursor"]

    all_state = await http.get(
        f"/v0/drives/{drive['id']}/grants", params={"state": "all"}
    )
    assert all_state.status_code == 200


async def test_list_grants_rejects_bad_query_params(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "glq", "kglq")
    resp = await http.get(f"/v0/drives/{drive['id']}/grants", params={"bogus": "1"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_QUERY"


# ── update / revoke ──────────────────────────────────────────────────────────


async def test_update_grant_role_and_clear_expiry(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gup", "kgup")
    created = await _create_grant(
        http, drive["id"], "kgup-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    gid = created.json()["id"]

    upgraded = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{gid}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "kgup-2", "If-Match": created.headers["etag"]},
    )
    assert upgraded.status_code == 200
    assert upgraded.json()["role"] == "editor"

    clear_expiry = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{gid}",
        json={"expires_at": None},
        headers={"Idempotency-Key": "kgup-3", "If-Match": upgraded.headers["etag"]},
    )
    assert clear_expiry.status_code == 200
    assert clear_expiry.json()["expires_at"] is None


async def test_update_grant_preconditions(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gpre", "kgpre")
    created = await _create_grant(
        http, drive["id"], "kgpre-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    gid = created.json()["id"]

    no_match = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{gid}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "kgpre-2"},
    )
    assert no_match.status_code == 428

    stale = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{gid}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "kgpre-3", "If-Match": '"grn_00000000000000ff"'},
    )
    assert stale.status_code == 412


async def test_revoke_grant(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "grev", "kgrev")
    created = await _create_grant(
        http, drive["id"], "kgrev-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    gid = created.json()["id"]

    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{gid}",
        headers={"Idempotency-Key": "kgrev-2", "If-Match": created.headers["etag"]},
    )
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"

    gone = await http.get(f"/v0/drives/{drive['id']}/grants/{gid}")
    assert gone.status_code == 200  # revoked grants are still readable
    assert gone.json()["state"] == "revoked"

    again = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{gid}",
        headers={"Idempotency-Key": "kgrev-3", "If-Match": revoked.headers["etag"]},
    )
    assert again.status_code == 409


# ── the creator's own drive-level manager grant is permanent ────────────────


async def _owner_grant(http, drive_id: str) -> dict:
    """The creator's drive-level manager row, as `grants_list` returns it.

    `create_drive` mints one for the creating subject and (for an agent) one
    for its sponsor. Only the CREATOR's is the pinned one, so the sponsor's
    row is filtered out by principal id rather than by taking the first.
    """
    listed = await http.get(f"/v0/drives/{drive_id}/grants")
    assert listed.status_code == 200, listed.text
    rows = [
        row
        for row in listed.json()["items"]
        if row["resource_type"] == "drive"
        and row["principal_id"] == AGENT
        and row["role"] == "manager"
    ]
    assert len(rows) == 1, rows
    return rows[0]


async def test_creator_cannot_revoke_their_own_drive_manager_grant(
    http, override_actor,
):
    """A drive's access is grants and nothing else, so before this the
    creator could revoke their own and lock themselves out of their own
    drive — the console offered a Revoke button next to their own name.
    Deleting the drive is the way out; leaving it standing and orphaned is
    not."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gown", "kgown")
    owner = await _owner_grant(http, drive["id"])

    denied = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{owner['id']}",
        headers={
            "Idempotency-Key": "kgown-1",
            "If-Match": f'"{owner["revision"]}"',
        },
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "GRANT_PERMANENT"

    still = await http.get(f"/v0/drives/{drive['id']}/grants/{owner['id']}")
    assert still.json()["state"] == "active"


async def test_a_second_manager_cannot_revoke_the_creators_grant(
    http, override_actor,
):
    """The rule is about the DRIVE, not about who is asking. A self-only
    rule would still let manager B revoke the creator and then walk away
    from a drive nobody can administer."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gown2", "kgown2")
    owner = await _owner_grant(http, drive["id"])
    promoted = await _create_grant(
        http, drive["id"], "kgown2-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="manager",
    )
    assert promoted.status_code == 201

    override_actor(make_actor(subject=OTHER_AGENT, sponsor=None))
    denied = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{owner['id']}",
        headers={
            "Idempotency-Key": "kgown2-2",
            "If-Match": f'"{owner["revision"]}"',
        },
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "GRANT_PERMANENT"


async def test_owner_grant_cannot_be_demoted_or_given_an_expiry(
    http, override_actor,
):
    """Revoke is not the only door to an unadministrable drive: demoting the
    creator to viewer, or handing their grant an expiry, arrives at the same
    place on a delay."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gown3", "kgown3")
    owner = await _owner_grant(http, drive["id"])
    etag = f'"{owner["revision"]}"'

    demote = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{owner['id']}",
        json={"role": "viewer"},
        headers={"Idempotency-Key": "kgown3-1", "If-Match": etag},
    )
    assert demote.status_code == 409
    assert demote.json()["error"]["code"] == "GRANT_PERMANENT"

    expire = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{owner['id']}",
        json={"expires_at": "2099-01-01T00:00:00Z"},
        headers={"Idempotency-Key": "kgown3-2", "If-Match": etag},
    )
    assert expire.status_code == 409
    assert expire.json()["error"]["code"] == "GRANT_PERMANENT"

    # A no-op re-PATCH of what the row already says is still allowed: a
    # client that submits the whole row back is not refused for it.
    noop = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{owner['id']}",
        json={"role": "manager"},
        headers={"Idempotency-Key": "kgown3-3", "If-Match": etag},
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["role"] == "manager"


async def test_pinning_is_scoped_to_the_creators_drive_level_grant(
    http, override_actor,
):
    """Everything else about the creator revokes normally.

    Their grant on a FOLDER inside the drive is ordinary sharing, and the
    sponsor's founding manager row is not the creator's — neither is what
    administration depends on, so neither is pinned. Without this the rule
    would quietly weld the whole founding set onto the drive forever.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "gown4", "kgown4")
    folder = await _mkdir(
        http, drive["id"], drive["root_folder_id"], "shared", "kgown4-0"
    )
    assert folder.status_code == 201

    on_folder = await _create_grant(
        http, drive["id"], "kgown4-1",
        principal_type="agent", principal_id=AGENT,
        resource_type="folder", resource_id=folder.json()["id"], role="manager",
    )
    assert on_folder.status_code == 201
    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{on_folder.json()['id']}",
        headers={
            "Idempotency-Key": "kgown4-2",
            "If-Match": on_folder.headers["etag"],
        },
    )
    assert revoked.status_code == 200, revoked.text

    listed = await http.get(f"/v0/drives/{drive['id']}/grants")
    sponsor = next(
        row for row in listed.json()["items"] if row["principal_id"] == SPONSOR
    )
    dropped = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{sponsor['id']}",
        headers={
            "Idempotency-Key": "kgown4-3",
            "If-Match": f'"{sponsor["revision"]}"',
        },
    )
    assert dropped.status_code == 200, dropped.text


async def test_revoke_grant_requires_manager(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "grev2", "kgrev2")
    # The creator is a drive manager; grant viewer to ANOTHER agent, then act
    # as that agent: a viewer cannot revoke.
    created = await _create_grant(
        http, drive["id"], "kgrev2-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    denied = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{created.json()['id']}",
        headers={"Idempotency-Key": "kgrev2-2", "If-Match": created.headers["etag"]},
    )
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "GRANT_NOT_FOUND"


async def test_update_revoke_without_authority_do_not_disclose_existence(
    http, override_actor,
):
    """A same-workspace actor with sharing:write but NO grant on the drive
    gets the uniform 404 on update/revoke — never 428/412 — regardless of the
    If-Match header, so grant ids cannot be probed before authorization."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gno", "kgno")
    created = await _create_grant(
        http, drive["id"], "kgno-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    gid = created.json()["id"]

    override_actor(make_actor(subject=INTRUDER, workspace=WS_A,
                              scopes={"sharing:write"}))

    patch = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{gid}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "kgno-2"},
    )
    assert patch.status_code == 404
    assert patch.json()["error"]["code"] == "GRANT_NOT_FOUND"

    stale_patch = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{gid}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "kgno-3", "If-Match": '"grn_00000000000000ff"'},
    )
    assert stale_patch.status_code == 404
    assert stale_patch.json()["error"]["code"] == "GRANT_NOT_FOUND"

    revoke = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{gid}",
        headers={"Idempotency-Key": "kgno-4"},
    )
    assert revoke.status_code == 404
    assert revoke.json()["error"]["code"] == "GRANT_NOT_FOUND"

    stale_revoke = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{gid}",
        headers={"Idempotency-Key": "kgno-5", "If-Match": '"grn_00000000000000ff"'},
    )
    assert stale_revoke.status_code == 404
    assert stale_revoke.json()["error"]["code"] == "GRANT_NOT_FOUND"


async def test_create_denial_messages_are_identical(http, override_actor):
    """Both create-denial shapes — target missing, and caller denied on a
    real target — render byte-identical error messages."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gmsg", "kgmsg")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "sub", "kgmsg-1")
    assert folder.status_code == 201

    missing = await _create_grant(
        http, drive["id"], "kgmsg-2",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id="fld_00000000000000ff", role="viewer",
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "GRANT_NOT_FOUND"

    override_actor(make_actor(subject=INTRUDER, workspace=WS_A,
                              scopes={"sharing:write"}))
    denied = await _create_grant(
        http, drive["id"], "kgmsg-3",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder.json()["id"], role="viewer",
    )
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "GRANT_NOT_FOUND"

    assert missing.json()["error"]["message"] == denied.json()["error"]["message"]


# ── folder/artifact grants + workspace scoping ───────────────────────────────


async def test_folder_grant_requires_folder_id(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gfld", "kgfld")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "sub", "kgfld-1")
    assert folder.status_code == 201
    resp = await _create_grant(
        http, drive["id"], "kgfld-2",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder.json()["id"], role="viewer",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["resource_type"] == "folder"


async def test_grant_ops_cross_workspace_are_404(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "g404", "kg404")
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    listed = await http.get(f"/v0/drives/{drive['id']}/grants")
    assert listed.status_code == 404
    assert listed.json()["error"]["code"] == "DRIVE_NOT_FOUND"


async def test_break_glass_recovery(http, override_actor):
    """Zero active managers → a workspace admin can mint their own drive
    manager grant. (Since the workspace-admin overlay this succeeds through
    the ordinary manager path; the residual zero-manager break-glass branch
    is covered directly in tests/test_v0_workspace_admin_overlay.py.)"""
    override_actor(make_actor())
    drive = await _create_drive(http, "gbg", "kgbg")
    # Remove every active manager grant so the drive has none.
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
    resp = await _create_grant(
        http, drive["id"], "kgbg-1",
        principal_type="user", principal_id=admin.subject,
        resource_type="drive", resource_id=drive["id"], role="manager",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "manager"
    assert resp.json()["principal_id"] == admin.subject


async def test_admin_self_grant_succeeds_via_overlay_despite_live_managers(
    http, override_actor
):
    """Formerly: break-glass denied while a manager lives. The workspace-admin
    overlay (§8, 2026-08-28) makes an admin a manager on every drive in their
    workspace, so this self-grant now succeeds through the ORDINARY manager
    path — break-glass is never consulted. The zero-manager gate itself is
    still covered, against the core, in
    tests/test_v0_workspace_admin_overlay.py."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gbg2", "kgbg2")
    admin = make_actor(
        subject="tcusr_0000000000000006", subject_type="user",
        workspace=WS_A, workspace_role="admin",
        scopes={"drives:read", "drives:write", "sharing:read", "sharing:write"},
    )
    override_actor(admin)
    resp = await _create_grant(
        http, drive["id"], "kgbg2-1",
        principal_type="user", principal_id=admin.subject,
        resource_type="drive", resource_id=drive["id"], role="manager",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "manager"


async def test_break_glass_ignores_folder_scoped_manager(http, override_actor):
    """A drive whose ONLY live manager grant is FOLDER-scoped has zero
    DRIVE-level managers, so a workspace admin's break-glass create of their
    own drive-manager grant must SUCCEED — the folder-scoped manager cannot
    administer the drive and must not permanently block recovery."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gbgf", "kgbgf")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "sub", "kgbgf-1")
    assert folder.status_code == 201, folder.text

    # Leave a live FOLDER-scoped manager grant; revoke every DRIVE-level
    # manager grant (the creator's and the sponsor's).
    granted = await _create_grant(
        http, drive["id"], "kgbgf-2",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder.json()["id"], role="manager",
    )
    assert granted.status_code == 201, granted.text
    async with conn() as c:
        await c.execute(
            "UPDATE grants SET revoked_at = now() "
            "WHERE drive_id = $1 AND resource_type = 'drive' AND role = 'manager'",
            drive["id"],
        )

    admin = make_actor(
        subject="tcusr_0000000000000007", subject_type="user",
        workspace=WS_A, workspace_role="admin",
        scopes={"drives:read", "drives:write", "sharing:read", "sharing:write"},
    )
    override_actor(admin)
    resp = await _create_grant(
        http, drive["id"], "kgbgf-3",
        principal_type="user", principal_id=admin.subject,
        resource_type="drive", resource_id=drive["id"], role="manager",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "manager"
    assert resp.json()["principal_id"] == admin.subject


async def test_admin_self_grant_with_drive_manager_present_still_succeeds(
    http, override_actor
):
    """Formerly the drive-level twin of the denial above; under the
    workspace-admin overlay the admin passes the ordinary manager check and
    the self-grant lands as a routine grant row."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gbgd", "kgbgd")
    admin = make_actor(
        subject="tcusr_0000000000000008", subject_type="user",
        workspace=WS_A, workspace_role="admin",
        scopes={"drives:read", "drives:write", "sharing:read", "sharing:write"},
    )
    override_actor(admin)
    resp = await _create_grant(
        http, drive["id"], "kgbgd-1",
        principal_type="user", principal_id=admin.subject,
        resource_type="drive", resource_id=drive["id"], role="manager",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "manager"


async def test_break_glass_denied_for_agent(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gbg3", "kgbg3")
    async with conn() as c:
        await c.execute(
            "UPDATE grants SET revoked_at = now() WHERE drive_id = $1 AND role = 'manager'",
            drive["id"],
        )
    resp = await _create_grant(
        http, drive["id"], "kgbg3-1",
        principal_type="agent", principal_id=AGENT,
        resource_type="drive", resource_id=drive["id"], role="manager",
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "GRANT_NOT_FOUND"


# ── replay re-checks authorization (grant administration) ───────────────────


async def _grant_other_manager(http, drive_id: str, key: str) -> dict:
    """Mint a drive-manager grant for OTHER_AGENT (as the creator)."""
    resp = await _create_grant(
        http, drive_id, key,
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive_id, role="manager",
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _revoke_grant_as_creator(
    http, override_actor, drive_id: str, grant_id: str, etag: str, key: str
) -> None:
    override_actor(make_actor())
    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive_id}/grants/{grant_id}",
        headers={"Idempotency-Key": key, "If-Match": etag},
    )
    assert revoked.status_code == 200, revoked.text


async def test_grant_create_replay_rechecks_authorization(http, override_actor):
    """§6.2: a manager who created a grant, had their grant revoked, then
    replays the create must get 404 — not the stored 201."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gcrev", "kgcrev")
    grant = await _grant_other_manager(http, drive["id"], "kgcrev-1")

    # OTHER_AGENT (a drive manager) creates a grant under a fixed key.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    created = await _create_grant(
        http, drive["id"], "kgcrev-2",
        principal_type="agent", principal_id="tcagt_0000000000000003",
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    assert created.status_code == 201, created.text

    # Revoke OTHER_AGENT's manager grant.
    await _revoke_grant_as_creator(
        http, override_actor, drive["id"], grant["id"],
        f'"{grant["revision"]}"', "kgcrev-3",
    )

    # Replay the original create after revocation → 404, not the stored 201.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    replay = await _create_grant(
        http, drive["id"], "kgcrev-2",
        principal_type="agent", principal_id="tcagt_0000000000000003",
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    assert replay.status_code == 404
    assert replay.json()["error"]["code"] == "GRANT_NOT_FOUND"


async def test_grant_update_replay_rechecks_authorization(http, override_actor):
    """Same for update: revoke the actor's manager grant between first
    execution and replay → 404, not the stored 200."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gurev", "kgurev")
    mgr = await _grant_other_manager(http, drive["id"], "kgurev-1")

    # OTHER_AGENT creates a target viewer grant, then updates it under a key.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    target = await _create_grant(
        http, drive["id"], "kgurev-2",
        principal_type="agent", principal_id="tcagt_0000000000000003",
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    assert target.status_code == 201, target.text
    updated = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{target.json()['id']}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "kgurev-3", "If-Match": target.headers["etag"]},
    )
    assert updated.status_code == 200, updated.text

    # Revoke OTHER_AGENT's manager grant.
    await _revoke_grant_as_creator(
        http, override_actor, drive["id"], mgr["id"], f'"{mgr["revision"]}"', "kgurev-4",
    )

    # Replay the update after revocation → 404.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    replay = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{target.json()['id']}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "kgurev-3", "If-Match": target.headers["etag"]},
    )
    assert replay.status_code == 404
    assert replay.json()["error"]["code"] == "GRANT_NOT_FOUND"


async def test_grant_revoke_replay_rechecks_authorization(http, override_actor):
    """Same for revoke: revoke the actor's manager grant between first
    execution and replay → 404, not the stored 200."""
    override_actor(make_actor())
    drive = await _create_drive(http, "grrev", "kgrrev")
    mgr = await _grant_other_manager(http, drive["id"], "kgrrev-1")

    # OTHER_AGENT revokes a target viewer grant under a fixed key.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    target = await _create_grant(
        http, drive["id"], "kgrrev-2",
        principal_type="agent", principal_id="tcagt_0000000000000003",
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    assert target.status_code == 201, target.text
    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{target.json()['id']}",
        headers={"Idempotency-Key": "kgrrev-3", "If-Match": target.headers["etag"]},
    )
    assert revoked.status_code == 200, revoked.text

    # Revoke OTHER_AGENT's manager grant.
    await _revoke_grant_as_creator(
        http, override_actor, drive["id"], mgr["id"], f'"{mgr["revision"]}"', "kgrrev-4",
    )

    # Replay the revoke after revocation → 404.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    replay = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{target.json()['id']}",
        headers={"Idempotency-Key": "kgrrev-3", "If-Match": target.headers["etag"]},
    )
    assert replay.status_code == 404
    assert replay.json()["error"]["code"] == "GRANT_NOT_FOUND"


# ── enumeration is manager-only ──────────────────────────────────────────────


async def test_non_manager_lists_only_their_own_grants(http, override_actor):
    """A drive viewer must not be able to page out the drive's access graph.

    `grants_list` used to require only drive `viewer`, which meant any
    read-only principal could enumerate every principal id, role and expiry
    in the drive — the roster of who-can-touch-what, handed to the least
    privileged caller who can reach the endpoint. Now: managers enumerate,
    everyone else sees exactly the rows that grant access to THEM.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "genum", "kgenum")

    # A viewer for OTHER_AGENT, plus an unrelated third-party grant that
    # OTHER_AGENT must never see.
    own = await _create_grant(
        http, drive["id"], "kgenum-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    assert own.status_code == 201, own.text
    third_party = await _create_grant(
        http, drive["id"], "kgenum-2",
        principal_type="agent", principal_id=INTRUDER,
        resource_type="drive", resource_id=drive["id"], role="editor",
    )
    assert third_party.status_code == 201, third_party.text

    # The manager (drive creator) still sees everything.
    as_manager = await http.get(f"/v0/drives/{drive['id']}/grants")
    assert as_manager.status_code == 200
    manager_ids = {g["id"] for g in as_manager.json()["items"]}
    assert own.json()["id"] in manager_ids
    assert third_party.json()["id"] in manager_ids
    assert len(manager_ids) >= 3  # creator + sponsor + the two above

    # The viewer sees only their own row — never the third party's.
    override_actor(make_actor(subject=OTHER_AGENT))
    as_viewer = await http.get(f"/v0/drives/{drive['id']}/grants")
    assert as_viewer.status_code == 200, as_viewer.text
    viewer_items = as_viewer.json()["items"]
    assert [g["id"] for g in viewer_items] == [own.json()["id"]]
    assert all(g["principal_id"] == OTHER_AGENT for g in viewer_items)
    # No third-party principal id leaked anywhere in the body.
    assert INTRUDER not in as_viewer.text
    assert AGENT not in as_viewer.text


async def test_non_manager_listing_is_never_refused(http, override_actor):
    """Seeing your own access is not a privilege — the gate filters rows, it
    does not 403/404 the operation. A viewer with no grants of their own
    still gets a 200 with an empty page, not an error."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gself", "kgself")
    await _create_grant(
        http, drive["id"], "kgself-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    override_actor(make_actor(subject=OTHER_AGENT))
    resp = await http.get(f"/v0/drives/{drive['id']}/grants")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


async def test_non_manager_cannot_read_a_third_party_grant_by_id(http, override_actor):
    """The by-id sibling of the same enumeration. If `grants_read` stayed
    open to any viewer, the listing gate would only be obfuscation — a
    caller who learns a grant id (logs, a shared trace, a former manager
    role) could still read the row. A hidden grant reads as absent."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gread", "kgread")
    await _create_grant(
        http, drive["id"], "kgread-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    secret = await _create_grant(
        http, drive["id"], "kgread-2",
        principal_type="agent", principal_id=INTRUDER,
        resource_type="drive", resource_id=drive["id"], role="editor",
    )
    secret_id = secret.json()["id"]

    # The manager reads it fine.
    assert (await http.get(
        f"/v0/drives/{drive['id']}/grants/{secret_id}"
    )).status_code == 200

    override_actor(make_actor(subject=OTHER_AGENT))
    hidden = await http.get(f"/v0/drives/{drive['id']}/grants/{secret_id}")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "GRANT_NOT_FOUND"


async def test_non_manager_sees_workspace_and_public_grants_that_cover_them(
    http, override_actor
):
    """"Own access" is the authorization rule, not a literal principal_id
    match: a `workspace` grant covering the caller and a `public` grant (which
    already exposes the resource to them) ARE their access, and hiding them
    would misreport what they hold. Same `_principal_matches` predicate the
    authorization path uses, so the listing cannot drift from the decision."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gws", "kgws")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "pub", "kgws-0")
    ws_grant = await _create_grant(
        http, drive["id"], "kgws-1",
        principal_type="workspace", principal_id=WS_A,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    assert ws_grant.status_code == 201, ws_grant.text
    public_grant = await _create_grant(
        http, drive["id"], "kgws-2",
        principal_type="public", resource_type="folder",
        resource_id=folder.json()["id"], role="viewer",
    )
    assert public_grant.status_code == 201, public_grant.text

    override_actor(make_actor(subject=OTHER_AGENT))
    resp = await http.get(f"/v0/drives/{drive['id']}/grants")
    assert resp.status_code == 200
    seen = {g["id"] for g in resp.json()["items"]}
    assert ws_grant.json()["id"] in seen
    assert public_grant.json()["id"] in seen
    # ...but still not the creator's own manager grant.
    assert AGENT not in resp.text


# ── resource filters ─────────────────────────────────────────────────────────


async def test_grants_list_filters_by_resource(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gfilt", "kgfilt")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "sub", "kgfilt-0")
    folder_id = folder.json()["id"]
    on_folder = await _create_grant(
        http, drive["id"], "kgfilt-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder_id, role="viewer",
    )
    assert on_folder.status_code == 201, on_folder.text
    await _create_grant(
        http, drive["id"], "kgfilt-2",
        principal_type="agent", principal_id=INTRUDER,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )

    resp = await http.get(
        f"/v0/drives/{drive['id']}/grants",
        params={"resource_type": "folder", "resource_id": folder_id},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [g["id"] for g in items] == [on_folder.json()["id"]]
    assert items[0]["resource_id"] == folder_id


async def test_grants_list_resource_id_requires_resource_type(http, override_actor):
    """A bare resource id is ambiguous across drive/folder/artifact, so the
    pair is validated rather than guessed from the id prefix."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gpair", "kgpair")
    resp = await http.get(
        f"/v0/drives/{drive['id']}/grants",
        params={"resource_id": drive["id"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAMETER"


async def test_grants_list_rejects_malformed_resource_id(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "gbad", "kgbad")
    resp = await http.get(
        f"/v0/drives/{drive['id']}/grants",
        params={"resource_type": "folder", "resource_id": "not-a-folder-id"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_grants_list_cursor_is_bound_to_the_resource_filter(http, override_actor):
    """A cursor minted under one resource filter must not resume a different
    filtered set — the filter fingerprint is sealed into the token."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gcur", "kgcur")
    for i in range(3):
        r = await _create_grant(
            http, drive["id"], f"kgcur-{i}",
            principal_type="agent", principal_id=f"tcagt_000000000000003{i}",
            resource_type="drive", resource_id=drive["id"], role="viewer",
        )
        assert r.status_code == 201, r.text

    page = await http.get(
        f"/v0/drives/{drive['id']}/grants",
        params={"resource_type": "drive", "resource_id": drive["id"], "limit": 1},
    )
    assert page.status_code == 200
    cursor = page.json()["next_cursor"]
    assert cursor

    swapped = await http.get(
        f"/v0/drives/{drive['id']}/grants",
        params={"resource_type": "drive", "cursor": cursor, "limit": 1},
    )
    assert swapped.status_code == 400
    assert swapped.json()["error"]["code"] == "INVALID_CURSOR"


async def test_folder_scoped_principal_can_see_their_own_access(http, override_actor):
    """A folder-scoped principal holds access but no DRIVE-level grant, and
    the old gate (`require_local("viewer","drive",...)`) 404'd them — while
    `grants_create` still answered 409 GRANT_CONFLICT for a resource they
    administer. A read surface must not refuse what a write surface reveals.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "gfold", "kgfold")
    folder = (await _mkdir(
        http, drive["id"], drive["root_folder_id"], "sub", "kgfold-0"
    )).json()
    own = await _create_grant(
        http, drive["id"], "kgfold-1",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder["id"], role="manager",
    )
    assert own.status_code == 201, own.text
    hidden = await _create_grant(
        http, drive["id"], "kgfold-2",
        principal_type="agent", principal_id=INTRUDER,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )

    override_actor(make_actor(subject=OTHER_AGENT))
    listed = await http.get(f"/v0/drives/{drive['id']}/grants")
    assert listed.status_code == 200, listed.text
    assert [g["id"] for g in listed.json()["items"]] == [own.json()["id"]]
    # ...and can read their own row by id.
    mine = await http.get(f"/v0/drives/{drive['id']}/grants/{own.json()['id']}")
    assert mine.status_code == 200
    # ...but still not a third party's.
    assert (await http.get(
        f"/v0/drives/{drive['id']}/grants/{hidden.json()['id']}"
    )).status_code == 404


async def test_a_principal_with_no_grant_at_all_is_still_refused(http, override_actor):
    """The gate widened to "any live grant in the drive", not to "anyone in
    the workspace" — a principal with nothing here still reads as absent."""
    override_actor(make_actor())
    drive = await _create_drive(http, "gnone", "kgnone")
    override_actor(make_actor(subject=INTRUDER))
    resp = await http.get(f"/v0/drives/{drive['id']}/grants")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_an_unfiltered_grants_cursor_survives_the_new_filter_params(
    http, override_actor
):
    """`resource_id` joins the cursor fingerprint ONLY when supplied. An
    unfiltered cursor therefore hashes exactly as it did before this change,
    so a client paging across the candidate/flip deploy does not flap between
    200 and 400 INVALID_CURSOR depending on which revision serves it."""
    from agentdrive.api.v0_cursors import seal as _seal

    override_actor(make_actor())
    drive = await _create_drive(http, "gcompat", "kgcompat")
    for i in range(3):
        r = await _create_grant(
            http, drive["id"], f"kgcompat-{i}",
            principal_type="agent", principal_id=f"tcagt_000000000000004{i}",
            resource_type="drive", resource_id=drive["id"], role="viewer",
        )
        assert r.status_code == 201, r.text

    page = await http.get(f"/v0/drives/{drive['id']}/grants", params={"limit": 1})
    assert page.status_code == 200
    anchor = page.json()["items"][0]["id"]

    # A cursor minted with the PRE-CHANGE fingerprint (no resource_id key).
    # The key is `lifecycle`, NOT `state`, and must stay that way: the query
    # parameter was renamed but the sealed fingerprint's key deliberately was
    # not, precisely so a cursor minted before the rename still resumes. If
    # this literal ever follows the parameter, the test stops proving that.
    legacy = _seal(
        "grants", drive["id"], {"id": anchor},
        bound={
            "lifecycle": "active",
            "resource_type": None,
            "principal_type": None,
        },
    )
    resumed = await http.get(
        f"/v0/drives/{drive['id']}/grants", params={"cursor": legacy, "limit": 1}
    )
    assert resumed.status_code == 200, resumed.text


async def test_malformed_principal_id_is_422_not_500(http, override_actor):
    """`schema.sql` has always rejected these; nothing mapped the violation.

    `grants_principal_id_shape` has enforced the `tcagt_` / `tcusr_` prefixes
    since day zero, and `asyncpg.CheckViolationError` was unmapped — so a
    malformed id surfaced as a 500. The boundary validator catches it first
    now; the mapper is the backstop for every other path into the table.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "pid", "kp-1")

    resp = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent", "principal_id": "oops_1",
            "resource_type": "drive", "resource_id": drive["id"], "role": "viewer",
        },
        headers={"Idempotency-Key": "kp-2"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_workspace_grant_requires_a_principal_id(http, override_actor):
    """The published field description used to say the opposite.

    A workspace grant is matched on `principal_id = <workspace>`, and the
    schema requires it NOT NULL — so omitting it is refused. Only `public`
    names no principal. Pinned because the wrong version of this rule shipped
    in the spec, where a generated SDK would repeat it.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "wsg", "kw-1")

    missing = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "workspace",
            "resource_type": "drive", "resource_id": drive["id"], "role": "viewer",
        },
        headers={"Idempotency-Key": "kw-2"},
    )
    assert missing.status_code == 400, missing.text

    supplied = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "workspace", "principal_id": WS_A,
            "resource_type": "drive", "resource_id": drive["id"], "role": "viewer",
        },
        headers={"Idempotency-Key": "kw-3"},
    )
    assert supplied.status_code == 201, supplied.text
