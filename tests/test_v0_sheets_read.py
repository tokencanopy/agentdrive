"""The two sheet read operations over the real mounted routes (plan Task 6).

Everything here runs the actual `/v0` surface against real Postgres and the
GCS emulator. All identifiers and content are synthetic.

Covers: the workbook index and its editability verdict, range reads and their
padding, the conditional-read path, and the refusals — with particular
attention to the ones that must NOT be 404, because an artifact the caller can
see in a listing must never be reported as missing.
"""

from __future__ import annotations

import io
import uuid

import openpyxl
import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext
from agentdrive.sheets import cache

pytestmark = pytest.mark.asyncio

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
WS = "tcws_0000000000000001"


def make_actor(*, scopes: set[str] | None = None) -> V0ActorContext:
    return V0ActorContext(
        subject=AGENT,
        subject_type="agent",
        workspace_id=WS,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(
            scopes
            if scopes is not None
            else {
                "drives:read", "drives:write", "usage:read",
                "content:read", "content:write", "sharing:read", "sharing:write",
            }
        ),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id=SPONSOR,
        workspace_role=None,
    )


def build_xlsx(sheets: dict[str, list[list[object]]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


VALUES = {
    "Q3": [["Region", "Q3", "Q4"], ["EMEA", 1200, 1450], ["APAC", 980, None]],
    "Notes": [["owner"], ["ops@example.test"]],
}
FORMULAS = {"Q3": [["a", "b"], [1, "=A2*2"]]}
# Same shape as VALUES with a different B2, so a version-scoped read of the
# base and of the head differ in exactly one cell.
RIVAL = {
    "Q3": [["Region", "Q3", "Q4"], ["EMEA", 9999, 1450], ["APAC", 980, None]],
    "Notes": [["owner"], ["ops@example.test"]],
}


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    app.dependency_overrides[v0_actor] = lambda: make_actor()
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
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


def _key() -> str:
    return uuid.uuid4().hex


async def _drive(http) -> dict:
    resp = await http.post(
        "/v0/drives", json={"name": "sheets"}, headers={"Idempotency-Key": _key()}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _artifact(http, drive, name: str, data: bytes, content_type: str) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        files={"content": (name, data, content_type)},
        data={"parent_id": drive["root_folder_id"], "name": name},
        headers={"Idempotency-Key": _key()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest_asyncio.fixture
async def workbook(http):
    drive = await _drive(http)
    art = await _artifact(http, drive, "q.xlsx", build_xlsx(VALUES), XLSX)
    return {"drive": drive, "artifact": art}


# ── sheets_list ────────────────────────────────────────────────────────────


async def test_index_reports_sheets_and_editability(http, workbook):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(f"/v0/drives/{d}/artifacts/{a}/sheets")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workbook"]["editability"] == {"status": "ok", "reason": None}
    assert body["workbook"]["format"] == "xlsx"
    assert [s["name"] for s in body["sheets"]] == ["Q3", "Notes"]
    assert [s["index"] for s in body["sheets"]] == [0, 1]


async def test_the_etag_is_the_artifact_revision(http, workbook):
    """One call gives structure, editability AND the revision a session's
    If-Match needs — so opening a session costs one read, not two."""
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(f"/v0/drives/{d}/artifacts/{a}/sheets")
    assert resp.headers["etag"] == f'"{resp.json()["workbook"]["revision"]}"'
    assert resp.json()["workbook"]["revision"] == workbook["artifact"]["revision"]


async def test_if_none_match_short_circuits(http, workbook):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    first = await http.get(f"/v0/drives/{d}/artifacts/{a}/sheets")
    again = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/sheets",
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 304


async def test_a_formula_workbook_reports_blocked_but_still_reads(http):
    """Blocked is a verdict about EDITING. Reading a formula workbook is
    always allowed — an agent may look at what it may not change."""
    drive = await _drive(http)
    art = await _artifact(http, drive, "m.xlsx", build_xlsx(FORMULAS), XLSX)
    resp = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}/sheets")
    assert resp.status_code == 200, resp.text
    assert resp.json()["workbook"]["editability"] == {
        "status": "blocked",
        "reason": "FORMULAS_PRESENT",
    }


async def test_a_non_spreadsheet_is_409_not_404(http):
    """The artifact exists and the caller can see it in a listing. Answering
    404 would be a lie about a resource they hold a grant on."""
    drive = await _drive(http)
    art = await _artifact(http, drive, "note.md", b"# hi", "text/markdown")
    resp = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}/sheets")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "WORKBOOK_NOT_EDITABLE"


async def test_a_missing_artifact_is_404(http, workbook):
    d = workbook["drive"]["id"]
    resp = await http.get(f"/v0/drives/{d}/artifacts/art_0000000000000000/sheets")
    assert resp.status_code == 404


async def test_content_read_scope_is_required(http, workbook):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    app.dependency_overrides[v0_actor] = lambda: make_actor(scopes={"drives:read"})
    resp = await http.get(f"/v0/drives/{d}/artifacts/{a}/sheets")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# ── sheet_cells_read ───────────────────────────────────────────────────────


async def test_reads_the_requested_rectangle(http, workbook):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/cells", params={"sheet": "Q3", "range": "A1:C3"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sheet"] == "Q3"
    assert body["range"] == "A1:C3"
    assert body["values"] == [
        ["Region", "Q3", "Q4"],
        ["EMEA", 1200, 1450],
        ["APAC", 980, None],
    ]


async def test_the_rectangle_is_padded_outside_the_used_range(http, workbook):
    """Always exactly the shape asked for. Ragged arrays would put the
    padding logic in every client instead."""
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/cells", params={"sheet": "Q3", "range": "A9:C10"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["values"] == [[None, None, None], [None, None, None]]


async def test_a_second_sheet_reads_independently(http, workbook):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/cells", params={"sheet": "Notes", "range": "A1:A2"}
    )
    assert resp.json()["values"] == [["owner"], ["ops@example.test"]]


