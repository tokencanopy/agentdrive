"""Shares vertical (slice 8): the 5 share operations over real Postgres.

Drive creation mints a drive-level manager grant for the creator, so the
default actor is already a drive manager and can mint shares on any drive
resource. Mirrors the drives/folders/grant test shape: mutations require
``Idempotency-Key``; revoke/rotate require ``If-Match``; reads carry ETag
and honor ``If-None-Match`` → 304; workspace scoping 404s; the create/rotate
responses are the only ones carrying the plaintext ``secret``.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.core import urls
from agentdrive.core.v0_shares import hash_secret
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
OTHER_AGENT = "tcagt_0000000000000002"
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
    resp = await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp


async def _create_share(http, drive_id: str, key: str, **body) -> object:
    return await http.post(
        f"/v0/drives/{drive_id}/shares",
        json=body,
        headers={"Idempotency-Key": key},
    )


# ── auth / scope ─────────────────────────────────────────────────────────────


async def test_share_ops_require_auth(http):
    resp = await http.get("/v0/drives/drv_00000000000000a1/shares")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_share_ops_enforce_token_scope(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "ssc", "kssc")
    override_actor(make_actor(scopes={"drives:read", "drives:write", "usage:read"}))
    resp = await _create_share(
        http, drive["id"], "kssc-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# ── create / read / list ─────────────────────────────────────────────────────


async def test_create_share_returns_secret_once(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "sc", "ksc")
    resp = await _create_share(
        http, drive["id"], "ksc-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("shr_")
    assert body["resource_type"] == "folder"
    assert body["resource_id"] == drive["root_folder_id"]
    assert body["state"] == "active"
    assert body["created_by"] == AGENT
    assert "secret" in body
    assert len(body["secret"]) >= 32
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    assert resp.headers["location"] == (
        f"{origin}/v0/drives/{drive['id']}/shares/{body['id']}"
    )

    read = await http.get(f"/v0/drives/{drive['id']}/shares/{body['id']}")
    assert read.status_code == 200
    assert "secret" not in read.json(), "secret must never appear on read"


async def test_create_share_returns_the_public_redemption_url(http, override_actor):
    """`Location` is the MANAGEMENT url; it is useless to whoever should open
    the link. The redemption URL lives on the public share origin and embeds
    the secret, and that origin is deployment configuration -- so a client
    genuinely cannot compose it. Minting a share without returning it left the
    caller holding a credential and no address to use it at.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "surl", "ksurl")
    resp = await _create_share(
        http, drive["id"], "ksurl-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["url"], "a minted share must carry its redemption url"
    assert body["url"] == urls.share_url(body["secret"])
    # It is the PUBLIC surface, not the management API.
    assert "/v0/" not in body["url"]
    assert f"/s/{body['secret']}" in body["url"]

    # The url embeds the credential, so it follows `secret` exactly: never on
    # read, never on list.
    read = await http.get(f"/v0/drives/{drive['id']}/shares/{body['id']}")
    assert read.status_code == 200
    assert "url" not in read.json() or read.json()["url"] is None
    listed = await http.get(f"/v0/drives/{drive['id']}/shares")
    assert listed.status_code == 200
    for item in listed.json()["items"]:
        assert not item.get("url"), "list must not leak a redemption url"


async def test_idempotent_share_replay_returns_no_url(http, override_actor):
    """A replay withholds the secret by design, so it must withhold the url
    too -- otherwise the ledger would hand back a live credential."""
    override_actor(make_actor())
    drive = await _create_drive(http, "surl2", "ksurl2")
    first = await _create_share(
        http, drive["id"], "ksurl2-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    assert first.status_code == 201, first.text
    assert first.json()["url"]
    replay = await _create_share(
        http, drive["id"], "ksurl2-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    assert replay.status_code == 201, replay.text
    assert not replay.json().get("secret")
    assert not replay.json().get("url")


async def test_create_share_snapshot_and_artifact(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "ss", "kss")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "sub", "kss-1")
    artifact = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + drive["root_folder_id"].encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            b"f.txt\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\"f.txt\"\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"hello\r\n--b--\r\n"
        ),
        headers={"Content-Type": "multipart/form-data; boundary=b", "Idempotency-Key": "kss-2"},
    )
    assert artifact.status_code == 201, artifact.text
    art_id = artifact.json()["id"]
    ver_id = artifact.json()["head_version_id"]

    folder_share = await _create_share(
        http, drive["id"], "kss-3",
        resource_type="folder", resource_id=folder.json()["id"],
    )
    assert folder_share.status_code == 201
    art_share = await _create_share(
        http, drive["id"], "kss-4",
        resource_type="artifact", resource_id=art_id,
    )
    assert art_share.status_code == 201
    ver_share = await _create_share(
        http, drive["id"], "kss-5",
        resource_type="artifact_version", resource_id=ver_id,
    )
    assert ver_share.status_code == 201
    assert ver_share.json()["resource_type"] == "artifact_version"
    assert ver_share.json()["resource_id"] == ver_id


