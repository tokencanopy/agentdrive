"""Artifacts + versions verticals (slice 6): 13 operations over real Postgres.

Mirrors the drives/folders vertical test shape (§6.2): mutations require
``Idempotency-Key``; mutation-of-existing ops require ``If-Match``; reads
carry ETag and honor ``If-None-Match``; workspace scoping; auth/scope gates.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import asyncpg
import pytest
import pytest_asyncio

from agentdrive.api.v0_artifacts import ArtifactCopyIn, ArtifactCreateIn, ArtifactUpdateIn
from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.core import v0_artifacts as artifact_core
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
    return V0ActorContext(
        subject=subject,
        subject_type=subject_type,
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


async def _mkdir(http, drive_id: str, parent_id: str, name: str, key: str) -> object:
    resp = await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp


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
    body = b"".join(encoded)
    return body


# ── auth/scope boundary ─────────────────────────────────────────────────────


async def test_artifact_ops_require_auth(http):
    resp = await http.get("/v0/drives/drv_00000000000000a1/artifacts")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_artifact_ops_enforce_token_scope(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "scope", "kscope-art")
    override_actor(make_actor(scopes={"content:read"}))
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "nope"},
        b"hello",
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "ka1"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# ── inline upload cap (15 MiB, enforced before buffering) ────────────────────


async def test_oversize_content_length_header_is_413_without_body_read(http, override_actor):
    """The early Content-Length reject: a multipart request whose DECLARED
    length already exceeds the content ceiling (+ framing slack) is 413 on the
    header alone, before any body is buffered. Proven by declaring a huge
    length while sending a tiny valid body — had the handler read the body,
    the small body would parse and create → 201."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cl413", "kcl413")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "cl.txt"}, b"tiny"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body,
        headers={
            "Content-Type": _CT_MULTIPART,
            "Content-Length": str(len(body) + 20 * 1024 * 1024),
            "Idempotency-Key": "kcl413-1",
        },
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "ARTIFACT_TOO_LARGE"


async def test_inline_upload_cap_chunked_abort(http, override_actor, monkeypatch):
    """The chunked cap check: with the ceiling monkeypatched small, an upload
    that crosses it mid-read aborts 413 instead of materializing the whole
    body."""
    from agentdrive.core import v0_artifacts as core

    monkeypatch.setattr(core, "MAX_BUFFERED_UPLOAD_BYTES", 1024)
    override_actor(make_actor())
    drive = await _create_drive(http, "cap", "kcap")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "big.bin"},
        b"x" * 2048,
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body,
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kcap-1"},
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "ARTIFACT_TOO_LARGE"


async def test_inline_upload_at_exact_cap_succeeds(http, override_actor, monkeypatch):
    """Content exactly at the ceiling is accepted — the cap check is strict
    `>`, never `>=`."""
    from agentdrive.core import v0_artifacts as core

    monkeypatch.setattr(core, "MAX_BUFFERED_UPLOAD_BYTES", 1024)
    override_actor(make_actor())
    drive = await _create_drive(http, "exact", "kexact")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "exact.bin"},
        b"x" * 1024,
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body,
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kexact-1"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "exact.bin"


# ── create + read + lifecycle ────────────────────────────────────────────────


async def test_artifact_http_models_measure_length_after_normalization() -> None:
    raw = "e\u0301" * 255
    canonical = "é" * 255
    parent_id = "fld_0000000000000001"

    assert ArtifactCreateIn(parent_id=parent_id, name=raw).name == canonical
    assert ArtifactUpdateIn(name=raw).name == canonical
    assert (
        ArtifactCopyIn(destination_parent_id=parent_id, destination_name=raw).destination_name
        == canonical
    )


@pytest.mark.parametrize(
    ("model", "property_name"),
    [
        (ArtifactCreateIn, "name"),
        (ArtifactUpdateIn, "name"),
        (ArtifactCopyIn, "destination_name"),
    ],
)
async def test_artifact_http_models_publish_item_name_length_bounds(
    model: type, property_name: str
) -> None:
    property_schema = model.model_json_schema()["properties"][property_name]
    if "anyOf" in property_schema:
        property_schema = next(
            branch for branch in property_schema["anyOf"] if branch.get("type") == "string"
        )

    assert property_schema["minLength"] == 1
    assert property_schema["maxLength"] == 255


async def test_create_artifact_multipart_is_idempotent_and_readable(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "ca", "kca")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        data={
            "parent_id": drive["root_folder_id"],
            "name": "hello.txt",
            "content_type": "text/plain",
        },
        files={"content": ("test.bin", b"hello world", "application/octet-stream")},
        headers={"Idempotency-Key": "kca-1"},
    )
    assert resp.status_code == 201, resp.text
    art = resp.json()
    assert art["name"] == "hello.txt"
    assert art["content_type"] == "text/plain"
    assert art["state"] == "active"
    assert art["revision"].startswith("rev_")
    assert art["head_version_id"].startswith("ver_")
    assert "Location" in resp.headers

    resp2 = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        data={
            "parent_id": drive["root_folder_id"],
            "name": "hello.txt",
            "content_type": "text/plain",
        },
        files={"content": ("test.bin", b"hello world", "application/octet-stream")},
        headers={"Idempotency-Key": "kca-1"},
    )
    assert resp2.status_code == 201
    assert resp2.json()["id"] == art["id"]

    get_resp = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}"
    )
    assert get_resp.status_code == 200
    assert get_resp.headers["etag"] == f'"{art["revision"]}"'
    assert get_resp.json()["name"] == "hello.txt"


async def test_multipart_upload_preserves_canonical_unicode_name(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "multipart", "k-multipart-drive")
    response = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {
                "parent_id": drive["root_folder_id"],
                "name": "Re\u0301sume\u0301 2026.pdf",
            },
            b"pdf",
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "k-multipart"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Résumé 2026.pdf"


