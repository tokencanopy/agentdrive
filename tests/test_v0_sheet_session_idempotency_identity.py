"""A sheet-session mutation's idempotency identity is its FULL logical route.

§7.2 matches a repeated `Idempotency-Key` on principal + method + path +
request hash, and replays the stored response without running the mutation.
The path is therefore the only thing in that identity naming the resource the
stored result belongs to — so an artifact-nested route whose canonical path
omits `/artifacts/{artifact_id}` lets one session id stand for two different
resources.

Three of these routes did exactly that. `require_local(... "artifact",
"artifact_id")` authorizes the artifact in the PATH, while the session/artifact
pairing check lives in `core.v0_sheet_sessions._row` — inside `execute`, which
a replay never runs. So a principal who kept `editor` on a sibling artifact
could present the old session id, key and body against that sibling and be
handed the first artifact's cached success: a replay authorized against a
resource the original request never named.

All identifiers, content and addresses here are synthetic.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentdrive.api.v0_deps import v0_actor
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext
from agentdrive.sheets import cache

from .test_v0_sheets_read import VALUES, XLSX, build_xlsx

pytestmark = pytest.mark.asyncio

WS = "tcws_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
OWNER = "tcagt_0000000000000001"
# The principal that starts with access to both artifacts and loses one.
TENANT = "tcagt_0000000000000077"

SCOPES = frozenset(
    {
        "drives:read", "drives:write", "usage:read",
        "content:read", "content:write", "sharing:read", "sharing:write",
    }
)


def actor(subject: str) -> V0ActorContext:
    return V0ActorContext(
        subject=subject,
        subject_type="agent",
        workspace_id=WS,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=SCOPES,
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id=SPONSOR,
        workspace_role=None,
    )


def key() -> str:
    return uuid.uuid4().hex


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    """The client plus a `be(subject)` switch, so one test can act as the
    drive owner to set grants up and as the tenant to exercise them."""
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:

        def be(subject: str) -> None:
            app_with_lifespan.dependency_overrides[v0_actor] = lambda: actor(subject)

        ac.be = be
        be(OWNER)
        yield ac
    app_with_lifespan.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean(app_with_lifespan):
    cache.clear()
    yield
    cache.clear()
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, storage_reservations, upload_sessions, "
            "sheet_sessions, workspace_storage, drives RESTART IDENTITY CASCADE"
        )


@pytest_asyncio.fixture
async def siblings(http):
    """Two sibling workbooks, A and B, and a tenant holding `editor` on both.

    Artifact-scoped grants rather than a drive-scoped one, because the whole
    scenario turns on losing access to exactly one of them.
    """
    drive = (
        await http.post(
            "/v0/drives", json={"name": "sheets"}, headers={"Idempotency-Key": key()}
        )
    ).json()

    async def artifact(name: str) -> dict:
        resp = await http.post(
            f"/v0/drives/{drive['id']}/artifacts",
            files={"content": (name, build_xlsx(VALUES), XLSX)},
            data={"parent_id": drive["root_folder_id"], "name": name},
            headers={"Idempotency-Key": key()},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    a = await artifact("a.xlsx")
    b = await artifact("b.xlsx")

    grants = {}
    for label, art in (("a", a), ("b", b)):
        resp = await http.post(
            f"/v0/drives/{drive['id']}/grants",
            json={
                "principal_type": "agent", "principal_id": TENANT,
                "resource_type": "artifact", "resource_id": art["id"],
                "role": "editor",
            },
            headers={"Idempotency-Key": key()},
        )
        assert resp.status_code == 201, resp.text
        grants[label] = resp.json()["id"]

    return {"drive": drive["id"], "a": a, "b": b, "grants": grants}


def _sessions(s, art: dict) -> str:
    return f"/v0/drives/{s['drive']}/artifacts/{art['id']}/sheet-sessions"


async def _open(http, s, art: dict) -> str:
    resp = await http.post(
        _sessions(s, art),
        json={},
        headers={"If-Match": f'"{art["revision"]}"', "Idempotency-Key": key()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _revoke(http, s, label: str) -> None:
    http.be(OWNER)
    resp = await http.delete(
        f"/v0/drives/{s['drive']}/grants/{s['grants'][label]}",
        headers={"If-Match": "*", "Idempotency-Key": key()},
    )
    assert resp.status_code == 200, resp.text
    http.be(TENANT)


WRITE_BODY = {"writes": [{"sheet": "Q3", "range": "B2", "values": [[4242]]}]}


async def _edit_count(http, s, art: dict, session_id: str) -> int:
    """How many edits the CURRENT actor can see on a session. Read as the
    drive owner so a revoked tenant's own 404 cannot be mistaken for zero."""
    http.be(OWNER)
    try:
        resp = await http.get(f"{_sessions(s, art)}/{session_id}/edits")
        assert resp.status_code == 200, resp.text
        return len(resp.json()["items"])
    finally:
        http.be(TENANT)