async def test_create_share_404_for_other_workspace(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "s404", "ks404")
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp = await _create_share(
        http, drive["id"], "ks404-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DRIVE_NOT_FOUND"


async def test_share_read_304(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "s304", "ks304")
    created = await _create_share(
        http, drive["id"], "ks304-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    sid = created.json()["id"]
    not_modified = await http.get(
        f"/v0/drives/{drive['id']}/shares/{sid}",
        headers={"If-None-Match": created.headers["etag"]},
    )
    assert not_modified.status_code == 304


async def test_list_shares_never_contains_secret(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "sls", "ksls")
    for i in range(3):
        await _create_share(
            http, drive["id"], f"ksls-{i}",
            resource_type="folder", resource_id=drive["root_folder_id"],
        )
    listed = await http.get(f"/v0/drives/{drive['id']}/shares", params={"limit": 2})
    assert listed.status_code == 200
    assert listed.json()["next_cursor"]
    for item in listed.json()["items"]:
        assert "secret" not in item


async def test_share_secret_is_redacted_from_idempotency_ledger(http, override_actor):
    """§6.2: a secret-bearing create/rotate response is stored in the
    idempotency ledger WITHOUT the plaintext secret, so a DB backup/leak can
    never yield share credentials."""
    override_actor(make_actor())
    drive = await _create_drive(http, "sled", "ksled")
    created = await _create_share(
        http, drive["id"], "ksled-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    secret = created.json()["secret"]
    assert secret  # the LIVE response carries it

    async with conn() as c:
        row = await c.fetchrow(
            "SELECT response_body FROM idempotency_records "
            "WHERE principal_id=$1 AND idempotency_key='ksled-1'",
            AGENT,
        )
    assert row is not None
    stored = json.loads(row["response_body"])
    assert "secret" not in stored
    assert stored["id"] == created.json()["id"]

    # Rotate likewise never persists the new secret.
    rotated = await http.post(
        f"/v0/drives/{drive['id']}/shares/{created.json()['id']}/rotate",
        headers={"Idempotency-Key": "ksled-2", "If-Match": created.headers["etag"]},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["secret"]
    async with conn() as c:
        row2 = await c.fetchrow(
            "SELECT response_body FROM idempotency_records "
            "WHERE principal_id=$1 AND idempotency_key='ksled-2'",
            AGENT,
        )
    assert "secret" not in json.loads(row2["response_body"])


# ── rotate / revoke ──────────────────────────────────────────────────────────


async def test_rotate_share_changes_secret(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "srot", "ksrot")
    created = await _create_share(
        http, drive["id"], "ksrot-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    sid = created.json()["id"]
    old_secret = created.json()["secret"]

    rotated = await http.post(
        f"/v0/drives/{drive['id']}/shares/{sid}/rotate",
        headers={"Idempotency-Key": "ksrot-2", "If-Match": created.headers["etag"]},
    )
    assert rotated.status_code == 200
    new_secret = rotated.json()["secret"]
    assert new_secret != old_secret
    assert rotated.json()["id"] == sid

    async with conn() as c:
        old_hash = await c.fetchval(
            "SELECT 1 FROM shares WHERE id=$1 AND secret_hash=$2",
            sid, hash_secret(old_secret),
        )
        assert old_hash is None, "the old secret must die on rotate"


async def test_rotate_and_revoke_require_if_match(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "spre", "kspre")
    created = await _create_share(
        http, drive["id"], "kspre-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    sid = created.json()["id"]

    no_match = await http.post(
        f"/v0/drives/{drive['id']}/shares/{sid}/rotate",
        headers={"Idempotency-Key": "kspre-2"},
    )
    assert no_match.status_code == 428

    revoke_no_match = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/shares/{sid}",
        headers={"Idempotency-Key": "kspre-3"},
    )
    assert revoke_no_match.status_code == 428

    stale = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/shares/{sid}",
        headers={"Idempotency-Key": "kspre-4", "If-Match": '"shr_00000000000000ff"'},
    )
    assert stale.status_code == 412


async def test_revoke_share(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "srev", "ksrev")
    created = await _create_share(
        http, drive["id"], "ksrev-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    sid = created.json()["id"]

    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/shares/{sid}",
        headers={"Idempotency-Key": "ksrev-2", "If-Match": created.headers["etag"]},
    )
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"

    again = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/shares/{sid}",
        headers={"Idempotency-Key": "ksrev-3", "If-Match": revoked.headers["etag"]},
    )
    assert again.status_code == 409


async def test_share_ops_require_manager(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "smgr", "ksmgr")
    created = await _create_share(
        http, drive["id"], "ksmgr-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    # A non-manager (not the creator, no manager grant) cannot list, read,
    # create, or revoke — shares are a drive-manager (admin) surface in v0.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    listed = await http.get(f"/v0/drives/{drive['id']}/shares")
    assert listed.status_code == 404
    assert listed.json()["error"]["code"] == "NOT_AUTHORIZED"
    denied = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/shares/{created.json()['id']}",
        headers={"Idempotency-Key": "ksmgr-2", "If-Match": created.headers["etag"]},
    )
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "NOT_AUTHORIZED"


# ── idempotent replay of secret-bearing operations ─────────────────────────


async def test_create_share_replay_returns_null_secret(http, override_actor):
    """Replaying create with the same Idempotency-Key must not 500: the
    ledger stores the body WITHOUT the secret, so a replay serves the stored
    result with `secret: null` (the secret is disclosed exactly once)."""
    override_actor(make_actor())
    drive = await _create_drive(http, "rep", "krep")
    body = {"resource_type": "folder", "resource_id": drive["root_folder_id"]}

    first = await http.post(
        f"/v0/drives/{drive['id']}/shares",
        json=body, headers={"Idempotency-Key": "krep-1"},
    )
    assert first.status_code == 201, first.text
    assert first.json()["secret"]  # first execution discloses it

    replay = await http.post(
        f"/v0/drives/{drive['id']}/shares",
        json=body, headers={"Idempotency-Key": "krep-1"},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["secret"] is None


async def test_rotate_share_replay_returns_null_secret(http, override_actor):
    """Same for rotate: replay serves the stored result with `secret: null`."""
    override_actor(make_actor())
    drive = await _create_drive(http, "rrot", "krrot")
    created = await _create_share(
        http, drive["id"], "krrot-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    sid = created.json()["id"]

    first = await http.post(
        f"/v0/drives/{drive['id']}/shares/{sid}/rotate",
        headers={"Idempotency-Key": "krrot-2", "If-Match": created.headers["etag"]},
    )
    assert first.status_code == 200, first.text
    assert first.json()["secret"]

    replay = await http.post(
        f"/v0/drives/{drive['id']}/shares/{sid}/rotate",
        headers={"Idempotency-Key": "krrot-2", "If-Match": created.headers["etag"]},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["secret"] is None


async def test_share_replay_rechecks_authorization_after_revoke(http, override_actor):
    """§6.2: replay re-checks authorization. A manager who created a share,
    had their grant revoked, then replays the original create must get 404 —
    not the stored 201."""
    override_actor(make_actor())
    drive = await _create_drive(http, "srev", "ksrev")
    root = drive["root_folder_id"]

    # Grant OTHER_AGENT manager, then have them create a share.
    granted = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent", "principal_id": OTHER_AGENT,
            "resource_type": "drive", "resource_id": drive["id"], "role": "manager",
        },
        headers={"Idempotency-Key": "ksrev-1"},
    )
    assert granted.status_code == 201, granted.text

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    created = await _create_share(
        http, drive["id"], "ksrev-2",
        resource_type="folder", resource_id=root,
    )
    assert created.status_code == 201, created.text

    # Revoke their manager grant (as the creator).
    override_actor(make_actor())
    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{granted.json()['id']}",
        headers={
            "Idempotency-Key": "ksrev-3",
            "If-Match": f'"{granted.json()["revision"]}"',
        },
    )
    assert revoked.status_code == 200, revoked.text

    # Replay the original create after revocation: the route-level
    # require_local(manager) runs on the replay too, so this is 404.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    replay = await _create_share(
        http, drive["id"], "ksrev-2",
        resource_type="folder", resource_id=root,
    )
    assert replay.status_code == 404
    assert replay.json()["error"]["code"] == "NOT_AUTHORIZED"


# ── local-authorization gates (shares are a drive-manager surface) ─────────


async def test_no_local_grant_cannot_list_read_or_create(http, override_actor):
    """A same-workspace principal with sharing:* scope but NO local grant on
    the drive cannot list, read, or create shares — uniform 404."""
    override_actor(make_actor())
    drive = await _create_drive(http, "nog", "knog")
    created = await _create_share(
        http, drive["id"], "knog-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    assert created.status_code == 201, created.text

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))

    listed = await http.get(f"/v0/drives/{drive['id']}/shares")
    assert listed.status_code == 404
    assert listed.json()["error"]["code"] == "NOT_AUTHORIZED"

    read = await http.get(f"/v0/drives/{drive['id']}/shares/{created.json()['id']}")
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "NOT_AUTHORIZED"

    create = await _create_share(
        http, drive["id"], "knog-2",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    assert create.status_code == 404
    assert create.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_editor_grant_cannot_list_shares(http, override_actor):
    """An editor (non-manager) local grant does not open the shares surface."""
    override_actor(make_actor())
    drive = await _create_drive(http, "edg", "kedg")
    await _create_share(
        http, drive["id"], "kedg-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )

    granted = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent", "principal_id": OTHER_AGENT,
            "resource_type": "drive", "resource_id": drive["id"], "role": "editor",
        },
        headers={"Idempotency-Key": "kedg-2"},
    )
    assert granted.status_code == 201, granted.text

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    listed = await http.get(f"/v0/drives/{drive['id']}/shares")
    assert listed.status_code == 404
    assert listed.json()["error"]["code"] == "NOT_AUTHORIZED"



# ── redemption (/s/{share_key}) ──────────────────────────────────────────────


async def test_redeem_artifact_share_streams_content(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "redem", "kredem")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + drive["root_folder_id"].encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            b"shared.bin\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\"shared.bin\"\r\n"
            b"Content-Type: application/octet-stream\r\n\r\n"
            b"possession-is-all\r\n--b--\r\n"
        ),
        headers={"Content-Type": "multipart/form-data; boundary=b", "Idempotency-Key": "kredem-1"},
    )
    assert created.status_code == 201, created.text
    art_id = created.json()["id"]

    share = await _create_share(
        http, drive["id"], "kredem-2",
        resource_type="artifact", resource_id=art_id,
    )
    assert share.status_code == 201, share.text
    secret = share.json()["secret"]

    # Redeem with NO auth — possession alone.
    resp = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert resp.content == b"possession-is-all"
    assert resp.headers["etag"] == f'"{created.json()["head_version_id"]}"'
    assert resp.headers["referrer-policy"] == "no-referrer"


