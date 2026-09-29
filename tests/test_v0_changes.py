"""Changes vertical (slice 9): the 1 change-feed operation over real Postgres.

The change feed is populated by every domain mutation (the vertical's append
wiring) and read via ``GET /v0/drives/{drive_id}/changes`` with D14 sealed
cursors. Tests verify: mutations emit changes; ``start=now``/``beginning``;
cursor advance + ``has_more``; re-presenting a cursor re-delivers the same
page (at-least-once); cursors are drive-scoped; ``changes:read`` scope gates;
a non-granted caller is 404.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
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
        "content:read", "content:write", "changes:read",
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


_MULTIPART_BOUNDARY = "----TestFormBoundaryXyZ"
_CT_MULTIPART = f"multipart/form-data; boundary={_MULTIPART_BOUNDARY}"


def _multipart_create(fields: dict[str, str], content: bytes) -> bytes:
    body_parts = []
    for name, value in fields.items():
        body_parts.append(
            f"--{_MULTIPART_BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f"\r\n"
            f"{value}\r\n"
        )
    body_parts.append(
        f"--{_MULTIPART_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="content"; filename="test.bin"\r\n'
        f"Content-Type: application/octet-stream\r\n"
        f"\r\n"
    )
    encoded = []
    for part in body_parts:
        encoded.append(part.encode())
    encoded.append(content)
    encoded.append(f"\r\n--{_MULTIPART_BOUNDARY}--\r\n".encode())
    return b"".join(encoded)


async def _create_artifact(
    http, drive_id: str, parent_id: str, name: str, key: str, content: bytes = b"hello"
):
    resp = await http.post(
        f"/v0/drives/{drive_id}/artifacts",
        content=_multipart_create({"parent_id": parent_id, "name": name}, content),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp


async def _create_folder(http, drive_id: str, parent_id: str, name: str, key: str):
    resp = await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp


async def _insert_artifact(c, drive_id: str, folder_id: str, art_id: str, name: str) -> None:
    """A headless artifact + one version, created directly (like the folders
    vertical's fixture) so cascade tests can exercise real rows."""
    version_id = "ver_" + art_id[len("art_"):]
    await c.execute(
        "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
        "VALUES ($1, $2, $3, $4, $5)",
        art_id, drive_id, folder_id, name, "rev_00000000000000a1",
    )
    await c.execute(
        "INSERT INTO artifact_versions "
        "(id, artifact_id, checksum, content_type, size_bytes, storage_object, "
        "actor_type, actor_id, ordinal) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        version_id, art_id, "sha256:1", "application/octet-stream", 10,
        "cas/source.bin", "agent", AGENT, 1,
    )
    await c.execute(
        "UPDATE artifacts SET head_version_id = $2 WHERE id = $1",
        art_id, version_id,
    )


async def test_mutations_emit_change_feed_rows(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chg", "kchg")
    folder = await _create_folder(http, drive["id"], drive["root_folder_id"], "a", "kchg-1")

    async with conn() as c:
        changes = await c.fetch(
            "SELECT type, resource_type, resource_id FROM drive_changes "
            "WHERE drive_id=$1 ORDER BY sequence",
            drive["id"],
        )
        types = [r["type"] for r in changes]
        assert "drive.updated" in types
        assert "folder.created" in types
        assert any(r["resource_id"] == folder.json()["id"] for r in changes)


async def test_changes_requires_scope(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chgsc", "kchgsc")
    override_actor(make_actor(scopes={"drives:read"}))
    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "now"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_changes_requires_exactly_one_of_start_or_cursor(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chg1", "kchg1")
    neither = await http.get(f"/v0/drives/{drive['id']}/changes")
    assert neither.status_code == 400
    assert neither.json()["error"]["code"] == "INVALID_REQUEST"

    both = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "now", "cursor": "cur_x"},
    )
    assert both.status_code == 400


async def test_changes_start_now_and_beginning(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chg2", "kchg2")
    await _create_folder(http, drive["id"], drive["root_folder_id"], "b", "kchg2-1")

    # start=beginning returns everything committed so far.
    beginning = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    assert beginning.status_code == 200
    body = beginning.json()
    assert body["next_cursor"]
    assert len(body["items"]) >= 1
    assert body["has_more"] is False  # drained to the high-water mark

    # start=now captures the current head → empty page but a usable cursor.
    now = await http.get(f"/v0/drives/{drive['id']}/changes", params={"start": "now"})
    assert now.status_code == 200
    now_body = now.json()
    assert now_body["items"] == []
    assert now_body["next_cursor"]
    assert now_body["has_more"] is False


async def test_changes_cursor_replays_partial_page_at_least_once(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chg3", "kchg3")
    await _create_folder(http, drive["id"], drive["root_folder_id"], "c1", "kchg3-1")
    await _create_folder(http, drive["id"], drive["root_folder_id"], "c2", "kchg3-2")

    # start=beginning returns the first page and a successor cursor. The
    # client presents the successor; re-presenting the SAME cursor re-delivers
    # the same page (at-least-once — a dropped response is not lost).
    first = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "beginning", "limit": 1},
    )
    body = first.json()
    assert len(body["items"]) == 1
    cursor = body["next_cursor"]

    page2 = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"cursor": cursor}
    )
    page2_body = page2.json()
    assert page2.status_code == 200

    # Re-presenting the SAME cursor returns the SAME page.
    replay = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"cursor": cursor}
    )
    assert replay.status_code == 200
    replay_body = replay.json()
    assert [i["id"] for i in replay_body["items"]] == [
        i["id"] for i in page2_body["items"]
    ]