async def _record_paths() -> list[str]:
    async with conn() as c:
        rows = await c.fetch("SELECT path FROM idempotency_records ORDER BY path")
    return [r["path"] for r in rows]


# ── the cross-artifact replay, on each mutation that was exposed ───────────


async def test_a_write_cannot_be_replayed_through_a_sibling_artifact(http, siblings):
    """The scenario in full: mutate A under key K, lose A, present K on B."""
    s = siblings
    http.be(TENANT)
    session = await _open(http, s, s["a"])

    first = await http.post(
        f"{_sessions(s, s['a'])}/{session}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": "idem-cross-artifact-write"},
    )
    assert first.status_code == 200, first.text

    await _revoke(http, s, "a")

    # A is gone for this principal, so the original route is refused outright.
    gone = await http.post(
        f"{_sessions(s, s['a'])}/{session}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": "idem-cross-artifact-write"},
    )
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "NOT_AUTHORIZED"

    # The attack: B's route, A's session, the same key and the same body.
    replay = await http.post(
        f"{_sessions(s, s['b'])}/{session}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": "idem-cross-artifact-write"},
    )
    assert replay.status_code == 404, replay.text
    assert replay.json()["error"]["code"] == "SHEET_SESSION_NOT_FOUND"
    assert "revision" not in replay.json(), "A's cached body must not surface"

    # And nothing landed on B: it has no session at all, let alone an edit.
    http.be(OWNER)
    listed = await http.get(_sessions(s, s["b"]))
    assert listed.json()["items"] == []
    cells = await http.get(
        f"/v0/drives/{s['drive']}/artifacts/{s['b']['id']}/cells",
        params={"sheet": "Q3", "range": "B2"},
    )
    assert cells.json()["values"] == [[VALUES["Q3"][1][1]]], "B is untouched"


async def test_a_delete_cannot_be_replayed_through_a_sibling_artifact(http, siblings):
    s = siblings
    http.be(TENANT)
    session = await _open(http, s, s["a"])
    read = await http.get(f"{_sessions(s, s['a'])}/{session}")
    assert read.status_code == 200, read.text
    delete_route = f"{_sessions(s, s['a'])}/{session}"

    first = await http.delete(
        delete_route,
        headers={
            "If-Match": read.headers["etag"],
            "Idempotency-Key": "idem-cross-artifact-delete",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "discarded"
    async with conn() as c:
        stored_path = await c.fetchval(
            "SELECT path FROM idempotency_records WHERE idempotency_key = $1",
            "idem-cross-artifact-delete",
        )
    assert stored_path == delete_route

    # A retry on the exact logical route still replays the stored success;
    # it must not fall through to the now-terminal session state.
    same_route = await http.delete(
        delete_route,
        headers={
            "If-Match": read.headers["etag"],
            "Idempotency-Key": "idem-cross-artifact-delete",
        },
    )
    assert same_route.status_code == 200, same_route.text
    assert same_route.json() == first.json()

    await _revoke(http, s, "a")

    replay = await http.delete(
        f"{_sessions(s, s['b'])}/{session}",
        headers={
            "If-Match": read.headers["etag"],
            "Idempotency-Key": "idem-cross-artifact-delete",
        },
    )
    assert replay.status_code == 404, replay.text
    assert replay.json()["error"]["code"] == "SHEET_SESSION_NOT_FOUND"
    assert "state" not in replay.json(), "A's cached body must not surface"


# ── global key reuse fails closed without cross-artifact replay ────────────


async def test_one_key_and_body_on_two_artifacts_fail_closed(http, siblings):
    """The global principal/key collision returns 409, never A's response."""
    s = siblings
    http.be(TENANT)
    session_a = await _open(http, s, s["a"])
    session_b = await _open(http, s, s["b"])

    shared = "idem-same-key-two-artifacts"
    first = await http.post(
        f"{_sessions(s, s['a'])}/{session_a}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": shared},
    )
    second = await http.post(
        f"{_sessions(s, s['b'])}/{session_b}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": shared},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # A conflict, not a replay: B saw no edit, and A's result was not handed
    # over. The 409 is §7.2's answer because the ledger is keyed on
    # (principal, key) alone — the key really was reused for a different
    # request. What matters is that B never inherits A's outcome.
    assert await _edit_count(http, s, s["a"], session_a) == 1
    assert await _edit_count(http, s, s["b"], session_b) == 0

    # With distinct keys, both land, and each record names its own artifact.
    again = await http.post(
        f"{_sessions(s, s['b'])}/{session_b}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": "idem-same-key-two-artifacts-b"},
    )
    assert again.status_code == 200, again.text
    assert await _edit_count(http, s, s["b"], session_b) == 1

    paths = await _record_paths()
    assert (
        f"/v0/drives/{s['drive']}/artifacts/{s['a']['id']}"
        f"/sheet-sessions/{session_a}/cells"
    ) in paths
    assert (
        f"/v0/drives/{s['drive']}/artifacts/{s['b']['id']}"
        f"/sheet-sessions/{session_b}/cells"
    ) in paths