async def test_redeem_share_404_after_target_soft_deleted(http, override_actor):
    """§6.9: a share is a capability over a LIVE artifact head. Soft-deleting
    the artifact stops the share from serving (the share row stays, but
    redemption resolves to nothing)."""
    override_actor(make_actor())
    drive = await _create_drive(http, "redsd", "kredsd")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + drive["root_folder_id"].encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            b"soon-gone.bin\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\"soon-gone.bin\"\r\n"
            b"Content-Type: application/octet-stream\r\n\r\n"
            b"bytes-to-protect\r\n--b--\r\n"
        ),
        headers={"Content-Type": "multipart/form-data; boundary=b", "Idempotency-Key": "kredsd-1"},
    )
    assert created.status_code == 201, created.text
    art_id = created.json()["id"]

    share = await _create_share(
        http, drive["id"], "kredsd-2",
        resource_type="artifact", resource_id=art_id,
    )
    assert share.status_code == 201, share.text
    secret = share.json()["secret"]

    # Redeems fine while live.
    before = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert before.status_code == 200

    # Soft-delete the artifact (manager, correct ETag).
    deleted = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        headers={
            "Idempotency-Key": "kredsd-3",
            "If-Match": f'"{created.json()["revision"]}"',
        },
    )
    assert deleted.status_code == 200, deleted.text

    # The share now serves nothing — 404, not a leak.
    after = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert after.status_code == 404