async def test_changes_drained_cursor_follows_new_commits(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chg3b", "kchg3b")

    # Fully drain the feed: start=now captures the head → empty page.
    first = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "now"}
    )
    cursor = first.json()["next_cursor"]

    # A new change commits after the capture.
    await _create_folder(http, drive["id"], drive["root_folder_id"], "c", "kchg3b-1")

    # Re-presenting a DRAINED cursor follows changes committed after capture.
    replay = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"cursor": cursor}
    )
    assert replay.status_code == 200
    replay_body = replay.json()
    assert len(replay_body["items"]) == 1
    assert replay_body["items"][0]["type"] == "folder.created"
    assert replay_body["next_cursor"] != cursor


async def test_changes_cursor_is_drive_scoped(http, override_actor):
    override_actor(make_actor())
    drive_a = await _create_drive(http, "chga", "kchga")
    drive_b = await _create_drive(http, "chgb", "kchgb")
    cursor = (await http.get(
        f"/v0/drives/{drive_a['id']}/changes", params={"start": "now"}
    )).json()["next_cursor"]

    replayed = await http.get(
        f"/v0/drives/{drive_b['id']}/changes", params={"cursor": cursor}
    )
    assert replayed.status_code == 400
    assert replayed.json()["error"]["code"] == "INVALID_CURSOR"


async def test_changes_cross_workspace_is_404(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chg404", "kchg404")
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "now"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DRIVE_NOT_FOUND"


async def test_changes_outsider_without_grant_is_404(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chgno", "kchgno")
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "now"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_change_document_wire_shape_is_exact_and_data_is_object(http, override_actor):
    """The change document's wire shape is exact — nothing extra leaks, and
    `data` is a JSON OBJECT on the wire, not the JSON-encoded string the
    asyncpg JSONB codec produces."""
    import re

    override_actor(make_actor())
    drive = await _create_drive(http, "shape", "kshape")
    await _create_folder(http, drive["id"], drive["root_folder_id"], "leaf", "kshape-1")

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]

    created = next(c for c in items if c["type"] == "folder.created")

    assert set(created.keys()) == {
        "id", "change_set_id", "type", "drive_id",
        "actor", "resource", "previous_revision", "revision", "occurred_at",
        "data",
    }
    assert re.match(r"^chg_[a-f0-9]{16}$", created["id"])
    assert re.match(r"^cset_[a-f0-9]{16}$", created["change_set_id"])
    assert created["actor"] == {"type": "agent", "id": AGENT}
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$", created["occurred_at"]
    )
    # Regression: `data` must be an OBJECT, never the JSON-encoded string "{}".
    assert isinstance(created["data"], dict)
    assert "name" in created["data"]