async def test_create_artifact_key_reuse_for_different_content_is_409(http, override_actor):
    """§7.2: key reuse for a DIFFERENT request conflicts — the request hash
    must fold the multipart fields and content digest, so a same-key retry
    with different bytes can never silently replay the first artifact."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cac", "kcac")

    def _post(content: bytes, key: str, name: str = "hello.txt") -> object:
        return http.post(
            f"/v0/drives/{drive['id']}/artifacts",
            data={
                "parent_id": drive["root_folder_id"],
                "name": name,
                "content_type": "text/plain",
            },
            files={"content": ("test.bin", content, "application/octet-stream")},
            headers={"Idempotency-Key": key},
        )

    first = await _post(b"payload one", "kcac-1")
    assert first.status_code == 201, first.text

    different = await _post(b"payload two", "kcac-1")
    assert different.status_code == 409, different.text
    assert different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # A different key with the same content still succeeds (fresh mutation).
    fresh = await _post(b"payload two", "kcac-2", name="fresh.txt")
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["id"] != first.json()["id"]


async def test_create_artifact_json_post_is_rejected(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "json", "kjson")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        json={"parent_id": drive["root_folder_id"], "name": "nope"},
        headers={"Idempotency-Key": "kjson-1"},
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


async def test_create_artifact_validation_errors_are_422(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cval", "kcval")

    def _post(fields: dict[str, str], key: str):
        return http.post(
            f"/v0/drives/{drive['id']}/artifacts",
            content=_multipart_create(fields, b"x"),
            headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": key},
        )

    bad_parent = await _post({"parent_id": "not-a-folder", "name": "a"}, "kcval-1")
    assert bad_parent.status_code == 422
    assert bad_parent.json()["error"]["code"] == "VALIDATION_ERROR"
    assert bad_parent.json()["error"]["details"]["fields"][0]["location"] == "parent_id"

    missing_name = await _post({"parent_id": drive["root_folder_id"]}, "kcval-2")
    assert missing_name.status_code == 422
    assert missing_name.json()["error"]["code"] == "VALIDATION_ERROR"

    bad_name = await _post(
        {"parent_id": drive["root_folder_id"], "name": "../evil"}, "kcval-3"
    )
    assert bad_name.status_code == 422

    bad_metadata = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "a", "metadata": "{oops"},
            b"x",
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kcval-4"},
    )
    assert bad_metadata.status_code == 422
    assert bad_metadata.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_update_artifact_validation_errors_are_422(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "uval", "kuval")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "uval.txt"}, b"x"
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kuval-1"},
    )
    art_id = resp.json()["id"]

    unknown_field = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        json={"bogus": 1},
        headers={"Idempotency-Key": "kuval-2", "If-Match": resp.headers["etag"]},
    )
    assert unknown_field.status_code == 422
    assert unknown_field.json()["error"]["code"] == "VALIDATION_ERROR"
    assert unknown_field.json()["error"]["details"]["fields"][0]["reason"] == "unknown_field"

    no_fields = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        json={},
        headers={"Idempotency-Key": "kuval-3", "If-Match": resp.headers["etag"]},
    )
    assert no_fields.status_code == 422

    bad_parent = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        json={"parent_id": "nope"},
        headers={"Idempotency-Key": "kuval-4", "If-Match": resp.headers["etag"]},
    )
    assert bad_parent.status_code == 422

    bad_name = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        json={"name": "../bad"},
        headers={"Idempotency-Key": "kuval-5", "If-Match": resp.headers["etag"]},
    )
    assert bad_name.status_code == 422

    bad_labels = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        json={"labels": "not-a-list"},
        headers={"Idempotency-Key": "kuval-6", "If-Match": resp.headers["etag"]},
    )
    assert bad_labels.status_code == 422


async def test_copy_artifact_validation_errors_are_422(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cpval", "kcpval")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "cpval.bin"}, b"x"
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kcpval-1"},
    )
    art_id = resp.json()["id"]

    missing_dest = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/copy",
        json={},
        headers={"Idempotency-Key": "kcpval-2"},
    )
    assert missing_dest.status_code == 422
    assert missing_dest.json()["error"]["code"] == "VALIDATION_ERROR"

    bad_parent = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/copy",
        json={"destination_parent_id": "nope", "destination_name": "c"},
        headers={"Idempotency-Key": "kcpval-3"},
    )
    assert bad_parent.status_code == 422

    unknown_field = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/copy",
        json={
            "destination_parent_id": drive["root_folder_id"],
            "destination_name": "c",
            "bogus": 1,
        },
        headers={"Idempotency-Key": "kcpval-4"},
    )
    assert unknown_field.status_code == 422
    assert unknown_field.json()["error"]["details"]["fields"][0]["reason"] == "unknown_field"


async def test_artifact_malformed_path_ids_are_400(http, override_actor):
    override_actor(make_actor())
    resp = await http.get("/v0/drives/not-a-drive/artifacts")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"

    drive = await _create_drive(http, "mal", "kmal")
    bad_art = await http.get(f"/v0/drives/{drive['id']}/artifacts/not-an-artifact")
    assert bad_art.status_code == 400
    assert bad_art.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_list_artifacts_rejects_bad_query_params(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "lq", "klq")

    unknown = await http.get(
        f"/v0/drives/{drive['id']}/artifacts", params={"bogus": "x"}
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "INVALID_QUERY"

    bad_state = await http.get(
        f"/v0/drives/{drive['id']}/artifacts", params={"state": "nope"}
    )
    assert bad_state.status_code == 400
    assert bad_state.json()["error"]["code"] == "INVALID_ARGUMENT"

    bad_parent = await http.get(
        f"/v0/drives/{drive['id']}/artifacts", params={"parent_id": "not-a-folder"}
    )
    assert bad_parent.status_code == 400
    assert bad_parent.json()["error"]["code"] == "INVALID_ARGUMENT"

    bad_name = await http.get(
        f"/v0/drives/{drive['id']}/artifacts", params={"name": "../evil"}
    )
    assert bad_name.status_code == 400


async def test_delete_and_restore_reject_request_body(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "nbd", "knbd")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "nbd.txt"}, b"x"
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "knbd-1"},
    )
    art_id = resp.json()["id"]

    delete_body = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        json={"bogus": 1},
        headers={"Idempotency-Key": "knbd-2", "If-Match": resp.headers["etag"]},
    )
    assert delete_body.status_code == 400
    assert delete_body.json()["error"]["code"] == "INVALID_ARGUMENT"

    restore_body = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/restore",
        json={"bogus": 1},
        headers={"Idempotency-Key": "knbd-3", "If-Match": resp.headers["etag"]},
    )
    assert restore_body.status_code == 400
    assert restore_body.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_create_artifact_requires_content_part(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "ncp", "kncp")
    body = (
        f"--{_MULTIPART_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="parent_id"\r\n\r\n'
        f"{drive['root_folder_id']}\r\n"
        f"--{_MULTIPART_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="name"\r\n\r\n'
        f"nocontent.txt\r\n"
        f"--{_MULTIPART_BOUNDARY}--\r\n"
    ).encode()
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body,
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kncp-1"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_artifact_content_404_for_missing_artifact(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "c404", "kc404")
    resp = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/art_00000000000000aa/content"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"


async def test_restore_active_artifact_is_conflict(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "rac", "krac")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "rac.txt"}, b"x"
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "krac-1"},
    )
    art_id = resp.json()["id"]

    restore = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/restore",
        headers={"Idempotency-Key": "krac-2", "If-Match": resp.headers["etag"]},
    )
    assert restore.status_code == 409
    assert restore.json()["error"]["code"] == "CONFLICT"


async def test_create_artifact_rejects_wrong_checksum(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chk", "kchk")
    body = _multipart_create(
        {
            "parent_id": drive["root_folder_id"],
            "name": "chk.txt",
            "sha256": "0" * 64,
        },
        b"actual-bytes",
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kchk-1"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CHECKSUM_MISMATCH"


async def test_read_artifact_if_none_match_304(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "et", "ket")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "et.txt"}, b"data"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "ket-1"},
    )
    etag = resp.headers["etag"]
    art_id = resp.json()["id"]
    resp2 = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        headers={"If-None-Match": etag},
    )
    assert resp2.status_code == 304

    weak = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        headers={"If-None-Match": f"W/{etag}"},
    )
    assert weak.status_code == 304

    multi = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        headers={"If-None-Match": '"rev_00000000000000ff", ' + etag},
    )
    assert multi.status_code == 304

    star = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        headers={"If-None-Match": "*"},
    )
    assert star.status_code == 304

    stale = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        headers={"If-None-Match": '"rev_00000000000000ff"'},
    )
    assert stale.status_code == 200


async def test_read_artifact_cross_workspace_is_404(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cw", "kcw")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "cw.txt"}, b"x"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kcw-1"},
    )
    art_id = resp.json()["id"]
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp2 = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art_id}")
    assert resp2.status_code == 404
    assert resp2.json()["error"]["code"] == "DRIVE_NOT_FOUND"


# ── sibling collision ────────────────────────────────────────────────────────


async def test_artifact_name_collision_with_folder(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "nc", "knc")
    await _mkdir(http, drive["id"], drive["root_folder_id"], "shared", "knc-1")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "shared"}, b"x"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "knc-2"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ARTIFACT_PATH_CONFLICT"


async def test_artifact_name_collision_with_artifact(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "aa", "kaa")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "dup"}, b"first"
    )
    resp1 = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kaa-1"},
    )
    assert resp1.status_code == 201
    resp2 = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kaa-2"},
    )
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "ARTIFACT_PATH_CONFLICT"


async def test_artifact_and_folder_names_remain_case_sensitive(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "case", "kcase")
    assert (
        await _mkdir(http, drive["id"], drive["root_folder_id"], "Report", "kcase-1")
    ).status_code == 201

    artifact = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "report"}, b"content"
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kcase-2"},
    )
    assert artifact.status_code == 201, artifact.text
    assert artifact.json()["name"] == "report"


async def test_create_accepts_metadata_sent_as_a_json_part(http, override_actor):
    """OpenAPI 3 serializes an object-typed multipart property as its OWN part
    carrying `Content-Type: application/json`, and generated clients do exactly
    that -- the TypeScript SDK sends `metadata` as a JSON blob rather than a
    form field. The parser kept only `isinstance(value, str)`, so that part was
    dropped and `metadata` fell back to its `"{}"` default. Silent, because an
    empty object is a VALID value: an artifact created with metadata came back
    with none and nothing raised.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "meta-part", "k-meta-part-drive")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        data={"parent_id": drive["root_folder_id"], "name": "meta.txt"},
        files={
            "content": ("c.bin", b"body", "application/octet-stream"),
            # A FILENAME is what makes Starlette treat a part as an upload
            # rather than a form field -- which is exactly how the SDK's
            # `new Blob([...], {type:"application/json"})` arrives on the wire.
            # Sending this with `None` as the filename would quietly become a
            # plain field and exercise nothing.
            "metadata": (
                "blob",
                b'{"stage":"v1","owner":"agent"}',
                "application/json",
            ),
        },
        headers={"Idempotency-Key": "k-meta-part-1"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["metadata"] == {"stage": "v1", "owner": "agent"}

    # Confirmed by an independent read, not just the create response.
    read = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{resp.json()['id']}"
    )
    assert read.status_code == 200, read.text
    assert read.json()["metadata"] == {"stage": "v1", "owner": "agent"}