async def test_redeem_share_404_after_drive_soft_deleted(http, override_actor):
    """§6.9: a share is a capability over content in a LIVE drive. Drive
    soft-delete does not cascade to child rows, so the target stays live — the
    DRIVE row's liveness is the one check the authenticated surface gets via
    `_ensure_drive` and the anonymous `/s/` path was missing. A soft-deleted
    drive's share links must stop serving, and a restore brings them back."""
    override_actor(make_actor())
    drive = await _create_drive(http, "reddrv", "kreddrv")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + drive["root_folder_id"].encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            b"drive-bound.bin\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\"drive-bound.bin\"\r\n"
            b"Content-Type: application/octet-stream\r\n\r\n"
            b"still-live-rows\r\n--b--\r\n"
        ),
        headers={"Content-Type": "multipart/form-data; boundary=b", "Idempotency-Key": "kreddrv-1"},
    )
    assert created.status_code == 201, created.text
    art_id = created.json()["id"]

    share = await _create_share(
        http, drive["id"], "kreddrv-2",
        resource_type="artifact", resource_id=art_id,
    )
    assert share.status_code == 201, share.text
    secret = share.json()["secret"]

    # Redeems fine while the drive is live.
    before = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert before.status_code == 200

    # Soft-delete the DRIVE — the artifact underneath stays live, so only the
    # drive-liveness condition can kill the share.
    drive_etag = (await http.get(f"/v0/drives/{drive['id']}")).headers["etag"]
    deleted = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}",
        headers={"Idempotency-Key": "kreddrv-3", "If-Match": drive_etag},
    )
    assert deleted.status_code == 200, deleted.text

    after = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert after.status_code == 404

    # Restore the drive → the share serves again.
    restored = await http.post(
        f"/v0/drives/{drive['id']}/restore",
        headers={"Idempotency-Key": "kreddrv-4", "If-Match": deleted.headers["etag"]},
    )
    assert restored.status_code == 200, restored.text

    again = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert again.status_code == 200
    assert again.content == b"still-live-rows"


