"""Sheet edit sessions end to end over the real routes (plan Tasks 7–9).

The whole §5.10 flow plus the failures that matter: the captured precondition
surfacing at completion, the state fence under concurrent completes, and the
idempotency contract on writes — which is the one that prevents a retried
write from silently resurrecting stale values.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.sheets import cache

from .test_v0_sheets_read import VALUES, XLSX, build_xlsx, make_actor

pytestmark = pytest.mark.asyncio


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


def key() -> str:
    return uuid.uuid4().hex


@pytest_asyncio.fixture
async def wb(http):
    d = await http.post(
        "/v0/drives", json={"name": "s"}, headers={"Idempotency-Key": key()}
    )
    drive = d.json()
    a = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        files={"content": ("q.xlsx", build_xlsx(VALUES), XLSX)},
        data={"parent_id": drive["root_folder_id"], "name": "q.xlsx"},
        headers={"Idempotency-Key": key()},
    )
    assert a.status_code == 201, a.text
    return {
        "drive": drive["id"],
        "root": drive["root_folder_id"],
        "artifact": a.json()["id"],
        "rev": a.json()["revision"],
    }


def _base(wb) -> str:
    """The session collection for this artifact.

    A helper because the nested path repeats in every call and reads worse
    inline than the thing being tested does.
    """
    return f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/sheet-sessions"


async def _open(http, wb, **body):
    return await http.post(
        f"{_base(wb)}",
        json=body,
        headers={"If-Match": f'"{wb["rev"]}"', "Idempotency-Key": key()},
    )


async def _write(http, wb, sid, writes, k=None):
    return await http.post(
        f"{_base(wb)}/{sid}/cells",
        json={"writes": writes},
        headers={"Idempotency-Key": k or key()},
    )


async def _complete(http, wb, sid, k=None):
    return await http.post(
        f"{_base(wb)}/{sid}/complete",
        json={},
        headers={"Idempotency-Key": k or key()},
    )


# ── the §5.10 flow ─────────────────────────────────────────────────────────


async def test_the_full_journey_publishes_exactly_one_version(http, wb):
    versions_url = f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/versions"
    before = len((await http.get(versions_url)).json()["items"])

    opened = await _open(http, wb)
    assert opened.status_code == 201, opened.text
    sid = opened.json()["session_id"]
    assert opened.json()["base_revision"] == wb["rev"]
    assert opened.headers["location"].endswith(f"/sheet-sessions/{sid}")

    for value in (1610, 1720, 1830):
        r = await _write(http, wb, sid, [{"sheet": "Q3", "range": "C2", "values": [[value]]}])
        assert r.status_code == 200, r.text

    done = await _complete(http, wb, sid)
    assert done.status_code == 201, done.text
    assert done.json()["version_id"].startswith("ver_")
    assert done.json()["artifact_revision"].startswith("rev_")

    after = (await http.get(versions_url)).json()["items"]
    assert len(after) == before + 1, "N writes must publish ONE version"

    cells = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/cells",
        params={"sheet": "Q3", "range": "A1:C3"},
    )
    grid = cells.json()["values"]
    assert grid[1][2] == 1830, "last write wins — seq is replay order"
    assert grid[0] == ["Region", "Q3", "Q4"], "untouched cells survive"
    assert grid[2][1] == 980


async def test_read_your_writes_before_completion(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[9999]]}])

    pending = await http.get(
        f"{_base(wb)}/{sid}/cells",
        params={"sheet": "Q3", "range": "B2"},
    )
    assert pending.json()["values"] == [[9999]]
    assert pending.json()["revision"].startswith("rev_")

    committed = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/cells",
        params={"sheet": "Q3", "range": "B2"},
    )
    assert committed.json()["values"] == [[1200]], "the artifact is untouched"


async def test_the_edit_log_carries_before_and_after(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[1450]]}])
    edits = await http.get(f"{_base(wb)}/{sid}/edits")
    item = edits.json()["items"][0]
    assert item["seq"] == 1
    assert item["range"] == "B2"
    assert item["previous"] == [[1200]]
    assert item["values"] == [[1450]]
    assert item["actor"] == {
        "subject_type": "agent",
        "subject": "tcagt_0000000000000001",
    }


async def test_each_edit_keeps_the_writers_own_subject_type(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    writer = replace(
        make_actor(),
        subject="tcusr_0000000000000010",
        subject_type="user",
        credential_id=None,
        runtime_id=None,
        sponsor_id=None,
        workspace_role="admin",
    )
    app.dependency_overrides[v0_actor] = lambda: writer

    written = await _write(
        http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[1450]]}]
    )
    assert written.status_code == 200, written.text
    edits = await http.get(f"{_base(wb)}/{sid}/edits")
    assert edits.json()["items"][0]["actor"] == {
        "subject_type": "user",
        "subject": "tcusr_0000000000000010",
    }


async def test_old_writer_edit_derives_type_from_its_own_subject(http, wb):
    """A mixed-version writer can differ from the session creator."""
    sid = (await _open(http, wb)).json()["session_id"]
    async with conn() as c:
        await c.execute(
            "INSERT INTO sheet_session_edits "
            "(session_id, seq, sheet, range_a1, values, actor_subject) "
            "VALUES ($1, 1, 'Q3', 'B2', '[[1450]]'::jsonb, $2)",
            sid,
            "tcusr_0000000000000010",
        )

    edits = await http.get(f"{_base(wb)}/{sid}/edits")
    assert edits.status_code == 200, edits.text
    assert edits.json()["items"][0]["actor"] == {
        "subject_type": "user",
        "subject": "tcusr_0000000000000010",
    }


async def test_a_batch_spanning_sheets_rolls_up_by_workbook_order(http, wb):
    """`sheets_touched` is what lets the console say "2 of 2 sheets changed"
    from one session read instead of paginating the whole edit log."""
    sid = (await _open(http, wb)).json()["session_id"]
    r = await _write(
        http, wb, sid,
        [
            {"sheet": "Notes", "range": "A1", "values": [["x"]]},
            {"sheet": "Q3", "range": "A1", "values": [["y"]]},
        ],
    )
    assert r.status_code == 200, r.text
    touched = r.json()["sheets_touched"]
    assert [t["name"] for t in touched] == ["Q3", "Notes"], "workbook order, not edit order"
    assert all(t["edit_count"] == 1 for t in touched)

    again = await _write(http, wb, sid, [{"sheet": "Q3", "range": "A2", "values": [["z"]]}])
    q3 = next(t for t in again.json()["sheets_touched"] if t["name"] == "Q3")
    assert q3["edit_count"] == 2, "a second write increments rather than duplicating"


async def test_a_zero_edit_completion_publishes_nothing(http, wb):
    versions_url = f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/versions"
    before = len((await http.get(versions_url)).json()["items"])
    sid = (await _open(http, wb)).json()["session_id"]

    done = await _complete(http, wb, sid)
    assert done.status_code == 200, done.text
    assert done.json()["version_id"] is None
    assert len((await http.get(versions_url)).json()["items"]) == before


# ── concurrency and preconditions ──────────────────────────────────────────


async def test_a_rival_version_makes_completion_412(http, wb):
    """The base VERSION captured at create is enforced at completion, so a
    head that moved surfaces here — and the loser's edits survive for a
    retry.

    Content-anchored, not revision-anchored: a rename would leave the head
    alone and publish (see the rename test), while new bytes under the
    session refuse, because that is the change a replay cannot absorb.
    """
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[1]]}])

    rival = await http.post(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/versions",
        files={"content": ("q.xlsx", build_xlsx(VALUES), XLSX)},
        headers={"Idempotency-Key": key(), "If-Match": f'"{wb["rev"]}"'},
    )
    assert rival.status_code == 201, rival.text

    done = await _complete(http, wb, sid)
    assert done.status_code == 412, done.text
    assert done.json()["error"]["details"]["current_revision"]

    edits = await http.get(f"{_base(wb)}/{sid}/edits")
    assert len(edits.json()["items"]) == 1, "the loser's work is still replayable"


async def test_concurrent_completions_publish_one_version(http, wb):
    """The state fence: a zero-row conditional UPDATE means another
    completion already won. This is the exactly-once guarantee, and it does
    not rest on the serializer producing stable bytes."""
    versions_url = f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}/versions"
    before = len((await http.get(versions_url)).json()["items"])
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[5]]}])

    results = await asyncio.gather(
        _complete(http, wb, sid), _complete(http, wb, sid), return_exceptions=True
    )
    codes = sorted(r.status_code for r in results if not isinstance(r, BaseException))
    assert codes == [201, 409], codes
    assert len((await http.get(versions_url)).json()["items"]) == before + 1


async def test_completion_replays_under_the_same_key(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[7]]}])
    k = key()
    first = await _complete(http, wb, sid, k)
    again = await _complete(http, wb, sid, k)
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["version_id"] == first.json()["version_id"]


async def test_create_replay_is_200_and_returns_the_same_session(http, wb):
    k = key()
    headers = {"If-Match": f'"{wb["rev"]}"', "Idempotency-Key": k}
    first = await http.post(_base(wb), json={}, headers=headers)
    replay = await http.post(_base(wb), json={}, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["session_id"] == first.json()["session_id"]


async def test_a_retried_write_does_not_resurrect_stale_values(http, wb):
    """The reason Idempotency-Key is required on writes. Without it a retry
    landing after a later overlapping write would silently revive 100."""
    sid = (await _open(http, wb)).json()["session_id"]
    k = key()
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[100]]}], k)
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[200]]}])
    replay = await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[100]]}], k)
    assert replay.status_code == 200

    now = await http.get(
        f"{_base(wb)}/{sid}/cells",
        params={"sheet": "Q3", "range": "B2"},
    )
    assert now.json()["values"] == [[200]]


async def test_create_requires_if_match(http, wb):
    r = await http.post(
        f"{_base(wb)}",
        json={},
        headers={"Idempotency-Key": key()},
    )
    assert r.status_code == 428
    assert r.json()["error"]["code"] == "PRECONDITION_REQUIRED"


async def test_create_requires_an_idempotency_key(http, wb):
    r = await http.post(
        f"{_base(wb)}",
        json={},
        headers={"If-Match": f'"{wb["rev"]}"'},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_a_stale_if_match_is_412_with_the_current_revision(http, wb):
    r = await http.post(
        f"{_base(wb)}",
        json={},
        headers={"If-Match": '"rev_0000000000000000"', "Idempotency-Key": key()},
    )
    assert r.status_code == 412
    assert r.json()["error"]["details"]["current_revision"] == wb["rev"]


# ── refusals and lifecycle ─────────────────────────────────────────────────


async def test_a_formula_workbook_is_refused_at_create_not_at_complete(http):
    """The refusal costs the agent nothing if it arrives before the work."""
    d = (
        await http.post(
            "/v0/drives", json={"name": "f"}, headers={"Idempotency-Key": key()}
        )
    ).json()
    a = await http.post(
        f"/v0/drives/{d['id']}/artifacts",
        files={"content": ("m.xlsx", build_xlsx({"Q3": [["a"], ["=A1*2"]]}), XLSX)},
        data={"parent_id": d["root_folder_id"], "name": "m.xlsx"},
        headers={"Idempotency-Key": key()},
    )
    art = a.json()
    r = await http.post(
        f"/v0/drives/{d['id']}/artifacts/{art['id']}/sheet-sessions",
        json={},
        headers={"If-Match": f'"{art["revision"]}"', "Idempotency-Key": key()},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "WORKBOOK_NOT_EDITABLE"


async def test_discard_publishes_nothing_and_drops_the_edits(http, wb):
    opened = (await _open(http, wb)).json()
    sid = opened["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[1]]}])
    read = await http.get(f"{_base(wb)}/{sid}")

    gone = await http.delete(
        f"{_base(wb)}/{sid}",
        headers={"If-Match": read.headers["etag"], "Idempotency-Key": key()},
    )
    assert gone.status_code == 200, gone.text
    assert gone.json()["state"] == "discarded"

    async with conn() as c:
        remaining = await c.fetchval(
            "SELECT count(*) FROM sheet_session_edits WHERE session_id = $1", sid
        )
    assert remaining == 1, "edits persist until GC; the session is merely terminal"

    after = await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[2]]}])
    assert after.status_code == 409
    assert after.json()["error"]["code"] == "SHEET_SESSION_ALREADY_COMPLETED"


async def test_discard_requires_if_match(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    r = await http.delete(
        f"{_base(wb)}/{sid}",
        headers={"Idempotency-Key": key()},
    )
    assert r.status_code == 428


async def test_an_expired_session_refuses_and_transitions_at_use(http, wb):
    """Expiry is enforced at USE, not by a timer — no sweeper race, and no
    window where a dead session still accepts writes."""
    sid = (await _open(http, wb)).json()["session_id"]
    async with conn() as c:
        await c.execute(
            "UPDATE sheet_sessions SET lease_expires_at = now() - interval '1 minute' "
            "WHERE id = $1",
            sid,
        )
    r = await _write(http, wb, sid, [{"sheet": "Q3", "range": "A1", "values": [["x"]]}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SHEET_SESSION_EXPIRED"

    state = await http.get(f"{_base(wb)}/{sid}")
    assert state.json()["state"] == "expired"


async def test_writes_extend_the_lease_but_reads_do_not(http, wb):
    """A polling console must never keep a dead agent's session alive."""
    sid = (await _open(http, wb)).json()["session_id"]
    first = await _write(http, wb, sid, [{"sheet": "Q3", "range": "A1", "values": [["a"]]}])
    lease = first.json()["lease_expires_at"]

    await http.get(f"{_base(wb)}/{sid}")
    after = await http.get(f"{_base(wb)}/{sid}")
    assert after.json()["lease_expires_at"] == lease