async def test_create_still_accepts_metadata_as_a_plain_field(http, override_actor):
    """The form-field spelling keeps working -- the fix widens what is
    accepted, it does not move the goalposts for existing callers."""
    override_actor(make_actor())
    drive = await _create_drive(http, "meta-field", "k-meta-field-drive")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        data={
            "parent_id": drive["root_folder_id"],
            "name": "meta2.txt",
            "metadata": '{"stage":"field"}',
        },
        files={"content": ("c.bin", b"body", "application/octet-stream")},
        headers={"Idempotency-Key": "k-meta-field-1"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["metadata"] == {"stage": "field"}


# ── update (move/rename/metadata) ────────────────────────────────────────────


async def test_update_artifact_move_and_rename(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "up", "kup")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "src.txt"}, b"hello"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kup-1"},
    )
    art = resp.json()
    etag = resp.headers["etag"]

    sub = await _mkdir(http, drive["id"], drive["root_folder_id"], "sub", "kup-2")
    patch = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}",
        json={"parent_id": sub.json()["id"], "name": "renamed.txt", "metadata": {"a": 1}},
        headers={"Idempotency-Key": "kup-3", "If-Match": etag},
    )
    assert patch.status_code == 200
    updated = patch.json()
    assert updated["name"] == "renamed.txt"
    assert updated["parent_id"] == sub.json()["id"]
    assert updated["metadata"] == {"a": 1}
    assert updated["revision"] != art["revision"]
    assert patch.headers["etag"] != etag


