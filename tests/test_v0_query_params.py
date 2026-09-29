"""Consistency batch on /v0 (one commit): E1 sweep + E2-E8 regressions.

E1 — unknown-query-parameter rejection on EVERY operation: every read, list,
and content op returns ``400 INVALID_QUERY`` for ``?bogus=1``, drive DELETE
(which accepts nothing) rejects ``?recursive=true``, and authentication wins
over the query check (no bearer → 401, not 400).

E2-E8 — the rest of the batch, exercised over the same one-drive fixture
set: artifact-copy If-Match, specific error codes, cache headers on private
JSON + 304, strong If-Match comparison, ``details.current_revision`` on 412,
``artifact_revision`` on version append, and ``DriveOut.state``.
"""

from __future__ import annotations

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

_ALL_SCOPES = {
    "drives:read", "drives:write", "usage:read",
    "content:read", "content:write", "sharing:read", "sharing:write",
    "changes:read",
}

_MULTIPART_BOUNDARY = "----ConsistencyBatchBoundaryXyZ"
_CT_MULTIPART = f"multipart/form-data; boundary={_MULTIPART_BOUNDARY}"


def make_actor(
    *,
    subject: str = AGENT,
    workspace: str = WS_A,
    scopes: set[str] | None = None,
    sponsor: str | None = SPONSOR,
) -> V0ActorContext:
    scopes = scopes if scopes is not None else _ALL_SCOPES
    return V0ActorContext(
        subject=subject,
        subject_type="agent",
        workspace_id=workspace,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(scopes),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id=sponsor,
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
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


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


async def _create_drive(http, name: str, key: str) -> dict:
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_artifact(
    http, drive_id: str, parent_id: str, name: str, key: str, content: bytes
) -> object:
    body = _multipart_create({"parent_id": parent_id, "name": name}, content)
    return await http.post(
        f"/v0/drives/{drive_id}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": key},
    )


async def _build_fixtures(http) -> dict[str, str]:
    """One drive with a folder, an artifact (+ head version), a grant, and a
    share — the fixture set every sweep op reads against."""
    drive_resp = await http.post(
        "/v0/drives", json={"name": "batch"}, headers={"Idempotency-Key": "kb-drive"}
    )
    assert drive_resp.status_code == 201, drive_resp.text
    drive = drive_resp.json()
    folder = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        json={"parent_id": drive["root_folder_id"], "name": "batch"},
        headers={"Idempotency-Key": "kb-folder"},
    )
    assert folder.status_code == 201, folder.text

    art_resp = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "hello.txt", "kb-art", b"hello world"
    )
    assert art_resp.status_code == 201, art_resp.text
    artifact = art_resp.json()

    grant = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent", "principal_id": OTHER_AGENT,
            "resource_type": "drive", "resource_id": drive["id"], "role": "viewer",
        },
        headers={"Idempotency-Key": "kb-grant"},
    )
    assert grant.status_code == 201, grant.text

    share = await http.post(
        f"/v0/drives/{drive['id']}/shares",
        json={"resource_type": "artifact", "resource_id": artifact["id"]},
        headers={"Idempotency-Key": "kb-share"},
    )
    assert share.status_code == 201, share.text

    return {
        "drive_id": drive["id"],
        "drive_etag": drive_resp.headers["etag"],
        "root_folder_id": drive["root_folder_id"],
        "folder_id": folder.json()["id"],
        "artifact_id": artifact["id"],
        "artifact_etag": art_resp.headers["etag"],
        "version_id": artifact["head_version_id"],
        "grant_id": grant.json()["id"],
        "share_id": share.json()["id"],
    }


# ---------------------------------------------------------------------------
# E1 — unknown-query-parameter rejection on every read/list/content op
# ---------------------------------------------------------------------------


