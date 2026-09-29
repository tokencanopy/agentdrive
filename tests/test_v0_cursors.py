"""Sealed-cursor pagination across the six resource lists (§6.3, D14).

The six lists (drives, folders, artifacts, versions, grants, shares) carry
their position in HMAC-sealed cursors bound to the collection kind, the
drive/workspace, and a normalized filter fingerprint. These tests pin the
cross-cutting contract:

  * page 2 works for every list (the artifacts case is the regression for the
    live bug where replaying the server's own next_cursor 400'd);
  * a cursor is rejected when its filters changed, when it crosses collections,
    when it crosses drives, when a versions cursor crosses artifacts, and when
    its position was tampered with — always 400 INVALID_CURSOR, never a 500
    or a leak of WHICH part mismatched.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentdrive.api.cursors import clamp_limit
from agentdrive.api.v0_deps import v0_actor
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"

_SCOPES = frozenset({
    "drives:read", "drives:write", "usage:read",
    "content:read", "content:write", "sharing:read", "sharing:write",
    "changes:read",
})


def make_actor(**over) -> V0ActorContext:
    base = dict(
        subject=AGENT,
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=_SCOPES,
        sponsor_id=SPONSOR,
    )
    base.update(over)
    is_agent = base["subject_type"] == "agent"
    return V0ActorContext(
        subject=base["subject"],
        subject_type=base["subject_type"],
        workspace_id=base["workspace_id"],
        membership_id=base["membership_id"],
        token_id=base["token_id"],
        scopes=base["scopes"],
        credential_id="tccred_0000000000000001" if is_agent else None,
        runtime_id="tcrun_0000000000000001" if is_agent else None,
        sponsor_id=base["sponsor_id"],
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
async def _clean_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


def _nid() -> str:
    return uuid.uuid4().hex[:10]


async def _drive(http, name: str) -> dict:
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": f"drv-{_nid()}"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _folder(http, drive_id: str, parent_id: str, name: str) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": f"fld-{_nid()}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _artifact(http, drive_id: str, parent_id: str, name: str) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive_id}/artifacts",
        files={
            "parent_id": (None, parent_id),
            "name": (None, name),
            "content": (name, b"x", "application/octet-stream"),
        },
        headers={"Idempotency-Key": f"art-{_nid()}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _grant(http, drive_id, **body) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive_id}/grants",
        json=body,
        headers={"Idempotency-Key": f"grn-{_nid()}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _share(http, drive_id, **body) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive_id}/shares",
        json=body,
        headers={"Idempotency-Key": f"shr-{_nid()}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _tamper_position(token: str, *, id_value: str) -> str:
    """Unseal-style decode, mutate `p.id`, re-encode WITHOUT re-MACing."""
    body = json.loads(base64.urlsafe_b64decode(token[len("cur_"):] + "==="))
    body["p"]["id"] = id_value
    return "cur_" + base64.urlsafe_b64encode(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Page-2 happy path for all six lists
# ---------------------------------------------------------------------------


async def test_drives_list_page_2(http, override_actor):
    override_actor(make_actor())
    created = {f"d{i}" for i in range(3)}
    for name in created:
        await _drive(http, name)

    p1 = await http.get("/v0/drives", params={"limit": 2})
    assert p1.status_code == 200, p1.text
    names1 = {d["name"] for d in p1.json()["items"]}
    assert len(names1) == 2
    cursor = p1.json()["next_cursor"]
    assert cursor

    p2 = await http.get("/v0/drives", params={"limit": 2, "cursor": cursor})
    assert p2.status_code == 200, p2.text
    names2 = {d["name"] for d in p2.json()["items"]}
    assert names1 & names2 == set()  # no overlap
    assert names1 | names2 == created  # no gap
    assert p2.json()["next_cursor"] is None  # drained


async def test_folders_list_page_2(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "fldpg")
    created = {f"f{i}" for i in range(3)}
    for name in created:
        await _folder(http, drive["id"], drive["root_folder_id"], name)

    p1 = await http.get(f"/v0/drives/{drive['id']}/folders", params={"limit": 2})
    assert p1.status_code == 200, p1.text
    names1 = {f["name"] for f in p1.json()["items"] if f["name"] is not None}
    assert len(names1) == 2
    cursor = p1.json()["next_cursor"]
    assert cursor

    p2 = await http.get(
        f"/v0/drives/{drive['id']}/folders", params={"limit": 2, "cursor": cursor}
    )
    assert p2.status_code == 200, p2.text
    names2 = {f["name"] for f in p2.json()["items"] if f["name"] is not None}
    assert names1 & names2 == set()
    assert names1 | names2 == created  # the root folder (name=None) is excluded
    assert p2.json()["next_cursor"] is None


async def test_artifacts_list_page_2(http, override_actor):
    """Regression: replaying the server's own artifacts next_cursor used to
    400 (created_at passed as a raw string into a timestamptz bind)."""
    override_actor(make_actor())
    drive = await _drive(http, "artpg")
    created = {f"a{i}.txt" for i in range(3)}
    for name in created:
        await _artifact(http, drive["id"], drive["root_folder_id"], name)

    p1 = await http.get(f"/v0/drives/{drive['id']}/artifacts", params={"limit": 2})
    assert p1.status_code == 200, p1.text
    names1 = {a["name"] for a in p1.json()["items"]}
    assert len(names1) == 2
    cursor = p1.json()["next_cursor"]
    assert cursor

    p2 = await http.get(
        f"/v0/drives/{drive['id']}/artifacts", params={"limit": 2, "cursor": cursor}
    )
    assert p2.status_code == 200, p2.text
    names2 = {a["name"] for a in p2.json()["items"]}
    assert names1 & names2 == set()
    assert names1 | names2 == created
    assert p2.json()["next_cursor"] is None


async def test_versions_list_page_2(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "verpg")
    art = await _artifact(http, drive["id"], drive["root_folder_id"], "v.bin")
    for _ in range(3):
        # If-Match is the artifact revision (the append response's ETag is the
        # new VERSION id, not the artifact's), so re-read it each round.
        current = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}")
        append = await http.post(
            f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions",
            files={"content": ("v.bin", b"v2", "application/octet-stream")},
            headers={
                "Idempotency-Key": f"ver-{_nid()}",
                "If-Match": f'"{current.json()["revision"]}"',
            },
        )
        assert append.status_code == 201, append.text

    p1 = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions", params={"limit": 2}
    )
    assert p1.status_code == 200, p1.text
    nums1 = {v["version_number"] for v in p1.json()["items"]}
    assert len(nums1) == 2
    cursor = p1.json()["next_cursor"]
    assert cursor

    p2 = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions",
        params={"limit": 2, "cursor": cursor},
    )
    assert p2.status_code == 200, p2.text
    nums2 = {v["version_number"] for v in p2.json()["items"]}
    assert nums1 & nums2 == set()
    assert nums1 | nums2 == {1, 2, 3, 4}  # original create + 3 appends
    assert p2.json()["next_cursor"] is None


async def test_grants_list_page_2(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "grnpg")
    # The drive create mints bootstrap manager grants for the creator AND the
    # sponsor, so the list is not empty even before we add viewer grants.
    for i in range(3):
        await _grant(
            http, drive["id"],
            principal_type="agent",
            principal_id=f"tcagt_000000000000000{i + 2}",
            resource_type="drive", resource_id=drive["id"], role="viewer",
        )

    p1 = await http.get(f"/v0/drives/{drive['id']}/grants", params={"limit": 2})
    assert p1.status_code == 200, p1.text
    ids1 = {g["id"] for g in p1.json()["items"]}
    assert len(ids1) == 2
    cursor = p1.json()["next_cursor"]
    assert cursor

    p2 = await http.get(
        f"/v0/drives/{drive['id']}/grants", params={"limit": 2, "cursor": cursor}
    )
    assert p2.status_code == 200, p2.text
    ids2 = {g["id"] for g in p2.json()["items"]}
    assert ids1 & ids2 == set()

    # Drain: the remaining grants (bootstrap managers + last viewer) come next.
    p3 = await http.get(
        f"/v0/drives/{drive['id']}/grants", params={"limit": 2, "cursor": p2.json()["next_cursor"]}
    )
    assert p3.status_code == 200, p3.text
    ids3 = {g["id"] for g in p3.json()["items"]}
    assert ids1 & ids3 == set() and ids2 & ids3 == set()
    assert p3.json()["next_cursor"] is None
    assert len(ids1 | ids2 | ids3) == 5  # 2 bootstrap managers + 3 viewers


async def test_shares_list_page_2(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "shrpg")
    for _ in range(3):
        await _share(
            http, drive["id"],
            resource_type="folder", resource_id=drive["root_folder_id"],
        )

    p1 = await http.get(f"/v0/drives/{drive['id']}/shares", params={"limit": 2})
    assert p1.status_code == 200, p1.text
    ids1 = {s["id"] for s in p1.json()["items"]}
    assert len(ids1) == 2
    cursor = p1.json()["next_cursor"]
    assert cursor

    p2 = await http.get(
        f"/v0/drives/{drive['id']}/shares", params={"limit": 2, "cursor": cursor}
    )
    assert p2.status_code == 200, p2.text
    ids2 = {s["id"] for s in p2.json()["items"]}
    assert ids1 & ids2 == set()
    assert len(ids1 | ids2) == 3
    assert p2.json()["next_cursor"] is None


# ---------------------------------------------------------------------------
# Filter mismatch → 400 INVALID_CURSOR
# ---------------------------------------------------------------------------


async def test_folders_cursor_rejected_when_parent_changes(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "fb")
    fa = await _folder(http, drive["id"], drive["root_folder_id"], "A")
    fb = await _folder(http, drive["id"], drive["root_folder_id"], "B")
    for i in range(2):
        await _folder(http, drive["id"], fa["id"], f"a{i}")
        await _folder(http, drive["id"], fb["id"], f"b{i}")

    under_a = await http.get(
        f"/v0/drives/{drive['id']}/folders",
        params={"limit": 1, "parent_id": fa["id"]},
    )
    cursor = under_a.json()["next_cursor"]
    assert cursor

    replayed_under_b = await http.get(
        f"/v0/drives/{drive['id']}/folders",
        params={"limit": 1, "parent_id": fb["id"], "cursor": cursor},
    )
    assert replayed_under_b.status_code == 400
    assert replayed_under_b.json()["error"]["code"] == "INVALID_CURSOR"


async def test_artifacts_cursor_rejected_when_label_changes(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "albl")
    arts = []
    for i in range(2):
        arts.append(await _artifact(http, drive["id"], drive["root_folder_id"], f"l{i}.txt"))
    for art in arts:
        patched = await http.patch(
            f"/v0/drives/{drive['id']}/artifacts/{art['id']}",
            json={"labels": ["x"]},
            headers={"Idempotency-Key": f"lbl-{_nid()}", "If-Match": f'"{art["revision"]}"'},
        )
        assert patched.status_code == 200, patched.text

    with_label = await http.get(
        f"/v0/drives/{drive['id']}/artifacts",
        params={"limit": 1, "label": "x"},
    )
    cursor = with_label.json()["next_cursor"]
    assert cursor

    without_label = await http.get(
        f"/v0/drives/{drive['id']}/artifacts",
        params={"limit": 1, "cursor": cursor},
    )
    assert without_label.status_code == 400
    assert without_label.json()["error"]["code"] == "INVALID_CURSOR"


async def test_grants_cursor_rejected_when_state_swaps(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "glc")
    for i in range(2):
        await _grant(
            http, drive["id"],
            principal_type="agent",
            principal_id=f"tcagt_000000000000000{i + 2}",
            resource_type="drive", resource_id=drive["id"], role="viewer",
        )

    active = await http.get(
        f"/v0/drives/{drive['id']}/grants", params={"limit": 1, "state": "active"}
    )
    cursor = active.json()["next_cursor"]
    assert cursor

    swapped = await http.get(
        f"/v0/drives/{drive['id']}/grants",
        params={"limit": 1, "state": "revoked", "cursor": cursor},
    )
    assert swapped.status_code == 400
    assert swapped.json()["error"]["code"] == "INVALID_CURSOR"


# ---------------------------------------------------------------------------
# Cross-collection / cross-drive / cross-artifact / tampered
# ---------------------------------------------------------------------------


async def test_grants_cursor_rejected_on_shares_list(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "xkind")
    for i in range(2):
        await _grant(
            http, drive["id"],
            principal_type="agent",
            principal_id=f"tcagt_000000000000000{i + 2}",
            resource_type="drive", resource_id=drive["id"], role="viewer",
        )
    await _share(
        http, drive["id"],
        resource_type="folder", resource_id=drive["root_folder_id"],
    )

    grants = await http.get(f"/v0/drives/{drive['id']}/grants", params={"limit": 1})
    grant_cursor = grants.json()["next_cursor"]
    assert grant_cursor

    on_shares = await http.get(
        f"/v0/drives/{drive['id']}/shares",
        params={"limit": 1, "cursor": grant_cursor},
    )
    assert on_shares.status_code == 400
    assert on_shares.json()["error"]["code"] == "INVALID_CURSOR"


async def test_folders_cursor_rejected_on_another_drive(http, override_actor):
    override_actor(make_actor())
    drive_a = await _drive(http, "da")
    drive_b = await _drive(http, "db")
    for i in range(2):
        await _folder(http, drive_a["id"], drive_a["root_folder_id"], f"a{i}")
    await _folder(http, drive_b["id"], drive_b["root_folder_id"], "b0")

    from_a = await http.get(
        f"/v0/drives/{drive_a['id']}/folders", params={"limit": 1}
    )
    cursor = from_a.json()["next_cursor"]
    assert cursor

    on_b = await http.get(
        f"/v0/drives/{drive_b['id']}/folders",
        params={"limit": 1, "cursor": cursor},
    )
    assert on_b.status_code == 400
    assert on_b.json()["error"]["code"] == "INVALID_CURSOR"


async def test_versions_cursor_rejected_on_another_artifact(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "va")
    art_a = await _artifact(http, drive["id"], drive["root_folder_id"], "a.bin")
    art_b = await _artifact(http, drive["id"], drive["root_folder_id"], "b.bin")
    for _ in range(2):
        current = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art_a['id']}")
        append = await http.post(
            f"/v0/drives/{drive['id']}/artifacts/{art_a['id']}/versions",
            files={"content": ("a.bin", b"v2", "application/octet-stream")},
            headers={
                "Idempotency-Key": f"ver-{_nid()}",
                "If-Match": f'"{current.json()["revision"]}"',
            },
        )
        assert append.status_code == 201, append.text

    from_a = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_a['id']}/versions",
        params={"limit": 1},
    )
    cursor = from_a.json()["next_cursor"]
    assert cursor

    on_b = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_b['id']}/versions",
        params={"limit": 1, "cursor": cursor},
    )
    assert on_b.status_code == 400
    assert on_b.json()["error"]["code"] == "INVALID_CURSOR"


async def test_tampered_position_is_rejected(http, override_actor):
    override_actor(make_actor())
    drive = await _drive(http, "tamper")
    for i in range(2):
        await _folder(http, drive["id"], drive["root_folder_id"], f"t{i}")

    p1 = await http.get(
        f"/v0/drives/{drive['id']}/folders", params={"limit": 1}
    )
    cursor = p1.json()["next_cursor"]
    assert cursor

    forged = _tamper_position(cursor, id_value="fld_ffffffffffffffff")
    resp = await http.get(
        f"/v0/drives/{drive['id']}/folders",
        params={"limit": 1, "cursor": forged},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_CURSOR"


# ---------------------------------------------------------------------------
# Limit policy. `clamp_limit` backs every list above; these moved here when the
# pre-reset schemas module that used to host them was deleted, and they are its
# only coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (None, 50),  # omitted → default page size (design §2)
        (1, 1),
        (50, 50),
        (100, 100),
        (0, 1),  # clamp-don't-reject: never a 422/INVALID_LIMIT (audit C-2)
        (-7, 1),
        (101, 100),
        (500, 100),
    ],
)
def test_clamp_limit_defaults_and_bounds(limit, expected):
    assert clamp_limit(limit) == expected


def test_clamp_limit_custom_bounds():
    """Endpoints may carry their own max/default; the clamp respects both."""
    assert clamp_limit(None, max_limit=200, default=25) == 25
    assert clamp_limit(999, max_limit=200, default=25) == 200
    assert clamp_limit(200, max_limit=200, default=25) == 200
    assert clamp_limit(-1, max_limit=200, default=25) == 1