async def test_artifact_update_returns_canonical_unicode_name(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "unicode-update", "k-unicode-update-drive")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "before.md"}, b"x"
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "k-art-create"},
    )
    updated = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{created.json()['id']}",
        json={"name": "Re\u0301sume\u0301 🚀.md"},
        headers={"If-Match": created.headers["etag"], "Idempotency-Key": "k-art-update"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Résumé 🚀.md"


async def test_update_artifact_direct_core_normalizes_and_collides_canonically(
    http, override_actor
):
    override_actor(make_actor())
    drive = await _create_drive(http, "nfc-rename", "knfc-art")
    occupied = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "Café.txt"}, b"occupied"
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "knfc-art-1"},
    )
    candidate = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "draft.txt"}, b"candidate"
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "knfc-art-2"},
    )
    assert occupied.status_code == 201, occupied.text
    assert candidate.status_code == 201, candidate.text

    async with conn() as c, c.transaction():
        renamed = await artifact_core.update_artifact(
            c,
            make_actor(),
            drive["id"],
            candidate.json()["id"],
            name="Re\u0301sume\u0301.txt",
            parent_id=None,
            metadata=None,
            labels=None,
            changed=frozenset({"name"}),
            if_match=candidate.json()["revision"],
        )
    assert renamed["name"] == "Résumé.txt"

    persisted = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{candidate.json()['id']}"
    )
    assert persisted.status_code == 200
    assert persisted.json()["name"] == "Résumé.txt"

    async with conn() as c, c.transaction():
        with pytest.raises(artifact_core.ArtifactNameConflictError):
            await artifact_core.update_artifact(
                c,
                make_actor(),
                drive["id"],
                candidate.json()["id"],
                name="Cafe\u0301.txt",
                parent_id=None,
                metadata=None,
                labels=None,
                changed=frozenset({"name"}),
                if_match=renamed["revision"],
            )


async def test_update_artifact_precondition_428_412(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "pre", "kpre-art")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "pre.txt"}, b"x"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kpre-1"},
    )
    art_id = resp.json()["id"]

    no_match = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        json={"name": "x"},
        headers={"Idempotency-Key": "kpre-2"},
    )
    assert no_match.status_code == 428

    stale = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}",
        json={"name": "x"},
        headers={"Idempotency-Key": "kpre-3", "If-Match": '"rev_00000000000000ff"'},
    )
    assert stale.status_code == 412


# ── soft-delete + restore ────────────────────────────────────────────────────


async def test_delete_and_restore_artifact(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "del", "kdel-art")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "del.txt"}, b"hi"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kdel-1"},
    )
    art = resp.json()
    etag = resp.headers["etag"]

    del_resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}",
        headers={"Idempotency-Key": "kdel-2", "If-Match": etag},
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["state"] == "deleted"

    gone = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}")
    assert gone.status_code == 404

    restore = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/restore",
        headers={"Idempotency-Key": "kdel-3", "If-Match": del_resp.headers["etag"]},
    )
    assert restore.status_code == 200
    assert restore.json()["state"] == "active"

    back = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}")
    assert back.status_code == 200


# ── list ─────────────────────────────────────────────────────────────────────


async def test_list_artifacts_paginates_and_filters(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "ls", "kls")
    for i in range(3):
        body = _multipart_create(
            {"parent_id": drive["root_folder_id"], "name": f"f-{i}.txt"}, b"x"
        )
        resp = await http.post(
            f"/v0/drives/{drive['id']}/artifacts",
            content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": f"kls-{i}"},
        )
        assert resp.status_code == 201

    page = await http.get(
        f"/v0/drives/{drive['id']}/artifacts", params={"limit": 2}
    )
    assert page.status_code == 200
    body = page.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    other = await http.get(f"/v0/drives/{drive['id']}/artifacts")
    assert other.status_code == 404


async def test_unicode_name_filter_matches_the_canonical_value(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "unicode-filter", "k-unicode-filter-drive")
    first_parent = await _mkdir(
        http, drive["id"], drive["root_folder_id"], "first", "k-filter-first-parent"
    )
    second_parent = await _mkdir(
        http, drive["id"], drive["root_folder_id"], "second", "k-filter-second-parent"
    )
    for index, parent in enumerate((first_parent, second_parent), start=1):
        created = await http.post(
            f"/v0/drives/{drive['id']}/artifacts",
            content=_multipart_create(
                {"parent_id": parent.json()["id"], "name": "Café notes.md"}, b"x"
            ),
            headers={
                "Content-Type": _CT_MULTIPART,
                "Idempotency-Key": f"k-filter-create-{index}",
            },
        )
        assert created.status_code == 201, created.text

    first_page = await http.get(
        f"/v0/drives/{drive['id']}/artifacts",
        params={"name": "Cafe\u0301 notes.md", "limit": 1},
    )
    assert first_page.status_code == 200, first_page.text
    assert [item["name"] for item in first_page.json()["items"]] == ["Café notes.md"]
    assert first_page.json()["next_cursor"] is not None

    second_page = await http.get(
        f"/v0/drives/{drive['id']}/artifacts",
        params={
            "name": "Café notes.md",
            "limit": 1,
            "cursor": first_page.json()["next_cursor"],
        },
    )
    assert second_page.status_code == 200, second_page.text
    assert [item["name"] for item in second_page.json()["items"]] == ["Café notes.md"]


# ── content read ─────────────────────────────────────────────────────────────


async def test_artifact_content_stream(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "ct", "kct")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "data.bin"}, b"stream me"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kct-1"},
    )
    art_id = resp.json()["id"]
    version_id = resp.json()["head_version_id"]

    content_resp = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/content"
    )
    assert content_resp.status_code == 200
    assert content_resp.content == b"stream me"

    etag = f'"{version_id}"'
    not_modified = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/content",
        headers={"If-None-Match": etag},
    )
    assert not_modified.status_code == 304


# ── copy ─────────────────────────────────────────────────────────────────────


async def test_copy_artifact_same_drive(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "cp", "kcp")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "orig.bin"}, b"copy-me"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kcp-1"},
    )
    art_id = resp.json()["id"]

    copy_resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/copy",
        json={
            "destination_parent_id": drive["root_folder_id"],
            "destination_name": "copied.bin",
        },
        headers={"Idempotency-Key": "kcp-2"},
    )
    assert copy_resp.status_code == 201
    copy = copy_resp.json()
    assert copy["name"] == "copied.bin"
    assert copy["id"] != art_id
    assert "Location" in copy_resp.headers


async def test_artifact_copy_returns_canonical_unicode_name(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "unicode-copy", "k-unicode-copy-drive")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart_create(
            {"parent_id": drive["root_folder_id"], "name": "source.bin"}, b"copy-me"
        ),
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "k-copy-create"},
    )
    copied = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{created.json()['id']}/copy",
        json={
            "destination_parent_id": drive["root_folder_id"],
            "destination_name": "Re\u0301sume\u0301 copy.bin",
        },
        headers={"Idempotency-Key": "k-copy-artifact"},
    )
    assert copied.status_code == 201, copied.text
    assert copied.json()["name"] == "Résumé copy.bin"


