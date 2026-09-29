"""`POST /v0/.../viewer-sessions` — the private viewer's mint (§7.1 both
halves, 2026-08-09 private-viewer design).

The properties that matter:

  * **The full intersection gates the mint.** `content:read` on the token AND
    a live local viewer grant on the artifact; a missing half is a 403 or the
    uniform 404 respectively, and cross-drive/cross-workspace attempts learn
    nothing.
  * **The credential is hashed at rest and shown once.** The row stores a
    SHA-256; no column ever holds the plaintext; the idempotency ledger
    stores the response WITHOUT it, so a replay returns `credential: null`.
  * **The session pins one immutable version at mint time.** The head moving
    afterwards changes nothing; an explicit version must belong to the
    artifact.

Fixtures follow `tests/test_public_share.py` (local `http` / `override_actor`
/ `_clean_tables`).
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.core import v0_viewer_sessions as core_sessions
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
AGENT_B = "tcagt_0000000000000002"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"
WS_B = "tcws_0000000000000002"


def make_actor(
    subject: str = AGENT,
    workspace: str = WS_A,
    scopes: frozenset[str] | None = None,
) -> V0ActorContext:
    return V0ActorContext(
        subject=subject,
        subject_type="agent",
        workspace_id=workspace,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=scopes
        if scopes is not None
        else frozenset({
            "drives:read", "drives:write",
            "content:read", "content:write",
            "sharing:read", "sharing:write",
        }),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id=SPONSOR,
        workspace_role=None,
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
        await c.execute(
            "TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE"
        )


@pytest.fixture(autouse=True)
def _bound_viewer(monkeypatch):
    """Bind a viewer host for this module. The mint fails closed with 503
    VIEWER_DISABLED while `viewer_base_url` is empty (a credential minted
    for an unbound deployment is redeemable nowhere), so every test of the
    mint's ordinary behavior needs a bound host. The gate's own tests undo
    this deliberately."""
    monkeypatch.setattr(settings, "viewer_base_url", "https://viewer.example.test")


async def _create_drive(http, name: str, key: str) -> dict:
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _multipart(parent_id: str, name: str, content_type: str, body: bytes) -> bytes:
    return (
        b'--b\r\nContent-Disposition: form-data; name="parent_id"\r\n\r\n'
        + parent_id.encode()
        + b'\r\n--b\r\nContent-Disposition: form-data; name="name"\r\n\r\n'
        + name.encode()
        + b'\r\n--b\r\nContent-Disposition: form-data; name="content"; filename="'
        + name.encode()
        + b'"\r\nContent-Type: '
        + content_type.encode()
        + b"\r\n\r\n"
        + body
        + b"\r\n--b--\r\n"
    )


async def _create_artifact(
    http, drive: dict, *, name: str, body: bytes, key: str,
    content_type: str = "text/markdown",
) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart(drive["root_folder_id"], name, content_type, body),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": key,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _mint(http, drive_id: str, artifact_id: str, key: str, body: dict | None = None):
    return await http.post(
        f"/v0/drives/{drive_id}/artifacts/{artifact_id}/viewer-sessions",
        json=body or {},
        headers={"Idempotency-Key": key},
    )


async def _setup(http, override_actor) -> tuple[dict, dict]:
    override_actor(make_actor())
    drive = await _create_drive(http, "d", "k-drive")
    artifact = await _create_artifact(
        http, drive, name="doc.md", body=b"# hello\n", key="k-art"
    )
    return drive, artifact


# ── happy path ───────────────────────────────────────────────────────────────


async def test_mint_returns_a_credential_once(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"].startswith("vwr_")
    assert body["drive_id"] == drive["id"]
    assert body["artifact_id"] == artifact["id"]
    assert body["version_id"] == artifact["head_version_id"]
    assert isinstance(body["credential"], str) and len(body["credential"]) >= 32
    assert 60 <= body["expires_in"] <= 300
    # A credential mint is never cacheable.
    assert resp.headers["cache-control"] == "no-store"


async def test_only_the_hash_touches_storage(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    credential = resp.json()["credential"]
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT * FROM viewer_sessions WHERE id=$1", resp.json()["id"]
        )
    assert row["credential_hash"] == hashlib.sha256(credential.encode()).hexdigest()
    # No column anywhere in the row carries the plaintext.
    assert credential not in [v for v in row.values() if isinstance(v, str)]


async def test_idempotent_replay_never_returns_the_credential(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    first = await _mint(http, drive["id"], artifact["id"], "k-mint")
    replay = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    # The ledger stored the response WITHOUT the plaintext (the share-secret
    # rule): a DB leak of stored responses yields no live credentials.
    assert replay.json()["credential"] is None


# ── version pinning ──────────────────────────────────────────────────────────


async def test_mint_pins_the_head_at_mint_time(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    minted = (await _mint(http, drive["id"], artifact["id"], "k-mint")).json()
    pinned = minted["version_id"]

    # Move the head. The session must not follow.
    append = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/versions",
        content=(
            b'--b\r\nContent-Disposition: form-data; name="content"; '
            b'filename="doc.md"\r\nContent-Type: text/markdown\r\n\r\n'
            b"# changed\n\r\n--b--\r\n"
        ),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": "k-append",
            "If-Match": f'"{artifact["revision"]}"',
        },
    )
    assert append.status_code == 201, append.text
    assert append.json()["id"] != pinned

    async with conn() as c:
        target = await core_sessions.resolve_credential(c, minted["credential"])
    assert target is not None
    assert target["binding"]["version_id"] == pinned
    assert target["etag"] == pinned


async def test_explicit_version_must_belong_to_the_artifact(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    other = await _create_artifact(
        http, drive, name="other.md", body=b"# other\n", key="k-art2"
    )
    resp = await _mint(
        http, drive["id"], artifact["id"], "k-mint",
        {"version_id": other["head_version_id"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"


async def test_explicit_version_pins_that_version(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    resp = await _mint(
        http, drive["id"], artifact["id"], "k-mint",
        {"version_id": artifact["head_version_id"]},
    )
    assert resp.status_code == 200
    assert resp.json()["version_id"] == artifact["head_version_id"]


# ── the §7.1 intersection ────────────────────────────────────────────────────


async def test_scope_is_required(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    override_actor(make_actor(scopes=frozenset({"drives:read", "content:write"})))
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_scope_cannot_substitute_for_a_grant(http, override_actor):
    """A token with content:read but no local grant gets the uniform 404 —
    token scope cannot expand a local grant (contract §13.2)."""
    drive, artifact = await _setup(http, override_actor)
    override_actor(make_actor(subject=AGENT_B))
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_a_viewer_grant_is_sufficient(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    grant = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent", "principal_id": AGENT_B,
            "resource_type": "artifact", "resource_id": artifact["id"],
            "role": "viewer",
        },
        headers={"Idempotency-Key": "k-grant"},
    )
    assert grant.status_code == 201, grant.text
    override_actor(make_actor(subject=AGENT_B))
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 200, resp.text


async def test_a_public_grant_alone_cannot_mint(http, override_actor):
    """Mint and resolve must agree about `public` grants.

    Resolution deliberately re-checks the minting principal's OWN standing
    (`include_public=False`), so if minting admitted a public grant, a
    caller whose only access is "the artifact is published" would get a
    200 carrying a credential that can never resolve — a success handing
    back something broken. Both halves are strict; the published artifact
    remains readable through the permalink publishing created.
    """
    drive, artifact = await _setup(http, override_actor)
    published = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "public", "principal_id": None,
            "resource_type": "artifact", "resource_id": artifact["id"],
            "role": "viewer",
        },
        headers={"Idempotency-Key": "k-public"},
    )
    assert published.status_code == 201, published.text

    # A workspace caller with content:read and NO grant of their own.
    override_actor(make_actor(subject=AGENT_B))
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_cross_workspace_is_absent(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    override_actor(make_actor(workspace=WS_B))
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DRIVE_NOT_FOUND"


async def test_unknown_artifact_is_absent(http, override_actor):
    drive, _ = await _setup(http, override_actor)
    resp = await _mint(http, drive["id"], "art_00000000000000ff", "k-mint")
    assert resp.status_code == 404


async def test_deleted_artifact_is_absent(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    deleted = await http.delete(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}",
        headers={
            "Idempotency-Key": "k-del",
            "If-Match": f'"{artifact["revision"]}"',
        },
    )
    assert deleted.status_code == 200, deleted.text
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 404


# ── the unbound-deployment gate ──────────────────────────────────────────────
#
# While `viewer_base_url` is empty there is no host a viewer credential can be
# redeemed on (`HostSurfaceMiddleware` binds `/view/` only when the origin is
# set), so a 200 mint would hand back a real 180-second credential that works
# nowhere. Same fail-closed posture as the transfer surface's
# 503 TRANSFER_DISABLED: operator enablement, no fallback, no Retry-After.


async def test_mint_fails_closed_while_the_viewer_host_is_unbound(
    http, override_actor, monkeypatch
):
    drive, artifact = await _setup(http, override_actor)
    monkeypatch.setattr(settings, "viewer_base_url", "")
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "VIEWER_DISABLED"
    # Operator enablement has no honest client retry time (the
    # TRANSFER_DISABLED rule).
    assert "Retry-After" not in resp.headers
    async with conn() as c:
        count = await c.fetchval("SELECT count(*) FROM viewer_sessions")
    assert count == 0, "the refused mint must not have created a session row"


async def test_unbound_refusal_leaves_the_idempotency_key_reusable(
    http, override_actor, monkeypatch
):
    """The gate answers before the idempotency claim: a mint refused while
    the deployment is unbound must not poison its key, so the same request
    retried after the operator binds the host executes for real."""
    drive, artifact = await _setup(http, override_actor)
    monkeypatch.setattr(settings, "viewer_base_url", "")
    refused = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert refused.status_code == 503

    monkeypatch.setattr(settings, "viewer_base_url", "https://viewer.example.test")
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 200, resp.text
    # A fresh execution, not a ledger replay: replays strip the credential.
    assert isinstance(resp.json()["credential"], str)


async def test_unbound_gate_never_touches_the_idempotency_ledger(
    http, override_actor, monkeypatch
):
    """Pin the gate's PLACEMENT, not just its outcome: the 503 must be
    decided before the idempotency claim. `idempotency.abandon` deletes an
    in-flight row when an error escapes the claimed section, so the
    reusable-key test above passes even with the gate moved inside
    `execute()` — only proving the claim was never invoked pins the
    before-the-ledger ordering."""
    from agentdrive.core import idempotency as idempotency_module

    drive, artifact = await _setup(http, override_actor)
    monkeypatch.setattr(settings, "viewer_base_url", "")
    calls: list[str] = []
    real_claim = idempotency_module.claim

    async def recording_claim(*args, **kwargs):
        calls.append("claim")
        return await real_claim(*args, **kwargs)

    monkeypatch.setattr(idempotency_module, "claim", recording_claim)
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 503, resp.text
    assert calls == [], "the unbound refusal must precede the idempotency claim"


async def test_completed_replay_is_refused_while_unbound(
    http, override_actor, monkeypatch
):
    """A key holding a stored 200 does not resurrect an unbound viewer:
    the gate answers before the ledger is consulted, so unbinding refuses
    replays of earlier successful mints too — the deployment-wide OFF
    switch has no memory holes."""
    drive, artifact = await _setup(http, override_actor)
    first = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert first.status_code == 200, first.text

    monkeypatch.setattr(settings, "viewer_base_url", "")
    replay = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert replay.status_code == 503, replay.text
    assert replay.json()["error"]["code"] == "VIEWER_DISABLED"


async def test_scope_refusal_still_precedes_the_unbound_gate(
    http, override_actor, monkeypatch
):
    """Authorization stays first: the gate lives in the handler body, after
    the route's scope + local-grant dependencies, so an unauthorized caller
    learns nothing about deployment state from a 503."""
    drive, artifact = await _setup(http, override_actor)
    monkeypatch.setattr(settings, "viewer_base_url", "")
    override_actor(make_actor(scopes=frozenset({"drives:read"})))
    resp = await _mint(http, drive["id"], artifact["id"], "k-mint")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# ── mutation plumbing ────────────────────────────────────────────────────────


async def test_idempotency_key_is_required(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}/viewer-sessions",
        json={},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_unknown_body_fields_are_rejected(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    resp = await _mint(
        http, drive["id"], artifact["id"], "k-mint", {"artifact_id": "art_x"}
    )
    assert resp.status_code == 422


async def test_expired_rows_are_swept(http, override_actor):
    drive, artifact = await _setup(http, override_actor)
    minted = (await _mint(http, drive["id"], artifact["id"], "k-mint")).json()
    async with conn() as c:
        await c.execute(
            "UPDATE viewer_sessions SET expires_at = now() - interval '2 hours' "
            "WHERE id=$1",
            minted["id"],
        )
        removed = await core_sessions.sweep_expired(c)
        assert removed == 1
        assert await c.fetchval(
            "SELECT count(*) FROM viewer_sessions WHERE id=$1", minted["id"]
        ) == 0