async def test_unknown_query_param_rejected_on_every_read_list_content_op(
    http, override_actor
):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    d = fx["drive_id"]
    base_urls = [
        "/v0/drives",
        f"/v0/drives/{d}",
        f"/v0/drives/{d}/usage",
        f"/v0/drives/{d}/folders",
        f"/v0/drives/{d}/folders/{fx['folder_id']}",
        f"/v0/drives/{d}/artifacts",
        f"/v0/drives/{d}/artifacts/{fx['artifact_id']}",
        f"/v0/drives/{d}/artifacts/{fx['artifact_id']}/content",
        f"/v0/drives/{d}/artifacts/{fx['artifact_id']}/versions",
        f"/v0/drives/{d}/artifacts/{fx['artifact_id']}/versions/{fx['version_id']}",
        f"/v0/drives/{d}/artifacts/{fx['artifact_id']}/versions/{fx['version_id']}/content",
        f"/v0/drives/{d}/grants",
        f"/v0/drives/{d}/grants/{fx['grant_id']}",
        f"/v0/drives/{d}/shares",
        f"/v0/drives/{d}/shares/{fx['share_id']}",
        f"/v0/drives/{d}/search",
        f"/v0/drives/{d}/changes",
    ]
    for url in base_urls:
        resp = await http.get(url, params={"bogus": "1"})
        assert resp.status_code == 400, (url, resp.status_code, resp.text)
        assert resp.json()["error"]["code"] == "INVALID_QUERY", url