async def test_copy_artifact_carries_content_preview_and_matches_search(
    http, override_actor,
):
    """Fix 4: a same-drive copy must carry the source's content_preview (not
    NULL) so drive search by body text returns BOTH the source and the copy."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cprev", "kcprev")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "report.txt"},
        b"needle report body",
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kcprev-1"},
    )
    assert resp.status_code == 201, resp.text
    art = resp.json()
    assert art["content_preview"] == "needle report body"

    copy_resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/copy",
        json={
            "destination_parent_id": drive["root_folder_id"],
            "destination_name": "report-copy.txt",
        },
        headers={"Idempotency-Key": "kcprev-2"},
    )
    assert copy_resp.status_code == 201, copy_resp.text
    copy = copy_resp.json()
    assert copy["content_preview"] == art["content_preview"], (
        "the copy's preview must match the source's, not be NULL"
    )

    search = await http.get(f"/v0/drives/{drive['id']}/search", params={"q": "needle"})
    assert search.status_code == 200, search.text
    names = {item["name"] for item in search.json()["items"]}
    assert {"report.txt", "report-copy.txt"} <= names, (
        "both source and copy must match drive search by body text"
    )


async def test_copy_artifact_with_older_version_reflects_that_versions_preview(
    http, override_actor,
):
    """Fix 4: copying a specific non-head version must derive the preview from
    THAT version's bytes, not the source artifact's current head preview."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cverprev", "kcverprev")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "v.txt"},
        b"first needle body",
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kcverprev-1"},
    )
    assert resp.status_code == 201, resp.text
    art = resp.json()
    art_id = art["id"]
    art_etag = resp.headers["etag"]

    append = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions",
        content=_multipart_create({}, b"second needle body"),
        headers={
            "Content-Type": _CT_MULTIPART,
            "Idempotency-Key": "kcverprev-2",
            "If-Match": art_etag,
        },
    )
    assert append.status_code == 201, append.text

    versions = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions")
    items = versions.json()["items"]
    old_ver = items[1]  # newest-first: ordinal 1 is the original version

    copy_resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/copy",
        json={
            "destination_parent_id": drive["root_folder_id"],
            "destination_name": "old-version.txt",
            "version_id": old_ver["id"],
        },
        headers={"Idempotency-Key": "kcverprev-3"},
    )
    assert copy_resp.status_code == 201, copy_resp.text
    copy = copy_resp.json()
    assert copy["content_preview"] == "first needle body", (
        "the preview must reflect the copied version's bytes, not the head's"
    )


async def test_copy_artifact_cross_drive_is_rejected(http, override_actor):
    override_actor(make_actor())
    drive1 = await _create_drive(http, "cd1", "kcd1")
    body = _multipart_create(
        {"parent_id": drive1["root_folder_id"], "name": "cross.bin"}, b"cross"
    )
    resp = await http.post(
        f"/v0/drives/{drive1['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kcd-1"},
    )
    art_id = resp.json()["id"]

    drive2 = await _create_drive(http, "cd2", "kcd2")
    copy_resp = await http.post(
        f"/v0/drives/{drive1['id']}/artifacts/{art_id}/copy",
        json={
            "destination_drive_id": drive2["id"],
            "destination_parent_id": drive2["root_folder_id"],
            "destination_name": "cross.bin",
        },
        headers={"Idempotency-Key": "kcd-2"},
    )
    assert copy_resp.status_code == 400
    assert copy_resp.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert "cross-drive" in copy_resp.json()["error"]["message"]

    async with conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM v0_jobs WHERE drive_id=$1", drive2["id"]
        ) == 0


# ── versions ─────────────────────────────────────────────────────────────────


async def test_versions_list_and_append(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "vr", "kvr")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "ver.bin"}, b"v1"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kvr-1"},
    )
    art_id = resp.json()["id"]
    art_version_etag_1 = resp.headers["etag"]

    body2 = _multipart_create({}, b"v2")
    append = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions",
        content=body2,
        headers={
            "Content-Type": _CT_MULTIPART,
            "Idempotency-Key": "kvr-2",
            "If-Match": art_version_etag_1,
        },
    )
    assert append.status_code == 201
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    assert append.headers["location"] == (
        f"{origin}/v0/drives/{drive['id']}/artifacts/{art_id}/versions/"
        f"{append.json()['id']}"
    )

    versions = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions"
    )
    assert versions.status_code == 200
    items = versions.json()["items"]
    assert len(items) == 2
    assert items[0]["version_number"] == 2
    assert items[1]["version_number"] == 1

    read_v2 = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions/{items[0]['id']}"
    )
    assert read_v2.status_code == 200
    assert read_v2.json()["size_bytes"] == 2

    read_v1 = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions/{items[1]['id']}"
    )
    assert read_v1.status_code == 200
    assert read_v1.json()["size_bytes"] == 2


async def test_versions_content(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "vc", "kvc")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "vc.bin"}, b"version-content"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kvc-1"},
    )
    art_id = resp.json()["id"]

    versions = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions"
    )
    ver_id = versions.json()["items"][0]["id"]

    content_resp = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions/{ver_id}/content"
    )
    assert content_resp.status_code == 200
    assert content_resp.content == b"version-content"


async def test_versions_restore(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "vrs", "kvrs")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "vrs.bin"}, b"old"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kvrs-1"},
    )
    art_id = resp.json()["id"]
    art_etag_1 = resp.headers["etag"]

    body2 = _multipart_create({}, b"latest")
    await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions",
        content=body2,
        headers={
            "Content-Type": _CT_MULTIPART,
            "Idempotency-Key": "kvrs-2",
            "If-Match": art_etag_1,
        },
    )
    versions = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions"
    )
    items = versions.json()["items"]
    old_ver = items[1]

    art_read = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art_id}")
    art_etag = art_read.headers["etag"]
    res = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions/{old_ver['id']}/restore",
        headers={"Idempotency-Key": "kvrs-3", "If-Match": art_etag},
    )
    assert res.status_code == 201
    assert res.json()["version_number"] == 3
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    assert res.headers["location"] == (
        f"{origin}/v0/drives/{drive['id']}/artifacts/{art_id}/versions/"
        f"{res.json()['id']}"
    )

    final_versions = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions"
    )
    assert len(final_versions.json()["items"]) == 3


