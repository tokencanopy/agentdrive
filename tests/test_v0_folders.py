"""Folders vertical (slice 5): the 7 folder operations over real Postgres.

Mirrors the drives vertical test shape (§6.2 semantics): mutations require
``Idempotency-Key``; mutation-of-existing ops require ``If-Match``
(428 absent / 412 stale) and bump the folder revision; reads carry the
folder's ETag and honor ``If-None-Match`` → 304; soft-deleted folders are
only reachable through ``?state=deleted|all`` listing; folders are
scoped to the drive's workspace (404 DRIVE_NOT_FOUND / FOLDER_NOT_FOUND).

Folder-specific coverage: the shared artifact/folder sibling collision
domain (folder-vs-folder AND folder-vs-artifact → 409 FOLDER_PATH_CONFLICT),
root immutability (the structural root cannot be patched/deleted), exact
recursive soft-delete cohorts with atomic restore, same-drive synchronous
subtree copy, and cross-drive copy returning 202 + a job Location (job
execution is a later slice's substrate).
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.api.v0_folders import FolderCopyIn, FolderCreateIn, FolderUpdateIn
from agentdrive.app import app
from agentdrive.core import v0_folders as folder_core
from agentdrive.core.v0_folders import InvalidFolderNameError, validate_name
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
    scopes = scopes if scopes is not None else {"content:read", "content:write"}
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
async def _clean_folder_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


async def _create_drive(http, name: str, key: str) -> dict:
    """Bootstrap a drive with a drives-scoped actor (drive creation is the
    drives vertical's op), restoring the caller's folder-scoped override."""
    prev = app.dependency_overrides.get(v0_actor)
    app.dependency_overrides[v0_actor] = lambda: make_actor(
        scopes={"drives:read", "drives:write", "usage:read"}
    )
    try:
        resp = await http.post(
            "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
        )
    finally:
        if prev is not None:
            app.dependency_overrides[v0_actor] = prev
        else:
            app.dependency_overrides.pop(v0_actor, None)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _root_of(drive: dict) -> str:
    return drive["root_folder_id"]


async def _mkdir(http, drive_id: str, parent_id: str, name: str, key: str, **body_over) -> object:
    body = {"parent_id": parent_id, "name": name}
    body.update(body_over)
    return await http.post(
        f"/v0/drives/{drive_id}/folders",
        json=body,
        headers={"Idempotency-Key": key},
    )


async def _insert_artifact(c, drive_id: str, folder_id: str, art_id: str, name: str) -> None:
    """A headless artifact + one version, created directly (the artifact
    vertical is a later slice) so folder ops can exercise the shared
    sibling collision domain and subtree copy over real rows."""
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


# ---------------------------------------------------------------------------
# item-name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Report 2026", "Report 2026"),
        ("Re\u0301sume\u0301.pdf", "Résumé.pdf"),
        ("研究 数据.xlsx", "研究 数据.xlsx"),
        ("Launch 🚀.md", "Launch 🚀.md"),
        ("Supplementary 😀.txt", "Supplementary 😀.txt"),
        ("Family 👩\u200d👩\u200d👧\u200d👦", "Family 👩\u200d👩\u200d👧\u200d👦"),
        ("Persian\u200cname", "Persian\u200cname"),
        ("Report", "Report"),
        ("report", "report"),
        ("x" * 255, "x" * 255),
    ],
)
async def test_item_name_normalizes_and_accepts_unicode(raw: str, canonical: str) -> None:
    assert validate_name(raw) == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ".",
        "..",
        " report",
        "report\u00a0",
        "a/b",
        "a\\b",
        "line\nbreak",
        "line\u2028break",
        "line\u2029break",
        "bom\ufeffname",
        "left\u202ename",
        "left\u2066name",
        "lone-high-\ud800-surrogate",
        "lone-low-\udfff-surrogate",
        "x" * 256,
    ],
)
async def test_item_name_rejects_unsafe_segments(raw: str) -> None:
    with pytest.raises(InvalidFolderNameError):
        validate_name(raw)


async def test_folder_http_models_measure_length_after_normalization() -> None:
    raw = "e\u0301" * 255
    canonical = "é" * 255
    parent_id = "fld_0000000000000001"

    assert FolderCreateIn(parent_id=parent_id, name=raw).name == canonical
    assert FolderUpdateIn(name=raw).name == canonical
    assert (
        FolderCopyIn(destination_parent_id=parent_id, destination_name=raw).destination_name
        == canonical
    )


@pytest.mark.parametrize(
    ("model", "property_name"),
    [
        (FolderCreateIn, "name"),
        (FolderUpdateIn, "name"),
        (FolderCopyIn, "destination_name"),
    ],
)
async def test_folder_http_models_publish_item_name_length_bounds(
    model: type, property_name: str
) -> None:
    property_schema = model.model_json_schema()["properties"][property_name]
    if "anyOf" in property_schema:
        property_schema = next(
            branch for branch in property_schema["anyOf"] if branch.get("type") == "string"
        )

    assert property_schema["minLength"] == 1
    assert property_schema["maxLength"] == 255


@pytest.mark.parametrize(
    "surrogate",
    ["\ud800", "\udfff"],
    ids=["high-surrogate", "low-surrogate"],
)
async def test_folder_create_rejects_lone_surrogates_at_http_boundary(
    http, override_actor, surrogate: str
) -> None:
    override_actor(make_actor())
    drive = await _create_drive(http, "surrogate", f"k-surrogate-drive-{ord(surrogate)}")
    response = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        content=json.dumps(
            {"parent_id": drive["root_folder_id"], "name": f"unsafe-{surrogate}"}
        ),
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": f"k-surrogate-folder-{ord(surrogate)}",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"]["fields"] == [
        {"location": "body.name", "reason": "invalid_value"}
    ]


# ---------------------------------------------------------------------------
# auth + scope boundary
# ---------------------------------------------------------------------------


async def test_folder_ops_require_auth(http):
    resp = await http.get("/v0/drives/drv_00000000000000a1/folders")
    assert resp.status_code == 401
    body = resp.json()
    assert body == {"error": {"code": "AUTHENTICATION_REQUIRED", "message": "missing bearer token"}}
    assert "detail" not in body

    resp = await http.post(
        "/v0/drives/drv_00000000000000a1/folders",
        json={"parent_id": "fld_00000000000000a1", "name": "x"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_folder_ops_enforce_token_scope(http, override_actor):
    drive = await _create_drive(http, "scope", "kscope")
    drive_id = drive["id"]
    override_actor(make_actor(scopes={"content:read"}))
    resp = await _mkdir(http, drive_id, drive["root_folder_id"], "nope", "k1")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_folder_returns_201_and_location(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "c", "kcreate")
    drive_id = drive["id"]
    resp = await _mkdir(http, drive_id, drive["root_folder_id"], "alpha", "kf1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("fld_")
    assert body["drive_id"] == drive_id
    assert body["parent_id"] == drive["root_folder_id"]
    assert body["name"] == "alpha"
    assert body["metadata"] == {}
    assert body["revision"].startswith("rev_")
    assert body["state"] == "active"
    assert body["deleted_at"] is None
    assert resp.headers["location"].endswith(f"/v0/drives/{drive_id}/folders/{body['id']}")
    assert resp.headers["etag"] == f'"{body["revision"]}"'


async def test_folder_create_returns_canonical_unicode_name(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "unicode", "k-unicode-drive")
    created = await _mkdir(
        http,
        drive["id"],
        drive["root_folder_id"],
        "Re\u0301sume\u0301 研究",
        "k-unicode-folder",
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "Résumé 研究"


async def test_create_folder_is_idempotent_and_requires_key(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "i", "kidem")
    first = await _mkdir(http, drive["id"], drive["root_folder_id"], "idem", "key-same")
    second = await _mkdir(http, drive["id"], drive["root_folder_id"], "idem", "key-same")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    async with conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM folders WHERE drive_id=$1 AND name='idem'", drive["id"]
        ) == 1

    no_key = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        json={"parent_id": drive["root_folder_id"], "name": "x"},
    )
    assert no_key.status_code == 400
    assert no_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_create_folder_rejects_key_reuse_for_different_request(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "r", "kreuse")
    await _mkdir(http, drive["id"], drive["root_folder_id"], "one", "key-reuse")
    resp = await _mkdir(http, drive["id"], drive["root_folder_id"], "two", "key-reuse")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_create_folder_sibling_collision_folder_vs_folder(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cc", "kcc")
    first = await _mkdir(http, drive["id"], drive["root_folder_id"], "dupe", "kcc-1")
    assert first.status_code == 201
    second = await _mkdir(http, drive["id"], drive["root_folder_id"], "dupe", "kcc-2")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "FOLDER_PATH_CONFLICT"


async def test_folder_create_collides_after_nfc_normalization(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "nfc", "knfc")
    first = await _mkdir(http, drive["id"], drive["root_folder_id"], "Café", "knfc-1")
    assert first.status_code == 201
    assert first.json()["name"] == "Café"

    second = await _mkdir(
        http, drive["id"], drive["root_folder_id"], "Cafe\u0301", "knfc-2"
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "FOLDER_PATH_CONFLICT"


async def test_create_folder_sibling_collision_folder_vs_artifact(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cc2", "kcc2")
    async with conn() as c:
        await _insert_artifact(
            c, drive["id"], drive["root_folder_id"], "art_00000000000000a1", "file.bin",
        )
    resp = await _mkdir(http, drive["id"], drive["root_folder_id"], "file.bin", "kcc2-1")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "FOLDER_PATH_CONFLICT"


async def test_create_folder_404_for_missing_parent_and_other_workspace(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "m", "km")
    missing = await _mkdir(http, drive["id"], "fld_00000000000000ff", "x", "km-1")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "FOLDER_NOT_FOUND"

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    other = await _mkdir(http, drive["id"], drive["root_folder_id"], "x", "km-2")
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "DRIVE_NOT_FOUND"


# ---------------------------------------------------------------------------
# read + list
# ---------------------------------------------------------------------------


async def test_read_folder_etag_and_not_modified(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "r", "kr")
    created = await _mkdir(http, drive["id"], drive["root_folder_id"], "readme", "kr-1")
    folder_id = created.json()["id"]
    etag = created.headers["etag"]

    resp = await http.get(f"/v0/drives/{drive['id']}/folders/{folder_id}")
    assert resp.status_code == 200
    assert resp.headers["etag"] == etag
    assert resp.json()["id"] == folder_id

    not_mod = await http.get(
        f"/v0/drives/{drive['id']}/folders/{folder_id}", headers={"If-None-Match": etag}
    )
    assert not_mod.status_code == 304


async def test_read_folder_404_for_missing_and_other_workspace(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "iso", "kiso")
    missing = await http.get(f"/v0/drives/{drive['id']}/folders/fld_00000000000000ff")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "FOLDER_NOT_FOUND"

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp = await http.get(f"/v0/drives/{drive['id']}/folders/{drive['root_folder_id']}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DRIVE_NOT_FOUND"


async def test_list_folders_paginates_and_is_workspace_scoped(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "l", "kl")
    for i in range(3):
        await _mkdir(http, drive["id"], drive["root_folder_id"], f"p-{i}", f"kl-{i}")

    resp = await http.get(
        f"/v0/drives/{drive['id']}/folders",
        params={"limit": 2, "parent_id": drive["root_folder_id"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]

    page2 = await http.get(
        f"/v0/drives/{drive['id']}/folders",
        params={"limit": 2, "parent_id": drive["root_folder_id"], "cursor": body["next_cursor"]},
    )
    body2 = page2.json()
    assert len(body2["items"]) == 1
    assert body2["next_cursor"] is None
    ids = [i["id"] for i in body["items"] + body2["items"]]
    assert len(set(ids)) == 3

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    other = await http.get(f"/v0/drives/{drive['id']}/folders")
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "DRIVE_NOT_FOUND"


async def test_list_folders_bad_cursor_uses_top_level_envelope(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "bc", "kbc")
    await _mkdir(http, drive["id"], drive["root_folder_id"], "bc-0", "kbc-0")

    resp = await http.get(
        f"/v0/drives/{drive['id']}/folders", params={"cursor": "not-a-valid-cursor"}
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "detail" not in body
    assert body["error"]["code"] == "INVALID_CURSOR"
    assert body["error"]["message"]


async def test_list_folders_filters_parent_and_state(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "f", "kf")
    parent = await _mkdir(http, drive["id"], drive["root_folder_id"], "sub", "kf-1")
    child = await _mkdir(http, drive["id"], parent.json()["id"], "child", "kf-2")

    root_list = await http.get(
        f"/v0/drives/{drive['id']}/folders", params={"parent_id": drive["root_folder_id"]}
    )
    names = {i["name"] for i in root_list.json()["items"]}
    assert names == {"sub"}

    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{parent.json()['id']}?recursive=true",
        headers={"Idempotency-Key": "kf-3", "If-Match": parent.headers["etag"]},
    )
    assert resp.status_code == 200

    deleted = await http.get(
        f"/v0/drives/{drive['id']}/folders", params={"state": "deleted"}
    )
    items = deleted.json()["items"]
    assert {i["id"] for i in items} == {parent.json()["id"], child.json()["id"]}
    assert all(i["state"] == "deleted" for i in items)


async def test_list_folders_rejects_unknown_query_params(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "q", "kq")
    resp = await http.get(f"/v0/drives/{drive['id']}/folders", params={"bogus": "1"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_QUERY"


async def test_folder_unicode_name_filter_matches_the_canonical_value(
    http, override_actor
):
    override_actor(make_actor())
    drive = await _create_drive(http, "filter", "k-filter-drive")
    created = await _mkdir(
        http, drive["id"], drive["root_folder_id"], "Café notes", "k-filter-folder"
    )
    assert created.status_code == 201, created.text

    result = await http.get(
        f"/v0/drives/{drive['id']}/folders", params={"name": "Cafe\u0301 notes"}
    )
    assert result.status_code == 200, result.text
    assert [item["name"] for item in result.json()["items"]] == ["Café notes"]


async def test_malformed_folder_id_is_400(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "m", "km")
    resp = await http.get(f"/v0/drives/{drive['id']}/folders/not-a-folder")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# patch (rename / move)
# ---------------------------------------------------------------------------


async def test_patch_folder_rename_with_preconditions_and_idempotent_retry(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "p", "kp")
    created = await _mkdir(http, drive["id"], drive["root_folder_id"], "old", "kp-1")
    folder_id = created.json()["id"]
    etag = created.headers["etag"]

    no_match = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{folder_id}",
        json={"name": "new"},
        headers={"Idempotency-Key": "kp-2"},
    )
    assert no_match.status_code == 428
    assert no_match.json()["error"]["code"] == "PRECONDITION_REQUIRED"

    stale = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{folder_id}",
        json={"name": "new"},
        headers={"Idempotency-Key": "kp-3", "If-Match": '"rev_00000000000000ff"'},
    )
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "PRECONDITION_FAILED"

    # A key whose mutation never executed stays usable for the corrected retry.
    retried = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{folder_id}",
        json={"name": "new"},
        headers={"Idempotency-Key": "kp-3", "If-Match": etag},
    )
    assert retried.status_code == 200
    assert retried.json()["name"] == "new"
    assert retried.json()["revision"] != created.json()["revision"]
    assert retried.headers["etag"] == f'"{retried.json()["revision"]}"'


async def test_folder_update_returns_canonical_unicode_name(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "unicode-update", "k-unicode-update-drive")
    created = await _mkdir(
        http, drive["id"], drive["root_folder_id"], "before", "k-unicode-update-create"
    )
    updated = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{created.json()['id']}",
        json={"name": "Re\u0301sume\u0301 研究"},
        headers={
            "Idempotency-Key": "k-unicode-update",
            "If-Match": created.headers["etag"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Résumé 研究"


async def test_patch_folder_direct_core_normalizes_and_collides_canonically(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "nfc-rename", "knfc-rename")
    occupied = await _mkdir(http, drive["id"], drive["root_folder_id"], "Café", "knfc-1")
    candidate = await _mkdir(http, drive["id"], drive["root_folder_id"], "draft", "knfc-2")
    assert occupied.status_code == 201
    assert candidate.status_code == 201

    async with conn() as c, c.transaction():
        renamed = await folder_core.patch_folder(
            c,
            make_actor(),
            drive["id"],
            candidate.json()["id"],
            name="Re\u0301sume\u0301",
            parent_id=None,
            metadata=None,
            changed=frozenset({"name"}),
            if_match=candidate.json()["revision"],
        )
    assert renamed["name"] == "Résumé"

    persisted = await http.get(
        f"/v0/drives/{drive['id']}/folders/{candidate.json()['id']}"
    )
    assert persisted.status_code == 200
    assert persisted.json()["name"] == "Résumé"

    async with conn() as c, c.transaction():
        with pytest.raises(folder_core.FolderNameConflictError):
            await folder_core.patch_folder(
                c,
                make_actor(),
                drive["id"],
                candidate.json()["id"],
                name="Cafe\u0301",
                parent_id=None,
                metadata=None,
                changed=frozenset({"name"}),
                if_match=renamed["revision"],
            )
async def test_patch_folder_move_within_drive(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "mv", "kmv")
    sub = await _mkdir(http, drive["id"], drive["root_folder_id"], "sub", "kmv-1")
    moving = await _mkdir(http, drive["id"], drive["root_folder_id"], "moving", "kmv-2")

    moved = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{moving.json()['id']}",
        json={"parent_id": sub.json()["id"]},
        headers={"Idempotency-Key": "kmv-3", "If-Match": moving.headers["etag"]},
    )
    assert moved.status_code == 200
    assert moved.json()["parent_id"] == sub.json()["id"]
    assert moved.json()["revision"] != moving.json()["revision"]


async def test_patch_folder_into_own_descendant_is_conflict(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cyc", "kcyc")
    a = await _mkdir(http, drive["id"], drive["root_folder_id"], "a", "kcyc-1")
    b = await _mkdir(http, drive["id"], a.json()["id"], "b", "kcyc-2")
    resp = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{a.json()['id']}",
        json={"parent_id": b.json()["id"]},
        headers={"Idempotency-Key": "kcyc-3", "If-Match": a.headers["etag"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


async def test_patch_root_folder_is_immutable(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "rt", "krt")
    root_etag = (await http.get(
        f"/v0/drives/{drive['id']}/folders/{drive['root_folder_id']}"
    )).headers["etag"]
    resp = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{drive['root_folder_id']}",
        json={"name": "renamed-root"},
        headers={"Idempotency-Key": "krt-1", "If-Match": root_etag},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


# ---------------------------------------------------------------------------
# soft-delete / restore
# ---------------------------------------------------------------------------


async def test_delete_folder_recursively_deletes_exact_cohort(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "del", "kdel")
    a = await _mkdir(http, drive["id"], drive["root_folder_id"], "a", "kdel-1")
    b = await _mkdir(http, drive["id"], a.json()["id"], "b", "kdel-2")
    deep = await _mkdir(http, drive["id"], b.json()["id"], "c", "kdel-3")
    async with conn() as c:
        await _insert_artifact(c, drive["id"], a.json()["id"], "art_00000000000000a2", "a.bin")
    sibling = await _mkdir(http, drive["id"], drive["root_folder_id"], "sib", "kdel-4")

    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{a.json()['id']}?recursive=true",
        headers={"Idempotency-Key": "kdel-5", "If-Match": a.headers["etag"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["folder"]["id"] == a.json()["id"]
    assert body["cascade"] == {"folders": 2, "artifacts": 1}
    assert body["folder"]["state"] == "deleted"

    async with conn() as c:
        deleted = await c.fetch(
            "SELECT id FROM folders WHERE drive_id=$1 AND deleted_at IS NOT NULL "
            "ORDER BY id", drive["id"],
        )
        assert {r["id"] for r in deleted} == {
            a.json()["id"], b.json()["id"], deep.json()["id"], body["folder"]["id"]
        }
        art_deleted = await c.fetchval(
            "SELECT deleted_at FROM artifacts WHERE id='art_00000000000000a2'"
        )
        assert art_deleted is not None
        sib_row = await c.fetchval(
            "SELECT deleted_at FROM folders WHERE id=$1", sibling.json()["id"],
        )
        assert sib_row is None

    gone = await http.get(f"/v0/drives/{drive['id']}/folders/{a.json()['id']}")
    assert gone.status_code == 404


async def test_delete_folder_nonempty_requires_recursive(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "rec", "krec")
    a = await _mkdir(http, drive["id"], drive["root_folder_id"], "a", "krec-1")
    b = await _mkdir(http, drive["id"], a.json()["id"], "b", "krec-2")

    refused = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{a.json()['id']}",
        headers={"Idempotency-Key": "krec-5", "If-Match": a.headers["etag"]},
    )
    assert refused.status_code == 409
    body = refused.json()
    assert body["error"]["code"] == "FOLDER_RECURSIVE_REQUIRED"
    assert "detail" not in body
    assert "recursive=true" in body["error"]["message"]

    async with conn() as c:
        still_live = await c.fetchval(
            "SELECT count(*) FROM folders WHERE drive_id=$1 AND deleted_at IS NULL",
            drive["id"],
        )
        assert still_live == 3
        b_live = await c.fetchval(
            "SELECT deleted_at FROM folders WHERE id=$1", b.json()["id"]
        )
        assert b_live is None


async def test_delete_folder_empty_without_recursive_succeeds(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "emp", "kemp")
    leaf = await _mkdir(http, drive["id"], drive["root_folder_id"], "leaf", "kemp-1")

    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{leaf.json()['id']}",
        headers={"Idempotency-Key": "kemp-5", "If-Match": leaf.headers["etag"]},
    )
    assert resp.status_code == 200
    assert resp.json()["cascade"] == {"folders": 0, "artifacts": 0}


async def test_delete_folder_requires_preconditions_and_root_is_immutable(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "pre", "kpre")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "pre", "kpre-1")

    no_match = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{folder.json()['id']}",
        headers={"Idempotency-Key": "kpre-2"},
    )
    assert no_match.status_code == 428
    assert no_match.json()["error"]["code"] == "PRECONDITION_REQUIRED"

    stale = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{folder.json()['id']}",
        headers={"Idempotency-Key": "kpre-3", "If-Match": '"rev_00000000000000ff"'},
    )
    assert stale.status_code == 412

    root = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{drive['root_folder_id']}",
        headers={"Idempotency-Key": "kpre-4", "If-Match": '"rev_00000000000000aa"'},
    )
    assert root.status_code == 409
    assert root.json()["error"]["code"] == "CONFLICT"


async def test_restore_folder_is_atomic(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "res", "kres")
    a = await _mkdir(http, drive["id"], drive["root_folder_id"], "a", "kres-1")
    await _mkdir(http, drive["id"], a.json()["id"], "b", "kres-2")
    async with conn() as c:
        await _insert_artifact(c, drive["id"], a.json()["id"], "art_00000000000000a3", "a.bin")

    deleted = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{a.json()['id']}?recursive=true",
        headers={"Idempotency-Key": "kres-5", "If-Match": a.headers["etag"]},
    )
    assert deleted.status_code == 200

    restore = await http.post(
        f"/v0/drives/{drive['id']}/folders/{a.json()['id']}/restore",
        headers={"Idempotency-Key": "kres-3", "If-Match": deleted.headers["etag"]},
    )
    assert restore.status_code == 200
    body = restore.json()
    assert body["folder"]["id"] == a.json()["id"]
    assert body["cascade"] == {"folders": 1, "artifacts": 1}
    assert body["folder"]["state"] == "active"

    async with conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM folders WHERE drive_id=$1 AND deleted_at IS NOT NULL",
            drive["id"],
        ) == 0
        assert await c.fetchval(
            "SELECT deleted_at FROM artifacts WHERE id='art_00000000000000a3'"
        ) is None

    back = await http.get(f"/v0/drives/{drive['id']}/folders/{a.json()['id']}")
    assert back.status_code == 200


async def test_restore_active_folder_is_conflict(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "rac", "krac")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "rac", "krac-1")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders/{folder.json()['id']}/restore",
        headers={"Idempotency-Key": "krac-2", "If-Match": folder.headers["etag"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


async def test_restore_folder_restores_exact_cohort_wedge(http, override_actor):
    """The wedge: artifact `a.txt` deleted individually, then a NEW `a.txt`
    created under the same folder, then the folder recursively deleted. A
    restore must bring back exactly the recursive delete's cohort — the new
    `a.txt` — and leave the old one deleted (the old bulk sweep un-deleted
    BOTH rows of one (parent_id, name) slot, violating the partial unique
    index into a permanent 500). A later individual restore of the old
    artifact is a 409 (name occupied), never a 500."""
    override_actor(make_actor())
    drive = await _create_drive(http, "wedge", "kwedge")
    f = await _mkdir(http, drive["id"], drive["root_folder_id"], "f", "kwedge-1")
    f_id = f.json()["id"]
    async with conn() as c:
        await _insert_artifact(c, drive["id"], f_id, "art_00000000000000c1", "a.txt")

    old_del = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/artifacts/art_00000000000000c1",
        headers={"Idempotency-Key": "kwedge-2", "If-Match": '"rev_00000000000000a1"'},
    )
    assert old_del.status_code == 200, old_del.text
    old_revision = old_del.json()["revision"]

    async with conn() as c:
        await _insert_artifact(c, drive["id"], f_id, "art_00000000000000c2", "a.txt")

    del_folder = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{f_id}?recursive=true",
        headers={"Idempotency-Key": "kwedge-3", "If-Match": f.headers["etag"]},
    )
    assert del_folder.status_code == 200, del_folder.text

    restore = await http.post(
        f"/v0/drives/{drive['id']}/folders/{f_id}/restore",
        headers={"Idempotency-Key": "kwedge-4", "If-Match": del_folder.headers["etag"]},
    )
    assert restore.status_code == 200, restore.text
    assert restore.json()["cascade"] == {"folders": 0, "artifacts": 1}

    async with conn() as c:
        new_live = await c.fetchval(
            "SELECT deleted_at FROM artifacts WHERE id='art_00000000000000c2'"
        )
        assert new_live is None, "the new a.txt must be live after restore"
        old_still_deleted = await c.fetchval(
            "SELECT deleted_at FROM artifacts WHERE id='art_00000000000000c1'"
        )
        assert old_still_deleted is not None, (
            "the individually-deleted old a.txt must NOT be swept up by the folder restore"
        )

    retry = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/art_00000000000000c1/restore",
        headers={"Idempotency-Key": "kwedge-5", "If-Match": f'"{old_revision}"'},
    )
    assert retry.status_code == 409, retry.text
    assert retry.json()["error"]["code"] == "ARTIFACT_PATH_CONFLICT"


async def test_restore_folder_isolates_independently_deleted_artifact(http, override_actor):
    """Independent-deletion isolation: artifact X deleted individually, then
    the folder recursively deleted. Restore brings back the recursive delete's
    cohort only — X stays deleted, everything else live."""
    override_actor(make_actor())
    drive = await _create_drive(http, "isol", "kisol")
    f = await _mkdir(http, drive["id"], drive["root_folder_id"], "f", "kisol-1")
    f_id = f.json()["id"]
    async with conn() as c:
        await _insert_artifact(c, drive["id"], f_id, "art_00000000000000d1", "x.bin")
        await _insert_artifact(c, drive["id"], f_id, "art_00000000000000d2", "y.bin")

    x_del = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/artifacts/art_00000000000000d1",
        headers={"Idempotency-Key": "kisol-2", "If-Match": '"rev_00000000000000a1"'},
    )
    assert x_del.status_code == 200, x_del.text

    del_folder = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{f_id}?recursive=true",
        headers={"Idempotency-Key": "kisol-3", "If-Match": f.headers["etag"]},
    )
    assert del_folder.status_code == 200, del_folder.text

    restore = await http.post(
        f"/v0/drives/{drive['id']}/folders/{f_id}/restore",
        headers={"Idempotency-Key": "kisol-4", "If-Match": del_folder.headers["etag"]},
    )
    assert restore.status_code == 200, restore.text
    assert restore.json()["cascade"] == {"folders": 0, "artifacts": 1}

    async with conn() as c:
        y_live = await c.fetchval(
            "SELECT deleted_at FROM artifacts WHERE id='art_00000000000000d2'"
        )
        assert y_live is None, "y.bin was part of the recursive delete and must be restored"
        x_still_deleted = await c.fetchval(
            "SELECT deleted_at FROM artifacts WHERE id='art_00000000000000d1'"
        )
        assert x_still_deleted is not None, (
            "x.bin was deleted BEFORE the recursive delete and must stay deleted"
        )


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


async def test_copy_folder_same_drive_is_synchronous(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cp", "kcp")
    src = await _mkdir(http, drive["id"], drive["root_folder_id"], "src", "kcp-1")
    child = await _mkdir(http, drive["id"], src.json()["id"], "child", "kcp-2")
    async with conn() as c:
        await _insert_artifact(c, drive["id"], src.json()["id"], "art_00000000000000a4", "a.bin")
    nested = await _mkdir(http, drive["id"], child.json()["id"], "nested", "kcp-3")

    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders/{src.json()['id']}/copy",
        json={"destination_parent_id": drive["root_folder_id"], "destination_name": "copy"},
        headers={"Idempotency-Key": "kcp-4"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("fld_")
    assert body["id"] != src.json()["id"]
    assert body["name"] == "copy"
    assert body["parent_id"] == drive["root_folder_id"]
    assert resp.headers["location"].endswith(f"/v0/drives/{drive['id']}/folders/{body['id']}")

    async with conn() as c:
        copied_children = await c.fetch(
            "SELECT name, parent_id FROM folders "
            "WHERE drive_id=$1 AND parent_id=$2 ORDER BY name", drive["id"], body["id"],
        )
        assert {r["name"] for r in copied_children} == {"child"}
        child_row = await c.fetchrow(
            "SELECT id FROM folders WHERE drive_id=$1 AND name='child' AND parent_id=$2",
            drive["id"], body["id"],
        )
        nested_row = await c.fetchrow(
            "SELECT name, id FROM folders WHERE drive_id=$1 AND parent_id=$2",
            drive["id"], child_row["id"],
        )
        assert nested_row["name"] == "nested"
        art = await c.fetchrow(
            "SELECT a.name, v.storage_object FROM artifacts a "
            "JOIN artifact_versions v ON v.artifact_id = a.id "
            "WHERE a.drive_id=$1 AND a.parent_id=$2", drive["id"], body["id"],
        )
        assert art is not None
        assert art["name"] == "a.bin"
        assert art["storage_object"] == "cas/source.bin"
        # The source subtree is untouched.
        assert await c.fetchval(
            "SELECT count(*) FROM folders WHERE drive_id=$1 AND id=ANY($2::text[])",
            drive["id"], [src.json()["id"], child.json()["id"], nested.json()["id"]],
        ) == 3


async def test_folder_copy_returns_canonical_unicode_name(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "unicode-copy", "k-unicode-copy-drive")
    source = await _mkdir(
        http, drive["id"], drive["root_folder_id"], "source", "k-unicode-copy-source"
    )
    copied = await http.post(
        f"/v0/drives/{drive['id']}/folders/{source.json()['id']}/copy",
        json={
            "destination_parent_id": drive["root_folder_id"],
            "destination_name": "Re\u0301sume\u0301 copy",
        },
        headers={"Idempotency-Key": "k-unicode-copy"},
    )
    assert copied.status_code == 201, copied.text
    assert copied.json()["name"] == "Résumé copy"


class _RejectFullSubtreeRowLocks:
    """Connection proxy proving an oversized copy stops before full row locking."""

    def __init__(self, delegate):
        self._delegate = delegate

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    async def fetch(self, query, *args):
        if "FOR UPDATE OF f" in query:
            raise AssertionError("oversized copy reached full subtree row materialization")
        return await self._delegate.fetch(query, *args)


async def test_folder_copy_preflight_allows_5000_and_rejects_5001_before_materialization(
    http, override_actor, monkeypatch,
):
    """The synchronous ceiling is exact and its rejecting path stays bounded."""
    override_actor(make_actor())
    drive = await _create_drive(http, "copy-boundary", "k-copy-boundary-drive")
    source = await _mkdir(
        http,
        drive["id"],
        drive["root_folder_id"],
        "source",
        "k-copy-boundary-source",
    )
    source_id = source.json()["id"]

    async with conn() as c:
        await c.execute(
            """
            INSERT INTO folders (id, drive_id, parent_id, name, revision)
            SELECT
              'fld_eeeeeeee' || lpad(to_hex(n), 8, '0'),
              $1,
              $2,
              'member-' || n,
              'rev_eeeeeeee' || lpad(to_hex(n), 8, '0')
            FROM generate_series(1, 4998) AS n
            """,
            drive["id"],
            source_id,
        )
        await _insert_artifact(
            c,
            drive["id"],
            source_id,
            "art_eeeeeeee00001387",
            "member-4999.bin",
        )

        materialized_counts = []

        async def record_materialization(*args, folder_rows, artifact_rows, **kwargs):
            materialized_counts.append((len(folder_rows), len(artifact_rows)))
            return {"id": "fld_eeeeeeee00001389", "name": "copy-at-boundary"}

        monkeypatch.setattr(folder_core, "_materialize_copy", record_materialization)
        async with c.transaction():
            accepted = await folder_core.copy_folder(
                c,
                make_actor(),
                drive["id"],
                source_id,
                destination_drive_id=drive["id"],
                destination_parent_id=drive["root_folder_id"],
                destination_name="copy-at-boundary",
                destination_etag=None,
                idempotency_key="k-copy-boundary-copy",
            )

        assert accepted["name"] == "copy-at-boundary"
        assert materialized_counts == [(4_999, 1)]

        await _insert_artifact(
            c,
            drive["id"],
            source_id,
            "art_eeeeeeee00001388",
            "member-5000.bin",
        )
        assert await folder_core._bounded_subtree_resource_count(
            c,
            drive["id"],
            source_id,
            max_resources=folder_core.MAX_SYNCHRONOUS_COPY_RESOURCES,
        ) == 5_001

        async with c.transaction():
            with pytest.raises(folder_core.SubtreeTooLargeError):
                await folder_core.copy_folder(
                    _RejectFullSubtreeRowLocks(c),
                    make_actor(),
                    drive["id"],
                    source_id,
                    destination_drive_id=drive["id"],
                    destination_parent_id=drive["root_folder_id"],
                    destination_name="copy-at-boundary",
                    destination_etag=None,
                    idempotency_key="k-copy-boundary-copy",
                )

        assert await c.fetchval(
            "SELECT count(*) FROM folders "
            "WHERE drive_id = $1 AND parent_id = $2 AND name = 'copy-at-boundary'",
            drive["id"],
            drive["root_folder_id"],
        ) == 0


async def test_oversized_folder_copy_is_atomic_and_does_not_burn_idempotency_key(
    http, override_actor, monkeypatch,
):
    """A rejected preflight leaves no partial copy and can be retried after correction."""
    monkeypatch.setattr(folder_core, "MAX_SYNCHRONOUS_COPY_RESOURCES", 2)
    override_actor(make_actor())
    drive = await _create_drive(http, "copy-atomic", "k-copy-atomic-drive")
    source = await _mkdir(
        http,
        drive["id"],
        drive["root_folder_id"],
        "source",
        "k-copy-atomic-source",
    )
    first_child = await _mkdir(
        http,
        drive["id"],
        source.json()["id"],
        "first",
        "k-copy-atomic-first",
    )
    await _mkdir(
        http,
        drive["id"],
        source.json()["id"],
        "second",
        "k-copy-atomic-second",
    )
    copy_path = f"/v0/drives/{drive['id']}/folders/{source.json()['id']}/copy"
    body = {
        "destination_parent_id": drive["root_folder_id"],
        "destination_name": "bounded-copy",
    }

    rejected = await http.post(
        copy_path,
        json=body,
        headers={"Idempotency-Key": "k-copy-atomic-copy"},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "SUBTREE_TOO_LARGE"

    async with conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM folders "
            "WHERE drive_id = $1 AND parent_id = $2 AND name = 'bounded-copy'",
            drive["id"],
            drive["root_folder_id"],
        ) == 0
        await c.execute(
            "UPDATE folders SET deleted_at = now() WHERE drive_id = $1 AND id = $2",
            drive["id"],
            first_child.json()["id"],
        )

    retried = await http.post(
        copy_path,
        json=body,
        headers={"Idempotency-Key": "k-copy-atomic-copy"},
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["name"] == "bounded-copy"


async def test_copy_folder_cross_drive_is_rejected(http, override_actor):
    override_actor(make_actor())
    src_drive = await _create_drive(http, "cp-src", "kcs")
    dst_drive = await _create_drive(http, "cp-dst", "kcd")
    src = await _mkdir(http, src_drive["id"], src_drive["root_folder_id"], "src", "kcs-1")

    resp = await http.post(
        f"/v0/drives/{src_drive['id']}/folders/{src.json()['id']}/copy",
        json={
            "destination_drive_id": dst_drive["id"],
            "destination_parent_id": dst_drive["root_folder_id"],
            "destination_name": "copied",
        },
        headers={"Idempotency-Key": "kcs-2"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert "cross-drive" in resp.json()["error"]["message"]

    async with conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM v0_jobs WHERE drive_id=$1", dst_drive["id"]
        ) == 0


async def test_copy_folder_root_is_immutable(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "crt", "kcrt")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders/{drive['root_folder_id']}/copy",
        json={"destination_parent_id": drive["root_folder_id"], "destination_name": "x"},
        headers={"Idempotency-Key": "kcrt-1"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


async def test_copy_folder_into_own_subtree_is_conflict(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cos", "kcos")
    a = await _mkdir(http, drive["id"], drive["root_folder_id"], "a", "kcos-1")
    b = await _mkdir(http, drive["id"], a.json()["id"], "b", "kcos-2")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders/{a.json()['id']}/copy",
        json={"destination_parent_id": b.json()["id"], "destination_name": "a-copy"},
        headers={"Idempotency-Key": "kcos-3"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


async def test_patch_folder_empty_body_is_422(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "peb", "kpeb")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "peb", "kpeb-1")

    resp = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{folder.json()['id']}",
        json={},
        headers={"Idempotency-Key": "kpeb-2", "If-Match": folder.headers["etag"]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_folder_malformed_drive_id_is_400(http, override_actor):
    override_actor(make_actor())
    resp = await http.get("/v0/drives/not-a-drive/folders")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_folder_delete_and_restore_reject_request_body(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "fbd", "kfbd")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "fbd", "kfbd-1")

    delete_body = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{folder.json()['id']}",
        json={"bogus": 1},
        headers={"Idempotency-Key": "kfbd-2", "If-Match": folder.headers["etag"]},
    )
    assert delete_body.status_code == 400
    assert delete_body.json()["error"]["code"] == "INVALID_ARGUMENT"

    restore_body = await http.post(
        f"/v0/drives/{drive['id']}/folders/{folder.json()['id']}/restore",
        json={"bogus": 1},
        headers={"Idempotency-Key": "kfbd-3", "If-Match": folder.headers["etag"]},
    )
    assert restore_body.status_code == 400
    assert restore_body.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_folder_delete_rejects_unknown_query_param(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "fqu", "kfqu")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "fqu", "kfqu-1")

    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/folders/{folder.json()['id']}?bogus=1",
        headers={"Idempotency-Key": "kfqu-2", "If-Match": folder.headers["etag"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_QUERY"


async def test_copy_folder_rejects_bad_destination_parent(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cbp", "kcbp")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "cbp", "kcbp-1")

    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders/{folder.json()['id']}/copy",
        json={"destination_parent_id": "not-a-folder", "destination_name": "x"},
        headers={"Idempotency-Key": "kcbp-2"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Move and sealing authorization (A7 audit findings)
# ---------------------------------------------------------------------------


async def test_folder_move_requires_editor_on_the_destination(http, override_actor):
    """A folder editor cannot relocate a subtree into a folder they hold nothing on.

    `artifacts_update` has enforced this since it shipped, with a comment
    saying why: a principal holding only a direct grant on the thing being
    moved must not be able to move it into a namespace they hold no capability
    on. Folder move resolved and locked the destination but never authorized
    it, so an editor on `src` could reparent it under `dest` while holding
    nothing there.
    """
    override_actor(make_actor(scopes={"content:read", "content:write", "sharing:write"}))
    drive = await _create_drive(http, "fmv", "kfmv-1")
    src = (await _mkdir(http, drive["id"], drive["root_folder_id"], "src", "kfmv-2")).json()
    dest = (await _mkdir(http, drive["id"], drive["root_folder_id"], "dest", "kfmv-3")).json()

    # OTHER_AGENT gets editor on `src` only — nothing on `dest`.
    grant = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent", "principal_id": OTHER_AGENT,
            "resource_type": "folder", "resource_id": src["id"], "role": "editor",
        },
        headers={"Idempotency-Key": "kfmv-4"},
    )
    assert grant.status_code == 201, grant.text

    override_actor(make_actor(subject=OTHER_AGENT))
    moved = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{src['id']}",
        json={"parent_id": dest["id"]},
        headers={"Idempotency-Key": "kfmv-5", "If-Match": f'"{src["revision"]}"'},
    )
    assert moved.status_code == 404, moved.text
    assert moved.json()["error"]["code"] == "NOT_AUTHORIZED"

    # The folder is where it started.
    override_actor(make_actor(scopes={"content:read", "content:write"}))
    read = await http.get(f"/v0/drives/{drive['id']}/folders/{src['id']}")
    assert read.status_code == 200
    assert read.json()["parent_id"] == drive["root_folder_id"]


async def test_folder_move_replay_rechecks_editor_on_destination(http, override_actor):
    """A revoked destination grant must not be replayable around.

    `_run_mutation` skips `execute` on an idempotent replay, so an
    authorization check living there runs exactly once unless a `replay_guard`
    re-runs it. `artifacts_update` has carried that guard since it shipped;
    the folder half did not, so a principal could keep a successful key and
    reuse it after losing editor on the destination.
    """
    owner = make_actor(scopes={"content:read", "content:write", "sharing:write"})
    override_actor(owner)
    drive = await _create_drive(http, "fmvr", "kfr-1")
    src = (await _mkdir(http, drive["id"], drive["root_folder_id"], "src", "kfr-2")).json()
    dest = (await _mkdir(http, drive["id"], drive["root_folder_id"], "dest", "kfr-3")).json()

    grants = {}
    for key, resource_id in (("kfr-4", src["id"]), ("kfr-5", dest["id"])):
        resp = await http.post(
            f"/v0/drives/{drive['id']}/grants",
            json={
                "principal_type": "agent", "principal_id": OTHER_AGENT,
                "resource_type": "folder", "resource_id": resource_id, "role": "editor",
            },
            headers={"Idempotency-Key": key},
        )
        assert resp.status_code == 201, resp.text
        grants[resource_id] = resp.json()

    override_actor(make_actor(subject=OTHER_AGENT))
    first = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{src['id']}",
        json={"parent_id": dest["id"]},
        headers={"Idempotency-Key": "kfr-6", "If-Match": f'"{src["revision"]}"'},
    )
    assert first.status_code == 200, first.text

    dest_grant = grants[dest["id"]]
    override_actor(owner)
    revoked = await http.delete(
        f"/v0/drives/{drive['id']}/grants/{dest_grant['id']}",
        headers={
            "Idempotency-Key": "kfr-7",
            "If-Match": f'"{dest_grant["revision"]}"',
        },
    )
    assert revoked.status_code == 200, revoked.text

    override_actor(make_actor(subject=OTHER_AGENT))
    replay = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{src['id']}",
        json={"parent_id": dest["id"]},
        headers={"Idempotency-Key": "kfr-6", "If-Match": f'"{src["revision"]}"'},
    )
    assert replay.status_code == 404, replay.text
    assert replay.json()["error"]["code"] == "NOT_AUTHORIZED"
