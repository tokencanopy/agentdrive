"""D13 unified namespace navigation: entries and whole-path lookup."""

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
    workspace: str = WS_A,
    scopes: set[str] | None = None,
) -> V0ActorContext:
    return V0ActorContext(
        subject=subject,
        subject_type="agent",
        workspace_id=workspace,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(scopes or {
            "drives:read", "drives:write", "content:read", "content:write",
            "sharing:read", "sharing:write", "changes:read", "usage:read",
        }),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id=SPONSOR,
        workspace_role=None,
    )


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


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
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


async def _create_drive(http, name: str = "navigation") -> dict:
    response = await http.post(
        "/v0/drives",
        json={"name": name},
        headers={"Idempotency-Key": f"drive-{name}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _mkdir(http, drive_id: str, parent_id: str, name: str) -> dict:
    response = await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": f"folder-{name}-{parent_id}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_artifact(
    http,
    drive_id: str,
    parent_id: str,
    name: str,
    *,
    content_type: str = "text/plain",
    content: bytes = b"hello",
) -> dict:
    response = await http.post(
        f"/v0/drives/{drive_id}/artifacts",
        data={"parent_id": parent_id, "name": name, "content_type": content_type},
        files={"content": ("payload.bin", content, "application/octet-stream")},
        headers={"Idempotency-Key": f"artifact-{name}-{parent_id}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_entries_requires_parent_and_rejects_unknown_query(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http)

    missing = await http.get(f"/v0/drives/{drive['id']}/entries")
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"

    unknown = await http.get(
        f"/v0/drives/{drive['id']}/entries",
        params={"parent_id": drive["root_folder_id"], "sort": "name"},
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "INVALID_QUERY"


async def test_entries_unifies_direct_children_with_compact_stable_shape(
    http, override_actor
):
    override_actor(make_actor())
    drive = await _create_drive(http)
    root = drive["root_folder_id"]
    folder = await _mkdir(http, drive["id"], root, "drafts")
    artifact = await _create_artifact(
        http,
        drive["id"],
        root,
        "summary.pdf",
        content_type="application/pdf",
        content=b"12345678",
    )
    await _mkdir(http, drive["id"], folder["id"], "nested")

    response = await http.get(
        f"/v0/drives/{drive['id']}/entries",
        params={"parent_id": root},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    assert {entry["id"] for entry in body["entries"]} == {folder["id"], artifact["id"]}

    by_type = {entry["type"]: entry for entry in body["entries"]}
    assert set(by_type["folder"]) == {
        "type", "id", "name", "revision", "updated_at", "state", "deleted_at",
    }
    assert set(by_type["artifact"]) == {
        "type", "id", "name", "revision", "updated_at", "state", "deleted_at",
        "size_bytes", "content_type", "head_version_id",
    }
    assert by_type["artifact"]["size_bytes"] == 8
    assert by_type["artifact"]["content_type"] == "application/pdf"


async def test_entries_filters_paginate_one_bound_cursor_and_include_deleted_shape(
    http, override_actor
):
    override_actor(make_actor())
    drive = await _create_drive(http)
    root = drive["root_folder_id"]
    await _mkdir(http, drive["id"], root, "folder")
    first = await _create_artifact(http, drive["id"], root, "first.txt")
    second = await _create_artifact(
        http, drive["id"], root, "second.json", content_type="application/json"
    )

    page_one = await http.get(
        f"/v0/drives/{drive['id']}/entries",
        params={"parent_id": root, "limit": 1},
    )
    assert page_one.status_code == 200, page_one.text
    cursor = page_one.json()["next_cursor"]
    assert cursor
    page_two = await http.get(
        f"/v0/drives/{drive['id']}/entries",
        params={"parent_id": root, "limit": 1, "cursor": cursor},
    )
    assert page_two.status_code == 200, page_two.text
    assert page_two.json()["entries"][0]["id"] != page_one.json()["entries"][0]["id"]

    rebound = await http.get(
        f"/v0/drives/{drive['id']}/entries",
        params={"parent_id": root, "limit": 1, "cursor": cursor, "type": "artifact"},
    )
    assert rebound.status_code == 400
    assert rebound.json()["error"]["code"] == "INVALID_CURSOR"

    exact = await http.get(
        f"/v0/drives/{drive['id']}/entries",
        params={"parent_id": root, "name": "second.json"},
    )
    assert [entry["id"] for entry in exact.json()["entries"]] == [second["id"]]

    artifact_filter = await http.get(
        f"/v0/drives/{drive['id']}/entries",
        params={"parent_id": root, "content_type": "text/plain"},
    )
    assert [entry["id"] for entry in artifact_filter.json()["entries"]] == [first["id"]]

    deleted = await http.delete(
        f"/v0/drives/{drive['id']}/artifacts/{first['id']}",
        headers={"Idempotency-Key": "delete-first", "If-Match": f'"{first["revision"]}"'},
    )
    assert deleted.status_code == 200, deleted.text
    deleted_page = await http.get(
        f"/v0/drives/{drive['id']}/entries",
        params={"parent_id": root, "state": "deleted"},
    )
    assert [entry["id"] for entry in deleted_page.json()["entries"]] == [first["id"]]
    assert deleted_page.json()["entries"][0]["state"] == "deleted"
    assert deleted_page.json()["entries"][0]["deleted_at"] is not None


async def test_entries_parent_authz_and_workspace_misses_do_not_enumerate(
    http, override_actor
):
    override_actor(make_actor())
    drive = await _create_drive(http)
    root = drive["root_folder_id"]

    override_actor(make_actor(subject=OTHER_AGENT))
    denied = await http.get(
        f"/v0/drives/{drive['id']}/entries", params={"parent_id": root}
    )
    assert denied.status_code == 404

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    foreign = await http.get(
        f"/v0/drives/{drive['id']}/entries", params={"parent_id": root}
    )
    assert foreign.status_code == 404


async def test_lookup_resolves_a_deep_path_and_returns_only_compact_identity(
    http, override_actor
):
    override_actor(make_actor())
    drive = await _create_drive(http)
    reports = await _mkdir(http, drive["id"], drive["root_folder_id"], "reports")
    q3 = await _mkdir(http, drive["id"], reports["id"], "q3")
    artifact = await _create_artifact(http, drive["id"], q3["id"], "summary.pdf")

    response = await http.get(
        f"/v0/drives/{drive['id']}/lookup",
        params={"path": "reports/q3/summary.pdf", "type": "artifact"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "type": "artifact",
        "id": artifact["id"],
        "parent_id": q3["id"],
        "revision": artifact["revision"],
    }


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/reports",
        "reports/",
        "reports//summary.pdf",
        ".",
        "..",
        "reports/../summary.pdf",
        "reports\\summary.pdf",
    ],
)
async def test_lookup_rejects_ambiguous_or_escaping_paths(http, override_actor, path):
    override_actor(make_actor())
    drive = await _create_drive(http, name=f"bad-{len(path)}-{path.count('/')}")
    response = await http.get(
        f"/v0/drives/{drive['id']}/lookup", params={"path": path}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_lookup_all_misses_are_whole_path_404(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http)
    root = drive["root_folder_id"]
    folder = await _mkdir(http, drive["id"], root, "folder")
    artifact = await _create_artifact(http, drive["id"], folder["id"], "target.txt")

    missing = await http.get(
        f"/v0/drives/{drive['id']}/lookup", params={"path": "folder/missing.txt"}
    )
    wrong_type = await http.get(
        f"/v0/drives/{drive['id']}/lookup",
        params={"path": "folder/target.txt", "type": "folder"},
    )
    assert missing.status_code == wrong_type.status_code == 404
    assert missing.json() == wrong_type.json()
    assert "segment" not in missing.text.lower()

    deleted = await http.delete(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}",
        headers={"Idempotency-Key": "delete-target", "If-Match": f'"{artifact["revision"]}"'},
    )
    assert deleted.status_code == 200
    gone = await http.get(
        f"/v0/drives/{drive['id']}/lookup", params={"path": "folder/target.txt"}
    )
    assert gone.status_code == 404
    assert gone.json() == missing.json()

    override_actor(make_actor(subject=OTHER_AGENT))
    unauthorized = await http.get(
        f"/v0/drives/{drive['id']}/lookup", params={"path": "folder"}
    )
    assert unauthorized.status_code == 404
    assert unauthorized.json() == missing.json()

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    foreign = await http.get(
        f"/v0/drives/{drive['id']}/lookup", params={"path": "folder"}
    )
    assert foreign.status_code == 404
    assert foreign.json() == missing.json()