async def test_versions_append_requires_if_match(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "vi", "kvi")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "vi.bin"}, b"x"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kvi-1"},
    )
    art_id = resp.json()["id"]

    body2 = _multipart_create({}, b"y")
    no_match = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art_id}/versions",
        content=body2,
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kvi-2"},
    )
    assert no_match.status_code == 428
    assert no_match.json()["error"]["code"] == "PRECONDITION_REQUIRED"


# ---------------------------------------------------------------------------
# concurrency: lock ordering between the drive advisory lock and row locks
# ---------------------------------------------------------------------------


async def _hold_drive_advisory(c, drive_id: str) -> None:
    """Take the drive-scoped advisory lock on an explicit transaction."""
    await c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"v0_drive_namespace:{drive_id}",
    )


async def test_artifact_update_waits_on_advisory_before_row_lock(http, override_actor):
    """Lock-ordering invariant: a mutation must block on the drive advisory
    lock BEFORE taking any row lock.

    Pre-fix, `update_artifact` takes the artifact row `FOR UPDATE` and only
    then blocks on the advisory lock — the ABBA precondition. Post-fix it
    blocks at the advisory first, so the artifact row is never row-locked
    while waiting. This test makes that observable deterministically by
    holding the advisory lock and probing the row with `FOR UPDATE NOWAIT`.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "lk", "klk")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "lk.txt"}, b"x"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "klk-1"},
    )
    art_id = resp.json()["id"]
    etag = resp.headers["etag"]

    # Hold the drive advisory lock on a dedicated connection/transaction.
    async with conn() as c_holder, c_holder.transaction():
        await _hold_drive_advisory(c_holder, drive["id"])
        # The update must block at the advisory lock; run it concurrently.
        task = asyncio.create_task(
            http.patch(
                f"/v0/drives/{drive['id']}/artifacts/{art_id}",
                json={"metadata": {"x": 1}},
                headers={"Idempotency-Key": "klk-2", "If-Match": etag},
            )
        )
        # Give the request time to reach the advisory lock.
        await asyncio.sleep(0.15)
        assert not task.done(), "update must be blocked on the advisory lock"

        # The artifact row must NOT be row-locked while the update waits.
        try:
            async with conn() as c_probe:
                await c_probe.execute(
                    "SELECT id FROM artifacts WHERE id=$1 FOR UPDATE NOWAIT",
                    art_id,
                )
            row_free = True
        except asyncpg.exceptions.LockNotAvailableError:
            row_free = False
        assert row_free, (
            "artifact row was row-locked while the update waited on the "
            "drive advisory lock — ABBA deadlock window present"
        )
        # advisory lock released when the holder transaction commits
    resp = await task
    assert resp.status_code == 200


async def test_folder_delete_waits_on_advisory_before_row_lock(http, override_actor):
    """Same invariant for folder soft-delete: it must block on the drive
    advisory lock before locking folder rows."""
    override_actor(make_actor())
    drive = await _create_drive(http, "ld", "kld")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "ld", "kld-1")
    folder_etag = folder.headers["etag"]

    async with conn() as c_holder, c_holder.transaction():
        await _hold_drive_advisory(c_holder, drive["id"])
        task = asyncio.create_task(
            http.request(
                "DELETE",
                f"/v0/drives/{drive['id']}/folders/{folder.json()['id']}",
                headers={"Idempotency-Key": "kld-2", "If-Match": folder_etag},
            )
        )
        await asyncio.sleep(0.15)
        assert not task.done(), "delete must be blocked on the advisory lock"

        try:
            async with conn() as c_probe:
                await c_probe.execute(
                    "SELECT id FROM folders WHERE id=$1 FOR UPDATE NOWAIT",
                    folder.json()["id"],
                )
            row_free = True
        except asyncpg.exceptions.LockNotAvailableError:
            row_free = False
        assert row_free, (
            "folder row was row-locked while the delete waited on the "
            "drive advisory lock — ABBA deadlock window present"
        )
    resp = await task
    assert resp.status_code == 200


async def test_create_artifact_rechecks_parent_after_advisory_lock(http, override_actor):
    """Gap-2 regression: `create_artifact` must re-check the parent folder's
    liveness AFTER acquiring the drive advisory lock.

    Pre-fix, `create_artifact` reads the parent (plain, unlocked) BEFORE the
    advisory lock. A concurrent folder delete (which holds the advisory and
    soft-deletes the parent) can land between that check and the INSERT, so
    the artifact is created under a soft-deleted folder — an orphan.
    Post-fix, the parent check runs under the advisory, so the create sees
    the deleted parent and fails 404.

    Deterministic: the holder simulates the folder delete in progress (it
    owns the advisory), the create blocks at the advisory, the holder
    soft-deletes the parent, then releases — the create must re-check.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "gc", "kgc")
    parent = await _mkdir(http, drive["id"], drive["root_folder_id"], "gc", "kgc-1")
    parent_id = parent.json()["id"]

    body = _multipart_create({"parent_id": parent_id, "name": "orphan.bin"}, b"x")
    async with conn() as c_holder, c_holder.transaction():
        await _hold_drive_advisory(c_holder, drive["id"])
        # The create must block at the advisory lock (the folder delete holds it).
        task = asyncio.create_task(
            http.post(
                f"/v0/drives/{drive['id']}/artifacts",
                content=body,
                headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kgc-2"},
            )
        )
        await asyncio.sleep(0.15)
        assert not task.done(), "create must be blocked on the advisory lock"

        # Simulate the folder delete's effect: soft-delete the parent folder.
        await c_holder.execute(
            "UPDATE folders SET deleted_at = now(), revision = $2, updated_at = now() "
            "WHERE id = $1",
            parent_id, "rev_ffffffffffffffff",
        )
        # holder transaction commits here → advisory released, parent deleted
    resp = await task
    # Post-fix: the create re-checks the parent under the advisory and 404s.
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"

    async with conn() as c:
        orphan = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE parent_id = $1", parent_id
        )
        assert orphan == 0, "an artifact was created under a soft-deleted folder"


# ---------------------------------------------------------------------------
# artifact PATCH move requires editor on the destination parent
# ---------------------------------------------------------------------------