async def test_completion_records_the_artifact_in_its_canonical_path(http, siblings):
    """Completion's own preflight (`prepare_completion`, which runs before the
    claim) already refuses a mispaired session, so the cross-artifact replay
    was never reachable there. Its stored identity was still artifact-blind,
    which is a latent version of the same defect: this pins the shape rather
    than the exploit."""
    s = siblings
    http.be(TENANT)
    session = await _open(http, s, s["a"])
    await http.post(
        f"{_sessions(s, s['a'])}/{session}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": key()},
    )
    done = await http.post(
        f"{_sessions(s, s['a'])}/{session}/complete",
        json={},
        headers={"Idempotency-Key": "idem-complete-identity"},
    )
    assert done.status_code == 201, done.text

    async with conn() as c:
        path = await c.fetchval(
            "SELECT path FROM idempotency_records WHERE idempotency_key = $1",
            "idem-complete-identity",
        )
    assert path == (
        f"/v0/drives/{s['drive']}/artifacts/{s['a']['id']}"
        f"/sheet-sessions/{session}/complete"
    )

    # And the mispaired call is the ordinary 404, before the ledger is touched.
    mispaired = await http.post(
        f"{_sessions(s, s['b'])}/{session}/complete",
        json={},
        headers={"Idempotency-Key": "idem-complete-identity"},
    )
    assert mispaired.status_code == 404
    assert mispaired.json()["error"]["code"] == "SHEET_SESSION_NOT_FOUND"


async def test_a_mispaired_route_never_claims_the_key(http, siblings):
    """§7.2: only executed mutations create records. The pairing check runs
    before the claim, so a wrong-artifact call leaves the key usable — and
    answers the same 404 whether or not that key has been seen before, which
    is the anti-enumeration rule the rest of the surface follows."""
    s = siblings
    http.be(TENANT)
    session = await _open(http, s, s["a"])

    fresh = "idem-mispaired-fresh"
    mispaired = await http.post(
        f"{_sessions(s, s['b'])}/{session}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": fresh},
    )
    assert mispaired.status_code == 404
    assert mispaired.json()["error"]["code"] == "SHEET_SESSION_NOT_FOUND"

    async with conn() as c:
        assert (
            await c.fetchval(
                "SELECT count(*) FROM idempotency_records WHERE idempotency_key = $1",
                fresh,
            )
            == 0
        ), "a refused mutation must leave its key usable"

    # Still usable, on the route that owns the session — and the record it
    # then writes names A, not the artifact-blind shape.
    ok = await http.post(
        f"{_sessions(s, s['a'])}/{session}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": fresh},
    )
    assert ok.status_code == 200, ok.text
    async with conn() as c:
        path = await c.fetchval(
            "SELECT path FROM idempotency_records WHERE idempotency_key = $1", fresh
        )
    assert path == (
        f"/v0/drives/{s['drive']}/artifacts/{s['a']['id']}"
        f"/sheet-sessions/{session}/cells"
    )


# ── the deployment boundary ───────────────────────────────────────────────


async def test_a_legacy_record_refuses_the_retry_instead_of_re_executing(
    http, siblings
):
    """Rollout: records written by the OLD revision live up to 24h.

    The ledger is keyed on `(principal_id, idempotency_key)` alone — `path`
    is only part of §7.2's same-request comparison — so a retry that spans
    the deploy does NOT miss its record and re-execute. It finds it, sees a
    different path, and is refused with `409 IDEMPOTENCY_CONFLICT`. That is
    fail-closed: no duplicate write, and no replay of a result recorded under
    an artifact-blind identity. This simulates the old revision by rewriting
    a fresh record's stored path to the legacy shape.
    """
    s = siblings
    http.be(TENANT)
    session = await _open(http, s, s["a"])

    stale = "idem-spanning-the-deploy"
    first = await http.post(
        f"{_sessions(s, s['a'])}/{session}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": stale},
    )
    assert first.status_code == 200, first.text
    assert await _edit_count(http, s, s["a"], session) == 1

    legacy = f"/v0/drives/{s['drive']}/sheet-sessions/{session}/cells"
    async with conn() as c:
        await c.execute(
            "UPDATE idempotency_records SET path = $2 WHERE idempotency_key = $1",
            stale, legacy,
        )

    retry = await http.post(
        f"{_sessions(s, s['a'])}/{session}/cells",
        json=WRITE_BODY,
        headers={"Idempotency-Key": stale},
    )
    assert retry.status_code == 409, retry.text
    assert retry.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert await _edit_count(http, s, s["a"], session) == 1, (
        "the retry must not execute a second time"
    )