# ---------------------------------------------------------------------------
# recursive operations emit one change row per affected resource (§6.7)
# ---------------------------------------------------------------------------


async def _read_feed(c, drive_id: str) -> list[dict]:
    rows = []
    for r in await c.fetch(
        "SELECT type, resource_type, resource_id, change_set_id, revision, data "
        "FROM drive_changes WHERE drive_id=$1 ORDER BY sequence",
        drive_id,
    ):
        row = dict(r)
        data = row["data"]
        row["data"] = json.loads(data) if isinstance(data, str) else (data or {})
        rows.append(row)
    return rows


async def test_recursive_delete_emits_one_change_per_member(http, override_actor):
    """A recursive folder delete must append one change row PER affected
    resource — the root, each descendant folder, and each artifact — all
    sharing one change_set_id, so a feed-driven mirror can replay the whole
    cascade instead of learning only that the root went away."""
    override_actor(make_actor())
    drive = await _create_drive(http, "casc", "kcasc")
    a = await _create_folder(http, drive["id"], drive["root_folder_id"], "a", "kcasc-1")
    a_id = a.json()["id"]
    b = await _create_folder(http, drive["id"], a_id, "b", "kcasc-2")
    sub = await _create_folder(http, drive["id"], a_id, "c", "kcasc-3")
    art_ids = ["art_00000000000000e0", "art_00000000000000e1", "art_00000000000000e2"]
    async with conn() as c:
        for i, art_id in enumerate(art_ids):
            await _insert_artifact(c, drive["id"], a_id, art_id, f"x{i}.bin")

    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{a_id}?recursive=true",
        headers={"Idempotency-Key": "kcasc-4", "If-Match": a.headers["etag"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cascade"] == {"folders": 2, "artifacts": 3}

    async with conn() as c:
        deleted = [r for r in await _read_feed(c, drive["id"])
                   if r["type"] in ("folder.deleted", "artifact.deleted")]

    assert len(deleted) == 6, "one row per affected resource (root + 2 folders + 3 artifacts)"
    sets = {r["change_set_id"] for r in deleted}
    assert len(sets) == 1, "the whole cascade must share one change_set_id"
    assert sets.pop().startswith("cset_")
    folders = [r for r in deleted if r["resource_type"] == "folder"]
    artifacts = [r for r in deleted if r["resource_type"] == "artifact"]
    assert {r["resource_id"] for r in folders} == {a_id, b.json()["id"], sub.json()["id"]}
    assert {r["resource_id"] for r in artifacts} == set(art_ids)
    # Deterministic ordering: the root (with its cascade payload) first, then
    # descendant folders by depth/id, then artifacts by id.
    assert [r["resource_type"] for r in deleted] == (
        ["folder", "folder", "folder", "artifact", "artifact", "artifact"]
    )
    assert deleted[0]["resource_id"] == a_id
    assert deleted[0]["data"] == {"name": "a", "cascade": {"folders": 2, "artifacts": 3}}


async def test_restore_emits_one_change_per_member(http, override_actor):
    """The restore of a recursive-delete cohort must mirror the delete: one
    change row per restored resource, all sharing the cohort's change_set_id."""
    override_actor(make_actor())
    drive = await _create_drive(http, "rscas", "krscas")
    a = await _create_folder(http, drive["id"], drive["root_folder_id"], "a", "krscas-1")
    a_id = a.json()["id"]
    b = await _create_folder(http, drive["id"], a_id, "b", "krscas-2")
    art_ids = ["art_00000000000000e4", "art_00000000000000e5", "art_00000000000000e6"]
    async with conn() as c:
        for i, art_id in enumerate(art_ids):
            await _insert_artifact(c, drive["id"], a_id, art_id, f"y{i}.bin")

    deleted = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{a_id}?recursive=true",
        headers={"Idempotency-Key": "krscas-4", "If-Match": a.headers["etag"]},
    )
    assert deleted.status_code == 200

    restored = await http.post(
        f"/v0/drives/{drive['id']}/folders/{a_id}/restore",
        headers={"Idempotency-Key": "krscas-5", "If-Match": deleted.headers["etag"]},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["cascade"] == {"folders": 1, "artifacts": 3}

    async with conn() as c:
        rows = [r for r in await _read_feed(c, drive["id"])
                if r["type"] in ("folder.restored", "artifact.restored")]

    assert len(rows) == 5, "one row per restored resource (root + 1 folder + 3 artifacts)"
    sets = {r["change_set_id"] for r in rows}
    assert len(sets) == 1, "the whole restore must share one change_set_id"
    folders = [r for r in rows if r["resource_type"] == "folder"]
    artifacts = [r for r in rows if r["resource_type"] == "artifact"]
    assert {r["resource_id"] for r in folders} == {a_id, b.json()["id"]}
    assert {r["resource_id"] for r in artifacts} == set(art_ids)
    assert rows[0]["resource_id"] == a_id
    assert rows[0]["data"] == {"name": "a", "cascade": {"folders": 1, "artifacts": 3}}


async def test_subtree_copy_emits_one_change_per_copied_resource(http, override_actor):
    """A subtree copy must append one change row PER copied resource — every
    copied folder and artifact — sharing one change_set_id, so a mirror learns
    the copied descendants exist, not just the root."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cpcas", "kcpcas")
    src = await _create_folder(http, drive["id"], drive["root_folder_id"], "src", "kcpcas-1")
    child = await _create_folder(http, drive["id"], src.json()["id"], "child", "kcpcas-2")
    async with conn() as c:
        await _insert_artifact(c, drive["id"], src.json()["id"], "art_00000000000000e7", "a.bin")

    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders/{src.json()['id']}/copy",
        json={"destination_parent_id": drive["root_folder_id"], "destination_name": "copy"},
        headers={"Idempotency-Key": "kcpcas-3"},
    )
    assert resp.status_code == 201, resp.text

    async with conn() as c:
        rows = [r for r in await _read_feed(c, drive["id"])
                if r["type"] in ("folder.created", "artifact.created")]

    # Setup emits one folder.created per create (each its own change_set_id);
    # the subtree copy is the only group larger than one.
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["change_set_id"]].append(r)
    copy_groups = [g for g in groups.values() if len(g) > 1]
    assert len(copy_groups) == 1, "the subtree copy must be one change_set_id"
    members = copy_groups[0]
    assert len(members) == 3, "copied root + child + artifact"
    assert sorted(r["resource_type"] for r in members) == ["artifact", "folder", "folder"]
    assert all(r["resource_id"] for r in members)
    assert {r["resource_id"] for r in members if r["resource_type"] == "folder"} != {
        src.json()["id"], child.json()["id"],
    }, "copied rows name NEW resources, not the sources"


# --------------------------------------------------------------------------- #
# order=newest — the browse walk
# --------------------------------------------------------------------------- #


async def _drain(http, drive_id: str, *, params: dict, limit: int, max_iter: int = 200):
    """Page a feed to exhaustion from an opening request.

    Returns ``(items, pages)``. The bounded iteration count is the same guard
    the permission suite uses: a cursor that fails to advance shows up here as
    a loop, not as a hang."""
    resp = await http.get(
        f"/v0/drives/{drive_id}/changes", params={**params, "limit": limit}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = list(body["items"])
    pages = [body]
    iters = 0
    while body["has_more"]:
        iters += 1
        assert iters < max_iter, "pagination did not terminate (cursor loop)"
        resp = await http.get(
            f"/v0/drives/{drive_id}/changes",
            params={"cursor": body["next_cursor"], "limit": limit},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        pages.append(body)
    return items, pages


async def test_changes_newest_is_the_exact_reverse_of_oldest(http, override_actor):
    """The two walks are the same feed read in opposite directions — same
    events, same count, opposite order. Asserted against the forward walk
    rather than against a hand-written expectation, so the property holds
    whatever the drive's mutations happen to emit."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chgd", "kchgd")
    for i in range(3):
        await _create_folder(
            http, drive["id"], drive["root_folder_id"], f"d{i}", f"kchgd-{i}"
        )

    oldest, _ = await _drain(http, drive["id"], params={"start": "beginning"}, limit=50)
    newest, _ = await _drain(
        http, drive["id"], params={"start": "now", "order": "newest"}, limit=50
    )

    assert len(oldest) >= 4
    assert [i["id"] for i in newest] == [i["id"] for i in reversed(oldest)]


async def test_changes_newest_paginates_down_to_the_floor(http, override_actor):
    """Across page boundaries, not just within one page — a descending walk
    whose successor cursor was built from the wrong end of the page would
    still look right on page one."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chgd2", "kchgd2")
    for i in range(4):
        await _create_folder(
            http, drive["id"], drive["root_folder_id"], f"e{i}", f"kchgd2-{i}"
        )

    oldest, _ = await _drain(http, drive["id"], params={"start": "beginning"}, limit=50)
    newest, pages = await _drain(
        http, drive["id"], params={"start": "now", "order": "newest"}, limit=2
    )

    assert len(pages) > 1, "the fixture must span more than one page"
    assert [i["id"] for i in newest] == [i["id"] for i in reversed(oldest)]
    for page in pages:
        assert len(page["items"]) <= 2
        if page["has_more"]:
            assert len(page["items"]) == 2, "short page while has_more=true"
    assert pages[-1]["has_more"] is False


async def test_changes_newest_drained_cursor_stays_drained(http, override_actor):
    """The one semantic that differs from the sync walk, pinned.

    New events land ABOVE a captured head, so a backward walk can never reach
    them: a drained descending cursor is drained forever, and "check for new
    activity" means capturing the head again. The forward walk's opposite
    behaviour is pinned by `test_changes_drained_cursor_follows_new_commits`,
    and the two tests are the pair that keeps the modes from being confused."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chgd3", "kchgd3")
    _, pages = await _drain(
        http, drive["id"], params={"start": "now", "order": "newest"}, limit=50
    )
    drained = pages[-1]["next_cursor"]
    assert drained

    await _create_folder(
        http, drive["id"], drive["root_folder_id"], "after", "kchgd3-a"
    )

    again = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"cursor": drained}
    )
    assert again.status_code == 200
    assert again.json()["items"] == []
    assert again.json()["has_more"] is False

    # A fresh capture DOES see it, and sees it first.
    fresh = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "now", "order": "newest"},
    )
    assert fresh.status_code == 200
    assert fresh.json()["items"][0]["type"] == "folder.created"