async def test_omitting_sheet_is_ambiguous_on_a_multi_sheet_workbook(http, workbook):
    """Guessing the first sheet would silently read the wrong data."""
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(f"/v0/drives/{d}/artifacts/{a}/cells", params={"range": "A1"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_omitting_sheet_is_fine_for_csv(http):
    drive = await _drive(http)
    art = await _artifact(http, drive, "d.csv", b"id,qty\n00123,7\n", "text/csv")
    resp = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/cells", params={"range": "A1:B2"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["values"] == [["id", "qty"], ["00123", "7"]]


async def test_csv_keeps_leading_zeros_over_the_wire(http):
    """The corruption that is silent and permanent, asserted at the wire."""
    drive = await _drive(http)
    art = await _artifact(
        http, drive, "d.csv", b"id\n00123\n12345678901234567890\n", "text/csv"
    )
    resp = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/cells", params={"range": "A1:A3"}
    )
    assert resp.json()["values"] == [["id"], ["00123"], ["12345678901234567890"]]


async def test_an_unknown_sheet_is_sheet_not_found(http, workbook):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/cells", params={"sheet": "Nope", "range": "A1"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SHEET_NOT_FOUND"


@pytest.mark.parametrize("bad", ["A0", "A1:", ":B2", "AAAA1", "not-a-range"])
async def test_a_malformed_range_is_invalid_argument(http, workbook, bad):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/cells", params={"sheet": "Q3", "range": bad}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_an_oversized_range_is_refused(http, workbook):
    """Range IS the pagination, so the cap is what stops a caller choosing
    the whole workbook."""
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/cells",
        params={"sheet": "Q3", "range": "A1:ZZ100000"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_unknown_query_parameters_are_rejected(http, workbook):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    resp = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/cells", params={"sheet": "Q3", "nope": "1"}
    )
    assert resp.status_code == 400


async def test_reads_do_not_require_the_workbook_to_be_editable(http):
    """Formulas block EDITING, not reading — the cached values are exactly
    what a reader wants."""
    drive = await _drive(http)
    art = await _artifact(http, drive, "m.xlsx", build_xlsx(FORMULAS), XLSX)
    resp = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/cells",
        params={"sheet": "Q3", "range": "A1:B1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["values"] == [["a", "b"]]


# ── classification and search extraction (plan Task 11) ────────────────────


async def test_a_workbook_becomes_searchable_by_its_contents(http):
    """Before extraction an .xlsx was findable by filename and by nothing
    inside it: `_derive_preview` decodes UTF-8 and a zip is binary to that."""
    drive = await _drive(http)
    await _artifact(http, drive, "q.xlsx", build_xlsx(VALUES), XLSX)

    hits = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "EMEA"}
    )
    assert hits.status_code == 200, hits.text
    assert [h["name"] for h in hits.json()["items"]] == ["q.xlsx"]