async def test_drive_delete_rejects_unknown_query_param(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    resp = await http.request(
        "DELETE",
        f"/v0/drives/{fx['drive_id']}?recursive=true",
        headers={"Idempotency-Key": "kb-del", "If-Match": fx["artifact_etag"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_QUERY"


async def test_auth_wins_over_unknown_query_param(http):
    """An unauthenticated request is 401, never 400 — the query check runs
    after authentication."""
    resp = await http.get("/v0/drives", params={"bogus": "1"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


# ---------------------------------------------------------------------------
# E2 — artifact copy honors the optional If-Match (source revision)
# ---------------------------------------------------------------------------


async def test_copy_artifact_with_correct_source_if_match(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    resp = await http.post(
        f"/v0/drives/{fx['drive_id']}/artifacts/{fx['artifact_id']}/copy",
        json={
            "destination_parent_id": fx["root_folder_id"],
            "destination_name": "copy-ok.bin",
        },
        headers={
            "Idempotency-Key": "kc-ok",
            "If-Match": fx["artifact_etag"],
        },
    )
    assert resp.status_code == 201, resp.text


async def test_copy_artifact_with_stale_if_match(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    resp = await http.post(
        f"/v0/drives/{fx['drive_id']}/artifacts/{fx['artifact_id']}/copy",
        json={
            "destination_parent_id": fx["root_folder_id"],
            "destination_name": "copy-stale.bin",
        },
        headers={
            "Idempotency-Key": "kc-stale",
            "If-Match": '"rev_00000000000000ff"',
        },
    )
    assert resp.status_code == 412
    assert resp.json()["error"]["code"] == "PRECONDITION_FAILED"


async def test_copy_artifact_without_if_match(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    resp = await http.post(
        f"/v0/drives/{fx['drive_id']}/artifacts/{fx['artifact_id']}/copy",
        json={
            "destination_parent_id": fx["root_folder_id"],
            "destination_name": "copy-none.bin",
        },
        headers={"Idempotency-Key": "kc-none"},
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# E3 — the specific registered error codes
# ---------------------------------------------------------------------------


async def test_checksum_mismatch_on_create_is_409(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    body = _multipart_create(
        {
            "parent_id": fx["root_folder_id"],
            "name": "chk.bin",
            "sha256": "0" * 64,
        },
        b"actual-bytes",
    )
    resp = await http.post(
        f"/v0/drives/{fx['drive_id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kc-chk"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CHECKSUM_MISMATCH"


async def test_checksum_mismatch_on_append_is_409(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    body = _multipart_create({"sha256": "0" * 64}, b"more-bytes")
    resp = await http.post(
        f"/v0/drives/{fx['drive_id']}/artifacts/{fx['artifact_id']}/versions",
        content=body,
        headers={
            "Content-Type": _CT_MULTIPART,
            "Idempotency-Key": "kc-chk-app",
            "If-Match": fx["artifact_etag"],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CHECKSUM_MISMATCH"


async def test_versions_read_with_foreign_version_id_is_version_not_found(
    http, override_actor
):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    other = await _create_artifact(
        http, fx["drive_id"], fx["root_folder_id"], "other.txt", "kb-other", b"other"
    )
    assert other.status_code == 201, other.text

    resp = await http.get(
        f"/v0/drives/{fx['drive_id']}/artifacts/{other.json()['id']}/versions/{fx['version_id']}"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"


# ---------------------------------------------------------------------------
# E4 — cache headers: private JSON everywhere, 304s repeat Cache-Control
# ---------------------------------------------------------------------------


async def test_private_json_200s_carry_cache_control(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    d = fx["drive_id"]

    usage = await http.get(f"/v0/drives/{d}/usage")
    assert usage.status_code == 200
    assert usage.headers["cache-control"] == "private"

    search = await http.get(f"/v0/drives/{d}/search", params={"q": "hello"})
    assert search.status_code == 200
    assert search.headers["cache-control"] == "private"

    changes = await http.get(f"/v0/drives/{d}/changes", params={"start": "beginning"})
    assert changes.status_code == 200
    assert changes.headers["cache-control"] == "private"


async def test_resource_read_304_repeats_cache_control(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    not_modified = await http.get(
        f"/v0/drives/{fx['drive_id']}",
        headers={"If-None-Match": fx["drive_etag"]},
    )
    assert not_modified.status_code == 304
    assert not_modified.headers["cache-control"] == "private"


# ---------------------------------------------------------------------------
# E5 — If-Match uses strong comparison (reject weak validators)
# ---------------------------------------------------------------------------


async def test_if_match_rejects_weak_validator(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    current = fx["artifact_etag"][1:-1]
    resp = await http.patch(
        f"/v0/drives/{fx['drive_id']}",
        json={"name": "renamed-by-weak"},
        headers={"Idempotency-Key": "kw-if", "If-Match": f'W/"{current}"'},
    )
    assert resp.status_code == 412
    assert resp.json()["error"]["code"] == "PRECONDITION_FAILED"


# ---------------------------------------------------------------------------
# E6 — 412 responses carry details.current_revision
# ---------------------------------------------------------------------------


async def test_stale_if_match_412_carries_current_revision(http, override_actor):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    resp = await http.patch(
        f"/v0/drives/{fx['drive_id']}",
        json={"name": "renamed"},
        headers={"Idempotency-Key": "kw-cur", "If-Match": '"rev_00000000000000ff"'},
    )
    assert resp.status_code == 412
    error = resp.json()["error"]
    assert error["code"] == "PRECONDITION_FAILED"
    assert error["details"]["current_revision"] == fx["drive_etag"][1:-1]


# ---------------------------------------------------------------------------
# E7 — version-append/restore responses expose the artifact's new revision
# ---------------------------------------------------------------------------


async def test_append_returns_artifact_revision_usable_as_next_if_match(
    http, override_actor
):
    override_actor(make_actor())
    fx = await _build_fixtures(http)
    body = _multipart_create({}, b"second")
    append = await http.post(
        f"/v0/drives/{fx['drive_id']}/artifacts/{fx['artifact_id']}/versions",
        content=body,
        headers={
            "Content-Type": _CT_MULTIPART,
            "Idempotency-Key": "kw-app-1",
            "If-Match": fx["artifact_etag"],
        },
    )
    assert append.status_code == 201, append.text
    artifact_revision = append.json()["artifact_revision"]
    assert artifact_revision.startswith("rev_")

    body2 = _multipart_create({}, b"third")
    second = await http.post(
        f"/v0/drives/{fx['drive_id']}/artifacts/{fx['artifact_id']}/versions",
        content=body2,
        headers={
            "Content-Type": _CT_MULTIPART,
            "Idempotency-Key": "kw-app-2",
            "If-Match": f'"{artifact_revision}"',
        },
    )
    assert second.status_code == 201, second.text


# ---------------------------------------------------------------------------
# E8 — DriveOut carries state
# ---------------------------------------------------------------------------


async def test_deleted_drive_listing_exposes_state(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "state-drive", "kw-state")
    assert drive["state"] == "active"

    deleted = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}",
        headers={"Idempotency-Key": "kw-state-del", "If-Match": f'"{drive["revision"]}"'},
    )
    assert deleted.status_code == 200

    listing = await http.get("/v0/drives", params={"state": "all"})
    items = listing.json()["items"]
    assert items[0]["id"] == drive["id"]
    assert items[0]["state"] == "deleted"