async def test_changes_newest_cursor_replays_the_same_page(http, override_actor):
    """At-least-once holds in reverse: re-presenting a descending cursor
    re-delivers its page rather than advancing past it."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chgd4", "kchgd4")
    for i in range(3):
        await _create_folder(
            http, drive["id"], drive["root_folder_id"], f"f{i}", f"kchgd4-{i}"
        )

    first = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "now", "order": "newest", "limit": 2},
    )
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    p1 = await http.get(f"/v0/drives/{drive['id']}/changes", params={"cursor": cursor})
    p2 = await http.get(f"/v0/drives/{drive['id']}/changes", params={"cursor": cursor})
    assert [i["id"] for i in p1.json()["items"]] == [i["id"] for i in p2.json()["items"]]


async def test_changes_newest_respects_the_type_filter(http, override_actor):
    """The type allow-list is a WHERE predicate in both directions."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chgd5", "kchgd5")
    await _create_folder(http, drive["id"], drive["root_folder_id"], "g", "kchgd5-1")

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "now", "order": "newest", "type": "folder.created"},
    )
    assert resp.status_code == 200
    types = {i["type"] for i in resp.json()["items"]}
    assert types == {"folder.created"}


async def test_changes_order_is_rejected_alongside_a_cursor(http, override_actor):
    """Direction rides in the cursor, so repeating it could only contradict
    it. Rejected rather than silently ignored."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chgd6", "kchgd6")
    opening = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    cursor = opening.json()["next_cursor"]

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"cursor": cursor, "order": "newest"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


async def test_changes_order_newest_rejects_start_beginning(http, override_actor):
    """`beginning` names the far end of a walk that already ends there."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chgd7", "kchgd7")
    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "beginning", "order": "newest"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