async def test_a_sheet_name_is_searchable(http):
    drive = await _drive(http)
    await _artifact(http, drive, "q.xlsx", build_xlsx(VALUES), XLSX)
    hits = await http.get(f"/v0/drives/{drive['id']}/search", params={"q": "Notes"})
    assert [h["name"] for h in hits.json()["items"]] == ["q.xlsx"]


async def test_the_kind_chip_says_xlsx_not_bundle(http):
    """The chip in front of a reader used to say `bundle` — true of a zip,
    useless about a spreadsheet."""
    from agentdrive.core.kinds import chip_label, kind_for

    assert kind_for(XLSX, "q.xlsx") == "dataset"
    assert chip_label(XLSX, "q.xlsx") == "xlsx"

    drive = await _drive(http)
    art = await _artifact(http, drive, "q.xlsx", build_xlsx(VALUES), XLSX)
    read = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}")
    assert read.status_code == 200


async def test_an_unparseable_upload_still_succeeds_without_a_preview(http):
    """An unsearchable artifact is far better than a failed upload."""
    drive = await _drive(http)
    art = await _artifact(http, drive, "broken.xlsx", b"not a workbook", XLSX)
    assert art["id"].startswith("art_")
    async with conn() as c:
        preview = await c.fetchval(
            "SELECT content_preview FROM artifacts WHERE id = $1", art["id"]
        )
    assert preview is None


# ── version-scoped reads ────────────────────────────────────────────────


async def test_a_version_scoped_read_survives_the_head_moving(http, workbook):
    """The whole point: an agent whose session lost a race has to be able to
    see what it BASED on in order to work out what changed.

    Before these routes existed the parsed views only ever read the head, so
    the moment a rival version landed the base became unreachable and the
    only move left was a blind replay.
    """
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    base = workbook["artifact"]["head_version_id"]

    rival = await http.post(
        f"/v0/drives/{d}/artifacts/{a}/versions",
        files={"content": ("q.xlsx", build_xlsx(RIVAL), XLSX)},
        headers={
            "If-Match": f'"{workbook["artifact"]["revision"]}"',
            "Idempotency-Key": _key(),
        },
    )
    assert rival.status_code == 201, rival.text

    head = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/cells", params={"sheet": "Q3", "range": "B2"}
    )
    based = await http.get(
        f"/v0/drives/{d}/artifacts/{a}/versions/{base}/cells",
        params={"sheet": "Q3", "range": "B2"},
    )
    assert based.status_code == 200, based.text
    assert head.json()["values"] == [[9999]]
    assert based.json()["values"] == [[1200]]


async def test_a_version_scoped_index_reads_that_versions_structure(http, workbook):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    base = workbook["artifact"]["head_version_id"]
    r = await http.get(f"/v0/drives/{d}/artifacts/{a}/versions/{base}/sheets")
    assert r.status_code == 200, r.text
    assert [s["name"] for s in r.json()["sheets"]] == ["Q3", "Notes"]


async def test_the_validator_is_the_version_id_not_the_artifact_revision(
    http, workbook
):
    """A version is immutable, so its own identity is the strongest ETag
    available — and unlike the artifact revision it cannot be rotated by a
    rename."""
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    base = workbook["artifact"]["head_version_id"]
    url = f"/v0/drives/{d}/artifacts/{a}/versions/{base}/sheets"

    first = await http.get(url)
    assert first.headers["ETag"] == f'"{base}"'

    again = await http.get(url, headers={"If-None-Match": f'"{base}"'})
    assert again.status_code == 304


async def test_a_malformed_version_id_is_refused(http, workbook):
    d, a = workbook["drive"]["id"], workbook["artifact"]["id"]
    r = await http.get(f"/v0/drives/{d}/artifacts/{a}/versions/nope/sheets")
    assert r.status_code in (400, 404), r.text


async def test_a_version_of_a_non_spreadsheet_is_409_not_404(http):
    """Same refusal the head route gives, for the same reason: the artifact
    exists and the caller can see it — it simply is not a workbook."""
    drive = await _drive(http)
    art = await _artifact(http, drive, "note.md", b"# hi\n", "text/markdown")
    r = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
        f"/versions/{art['head_version_id']}/sheets"
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "WORKBOOK_NOT_EDITABLE"
