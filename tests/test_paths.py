"""Paths are derived from the parent chain, not stored.

The day-0 schema has no `path` column on `artifacts` or `folders`: `parent_id`
+ `name` are authoritative (contract §4.1) and the path is a derived
convenience. The drive's root folder has a NULL name; it contributes nothing
to the path, so an artifact at the top of a drive is "report.md" and not
"/report.md".
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.core.paths import artifact_path, folder_path
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"


def make_actor() -> V0ActorContext:
    return V0ActorContext(
        subject=AGENT,
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({
            "drives:read", "drives:write", "usage:read",
            "content:read", "content:write", "sharing:read", "sharing:write",
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


@pytest_asyncio.fixture(autouse=True)
async def _actor(app_with_lifespan):
    app.dependency_overrides[v0_actor] = make_actor
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


def _key() -> str:
    return uuid.uuid4().hex


async def _create_drive(http, name: str = "paths") -> dict:
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": _key()}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest_asyncio.fixture
async def drive(http) -> dict:
    return await _create_drive(http)


async def _create_folder(
    http, drive: dict, *, name: str, parent_id: str | None = None
) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        json={"parent_id": parent_id or drive["root_folder_id"], "name": name},
        headers={"Idempotency-Key": _key()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_artifact(
    http, drive: dict, *, name: str, body: bytes, parent_id: str | None = None
) -> dict:
    parent = (parent_id or drive["root_folder_id"]).encode()
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + parent + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            + name.encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\""
            + name.encode() + b"\"\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            + body + b"\r\n--b--\r\n"
        ),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": _key(),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_artifact_at_the_root_is_just_its_name(http, drive):
    art = await _create_artifact(http, drive, name="report.md", body=b"x")
    async with conn() as c:
        assert await artifact_path(c, art["id"]) == "report.md"


async def test_nested_artifact_joins_its_ancestors(http, drive):
    reports = await _create_folder(http, drive, name="reports")
    q3 = await _create_folder(http, drive, name="q3", parent_id=reports["id"])
    art = await _create_artifact(
        http, drive, name="a.md", parent_id=q3["id"], body=b"x"
    )
    async with conn() as c:
        assert await artifact_path(c, art["id"]) == "reports/q3/a.md"


async def test_unicode_rename_and_move_form_an_unambiguous_display_path(http, drive):
    folder = await _create_folder(http, drive, name="drafts")
    artifact = await _create_artifact(http, drive, name="draft.md", body=b"x")

    renamed_folder = await http.patch(
        f"/v0/drives/{drive['id']}/folders/{folder['id']}",
        json={"name": "研究 数据"},
        headers={"Idempotency-Key": _key(), "If-Match": f'"{folder["revision"]}"'},
    )
    assert renamed_folder.status_code == 200, renamed_folder.text
    moved_artifact = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{artifact['id']}",
        json={"name": "Re\u0301sume\u0301 🚀.md", "parent_id": folder["id"]},
        headers={"Idempotency-Key": _key(), "If-Match": f'"{artifact["revision"]}"'},
    )
    assert moved_artifact.status_code == 200, moved_artifact.text

    async with conn() as c:
        assert await artifact_path(c, artifact["id"]) == "研究 数据/Résumé 🚀.md"


async def test_folder_path_excludes_the_root(http, drive):
    reports = await _create_folder(http, drive, name="reports")
    async with conn() as c:
        assert await folder_path(c, reports["id"]) == "reports"


async def test_nested_folder_joins_its_ancestors(http, drive):
    reports = await _create_folder(http, drive, name="reports")
    q3 = await _create_folder(http, drive, name="q3", parent_id=reports["id"])
    async with conn() as c:
        assert await folder_path(c, q3["id"]) == "reports/q3"


async def test_the_root_folder_itself_is_the_empty_path(http, drive):
    async with conn() as c:
        assert await folder_path(c, drive["root_folder_id"]) == ""


async def test_unknown_ids_return_none(app_with_lifespan):
    async with conn() as c:
        assert await artifact_path(c, "art_ffffffffffffffff") is None
        assert await folder_path(c, "fld_ffffffffffffffff") is None