async def test_changes_order_rejects_an_unknown_direction(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chgd8", "kchgd8")
    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "now", "order": "descending"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_changes_default_order_is_unchanged(http, override_actor):
    """Omitting `order` is byte-for-byte the forward walk it always was —
    the compatibility promise for every sync consumer already in the field."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chgd9", "kchgd9")
    await _create_folder(http, drive["id"], drive["root_folder_id"], "h", "kchgd9-1")

    implicit, _ = await _drain(
        http, drive["id"], params={"start": "beginning"}, limit=50
    )
    explicit, _ = await _drain(
        http, drive["id"], params={"start": "beginning", "order": "oldest"}, limit=50
    )
    assert [i["id"] for i in implicit] == [i["id"] for i in explicit]


async def test_changes_create_event_carries_resource_name(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "crname", "kcrname")
    f_resp = await _create_folder(
        http, drive["id"], drive["root_folder_id"], "subfolder", "kcrname-f"
    )
    a_resp = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "report.xlsx", "kcrname-a"
    )

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]

    f_created = next(
        c for c in items
        if c["type"] == "folder.created" and c["resource"]["id"] == f_resp.json()["id"]
    )
    assert f_created["data"] == {"name": "subfolder"}

    a_created = next(
        c for c in items
        if c["type"] == "artifact.created" and c["resource"]["id"] == a_resp.json()["id"]
    )
    assert a_created["data"] == {"name": "report.xlsx"}

    v_created = next(
        c for c in items
        if c["type"] == "artifact.version.created" and c["resource"]["id"] == a_resp.json()["id"]
    )
    assert v_created["data"]["name"] == "report.xlsx"
    assert "version_id" in v_created["data"]


async def test_changes_rename_carries_name_and_name_before(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "rnname", "krnname")
    f_resp = await _create_folder(
        http, drive["id"], drive["root_folder_id"], "old-folder", "krnname-f"
    )
    f_id = f_resp.json()["id"]
    f_etag = f_resp.headers["etag"]

    a_resp = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "old-file.txt", "krnname-a"
    )
    a_id = a_resp.json()["id"]
    a_etag = a_resp.headers["etag"]

    # Rename folder
    patch_f = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{f_id}",
        json={"name": "new-folder"},
        headers={"If-Match": f_etag, "Idempotency-Key": "krnname-rf"},
    )
    assert patch_f.status_code == 200, patch_f.text

    # Rename artifact
    patch_a = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{a_id}",
        json={"name": "new-file.txt"},
        headers={"If-Match": a_etag, "Idempotency-Key": "krnname-ra"},
    )
    assert patch_a.status_code == 200, patch_a.text

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    items = resp.json()["items"]

    f_updated = next(
        c for c in items
        if c["type"] == "folder.updated" and c["resource"]["id"] == f_id
    )
    assert f_updated["data"] == {"name": "new-folder", "name_before": "old-folder"}
    assert "previous_parent_id" not in f_updated["data"]

    a_updated = next(
        c for c in items
        if c["type"] == "artifact.updated" and c["resource"]["id"] == a_id
    )
    assert a_updated["data"] == {"name": "new-file.txt", "name_before": "old-file.txt"}
    assert "previous_parent_id" not in a_updated["data"]


async def test_changes_move_carries_previous_parent_id_and_no_name_before(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "mvname", "kmvname")
    p1 = await _create_folder(http, drive["id"], drive["root_folder_id"], "p1", "kmvname-p1")
    p2 = await _create_folder(http, drive["id"], drive["root_folder_id"], "p2", "kmvname-p2")
    p1_id = p1.json()["id"]
    p2_id = p2.json()["id"]

    f_resp = await _create_folder(http, drive["id"], p1_id, "child-folder", "kmvname-f")
    f_id = f_resp.json()["id"]
    f_etag = f_resp.headers["etag"]

    a_resp = await _create_artifact(http, drive["id"], p1_id, "child-file.txt", "kmvname-a")
    a_id = a_resp.json()["id"]
    a_etag = a_resp.headers["etag"]

    # Move folder to p2
    move_f = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{f_id}",
        json={"parent_id": p2_id},
        headers={"If-Match": f_etag, "Idempotency-Key": "kmvname-mf"},
    )
    assert move_f.status_code == 200, move_f.text

    # Move artifact to p2
    move_a = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{a_id}",
        json={"parent_id": p2_id},
        headers={"If-Match": a_etag, "Idempotency-Key": "kmvname-ma"},
    )
    assert move_a.status_code == 200, move_a.text

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    items = resp.json()["items"]

    f_moved = next(
        c for c in items
        if c["type"] == "folder.updated" and c["resource"]["id"] == f_id
    )
    assert f_moved["data"] == {"name": "child-folder", "previous_parent_id": p1_id}
    assert "name_before" not in f_moved["data"]

    a_moved = next(
        c for c in items
        if c["type"] == "artifact.updated" and c["resource"]["id"] == a_id
    )
    assert a_moved["data"] == {"name": "child-file.txt", "previous_parent_id": p1_id}
    assert "name_before" not in a_moved["data"]


async def test_changes_copy_keeps_existing_copy_of_and_name(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cpname", "kcpname")
    a_resp = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "orig.txt", "kcpname-a"
    )
    a_id = a_resp.json()["id"]

    cp_resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{a_id}/copy",
        json={"destination_parent_id": drive["root_folder_id"], "destination_name": "dup.txt"},
        headers={"Idempotency-Key": "kcpname-cp"},
    )
    assert cp_resp.status_code == 201, cp_resp.text
    cp_id = cp_resp.json()["id"]

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    items = resp.json()["items"]

    cp_created = next(
        c for c in items
        if c["type"] == "artifact.created" and c["resource"]["id"] == cp_id
    )
    assert cp_created["data"] == {"name": "dup.txt", "copy_of": a_id}

    cp_ver = next(
        c for c in items
        if c["type"] == "artifact.version.created" and c["resource"]["id"] == cp_id
    )
    assert cp_ver["data"]["name"] == "dup.txt"
    assert "copy_of_version" in cp_ver["data"]
    assert "version_id" in cp_ver["data"]


# ---------------------------------------------------------------------------
# Founding events emitted by create_drive: root folder.created + grant.created
# ---------------------------------------------------------------------------


async def test_create_drive_emits_root_folder_created(http, override_actor):
    """create_drive emits folder.created for the root folder with data={"name": None}."""
    actor = make_actor()
    override_actor(actor)
    drive = await _create_drive(http, "rf-drive", "krf-1")

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]

    root_created = next(
        c for c in items
        if c["type"] == "folder.created" and c["resource"]["id"] == drive["root_folder_id"]
    )
    assert root_created["resource"] == {"type": "folder", "id": drive["root_folder_id"]}
    assert root_created["data"] == {"name": None}
    assert root_created["actor"] == {"type": actor.subject_type, "id": actor.subject}


async def test_create_drive_emits_founding_grants_user_creator(http, override_actor):
    """create_drive with a user creator emits exactly ONE founding grant.created event."""
    user_actor = make_actor(
        subject="tcusr_0000000000000001",
        subject_type="user",
        sponsor=None,
    )
    override_actor(user_actor)
    resp = await http.post(
        "/v0/drives", json={"name": "usr-drive"}, headers={"Idempotency-Key": "kusr-drv"}
    )
    assert resp.status_code == 201, resp.text
    drive = resp.json()

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]

    grant_events = [c for c in items if c["type"] == "grant.created"]
    assert len(grant_events) == 1
    g = grant_events[0]
    assert g["resource"] == {"type": "drive", "id": drive["id"]}
    assert g["data"]["principal_type"] == "user"
    assert g["data"]["principal_id"] == "tcusr_0000000000000001"
    assert g["data"]["role"] == "manager"
    assert g["data"]["expires_at"] is None
    assert g["data"]["grant_id"].startswith("grn_")


async def test_create_drive_emits_two_founding_grants_for_agent_with_sponsor(http, override_actor):
    """An agent creator with a sponsor produces TWO grant.created events,
    matching the shape of an ordinary POST /v0/drives/{id}/grants creation."""
    actor = make_actor(
        subject=AGENT,
        subject_type="agent",
        sponsor=SPONSOR,
        scopes={
            "drives:read", "drives:write", "usage:read",
            "content:read", "content:write", "changes:read",
            "sharing:read", "sharing:write",
        },
    )
    override_actor(actor)
    resp = await http.post(
        "/v0/drives", json={"name": "agent-drive"}, headers={"Idempotency-Key": "kagt-drv"}
    )
    assert resp.status_code == 201, resp.text
    drive = resp.json()

    # Create an ordinary grant to compare payload shape
    ordinary_resp = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent",
            "principal_id": OTHER_AGENT,
            "resource_type": "drive",
            "resource_id": drive["id"],
            "role": "viewer",
        },
        headers={"Idempotency-Key": "kord-grant"},
    )
    assert ordinary_resp.status_code == 201, ordinary_resp.text

    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]

    grant_events = [c for c in items if c["type"] == "grant.created"]
    # 2 founding grants + 1 ordinary grant
    assert len(grant_events) == 3

    agent_grant = next(g for g in grant_events if g["data"]["principal_id"] == AGENT)
    assert agent_grant["resource"] == {"type": "drive", "id": drive["id"]}
    assert agent_grant["data"]["principal_type"] == "agent"
    assert agent_grant["data"]["role"] == "manager"
    assert agent_grant["data"]["expires_at"] is None
    assert agent_grant["data"]["grant_id"].startswith("grn_")

    sponsor_grant = next(g for g in grant_events if g["data"]["principal_id"] == SPONSOR)
    assert sponsor_grant["resource"] == {"type": "drive", "id": drive["id"]}
    assert sponsor_grant["data"]["principal_type"] == "user"
    assert sponsor_grant["data"]["role"] == "manager"
    assert sponsor_grant["data"]["expires_at"] is None
    assert sponsor_grant["data"]["grant_id"].startswith("grn_")

    ordinary_grant = next(g for g in grant_events if g["data"]["principal_id"] == OTHER_AGENT)

    # Shape comparison: founding grant payload keys must exactly match ordinary grant keys
    assert set(agent_grant["data"].keys()) == set(ordinary_grant["data"].keys())
    assert set(sponsor_grant["data"].keys()) == set(ordinary_grant["data"].keys())
    assert set(agent_grant["data"].keys()) == {
        "grant_id", "principal_type", "principal_id", "role", "expires_at",
    }

    # Non-manager filter: a viewer sees folder.created/drive.updated,
    # but grant.* events are filtered out.
    viewer_actor = make_actor(
        subject=OTHER_AGENT,
        subject_type="agent",
        scopes={"changes:read"},
    )
    override_actor(viewer_actor)
    viewer_feed = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"}
    )
    assert viewer_feed.status_code == 200
    viewer_types = {c["type"] for c in viewer_feed.json()["items"]}
    assert "grant.created" not in viewer_types
    assert "folder.created" in viewer_types
    assert "drive.updated" in viewer_types