async def test_redeem_share_404_for_unknown_secret(http):
    resp = await http.get("/s/not-a-real-secret", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "SHARE_NOT_FOUND"
    assert "detail" not in body


async def test_redeem_share_404_after_revoke(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "redem2", "kredem2")
    share = await _create_share(
        http, drive["id"], "kredem2-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    secret = share.json()["secret"]

    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/shares/{share.json()['id']}",
        headers={"Idempotency-Key": "kredem2-2", "If-Match": share.headers["etag"]},
    )
    assert revoked.status_code == 200

    resp = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SHARE_NOT_FOUND"


async def test_public_redeem_above_threshold_never_signs(
    http, override_actor, monkeypatch
):
    """Public bytes stay on the metered proxy above the private threshold."""
    from agentdrive import storage

    # Force the artifact over the signed-download threshold and stub signing
    # so the 307 branch actually runs (fake-gcs returns None by default).
    monkeypatch.setattr(settings, "download_signed_min_bytes", 8)
    calls = []

    async def _fake_signed(*args, **kwargs):
        calls.append((args, kwargs))
        return "https://signed.example/presigned-url"

    monkeypatch.setattr(storage, "signed_download_url", _fake_signed)

    override_actor(make_actor())
    drive = await _create_drive(http, "red307", "kred307")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + drive["root_folder_id"].encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            b"big.bin\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\"big.bin\"\r\n"
            b"Content-Type: application/octet-stream\r\n\r\n"
            b"this-payload-exceeds-eight-bytes\r\n--b--\r\n"
        ),
        headers={"Content-Type": "multipart/form-data; boundary=b", "Idempotency-Key": "kred307-1"},
    )
    assert created.status_code == 201, created.text
    art_id = created.json()["id"]

    share = await _create_share(
        http, drive["id"], "kred307-2",
        resource_type="artifact", resource_id=art_id,
    )
    secret = share.json()["secret"]

    resp = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert resp.content == b"this-payload-exceeds-eight-bytes"
    assert "location" not in resp.headers
    assert calls == []