async def _grant_editor_on_artifact(
    http, override_actor, drive_id: str, artifact_id: str, key: str
) -> None:
    """As a drive manager, grant OTHER_AGENT a DIRECT editor grant on the
    artifact only (no folder grant)."""
    override_actor(make_actor(scopes={
        "drives:read", "drives:write", "usage:read",
        "content:read", "content:write", "sharing:read", "sharing:write",
    }))
    resp = await http.post(
        f"/v0/drives/{drive_id}/grants",
        json={
            "principal_type": "agent", "principal_id": OTHER_AGENT,
            "resource_type": "artifact", "resource_id": artifact_id, "role": "editor",
        },
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text


async def test_artifact_move_requires_editor_on_destination_parent(http, override_actor):
    """A principal with a DIRECT editor grant on an artifact only (no folder
    grant) cannot move it into another folder — that would occupy a name in a
    namespace they hold no capability on. Uniform 404, artifact unmoved."""
    override_actor(make_actor())
    drive = await _create_drive(http, "mvd", "kmvd")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "mv.txt"}, b"x"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kmvd-1"},
    )
    assert resp.status_code == 201, resp.text
    art = resp.json()

    other_folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "elsewhere", "kmvd-2")

    await _grant_editor_on_artifact(
        http, override_actor, drive["id"], art["id"], "kmvd-3"
    )

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    patch = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}",
        json={"parent_id": other_folder.json()["id"]},
        headers={"Idempotency-Key": "kmvd-4", "If-Match": f'"{art["revision"]}"'},
    )
    assert patch.status_code == 404
    assert patch.json()["error"]["code"] == "NOT_AUTHORIZED"

    # The artifact is unmoved.
    override_actor(make_actor())
    read = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}")
    assert read.status_code == 200
    assert read.json()["parent_id"] == drive["root_folder_id"]


async def test_artifact_move_replay_rechecks_editor_on_parent(http, override_actor):
    """A legitimate first move, then the actor's folder-editor grant revoked,
    then replay under the same Idempotency-Key → 404, not the stored 200."""
    override_actor(make_actor())
    drive = await _create_drive(http, "mvr", "kmvr")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "mv.txt"}, b"x"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body, headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kmvr-1"},
    )
    assert resp.status_code == 201, resp.text
    art = resp.json()

    dest = await _mkdir(http, drive["id"], drive["root_folder_id"], "dest", "kmvr-2")

    # Grant OTHER_AGENT a DIRECT editor grant on the artifact (covers the
    # route gate) AND editor on the DESTINATION folder (covers the move).
    override_actor(make_actor(scopes={
        "drives:read", "drives:write", "usage:read",
        "content:read", "content:write", "sharing:read", "sharing:write",
    }))
    artifact_grant = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent", "principal_id": OTHER_AGENT,
            "resource_type": "artifact", "resource_id": art["id"], "role": "editor",
        },
        headers={"Idempotency-Key": "kmvr-3"},
    )
    assert artifact_grant.status_code == 201, artifact_grant.text

    folder_grant = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": "agent", "principal_id": OTHER_AGENT,
            "resource_type": "folder", "resource_id": dest.json()["id"], "role": "editor",
        },
        headers={"Idempotency-Key": "kmvr-3b"},
    )
    assert folder_grant.status_code == 201, folder_grant.text
    folder_grant_id = folder_grant.json()["id"]
    folder_grant_etag = folder_grant.headers["etag"]

    # OTHER_AGENT moves the artifact into dest under a fixed key.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    moved = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}",
        json={"parent_id": dest.json()["id"]},
        headers={"Idempotency-Key": "kmvr-4", "If-Match": f'"{art["revision"]}"'},
    )
    assert moved.status_code == 200, moved.text

    # Revoke OTHER_AGENT's folder-editor grant (as the creator).
    override_actor(make_actor(scopes={
        "drives:read", "drives:write", "usage:read",
        "content:read", "content:write", "sharing:read", "sharing:write",
    }))
    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{folder_grant_id}",
        headers={"Idempotency-Key": "kmvr-5", "If-Match": folder_grant_etag},
    )
    assert revoked.status_code == 200, revoked.text

    # Replay the move after revocation → 404, not the stored 200.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    replay = await http.patch(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}",
        json={"parent_id": dest.json()["id"]},
        headers={"Idempotency-Key": "kmvr-4", "If-Match": f'"{art["revision"]}"'},
    )
    assert replay.status_code == 404
    assert replay.json()["error"]["code"] == "NOT_AUTHORIZED"


async def test_a_hostile_stored_content_type_cannot_crash_the_download(
    http, override_actor
):
    """A CRLF in the stored `content_type` must not reach a response header.

    `content_type` is uploader-controlled and validated nowhere, so it can
    hold a CRLF. uvicorn refuses the malformed header by RAISING, which drops
    the connection and returns zero bytes — reproduced directly against
    uvicorn: a benign type serves 200, a CRLF one gives status 000 and
    `RuntimeError: Invalid HTTP header value.` in the log. One bad upload
    would break that artifact's bytes for every reader, permanently.

    NOTE: this test asserts the sanitised header, not the crash. It cannot
    assert the crash — the ASGI transport never serialises headers, which is
    exactly why the bug survived the suite in the first place.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "hostct", "k-hct")
    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "ok.md",
         "content_type": "text/markdown"},
        b"# fine\n",
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body,
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "k-hct-1"},
    )
    assert resp.status_code == 201, resp.text
    art = resp.json()

    hostile = "text/markdown\r\nX-Evil: injected"
    async with conn() as c:
        await c.execute(
            "UPDATE artifacts SET content_type = $2 WHERE id = $1", art["id"], hostile
        )
        old = await c.fetchrow(
            "SELECT storage_object, size_bytes, checksum FROM artifact_versions"
            " WHERE artifact_id = $1", art["id"],
        )
        ver = "ver_" + "beefcafe" * 2
        await c.execute(
            "INSERT INTO artifact_versions (id, artifact_id, ordinal, storage_object,"
            " size_bytes, content_type, checksum, actor_type, actor_id)"
            " VALUES ($1,$2,2,$3,$4,$5,$6,'system',NULL)",
            ver, art["id"], old["storage_object"], old["size_bytes"], hostile,
            old["checksum"],
        )
        await c.execute(
            "UPDATE artifacts SET head_version_id = $2 WHERE id = $1", art["id"], ver
        )

    r = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/content"
    )

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert "x-evil" not in {k.lower() for k in r.headers}
    for value in r.headers.values():
        assert "\r" not in value and "\n" not in value


async def test_replay_of_a_pre_deploy_record_backfills_effective_visibility(
    http, override_actor
):
    """An idempotency record written by the PREVIOUS deployment does not
    carry a response field this one made required, and replay returns the
    stored body verbatim — so the retry would fail response-model validation
    and 500 for the record's whole lifetime, while the client's only escape
    (a fresh Idempotency-Key) creates the duplicate artifact idempotency
    exists to prevent. The replay path recomputes the derived field instead.

    Simulated by stripping the field from the stored record, which is exactly
    the shape the old image wrote.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "replay", "kreplay")
    key = "kreplay-1"
    first = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        files={
            "parent_id": (None, drive["root_folder_id"]),
            "name": (None, "a.txt"),
            "content": ("a.txt", b"hello", "text/plain"),
        },
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201, first.text

    async with conn() as c:
        await c.execute(
            "UPDATE idempotency_records "
            "SET response_body = (response_body - 'effective_visibility')::jsonb "
            "WHERE idempotency_key = $1",
            key,
        )
        stored = await c.fetchval(
            "SELECT response_body FROM idempotency_records WHERE idempotency_key=$1",
            key,
        )
    assert "effective_visibility" not in stored

    replay = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        files={
            "parent_id": (None, drive["root_folder_id"]),
            "name": (None, "a.txt"),
            "content": ("a.txt", b"hello", "text/plain"),
        },
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["effective_visibility"] == "private"
    assert replay.json()["id"] == first.json()["id"]


