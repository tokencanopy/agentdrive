"""The journey-matrix rows not covered by the per-operation suites (§12.3).

These are the sequences and transitions, not the endpoints: what happens when
an artifact is deleted under a live session, when an agent restarts and has to
find its own work, when a request is rejected before it executes, and when
authorization changes between a call and its retry.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.db import conn
from agentdrive.sheets import cache

from .test_v0_sheets_read import VALUES, XLSX, build_xlsx, make_actor

pytestmark = pytest.mark.asyncio


def _base(wb) -> str:
    """The session collection for this artifact.

    A helper because the nested path repeats in every call and reads worse
    inline than the thing being tested does.
    """
    return f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/sheet-sessions"


def key() -> str:
    return uuid.uuid4().hex


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    app.dependency_overrides[v0_actor] = lambda: make_actor()
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lifespan), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


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
async def wb(http):
    drive = (
        await http.post(
            "/v0/drives", json={"name": "j"}, headers={"Idempotency-Key": key()}
        )
    ).json()
    art = (
        await http.post(
            f"/v0/drives/{drive['id']}/artifacts",
            files={"content": ("q.xlsx", build_xlsx(VALUES), XLSX)},
            data={"parent_id": drive["root_folder_id"], "name": "q.xlsx"},
            headers={"Idempotency-Key": key()},
        )
    ).json()
    return {
        "drive": drive["id"],
        "root": drive["root_folder_id"],
        "artifact": art["id"],
        "rev": art["revision"],
    }


async def _open(http, wb):
    return await http.post(
        f"{_base(wb)}",
        json={},
        headers={"If-Match": f'"{wb["rev"]}"', "Idempotency-Key": key()},
    )


async def _write(http, wb, sid, writes, k=None):
    return await http.post(
        f"{_base(wb)}/{sid}/cells",
        json={"writes": writes},
        headers={"Idempotency-Key": k or key()},
    )


# ── J5: writing past the used range, over the wire ─────────────────────────


async def test_writing_beyond_the_used_range_appends_rows(http, wb):
    """How an agent appends: there is no insert operation and none is needed."""
    sid = (await _open(http, wb)).json()["session_id"]
    r = await _write(
        http, wb, sid, [{"sheet": "Q3", "range": "A9:C9", "values": [["LATAM", 410, 500]]}]
    )
    assert r.status_code == 200, r.text
    await http.post(
        f"{_base(wb)}/{sid}/complete",
        json={},
        headers={"Idempotency-Key": key()},
    )
    cells = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/cells",
        params={"sheet": "Q3", "range": "A9:C9"},
    )
    assert cells.json()["values"] == [["LATAM", 410, 500]]


# ── R8: awkward but legal sheet names, over the wire ───────────────────────


async def test_a_sheet_name_with_spaces_and_non_ascii_round_trips(http):
    """Excel forbids \\ / ? * : [ ] in titles, so the encoding hazard is
    spaces and non-ASCII — which is why `sheet` is a query/body field and
    never a path segment."""
    name = "予算 — Q3 plan"
    drive = (
        await http.post(
            "/v0/drives", json={"name": "u"}, headers={"Idempotency-Key": key()}
        )
    ).json()
    art = (
        await http.post(
            f"/v0/drives/{drive['id']}/artifacts",
            files={"content": ("u.xlsx", build_xlsx({name: [["名前", 42]]}), XLSX)},
            data={"parent_id": drive["root_folder_id"], "name": "u.xlsx"},
            headers={"Idempotency-Key": key()},
        )
    ).json()
    wb = {"drive": drive["id"], "artifact": art["id"], "rev": art["revision"]}

    read = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/cells",
        params={"sheet": name, "range": "A1:B1"},
    )
    assert read.status_code == 200, read.text
    assert read.json()["sheet"] == name

    sid = (await _open(http, wb)).json()["session_id"]
    written = await _write(
        http, wb, sid, [{"sheet": name, "range": "B1", "values": [[99]]}]
    )
    assert written.status_code == 200, written.text
    assert written.json()["sheets_touched"][0]["name"] == name


# ── C4: the artifact disappears under a live session ───────────────────────


async def test_a_deleted_artifact_fails_completion_cleanly(http, wb):
    """No version, no dangling reservation, and a refusal the caller can act
    on rather than a 500."""
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[1]]}])

    read = await http.get(f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}")
    gone = await http.delete(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}",
        headers={"If-Match": read.headers["etag"], "Idempotency-Key": key()},
    )
    assert gone.status_code == 200, gone.text

    done = await http.post(
        f"{_base(wb)}/{sid}/complete",
        json={},
        headers={"Idempotency-Key": key()},
    )
    assert done.status_code in (404, 409, 412), done.text
    assert done.status_code != 500


# ── I6: a rejected request leaves its key usable ───────────────────────────


async def test_a_key_rejected_before_execution_stays_usable(http, wb):
    """Contract §6.2: requests refused before execution record nothing, so the
    same key works for the corrected retry. Otherwise a single missing header
    would burn the key and force a duplicate."""
    k = key()
    refused = await http.post(
        f"{_base(wb)}",
        json={},
        headers={"Idempotency-Key": k},  # no If-Match
    )
    assert refused.status_code == 428

    corrected = await http.post(
        f"{_base(wb)}",
        json={},
        headers={"If-Match": f'"{wb["rev"]}"', "Idempotency-Key": k},
    )
    assert corrected.status_code == 201, corrected.text


async def test_a_stale_precondition_also_leaves_the_key_usable(http, wb):
    k = key()
    stale = await http.post(
        f"{_base(wb)}",
        json={},
        headers={"If-Match": '"rev_0000000000000000"', "Idempotency-Key": k},
    )
    assert stale.status_code == 412
    good = await http.post(
        f"{_base(wb)}",
        json={},
        headers={"If-Match": f'"{wb["rev"]}"', "Idempotency-Key": k},
    )
    assert good.status_code == 201, good.text


# ── L3: crash and resume ───────────────────────────────────────────────────


async def test_a_restarted_agent_finds_and_finishes_its_own_session(http, wb):
    """Durable session state is what makes this possible at all — and why the
    listing exists rather than the agent being expected to remember an id."""
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[11]]}])

    # ...the agent restarts and knows only the artifact.
    found = await http.get(
        f"{_base(wb)}",
        params={"state": "open"},
    )
    assert [s["session_id"] for s in found.json()["items"]] == [sid]
    resumed = found.json()["items"][0]
    assert resumed["edit_count"] == 1, "its work is still there"

    await _write(http, wb, sid, [{"sheet": "Q3", "range": "C2", "values": [[22]]}])
    done = await http.post(
        f"{_base(wb)}/{sid}/complete",
        json={},
        headers={"Idempotency-Key": key()},
    )
    assert done.status_code == 201, done.text

    cells = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/cells",
        params={"sheet": "Q3", "range": "B2:C2"},
    )
    assert cells.json()["values"] == [[11, 22]], "both halves of the work landed"


# ── A2 / A3 / A4: authorization ────────────────────────────────────────────


async def test_content_read_alone_reads_but_cannot_open_a_session(http, wb):
    app.dependency_overrides[v0_actor] = lambda: make_actor(
        scopes={"drives:read", "content:read"}
    )
    read = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/cells",
        params={"sheet": "Q3", "range": "A1"},
    )
    assert read.status_code == 200, read.text

    opened = await http.post(
        f"{_base(wb)}",
        json={},
        headers={"If-Match": f'"{wb["rev"]}"', "Idempotency-Key": key()},
    )
    assert opened.status_code == 403
    assert opened.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_scope_lost_mid_session_blocks_further_writes(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    app.dependency_overrides[v0_actor] = lambda: make_actor(
        scopes={"drives:read", "content:read"}
    )
    blocked = await _write(http, wb, sid, [{"sheet": "Q3", "range": "A1", "values": [["x"]]}])
    assert blocked.status_code == 403


async def test_a_replay_after_losing_scope_is_denied_not_replayed(http, wb):
    """Contract §6.2: a replay re-checks authorization FIRST, so a principal
    whose access was revoked gets the normal refusal rather than the stored
    success. Otherwise a key would outlive the permission that earned it."""
    sid = (await _open(http, wb)).json()["session_id"]
    k = key()
    first = await _write(
        http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[5]]}], k
    )
    assert first.status_code == 200

    app.dependency_overrides[v0_actor] = lambda: make_actor(
        scopes={"drives:read", "content:read"}
    )
    replay = await _write(
        http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[5]]}], k
    )
    assert replay.status_code == 403
    assert replay.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_a_sessions_listing_never_leaks_a_sibling_artifact(http, wb):
    """The listing is artifact-scoped, so a sibling's sessions are absent —
    not filtered out afterwards, but never selected."""
    sid = (await _open(http, wb)).json()["session_id"]

    sibling = await http.post(
        f"/v0/drives/{wb['drive']}/artifacts",
        files={"content": ("other.xlsx", build_xlsx(VALUES), XLSX)},
        data={"parent_id": wb["root"], "name": "other.xlsx"},
        headers={"Idempotency-Key": key()},
    )
    listed = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{sibling.json()['id']}/sheet-sessions"
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []
    assert sid not in listed.text


async def test_a_read_only_principal_can_watch_a_session_it_cannot_touch(http, wb):
    """The awareness case, and the reason the four session GETs dropped to
    `content:read`.

    Somebody who may read the artifact may see what an agent is doing to it
    (O5) — and needs no write authority to do so. Before this split, a
    read-only viewer either saw nothing or had to be handed a token that
    could also write, which is precisely what the BFF rule's
    "minimally scoped" clause exists to prevent.
    """
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[42]]}])

    app.dependency_overrides[v0_actor] = lambda: make_actor(
        scopes={"drives:read", "content:read"}
    )

    listed = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}"
        f"/sheet-sessions", params={}
    )
    assert listed.status_code == 200, listed.text
    assert [s["session_id"] for s in listed.json()["items"]] == [sid]

    read = await http.get(f"{_base(wb)}/{sid}")
    assert read.status_code == 200

    edits = await http.get(f"{_base(wb)}/{sid}/edits")
    assert edits.status_code == 200
    assert edits.json()["items"][0]["values"] == [[42]]

    working = await http.get(
        f"{_base(wb)}/{sid}/cells",
        params={"sheet": "Q3", "range": "B2"},
    )
    assert working.status_code == 200
    assert working.json()["values"] == [[42]]

    # ...and still cannot change anything.
    blocked = await _write(
        http, wb, sid, [{"sheet": "Q3", "range": "B3", "values": [[1]]}]
    )
    assert blocked.status_code == 403
    finished = await http.post(
        f"{_base(wb)}/{sid}/complete",
        json={},
        headers={"Idempotency-Key": key()},
    )
    assert finished.status_code == 403


async def test_a_completed_version_records_the_session_that_made_it(http, wb):
    """Migration 0053. Without this the six cell changes that produced a
    version were unreachable the moment the session was swept, and the
    `message` the API documents was hashed for idempotency and thrown away.
    """
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[7]]}])

    done = await http.post(
        f"{_base(wb)}/{sid}/complete",
        json={"message": "Q3 actuals from the nightly run"},
        headers={"Idempotency-Key": key()},
    )
    assert done.status_code == 201, done.text
    version_id = done.json()["completed_version_id"]

    versions = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/versions"
    )
    head = next(v for v in versions.json()["items"] if v["id"] == version_id)
    assert head["origin_session_id"] == sid
    assert head["origin_message"] == "Q3 actuals from the nightly run"


async def test_a_version_from_a_plain_upload_claims_no_origin(http, wb):
    """Null, not an empty string: "came from nowhere in particular" and
    "came from a session that said nothing" are different facts."""
    versions = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/versions"
    )
    first = versions.json()["items"][-1]
    assert first["origin_session_id"] is None
    assert first["origin_message"] is None