async def test_redeem_at_or_under_threshold_streams_content_with_length(
    http, override_actor, monkeypatch
):
    """The ≤threshold branch still streams 200 with correct Content-Type and
    Content-Length (pin the good branch)."""
    monkeypatch.setattr(settings, "download_signed_min_bytes", 8)

    override_actor(make_actor())
    drive = await _create_drive(http, "red200", "kred200")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + drive["root_folder_id"].encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            b"small.bin\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\"small.bin\"\r\n"
            b"Content-Type: application/octet-stream\r\n\r\n"
            b"tiny\r\n--b--\r\n"
        ),
        headers={"Content-Type": "multipart/form-data; boundary=b", "Idempotency-Key": "kred200-1"},
    )
    assert created.status_code == 201, created.text
    art_id = created.json()["id"]

    share = await _create_share(
        http, drive["id"], "kred200-2",
        resource_type="artifact", resource_id=art_id,
    )
    secret = share.json()["secret"]

    resp = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert resp.content == b"tiny"
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-length"] == "4"


async def test_redeem_folder_share_returns_marker(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "redem3", "kredem3")
    share = await _create_share(
        http, drive["id"], "kredem3-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )
    secret = share.json()["secret"]

    resp = await http.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource_type"] == "folder"
    assert body["resource_id"] == drive["root_folder_id"]


# ── render metadata on resolved targets ──────────────────────────────────────


async def test_resolve_secret_carries_render_metadata(http, override_actor):
    """The viewer needs more than bytes: a title, a path, and a timestamp.

    Without these the OG tags cannot name the artifact, which is the whole
    point of server-rendering the page.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "rmeta", "krmeta")
    reports = await _mkdir(http, drive["id"], drive["root_folder_id"], "reports", "krmeta-1")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + reports.json()["id"].encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            b"report.md\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\"report.md\"\r\n"
            b"Content-Type: text/markdown\r\n\r\n"
            b"# Title\n\r\n--b--\r\n"
        ),
        headers={"Content-Type": "multipart/form-data; boundary=b", "Idempotency-Key": "krmeta-2"},
    )
    assert created.status_code == 201, created.text
    art = created.json()

    share = await _create_share(
        http, drive["id"], "krmeta-3",
        resource_type="artifact", resource_id=art["id"],
    )
    assert share.status_code == 201, share.text

    from agentdrive.core import v0_shares

    async with conn() as c:
        target = await v0_shares.resolve_secret(c, secret=share.json()["secret"])

    assert target["kind"] == "artifact"
    assert target["artifact_id"] == art["id"]
    assert target["updated_at"] is not None
    assert target["path"] == "reports/report.md"
    # Unchanged keys the byte path still relies on:
    assert target["name"] == "report.md"
    assert target["etag"] == art["head_version_id"]


async def test_resolve_secret_render_metadata_on_version_shares(http, override_actor):
    """A version share pins immutable bytes but still names its live artifact,
    so the viewer can title the page and link back to the current head."""
    override_actor(make_actor())
    drive = await _create_drive(http, "rmetav", "krmetav")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + drive["root_folder_id"].encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            b"pinned.md\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\"pinned.md\"\r\n"
            b"Content-Type: text/markdown\r\n\r\n"
            b"v1\r\n--b--\r\n"
        ),
        headers={"Content-Type": "multipart/form-data; boundary=b", "Idempotency-Key": "krmetav-1"},
    )
    assert created.status_code == 201, created.text
    art = created.json()

    share = await _create_share(
        http, drive["id"], "krmetav-2",
        resource_type="artifact_version", resource_id=art["head_version_id"],
    )
    assert share.status_code == 201, share.text

    from agentdrive.core import v0_shares

    async with conn() as c:
        target = await v0_shares.resolve_secret(c, secret=share.json()["secret"])

    assert target["kind"] == "artifact"
    assert target["artifact_id"] == art["id"]
    assert target["updated_at"] is not None
    assert target["path"] == "pinned.md"
    assert target["name"] == "pinned.md"
    assert target["etag"] == art["head_version_id"]


# ── resource filters ─────────────────────────────────────────────────────────


async def test_shares_list_filters_by_resource(http, override_actor):
    """"What links exist on THIS resource" — the only query shape the
    pre-reset surface had, and the one the share dialog needs. Without it a
    caller has to page the whole drive's link list and filter client-side."""
    override_actor(make_actor())
    drive = await _create_drive(http, "sfilt", "ksfilt")
    folder = (await _mkdir(
        http, drive["id"], drive["root_folder_id"], "sub", "ksfilt-0"
    )).json()
    other = (await _mkdir(
        http, drive["id"], drive["root_folder_id"], "other", "ksfilt-1"
    )).json()

    on_folder = await _create_share(
        http, drive["id"], "ksfilt-2",
        resource_type="folder", resource_id=folder["id"],
    )
    assert on_folder.status_code == 201, on_folder.text
    on_other = await _create_share(
        http, drive["id"], "ksfilt-3",
        resource_type="folder", resource_id=other["id"],
    )
    assert on_other.status_code == 201, on_other.text

    unfiltered = await http.get(f"/v0/drives/{drive['id']}/shares")
    assert len({s["id"] for s in unfiltered.json()["items"]}) == 2

    resp = await http.get(
        f"/v0/drives/{drive['id']}/shares",
        params={"resource_type": "folder", "resource_id": folder["id"]},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [s["id"] for s in items] == [on_folder.json()["id"]]
    assert items[0]["resource_id"] == folder["id"]
    # The management representation still carries no secret material.
    assert "secret" not in items[0]


async def test_shares_list_resource_id_requires_resource_type(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "spair", "kspair")
    resp = await http.get(
        f"/v0/drives/{drive['id']}/shares",
        params={"resource_id": drive["root_folder_id"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAMETER"


async def test_shares_list_rejects_bad_resource_type_and_id(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "sbad", "ksbad")
    bad_type = await http.get(
        f"/v0/drives/{drive['id']}/shares",
        params={"resource_type": "drive", "resource_id": drive["id"]},
    )
    assert bad_type.status_code == 400
    assert bad_type.json()["error"]["code"] == "INVALID_ARGUMENT"

    bad_id = await http.get(
        f"/v0/drives/{drive['id']}/shares",
        params={"resource_type": "folder", "resource_id": "nope"},
    )
    assert bad_id.status_code == 400
    assert bad_id.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_shares_list_filter_stays_manager_only(http, override_actor):
    """The filter must not become a side door: `shares_list` requires drive
    manager, and a filtered request is refused for a non-manager exactly like
    an unfiltered one. A viewer must not be able to probe link existence."""
    override_actor(make_actor())
    drive = await _create_drive(http, "sgate", "ksgate")
    folder = (await _mkdir(
        http, drive["id"], drive["root_folder_id"], "sub", "ksgate-0"
    )).json()
    await _create_share(
        http, drive["id"], "ksgate-1",
        resource_type="folder", resource_id=folder["id"],
    )
    from agentdrive.db import conn as _conn

    async with _conn() as c:
        await c.execute(
            "INSERT INTO grants (id, drive_id, resource_type, resource_id, "
            "principal_type, principal_id, role, revision) "
            "VALUES ('grn_00000000000000f1', $1, 'drive', $1, 'agent', $2, "
            "'viewer', 'rev_00000000000000f1')",
            drive["id"], OTHER_AGENT,
        )

    override_actor(make_actor(subject=OTHER_AGENT))
    resp = await http.get(
        f"/v0/drives/{drive['id']}/shares",
        params={"resource_type": "folder", "resource_id": folder["id"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_expired_share_reports_expired_not_active(http, override_actor):
    """`state` must reflect expiry, as grant state already does.

    Redemption has always honoured `expires_at`. Only this field did not, so an
    expired link reported `active` and `state=active` listed it — a caller
    trusting either had to re-derive expiry itself.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "exp", "kexp-1")
    folder = (
        await _mkdir(http, drive["id"], drive["root_folder_id"], "expdir", "kexp-2")
    ).json()

    created = await _create_share(
        http, drive["id"], "kexp-3",
        resource_type="folder", resource_id=folder["id"],
    )
    assert created.status_code == 201, created.text
    async with conn() as c:
        await c.execute(
            "UPDATE shares SET expires_at='2020-01-01T00:00:00Z' WHERE id=$1",
            created.json()["id"],
        )

    read = await http.get(f"/v0/drives/{drive['id']}/shares/{created.json()['id']}")
    assert read.status_code == 200
    assert read.json()["state"] == "expired"


async def test_active_listing_excludes_an_expired_share(http, override_actor):
    """`state=active` must agree with the `state` it returns.

    Fixing the computed field alone left the listing contradicting itself: an
    expired link came back under `state=active` labelled `expired`. The
    authorization resolver has always excluded expired rows, so the listing
    was the outlier.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "expl", "kexl-1")
    folder = (
        await _mkdir(http, drive["id"], drive["root_folder_id"], "expldir", "kexl-2")
    ).json()

    live = await _create_share(
        http, drive["id"], "kexl-3", resource_type="folder", resource_id=folder["id"],
    )
    assert live.status_code == 201, live.text
    expired = await _create_share(
        http, drive["id"], "kexl-4",
        resource_type="folder", resource_id=folder["id"],
    )
    assert expired.status_code == 201, expired.text
    async with conn() as c:
        await c.execute(
            "UPDATE shares SET expires_at='2020-01-01T00:00:00Z' WHERE id=$1",
            expired.json()["id"],
        )

    active = await http.get(f"/v0/drives/{drive['id']}/shares?state=active")
    assert active.status_code == 200, active.text
    ids = [row["id"] for row in active.json()["items"]]
    assert live.json()["id"] in ids
    assert expired.json()["id"] not in ids

    every = await http.get(f"/v0/drives/{drive['id']}/shares?state=all")
    assert expired.json()["id"] in [row["id"] for row in every.json()["items"]]