# ── search preview vs NUL bytes ──────────────────────────────────────────────
#
# `_derive_preview` decides "is this text?" by whether the bytes decode as
# UTF-8. NUL is valid UTF-8 (U+0000) but ILLEGAL in a Postgres text value, so a
# successful decode was never proof the result could be stored: the preview
# reached the driver's bind and the whole write failed with a 400 out-of-range,
# not merely an empty preview. Every affected body is a plausible real upload —
# an MP4's box headers begin `00 00 00 xx`, an empty ZIP is NUL padding,
# ASCII-range UTF-16 interleaves NULs — so this is the create path refusing
# ordinary binary files.


async def test_create_artifact_accepts_content_with_nul_bytes(http, override_actor):
    """A real MP4 header: `00 00 00 18 ftypisom`. Decodes as UTF-8, carries
    NULs, and before the fix answered 400 BAD_REQUEST."""
    override_actor(make_actor())
    drive = await _create_drive(http, "nul", "knul")
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        data={
            "parent_id": drive["root_folder_id"],
            "name": "clip.mp4",
            "content_type": "video/mp4",
        },
        files={"content": ("clip.mp4", mp4, "video/mp4")},
        headers={"Idempotency-Key": "knul-1"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "clip.mp4"


async def test_create_artifact_accepts_empty_zip(http, override_actor):
    """The 22-byte end-of-central-directory record — a valid empty ZIP, and
    NUL padding after the signature."""
    override_actor(make_actor())
    drive = await _create_drive(http, "zip", "kzip")
    empty_zip = b"PK\x05\x06" + b"\x00" * 18
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        data={
            "parent_id": drive["root_folder_id"],
            "name": "bundle.zip",
            "content_type": "application/zip",
        },
        files={"content": ("bundle.zip", empty_zip, "application/zip")},
        headers={"Idempotency-Key": "kzip-1"},
    )
    assert resp.status_code == 201, resp.text


async def test_append_version_accepts_content_with_nul_bytes(http, override_actor):
    """The append path derives a preview too (`versions_append`), so it has
    the same defect and needs the same proof."""
    override_actor(make_actor())
    drive = await _create_drive(http, "nulv", "knulv")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        data={
            "parent_id": drive["root_folder_id"],
            "name": "blob.bin",
            "content_type": "application/octet-stream",
        },
        files={"content": ("blob.bin", b"first", "application/octet-stream")},
        headers={"Idempotency-Key": "knulv-1"},
    )
    assert created.status_code == 201, created.text
    art = created.json()

    appended = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions",
        data={"content_type": "application/octet-stream"},
        files={"content": ("blob.bin", b"A\x00B", "application/octet-stream")},
        headers={"Idempotency-Key": "knulv-2", "If-Match": art["revision"]},
    )
    assert appended.status_code == 201, appended.text


async def test_inline_create_over_quota_is_422_not_500(http, override_actor):
    """Every inline write raises `QuotaExceededError`, not just the upload path.

    Only `v0_uploads` mapped it; the artifacts and folders mappers ended in
    `raise exc`, so configuring a workspace ceiling would have turned inline
    create, version append, version restore, and both copies into 500s. The
    answer must match what the direct path already gives.
    """
    actor = make_actor()
    actor = replace(
        actor,
        drive_limits=replace(
            actor.drive_limits,
            storage_bytes_workspace=5,
            storage_bytes_drive=5,
        ),
    )
    override_actor(actor)
    drive = await _create_drive(http, "quota", "kq-1")

    body = _multipart_create(
        {"parent_id": drive["root_folder_id"], "name": "big.txt"}, b"x" * 4096
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body,
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kq-2"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "TRANSFER_LIMIT_EXCEEDED"


async def test_restore_into_a_deleted_parent_is_409_while_move_stays_404(
    http, override_actor
):
    """Restore names no destination, so 404 would be a lie.

    A MOVE to a dead parent answers 404 as-if-absent, because the caller is
    naming a destination they may not be entitled to know about. A RESTORE
    names nothing — the parent is wherever the artifact already was — so the
    old 404 claimed the artifact did not exist seconds after
    `?state=deleted` listed it. The folder equivalent always answered 409.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "rst", "kr-1")
    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "holder", "kr-2")
    folder_id = folder.json()["id"] if hasattr(folder, "json") else folder["id"]

    body = _multipart_create({"parent_id": folder_id, "name": "doc.txt"}, b"hello")
    created = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=body,
        headers={"Content-Type": _CT_MULTIPART, "Idempotency-Key": "kr-3"},
    )
    assert created.status_code == 201, created.text
    art = created.json()

    deleted = await http.delete(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}",
        headers={"Idempotency-Key": "kr-4", "If-Match": f'"{art["revision"]}"'},
    )
    assert deleted.status_code == 200, deleted.text
    art_rev = deleted.json()["revision"]

    folder_read = await http.get(f"/v0/drives/{drive['id']}/folders/{folder_id}")
    drop_folder = await http.delete(
        f"/v0/drives/{drive['id']}/folders/{folder_id}?recursive=true",
        headers={
            "Idempotency-Key": "kr-5",
            "If-Match": f'"{folder_read.json()["revision"]}"',
        },
    )
    assert drop_folder.status_code == 200, drop_folder.text

    restored = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/restore",
        headers={"Idempotency-Key": "kr-6", "If-Match": f'"{art_rev}"'},
    )
    assert restored.status_code == 409, restored.text
    assert restored.json()["error"]["code"] == "CONFLICT"