async def test_an_unknown_sheet_in_a_write_is_refused(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    r = await _write(http, wb, sid, [{"sheet": "Nope", "range": "A1", "values": [["x"]]}])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SHEET_NOT_FOUND"


async def test_a_shape_mismatch_is_rejected(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    r = await _write(http, wb, sid, [{"sheet": "Q3", "range": "A1:B2", "values": [[1, 2]]}])
    assert r.status_code == 422


async def test_a_batch_is_all_or_nothing(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    r = await _write(
        http, wb, sid,
        [
            {"sheet": "Q3", "range": "A1", "values": [["ok"]]},
            {"sheet": "Nope", "range": "A1", "values": [["bad"]]},
        ],
    )
    assert r.status_code == 404
    edits = await http.get(f"{_base(wb)}/{sid}/edits")
    assert edits.json()["items"] == [], "the good write must not have landed either"


async def test_a_session_reached_through_the_wrong_artifact_is_404(http, wb):
    """Anti-enumeration, on the axis the nested path introduces.

    `/artifacts/{a}/sheet-sessions/{s}` pairs two ids, so a caller can name
    a real session under an artifact that does not own it. The artifact is
    in the WHERE clause rather than asserted afterwards, so the answer is
    the same 404 an unknown id gets — never a 403, which would confirm the
    session exists somewhere.
    """
    sid = (await _open(http, wb)).json()["session_id"]

    # A second artifact in the SAME drive, which the caller may read.
    sibling = await http.post(
        f"/v0/drives/{wb['drive']}/artifacts",
        files={"content": ("other.xlsx", build_xlsx(VALUES), XLSX)},
        data={"parent_id": wb["root"], "name": "other.xlsx"},
        headers={"Idempotency-Key": key()},
    )
    assert sibling.status_code == 201, sibling.text

    mispaired = await http.get(
        f"/v0/drives/{wb['drive']}/artifacts/{sibling.json()['id']}"
        f"/sheet-sessions/{sid}"
    )
    assert mispaired.status_code == 404
    assert mispaired.json()["error"]["code"] == "SHEET_SESSION_NOT_FOUND"

    # And it is still reachable through its own artifact.
    ok = await http.get(f"{_base(wb)}/{sid}")
    assert ok.status_code == 200


async def test_sessions_are_listable_and_filterable(http, wb):
    """O5: an agent sees another agent's open sessions, because it cannot
    otherwise act on the `other_open_sessions` warning it was given."""
    sid = (await _open(http, wb)).json()["session_id"]
    listed = await http.get(
        f"{_base(wb)}",
        params={"state": "open"},
    )
    assert listed.status_code == 200, listed.text
    assert [s["session_id"] for s in listed.json()["items"]] == [sid]
    assert listed.json()["next_cursor"] is None


async def test_bad_session_cursors_are_public_400s(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]

    for url in (_base(wb), f"{_base(wb)}/{sid}/edits"):
        response = await http.get(url, params={"cursor": "not-a-sealed-cursor"})
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "INVALID_CURSOR"


async def test_a_second_session_sees_the_first(http, wb):
    await _open(http, wb)
    second = await _open(http, wb)
    assert second.json()["other_open_sessions"] >= 1


async def test_the_per_request_cell_cap_is_enforced(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    rows = settings.sheet_max_write_cells + 10
    r = await _write(
        http, wb, sid,
        [{"sheet": "Q3", "range": f"A1:A{rows}", "values": [["x"] for _ in range(rows)]}],
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_a_rename_mid_session_still_publishes(http, wb):
    """The completion precondition is the artifact's CONTENT, not its
    revision.

    §4.1 rotates the revision on every mutation of mutable state, so
    anchoring there meant a rename, a move or a relabel aborted the
    session — and since `base_revision` is captured once at create and
    there is no re-anchor operation, the session stayed `open` and could
    never complete again. Eleven cells of agent work lost to someone
    tidying a filename.
    """
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[7]]}])

    renamed = await http.patch(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}",
        json={"name": "renamed.xlsx"},
        headers={"If-Match": f'"{wb["rev"]}"', "Idempotency-Key": key()},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["revision"] != wb["rev"], "a rename must rotate the revision"

    done = await _complete(http, wb, sid)
    assert done.status_code == 201, done.text
    assert done.json()["completed_version_id"]


async def test_a_relabel_mid_session_still_publishes(http, wb):
    sid = (await _open(http, wb)).json()["session_id"]
    await _write(http, wb, sid, [{"sheet": "Q3", "range": "B2", "values": [[9]]}])

    relabelled = await http.patch(
        f"/v0/drives/{wb['drive']}/artifacts/{wb['artifact']}",
        json={"labels": ["q3", "reviewed"]},
        headers={"If-Match": f'"{wb["rev"]}"', "Idempotency-Key": key()},
    )
    assert relabelled.status_code == 200, relabelled.text

    done = await _complete(http, wb, sid)
    assert done.status_code == 201, done.text
