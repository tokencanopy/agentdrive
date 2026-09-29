"""Task 10 Step 1 — cross-workspace/drive isolation at the HTTP boundary (§11.4).

The unit tests prove the authz *core*; these prove the *boundary* renders it.
Three isolation invariants a client must be able to rely on:

  1. **Cross-workspace isolation** — a token for workspace A cannot read or
     see drive B: direct reads, usage, and listing all render absence (404 /
     filtered-out), never a cross-workspace 200.
  2. **Public grants are viewer-only** — `public` confers read capability to
     anyone, but NEVER a write: a `public:viewer` grant lets a stranger read,
     and a `public:editor`/`public:manager` grant cannot even be created
     (schema + core both reject it).
  3. **Revocation is rechecked on idempotent replay** — a principal whose
     grant is revoked after a successful mutation must get the normal
     authorization failure when replaying the same Idempotency-Key, not the
     stored result (§6.2). The replay path skips `execute`, so this only
     holds because authorization is enforced as a request-boundary dependency
     (runs on every request, replay included).
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
        workspace_role=None if is_agent else "admin",
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
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_grant(http, drive_id: str, key: str, **body) -> object:
    resp = await http.post(
        f"/v0/drives/{drive_id}/grants",
        json=body,
        headers={"Idempotency-Key": key},
    )
    return resp


# ---------------------------------------------------------------------------
# 1. Cross-workspace isolation
# ---------------------------------------------------------------------------


async def test_cross_workspace_token_cannot_read_or_see_drive(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "iso", "kiso-1")

    # A workspace-B token cannot read the drive, its usage, or see it in a list.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))

    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "DRIVE_NOT_FOUND"

    usage = await http.get(f"/v0/drives/{drive['id']}/usage")
    assert usage.status_code == 404
    assert usage.json()["error"]["code"] == "DRIVE_NOT_FOUND"

    listed = await http.get("/v0/drives")
    assert listed.status_code == 200
    assert all(item["id"] != drive["id"] for item in listed.json()["items"])


async def test_cross_workspace_token_cannot_list_or_read_folders_and_artifacts(
    http, override_actor
):
    override_actor(make_actor())
    drive = await _create_drive(http, "iso2", "kiso2-1")

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))

    folders = await http.get(f"/v0/drives/{drive['id']}/folders")
    assert folders.status_code == 404
    assert folders.json()["error"]["code"] == "DRIVE_NOT_FOUND"

    artifacts = await http.get(f"/v0/drives/{drive['id']}/artifacts")
    assert artifacts.status_code == 404
    assert artifacts.json()["error"]["code"] == "DRIVE_NOT_FOUND"

    changes = await http.get(f"/v0/drives/{drive['id']}/changes", params={"start": "now"})
    assert changes.status_code == 404
    assert changes.json()["error"]["code"] == "DRIVE_NOT_FOUND"


# ---------------------------------------------------------------------------
# 2. Public grants are viewer-only
# ---------------------------------------------------------------------------


async def test_public_grant_is_read_only_at_the_boundary(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "pub", "kpub-1")
    root = drive["root_folder_id"]

    # Grant public viewer; a same-workspace principal with NO other grant can
    # now read via the public grant — but never write.
    granted = await _create_grant(
        http, drive["id"], "kpub-2",
        principal_type="public",
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    assert granted.status_code == 201, granted.text

    public_member = make_actor(subject=OTHER_AGENT, workspace=WS_A)
    override_actor(public_member)

    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 200, read.text

    folders = await http.get(f"/v0/drives/{drive['id']}/folders")
    assert folders.status_code == 200, folders.text

    # ...but a public viewer cannot WRITE: create-artifact under the root
    # requires editor on the parent, which public does not confer.
    denied = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        files={
            "parent_id": (None, root),
            "name": (None, "nope.txt"),
            "content": ("nope.txt", b"hi", "application/octet-stream"),
        },
        headers={"Idempotency-Key": "kpub-3"},
    )
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_public_grant_does_not_bridge_workspaces(http, override_actor):
    """§6.1: a token addressing a resource outside its workspace is an
    object-level miss (404) even with a `public` grant — the anti-enumeration
    rule wins. Public access over the authenticated API is same-workspace;
    cross-workspace public access is the possession-based /s/{share_key}
    surface, not a token."""
    override_actor(make_actor())
    drive = await _create_drive(http, "pubw", "kpubw-1")

    granted = await _create_grant(
        http, drive["id"], "kpubw-2",
        principal_type="public",
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    assert granted.status_code == 201, granted.text

    # A cross-workspace token cannot read via the public grant.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    read = await http.get(f"/v0/drives/{drive['id']}")
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "DRIVE_NOT_FOUND"


async def test_public_editor_grant_is_rejected(http, override_actor):
    """`public` above viewer is refused by the schema (§6.8), surfaced as a
    400 at the boundary — an anonymous write can never be represented."""
    override_actor(make_actor())
    drive = await _create_drive(http, "pubw", "kpubw-1")

    denied = await _create_grant(
        http, drive["id"], "kpubw-2",
        principal_type="public",
        resource_type="drive", resource_id=drive["id"], role="editor",
    )
    assert denied.status_code == 400
    assert denied.json()["error"]["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# 3. Grant revocation is rechecked on idempotent replay
# ---------------------------------------------------------------------------


async def test_revoked_editor_replaying_a_write_key_is_denied(http, override_actor):
    """§6.2: replay re-checks authorization. Grant an editor, they create a
    folder under a key; revoke them; replaying the same key must 404, not
    return the stored 201."""
    override_actor(make_actor())
    drive = await _create_drive(http, "rev", "krev-1")
    root = drive["root_folder_id"]

    granted = await _create_grant(
        http, drive["id"], "krev-2",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="drive", resource_id=drive["id"], role="editor",
    )
    assert granted.status_code == 201, granted.text

    # The editor creates a folder under a fixed key.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    created = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        json={"parent_id": root, "name": "their-folder"},
        headers={"Idempotency-Key": "krev-3"},
    )
    assert created.status_code == 201, created.text

    # Revoke the editor's grant (as the manager).
    override_actor(make_actor())
    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{granted.json()['id']}",
        headers={
            "Idempotency-Key": "krev-4",
            "If-Match": f'"{granted.json()["revision"]}"',
        },
    )
    assert revoked.status_code == 200, revoked.text

    # Replaying the SAME key after revocation: the stored 201 must NOT be
    # returned — the principal no longer holds editor, so this reads as
    # absence (404), never the stale success.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    replay = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        json={"parent_id": root, "name": "their-folder"},
        headers={"Idempotency-Key": "krev-3"},
    )
    assert replay.status_code == 404
    assert replay.json()["error"]["code"] == "NOT_AUTHORIZED"