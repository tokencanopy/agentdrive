"""B3 packet 4: the generation-pinned download-capability mint (§5.7).

Governing contract: TokenCanopy
``docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md``.

Everything here runs the REAL mounted route over real Postgres, with the V4
signing callable behind an injected fake so the mint's own semantic URL
validation still executes end to end. All identifiers, hosts, and byte
strings are synthetic.

Sections:
  * contract statics (manifest row, 47-operation count, readiness index)
  * disabled/unready gate (503 TRANSFER_DISABLED, no fallback)
  * auth / scope / anti-enumeration
  * strict body, forbidden Idempotency-Key
  * head resolution + version pinning + persisted-generation proofs
  * signer fail-closed + hostile-signer semantic validation
  * secret-hygiene, no-persistence, and compatibility proofs
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from dataclasses import replace
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.core.usage.policy import default_drive_limits
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

# The signed-download capability is minted against GCS; a filesystem store
# proxy-streams instead and has nothing to sign, so this module is GCS-only.
pytestmark = pytest.mark.skipif(
    settings.storage_backend != "gcs", reason="signed-download capabilities are GCS-only"
)


AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
OTHER_AGENT = "tcagt_0000000000000002"
WS_A = "tcws_0000000000000001"
WS_B = "tcws_0000000000000002"

DL_ENDPOINT = "https://storage-dl.example"
FAKE_TRANSFER_BUCKET = "transfer-bucket-demo"

# The fixed signing instant the fake signer stamps — expires_at must be
# derived from THIS plus the TTL, not from a server clock. The injected
# validator clock sits 30s later, inside the freshness skew window.
SIGNING_DATE = "20260815T120000Z"
SIGNING_EXPIRES_AT = "2026-08-15T12:05:00Z"  # + the 300s configured TTL

from datetime import UTC as _UTC  # noqa: E402
from datetime import datetime as _datetime  # noqa: E402

FIXED_NOW = _datetime(2026, 8, 15, 12, 0, 30, tzinfo=_UTC)


def crc32c_of(data: bytes) -> str:
    value = zlib.crc32(data) & 0xFFFFFFFF
    return base64.b64encode(struct.pack(">I", value)).decode("ascii")


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
        "content:read", "content:write", "sharing:read", "sharing:write",
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
    """Truncate BEFORE as well as after.

    This suite is readiness-gated: every route it exercises calls
    `_require_ready()`, which fails closed while ANY row in
    `artifact_versions` lacks resolved storage coordinates — a deliberately
    global predicate (`core.v0_uploads.transfer_readiness`), because direct
    transfer must not serve while a backfill is outstanding anywhere.

    Cleaning only on the way out made that gate read whatever the PREVIOUS
    test in this worker left behind. Several suites create unresolved rows
    on purpose (`test_reconcile_generations`, `test_gc_direct_transfers`,
    `test_v0_logical_accounting`, `test_v0_drives`), and under `-n auto`
    which of them precedes this file differs per run — so the symptom was
    one test somewhere in this file answering `503 TRANSFER_DISABLED` where
    it expected a 4xx, a different test each time. The two suites that
    CREATE those rows already clean on both sides for the same reason.

    The assertion is the other half: if a future suite finds a way to leave
    an unresolved row that this truncate does not reach, the failure says
    so by name instead of surfacing as an unexplained 503 three files away.
    """
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, usage_operations, usage_windows, "
            "drives, workspace_storage "
            "RESTART IDENTITY CASCADE"
        )
        unresolved = await c.fetchval(
            "SELECT count(*) FROM artifact_versions "
            "WHERE storage_generation IS NULL OR storage_bucket IS NULL "
            "   OR storage_bucket = ''"
        )
    assert unresolved == 0, (
        f"{unresolved} unresolved artifact_versions rows survived the "
        "truncate; direct-transfer readiness is global, so every "
        "readiness-gated test in this file would answer 503"
    )
    yield
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, drives, workspace_storage "
            "RESTART IDENTITY CASCADE"
        )


@pytest.fixture
def enabled_transfer(monkeypatch):
    """A complete, bounded §9 runtime policy (mirrors test_v0_uploads)."""
    monkeypatch.setattr(settings, "direct_transfer_enabled", True)
    monkeypatch.setattr(settings, "direct_transfer_min_bytes", 1)
    monkeypatch.setattr(settings, "direct_transfer_max_bytes", 10 * 1024 * 1024)
    monkeypatch.setattr(settings, "direct_transfer_allow_zero_bytes", False)
    monkeypatch.setattr(settings, "direct_transfer_session_ttl_seconds", 3600)
    monkeypatch.setattr(settings, "direct_transfer_terminal_retention_seconds", 3600)
    monkeypatch.setattr(settings, "direct_transfer_gc_grace_seconds", 60)
    monkeypatch.setattr(settings, "direct_transfer_max_active_sessions_principal", 5)
    monkeypatch.setattr(settings, "direct_transfer_max_active_sessions_workspace", 8)
    monkeypatch.setattr(settings, "direct_transfer_max_active_sessions_drive", 8)
    monkeypatch.setattr(settings, "direct_transfer_rate_principal", 10_000)
    monkeypatch.setattr(settings, "direct_transfer_rate_workspace", 10_000)
    monkeypatch.setattr(settings, "direct_transfer_rate_drive", 10_000)
    monkeypatch.setattr(
        settings, "direct_transfer_hard_logical_version_bytes_workspace", 10**9
    )
    monkeypatch.setattr(
        settings, "direct_transfer_hard_logical_version_bytes_drive", 10**9
    )
    monkeypatch.setattr(
        settings, "direct_transfer_canonical_browser_origin", "https://console.example"
    )
    monkeypatch.setattr(
        settings, "direct_transfer_upload_endpoint", "https://storage.example"
    )
    monkeypatch.setattr(settings, "direct_transfer_download_endpoint", DL_ENDPOINT)
    monkeypatch.setattr(settings, "direct_transfer_bucket", FAKE_TRANSFER_BUCKET)
    monkeypatch.setattr(settings, "direct_transfer_scratch_prefix", "scratch/")
    monkeypatch.setattr(settings, "direct_transfer_immutable_prefix", "immutable/")
    monkeypatch.setattr(settings, "direct_download_capability_ttl_seconds", 300)
    return settings


def _v4_query(gen_value, ttl_value, type_value, disposition_value, **overrides):
    """A complete, valid V4 query for hand-built fixtures. Overrides with a
    value of None DELETE the key; a `_dupe_<key>` entry appends a duplicate.
    (Positional names avoid clashing with same-named query-key overrides.)"""
    params = {
        "generation": str(gen_value),
        "response-content-type": type_value,
        "response-content-disposition": disposition_value,
        "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
        "X-Goog-Credential": "signer-demo@example.test/20260815/auto/storage/goog4_request",
        "X-Goog-Date": SIGNING_DATE,
        "X-Goog-Expires": str(ttl_value),
        "X-Goog-SignedHeaders": "host",
        # A structurally valid provider signature: 512 hex chars (the
        # 2048-bit RSA size signBlob produces).
        "X-Goog-Signature": "ab" * 256,
    }
    extras = []
    for key, value in overrides.items():
        if key.startswith("_dupe_"):
            extras.append((key.removeprefix("_dupe_"), value))
        elif value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return urlencode(list(params.items()) + extras)


@pytest.fixture
def fake_signer(monkeypatch, enabled_transfer):
    """Wire the REAL GenerationDownloadSigner over a fake V4 callable, so
    the mint's semantic URL validation executes while nothing external is
    contacted. Returns the call ledger."""
    from agentdrive.api import v0_download_capabilities as dl_api
    from agentdrive.storage_transfers import GenerationDownloadSigner

    calls: list[dict] = []

    def url_signer(bucket, object_name, generation, ttl_seconds, response_type,
                   disposition):
        calls.append({
            "bucket": bucket,
            "object_name": object_name,
            "generation": generation,
            "ttl_seconds": ttl_seconds,
            "response_type": response_type,
            "disposition": disposition,
        })
        query = _v4_query(generation, ttl_seconds, response_type, disposition)
        return f"{DL_ENDPOINT}/{bucket}/{quote(object_name, safe='/')}?{query}"

    def _build():
        return GenerationDownloadSigner(
            download_endpoint=DL_ENDPOINT,
            namespaces={
                settings.gcs_bucket: "cas/",
                FAKE_TRANSFER_BUCKET: "immutable/",
            },
            url_signer=url_signer,
            clock=lambda: FIXED_NOW,
        )

    monkeypatch.setattr(dl_api, "capability_signer", _build)

    return calls


async def _create_drive(http, name: str, key: str) -> dict:
    prev = app.dependency_overrides.get(v0_actor)
    app.dependency_overrides[v0_actor] = lambda: make_actor()
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


async def _create_artifact(
    http, drive_id: str, parent: str, name: str, key: str,
    data: bytes = b"hello there",
) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive_id}/artifacts",
        files={"content": (name, data, "text/plain")},
        data={"parent_id": parent, "name": name},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _append_version(http, drive_id, artifact_id, key, data: bytes) -> dict:
    read = await http.get(f"/v0/drives/{drive_id}/artifacts/{artifact_id}")
    assert read.status_code == 200, read.text
    resp = await http.post(
        f"/v0/drives/{drive_id}/artifacts/{artifact_id}/versions",
        files={"content": ("notes.txt", data, "text/plain")},
        headers={"Idempotency-Key": key, "If-Match": read.headers["ETag"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def artifact_target(artifact_id: str) -> dict:
    return {"target": {"kind": "artifact", "artifact_id": artifact_id}}


def version_target(artifact_id: str, version_id: str) -> dict:
    return {
        "target": {
            "kind": "version",
            "artifact_id": artifact_id,
            "version_id": version_id,
        }
    }


async def _mint(http, drive_id: str, body: dict, **headers):
    return await http.post(
        f"/v0/drives/{drive_id}/download-capabilities",
        content=json.dumps(body),
        headers={"Content-Type": "application/json; charset=utf-8", **headers},
    )


async def _version_row(version_id: str):
    async with conn() as c:
        return await c.fetchrow(
            "SELECT * FROM artifact_versions WHERE id = $1", version_id
        )


@pytest_asyncio.fixture
async def seeded(http, override_actor, fake_signer):
    """One drive + one artifact with two versions, as the creating agent."""
    override_actor(make_actor())
    drive = await _create_drive(http, "mint-demo", "mint-drive-1")
    drive_id = drive["id"]
    root = drive["root_folder_id"]
    artifact = await _create_artifact(
        http, drive_id, root, "notes.txt", "mint-art-1"
    )
    art_id = artifact["id"]
    v1 = artifact["head_version_id"]
    appended = await _append_version(http, drive_id, art_id, "mint-ver-2", b"goodbye now")
    v2 = appended["id"]
    return {
        "drive_id": drive_id,
        "root": root,
        "artifact_id": art_id,
        "v1": v1,
        "v2": v2,
    }


# ─── contract statics ───────────────────────────────────────────────────────


async def test_manifest_pins_the_download_mint_operation():
    from agentdrive.api.v0_manifest import OPERATION_COUNT, operations

    assert OPERATION_COUNT == 59
    by_id = {o["operation_id"]: o for o in operations}
    op = by_id["download_capabilities_create"]
    assert op["method"] == "POST"
    assert op["path"] == "/v0/drives/{drive_id}/download-capabilities"
    assert op["scopes"] == ["content:read"]
    assert op["precondition_class"] == "creation-flavored"
    assert op["expected_statuses"] == [200]
    assert op["idempotency_class"] == "forbidden"
    assert op["shape_fixture"] == "download-capability-create"


async def test_error_registry_carries_the_signing_unavailable_code():
    from agentdrive.api.error_codes import ERROR_CODES

    assert "DOWNLOAD_SIGNING_UNAVAILABLE" in ERROR_CODES


async def test_readiness_predicate_is_indexed(app_with_lifespan):
    """Packet 3's deferred follow-up: the per-request readiness predicate
    (unresolved generation rows) must be backed by a partial index so the
    uncached gate stays O(unresolved), not O(all versions)."""
    async with conn() as c:
        definition = await c.fetchval(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'artifact_versions_unresolved_coordinates'"
        )
    assert definition is not None, "partial readiness index is missing"
    assert "storage_generation IS NULL" in definition
    assert "storage_bucket IS NULL" in definition


async def test_ttl_ceiling_has_one_authority():
    """Packet 2/3 follow-up: the signed-download TTL ceiling must be a
    single authoritative constant shared by config validation and the
    signer boundary — not two drifting literals."""
    from agentdrive import config as config_module
    from agentdrive import storage_transfers as st

    assert st.MAX_DOWNLOAD_TTL_SECONDS is config_module.MAX_DOWNLOAD_CAPABILITY_TTL_SECONDS


async def test_multipart_ceiling_and_baseline_operations_are_unchanged():
    """Compatibility (§12): the 15 MiB buffered multipart limit and the
    existing content/multipart operations survive the mint untouched."""
    from agentdrive.api.v0_manifest import operations
    from agentdrive.core.v0_artifacts import MAX_BUFFERED_UPLOAD_BYTES

    assert MAX_BUFFERED_UPLOAD_BYTES == 15 * 1024 * 1024
    by_id = {o["operation_id"]: o for o in operations}
    assert by_id["artifacts_create"]["expected_statuses"] == [201]
    assert by_id["versions_append"]["expected_statuses"] == [201]
    assert by_id["artifacts_content"]["expected_statuses"] == [200, 304, 307]
    assert by_id["versions_content"]["expected_statuses"] == [200, 304, 307]
    assert by_id["artifacts_content"]["precondition_class"] == "read"
    assert by_id["versions_content"]["precondition_class"] == "read"
    assert by_id["artifacts_content"]["idempotency_class"] == "not_required"


# ─── disabled / unready gate ────────────────────────────────────────────────


async def test_mint_is_disabled_by_default(http, override_actor):
    override_actor(make_actor())
    resp = await _mint(
        http, "drv_0000000000000abc",
        artifact_target("art_0000000000000abc"),
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "TRANSFER_DISABLED"
    assert "Retry-After" not in resp.headers
    assert resp.headers["Cache-Control"] == "no-store"


async def test_mint_stays_disabled_while_generation_rows_are_unresolved(
    http, override_actor, seeded,
):
    """§7: zero unresolved rows is a mint precondition — one NULL-coordinate
    row anywhere flips the readiness gate closed."""
    async with conn() as c:
        await c.execute(
            "INSERT INTO artifact_versions "
            "(id, artifact_id, checksum, content_type, size_bytes, "
            " storage_object, actor_type, actor_id, ordinal) "
            "VALUES ('ver_00000000000000aa', $1, 'sha256:00', 'text/plain', 2, "
            "        'cas/legacy/demo', 'agent', $2, 99)",
            seeded["artifact_id"], AGENT,
        )
    resp = await _mint(http, seeded["drive_id"], artifact_target(seeded["artifact_id"]))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "TRANSFER_DISABLED"


# ─── auth / scope / anti-enumeration ────────────────────────────────────────


async def test_mint_requires_content_read_scope(http, override_actor, seeded):
    override_actor(make_actor(scopes={"drives:read", "content:write"}))
    resp = await _mint(http, seeded["drive_id"], artifact_target(seeded["artifact_id"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize("case", [
    "unknown_drive", "foreign_workspace", "unknown_artifact",
    "cross_drive_artifact", "unknown_version", "cross_artifact_version",
    "deleted_artifact", "foreign_principal",
])
async def test_every_miss_is_the_same_anti_enumerating_404(
    http, override_actor, seeded, case,
):
    drive_id = seeded["drive_id"]
    body = artifact_target(seeded["artifact_id"])
    if case == "unknown_drive":
        drive_id = "drv_00000000000000ff"
    elif case == "foreign_workspace":
        override_actor(make_actor(workspace=WS_B))
    elif case == "unknown_artifact":
        body = artifact_target("art_00000000000000ff")
    elif case == "cross_drive_artifact":
        other = await _create_drive(http, "other-drive", "mint-drive-2")
        override_actor(make_actor())
        drive_id = other["id"]
    elif case == "unknown_version":
        body = version_target(seeded["artifact_id"], "ver_00000000000000ff")
    elif case == "cross_artifact_version":
        override_actor(make_actor())
        other_art = await _create_artifact(
            http, seeded["drive_id"], seeded["root"], "other.txt", "mint-art-2"
        )
        body = version_target(other_art["id"], seeded["v1"])
    elif case == "deleted_artifact":
        read = await http.get(
            f"/v0/drives/{drive_id}/artifacts/{seeded['artifact_id']}"
        )
        resp = await http.delete(
            f"/v0/drives/{drive_id}/artifacts/{seeded['artifact_id']}",
            headers={
                "Idempotency-Key": "mint-del-1",
                "If-Match": read.headers["ETag"],
            },
        )
        assert resp.status_code == 200, resp.text
    elif case == "foreign_principal":
        override_actor(make_actor(subject=OTHER_AGENT))
    resp = await _mint(http, drive_id, body)
    assert resp.status_code == 404, (case, resp.text)
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ─── strict body and forbidden idempotency ──────────────────────────────────


async def test_supplied_idempotency_key_is_rejected_and_never_stored(
    http, override_actor, seeded,
):
    async with conn() as c:
        before = await c.fetchval("SELECT count(*) FROM idempotency_records")
    resp = await _mint(
        http, seeded["drive_id"], artifact_target(seeded["artifact_id"]),
        **{"Idempotency-Key": "mint-key-1"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"
    async with conn() as c:
        after = await c.fetchval("SELECT count(*) FROM idempotency_records")
        key_rows = await c.fetchval(
            "SELECT count(*) FROM idempotency_records "
            "WHERE idempotency_key = 'mint-key-1'"
        )
    # The mint created NO record (the seeded fixture's required-key ops own
    # the preexisting rows) and the supplied key was never claimed.
    assert after == before
    assert key_rows == 0


@pytest.mark.parametrize("body", [
    {},
    {"target": {}},
    {"target": {"kind": "artifact"}},
    {"target": {"kind": "artifact", "artifact_id": "not-an-id"}},
    {"target": {"kind": "version", "artifact_id": "art_0000000000000001"}},
    {"target": {"kind": "folder", "artifact_id": "art_0000000000000001"}},
    {"target": {"kind": "artifact", "artifact_id": "art_0000000000000001",
                "extra": 1}},
    {"target": {"kind": "artifact", "artifact_id": "art_0000000000000001"},
     "extra": 1},
    {"target": {"kind": "version", "artifact_id": "art_0000000000000001",
                "version_id": "bad"}},
])
async def test_strict_body_rejections(http, override_actor, seeded, body):
    resp = await _mint(http, seeded["drive_id"], body)
    assert resp.status_code == 400, (body, resp.text)
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


async def test_duplicate_json_keys_are_rejected(http, override_actor, seeded):
    raw = (
        '{"target": {"kind": "artifact", '
        '"artifact_id": "art_0000000000000001", '
        '"artifact_id": "art_0000000000000002"}}'
    )
    resp = await http.post(
        f"/v0/drives/{seeded['drive_id']}/download-capabilities",
        content=raw,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


async def test_non_utf8_json_charset_is_415(http, override_actor, seeded):
    body = json.dumps(artifact_target(seeded["artifact_id"]))
    for content_type in (
        "application/json; charset=iso-8859-1",
        "application/json; charset=utf-16",
        "application/json; boundary=x",
    ):
        resp = await http.post(
            f"/v0/drives/{seeded['drive_id']}/download-capabilities",
            content=body,
            headers={"Content-Type": content_type},
        )
        assert resp.status_code == 415, content_type
        assert resp.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    for accepted in (
        "application/json",
        "application/json; charset=utf-8",
        'application/json; charset="UTF-8"',
    ):
        resp = await http.post(
            f"/v0/drives/{seeded['drive_id']}/download-capabilities",
            content=body,
            headers={"Content-Type": accepted},
        )
        assert resp.status_code != 415, accepted


async def test_oversized_declared_body_is_rejected_before_reading(
    app_with_lifespan,
):
    """A valid Content-Length over the 16 KiB bound must be refused without
    consuming a single body byte."""
    from starlette.requests import Request

    from agentdrive.api.v0_download_capabilities import _read_bounded_body
    from agentdrive.api.v0_errors import V0ApiError

    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    scope = {
        "type": "http", "method": "POST", "path": "/x",
        "headers": [(b"content-length", str(2 * 1024 * 1024).encode())],
        "query_string": b"",
    }
    with pytest.raises(V0ApiError) as excinfo:
        await _read_bounded_body(Request(scope, receive))
    assert excinfo.value.status_code == 400
    assert reads == 0  # not one body byte was consumed


async def test_oversized_chunked_body_stops_near_the_bound(
    http, override_actor, seeded,
):
    """A chunked body with no Content-Length must be stopped within one
    chunk of MAX_MINT_BODY_BYTES — never fully buffered."""
    from agentdrive.api.v0_download_capabilities import MAX_MINT_BODY_BYTES

    chunk = b"x" * 1024
    served = 0

    async def body_chunks():
        nonlocal served
        for _ in range(2048):  # 2 MiB on offer
            served += 1
            yield chunk

    resp = await http.post(
        f"/v0/drives/{seeded['drive_id']}/download-capabilities",
        content=body_chunks(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"
    # Consumption stopped within one chunk of the bound, not at 2 MiB.
    assert served * len(chunk) <= MAX_MINT_BODY_BYTES + 2 * len(chunk), served


async def test_contradictory_content_length_is_rejected(
    http, override_actor, seeded, app_with_lifespan,
):
    from starlette.requests import Request

    from agentdrive.api.v0_download_capabilities import _read_bounded_body
    from agentdrive.api.v0_errors import V0ApiError

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    for headers in (
        [(b"content-length", b"10"), (b"content-length", b"20")],
        [(b"content-length", b"nonsense")],
        [(b"content-length", b"-5")],
        # int()'s 4300-digit conversion cap must never surface as an
        # escaping ValueError — an absurd declared length is refused by
        # its digit count.
        [(b"content-length", b"1" * 5000)],
    ):
        scope = {
            "type": "http", "method": "POST", "path": "/x",
            "headers": headers, "query_string": b"",
        }
        with pytest.raises(V0ApiError) as excinfo:
            await _read_bounded_body(Request(scope, receive))
        assert excinfo.value.status_code == 400


async def test_wrong_media_type_is_415(http, override_actor, seeded):
    resp = await http.post(
        f"/v0/drives/{seeded['drive_id']}/download-capabilities",
        content=b"target=artifact",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


async def test_non_json_accept_is_406(http, override_actor, seeded):
    resp = await _mint(
        http, seeded["drive_id"], artifact_target(seeded["artifact_id"]),
        Accept="text/html",
    )
    assert resp.status_code == 406
    assert resp.json()["error"]["code"] == "NOT_ACCEPTABLE"


async def test_query_parameters_are_rejected(http, override_actor, seeded):
    resp = await http.post(
        f"/v0/drives/{seeded['drive_id']}/download-capabilities?foo=1",
        content=json.dumps(artifact_target(seeded["artifact_id"])),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


async def test_malformed_drive_id_is_400(http, override_actor, fake_signer):
    override_actor(make_actor())
    resp = await _mint(http, "not-a-drive", artifact_target("art_0000000000000001"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


# ─── head resolution and version pinning ────────────────────────────────────


def _signed_parts(payload: dict) -> tuple:
    url = payload["download"]["target"]["url"]
    parts = urlsplit(url)
    return parts, dict(parse_qsl(parts.query, keep_blank_values=True))


async def test_artifact_target_resolves_the_current_head(
    http, override_actor, seeded, fake_signer,
):
    resp = await _mint(http, seeded["drive_id"], artifact_target(seeded["artifact_id"]))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    download = payload["download"]
    assert download["artifact_id"] == seeded["artifact_id"]
    assert download["version_id"] == seeded["v2"]  # the appended head, not v1
    row = await _version_row(seeded["v2"])
    assert row["storage_generation"] is not None
    parts, query = _signed_parts(payload)
    assert query["generation"] == str(row["storage_generation"])
    assert parts.path == f"/{row['storage_bucket']}/" + row["storage_object"]


async def test_version_target_pins_the_requested_owned_version(
    http, override_actor, seeded, fake_signer,
):
    resp = await _mint(
        http, seeded["drive_id"],
        version_target(seeded["artifact_id"], seeded["v1"]),
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["download"]["version_id"] == seeded["v1"]
    row = await _version_row(seeded["v1"])
    _, query = _signed_parts(payload)
    assert query["generation"] == str(row["storage_generation"])


async def test_response_shape_headers_and_expiry_derivation(
    http, override_actor, seeded, fake_signer,
):
    resp = await _mint(http, seeded["drive_id"], artifact_target(seeded["artifact_id"]))
    assert resp.status_code == 200, resp.text
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    payload = resp.json()
    download = payload["download"]
    assert set(download) == {"artifact_id", "version_id", "expires_at", "target"}
    target = download["target"]
    assert set(target) == {"url", "method", "required_headers", "content_disposition"}
    assert target["method"] == "GET"
    assert target["required_headers"] == {}
    # expires_at corresponds to the SIGNED URL's X-Goog-Date + X-Goog-Expires.
    from datetime import datetime

    returned = datetime.fromisoformat(download["expires_at"].replace("Z", "+00:00"))
    expected = datetime.fromisoformat(SIGNING_EXPIRES_AT.replace("Z", "+00:00"))
    assert returned == expected
    parts, query = _signed_parts(payload)
    assert parts.scheme == "https"
    assert parts.netloc == urlsplit(DL_ENDPOINT).netloc
    assert not parts.fragment
    assert query["X-Goog-Expires"] == "300"
    assert query["response-content-type"] == "application/octet-stream"
    disposition = query["response-content-disposition"]
    assert disposition.startswith("attachment")
    assert "notes.txt" in disposition
    assert target["content_disposition"] == disposition
    # The closed exact query-key set, each exactly once.
    keys = [k for k, _ in parse_qsl(parts.query, keep_blank_values=True)]
    assert sorted(keys) == sorted([
        "generation", "response-content-type", "response-content-disposition",
        "X-Goog-Algorithm", "X-Goog-Credential", "X-Goog-Date",
        "X-Goog-Expires", "X-Goog-SignedHeaders", "X-Goog-Signature",
    ])
    async with conn() as connection:
        retrieval = await connection.fetchval(
            "SELECT retrieval_bytes FROM drives WHERE id=$1", seeded["drive_id"]
        )
        windows = await connection.fetch(
            "SELECT period, used FROM usage_windows "
            "WHERE metric='download_bytes' AND scope_type='workspace' "
            "ORDER BY period"
        )
    assert retrieval == len(b"goodbye now")
    assert {(row["period"], row["used"]) for row in windows} == {
        ("day", len(b"goodbye now")),
        ("month", len(b"goodbye now")),
    }


async def test_every_mint_reauthorizes_and_remints(
    http, override_actor, seeded, fake_signer,
):
    body = artifact_target(seeded["artifact_id"])
    first = await _mint(http, seeded["drive_id"], body)
    second = await _mint(http, seeded["drive_id"], body)
    assert first.status_code == second.status_code == 200
    assert len(fake_signer) == 2  # a fresh signature every call, no cache
    # Revoking local capability makes the NEXT mint a 404, not a replay.
    async with conn() as c:
        await c.execute(
            "UPDATE grants SET revoked_at = now() WHERE drive_id = $1",
            seeded["drive_id"],
        )
    third = await _mint(http, seeded["drive_id"], body)
    assert third.status_code == 404
    assert len(fake_signer) == 2


async def test_monthly_download_refuses_before_signing(
    http, override_actor, seeded, fake_signer
):
    limits = replace(
        default_drive_limits(),
        download_bytes_day_workspace=1,
        download_bytes_month_workspace=1,
    )
    override_actor(replace(make_actor(), drive_limits=limits))
    before = len(fake_signer)

    response = await _mint(
        http,
        seeded["drive_id"],
        artifact_target(seeded["artifact_id"]),
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "BANDWIDTH_LIMIT_EXCEEDED"
    assert len(fake_signer) == before


async def test_mint_charges_the_transfer_rate_windows(
    http, override_actor, seeded, fake_signer, monkeypatch,
):
    monkeypatch.setattr(settings, "direct_transfer_rate_principal", 1)

    body = artifact_target(seeded["artifact_id"])
    first = await _mint(http, seeded["drive_id"], body)
    assert first.status_code == 200
    second = await _mint(http, seeded["drive_id"], body)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "RATE_LIMITED"


# ─── fail-closed signing ────────────────────────────────────────────────────


async def test_missing_coordinates_fail_closed_even_past_readiness(
    http, override_actor, seeded, fake_signer, monkeypatch,
):
    """Defense in depth: if the readiness gate were bypassed, a NULL-
    coordinate row still cannot reach the signer."""
    from agentdrive.core import v0_uploads as uploads_core

    async def _ready(_c):
        return True, []

    monkeypatch.setattr(uploads_core, "transfer_readiness", _ready)
    async with conn() as c:
        await c.execute(
            "INSERT INTO artifact_versions "
            "(id, artifact_id, checksum, content_type, size_bytes, "
            " storage_object, actor_type, actor_id, ordinal) "
            "VALUES ('ver_00000000000000ab', $1, 'sha256:01', 'text/plain', 2, "
            "        'cas/legacy/demo2', 'agent', $2, 98)",
            seeded["artifact_id"], AGENT,
        )
    resp = await _mint(
        http, seeded["drive_id"],
        version_target(seeded["artifact_id"], "ver_00000000000000ab"),
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "DOWNLOAD_SIGNING_UNAVAILABLE"


async def test_foreign_bucket_coordinates_fail_closed(
    http, override_actor, seeded, fake_signer,
):
    """A persisted bucket outside the closed configured namespace set must
    never be signed — fail closed, no host-only trust."""
    async with conn() as c:
        await c.execute(
            "INSERT INTO artifact_versions "
            "(id, artifact_id, checksum, content_type, size_bytes, "
            " storage_object, storage_bucket, storage_generation, "
            " actor_type, actor_id, ordinal) "
            "VALUES ('ver_00000000000000ac', $1, 'sha256:02', 'text/plain', 2, "
            "        'cas/legacy/demo3', 'foreign-bucket-demo', 5, "
            "        'agent', $2, 97)",
            seeded["artifact_id"], AGENT,
        )
    resp = await _mint(
        http, seeded["drive_id"],
        version_target(seeded["artifact_id"], "ver_00000000000000ac"),
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "DOWNLOAD_SIGNING_UNAVAILABLE"


async def test_signer_failure_returns_safe_503(
    http, override_actor, seeded, enabled_transfer, monkeypatch,
):
    from agentdrive.api import v0_download_capabilities as dl_api
    from agentdrive.storage_transfers import GenerationDownloadSigner

    def broken_signer(*args, **kwargs):
        raise RuntimeError("no identity for https://storage-dl.example/leak")

    def _build():
        return GenerationDownloadSigner(
            download_endpoint=DL_ENDPOINT,
            namespaces={
                settings.gcs_bucket: "cas/",
                FAKE_TRANSFER_BUCKET: "immutable/",
            },
            url_signer=broken_signer,
            clock=lambda: FIXED_NOW,
        )

    monkeypatch.setattr(dl_api, "capability_signer", _build)

    resp = await _mint(http, seeded["drive_id"], artifact_target(seeded["artifact_id"]))
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "DOWNLOAD_SIGNING_UNAVAILABLE"
    assert "https://" not in json.dumps(body)
    assert "RuntimeError" not in json.dumps(body)
    assert "download" not in body  # no null target, no partial capability
    # B8 owns retry policy: no invented Retry-After on this refusal.
    assert "Retry-After" not in resp.headers


async def test_hostile_signer_outputs_fail_closed(
    http, override_actor, seeded, enabled_transfer, monkeypatch,
):
    """The mint validates its own signer's output semantically before
    returning it — host, bucket, path, generation, semantic response
    params, V4 field set, expiry, duplicates, fragments, and authority."""
    from agentdrive.api import v0_download_capabilities as dl_api
    from agentdrive.storage_transfers import GenerationDownloadSigner

    row = await _version_row(seeded["v2"])
    bucket = row["storage_bucket"]
    object_name = row["storage_object"]
    generation = row["storage_generation"]
    good_disposition = (
        'attachment; filename="notes.txt"; filename*=UTF-8\'\'notes.txt'
    )
    good_query = _v4_query(
        generation, 300, "application/octet-stream", good_disposition
    )
    good_path = f"/{bucket}/{quote(object_name, safe='/')}"

    hostile_urls = [
        # plaintext scheme
        f"http://{urlsplit(DL_ENDPOINT).netloc}{good_path}?{good_query}",
        # host-only trust is insufficient: wrong host entirely
        f"https://evil.example{good_path}?{good_query}",
        # alternate authority: userinfo
        f"https://user@{urlsplit(DL_ENDPOINT).netloc}{good_path}?{good_query}",
        # alternate authority: port
        f"https://{urlsplit(DL_ENDPOINT).netloc}:8443{good_path}?{good_query}",
        # wrong bucket on the right host
        f"{DL_ENDPOINT}/other-bucket-demo/{quote(object_name, safe='/')}?{good_query}",
        # wrong object path
        f"{DL_ENDPOINT}/{bucket}/cas/other-object?{good_query}",
        # ambiguous encoded slash in the path
        f"{DL_ENDPOINT}/{bucket}/{quote(object_name, safe='')}?{good_query}",
        # unpinned generation
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "application/octet-stream", good_disposition,
            generation=None,
        ),
        # WRONG generation
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            999999, 300, "application/octet-stream", good_disposition,
        ),
        # duplicate generation parameter
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "application/octet-stream", good_disposition,
            **{"_dupe_generation": "999999"},
        ),
        # unsafe response type
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "text/html", good_disposition,
        ),
        # inline disposition
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "application/octet-stream", "inline",
        ),
        # excessive expiry on the returned target
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "application/octet-stream", good_disposition,
            **{"X-Goog-Expires": "604800"},
        ),
        # missing signature
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "application/octet-stream", good_disposition,
            **{"X-Goog-Signature": None},
        ),
        # unexpected extra security-relevant key
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "application/octet-stream", good_disposition,
            **{"_dupe_X-Goog-Meta-Extra": "1"},
        ),
        # fragment
        f"{DL_ENDPOINT}{good_path}?{good_query}#frag",
        # unicode digits in the expiry: isdigit()-true but not a wire
        # integer — must be the typed 503, never an escaping ValueError/500
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "application/octet-stream", good_disposition,
            **{"X-Goog-Expires": "²²²"},
        ),
        # regex-valid but calendar-invalid signing date — strptime inside
        # expiry derivation must never escape as a 500
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "application/octet-stream", good_disposition,
            **{"X-Goog-Date": "20261399T996099Z"},
        ),
        # a percent-encoded query KEY that decodes to 'generation' must not
        # satisfy the literal closed key set
        f"{DL_ENDPOINT}{good_path}?" + _v4_query(
            generation, 300, "application/octet-stream", good_disposition,
            generation=None,
        ) + f"&%67eneration={generation}",
    ]

    for hostile in hostile_urls:
        def url_signer(*args, _u=hostile, **kwargs):
            return _u

        def _build(_signer=url_signer):
            return GenerationDownloadSigner(
                download_endpoint=DL_ENDPOINT,
                namespaces={
                    settings.gcs_bucket: "cas/",
                    FAKE_TRANSFER_BUCKET: "immutable/",
                },
                url_signer=_signer,
                clock=lambda: FIXED_NOW,
            )

        monkeypatch.setattr(dl_api, "capability_signer", _build)
        resp = await _mint(
            http, seeded["drive_id"], artifact_target(seeded["artifact_id"])
        )
        assert resp.status_code == 503, hostile
        assert resp.json()["error"]["code"] == "DOWNLOAD_SIGNING_UNAVAILABLE", hostile


async def test_namespace_marker_row_corruption_fails_closed(
    http, override_actor, seeded, fake_signer,
):
    """End-to-end row corruption: a persisted object name equal to the bare
    namespace marker (or with empty segments) must return the safe 503
    without any signing."""
    for suffix, object_name in (("ad", "cas/"), ("ae", "cas//x")):
        async with conn() as c:
            await c.execute(
                "INSERT INTO artifact_versions "
                "(id, artifact_id, checksum, content_type, size_bytes, "
                " storage_object, storage_bucket, storage_generation, "
                " actor_type, actor_id, ordinal) "
                "VALUES ($1, $2, 'sha256:03', 'text/plain', 2, "
                "        $3, $4, 5, 'agent', $5, $6)",
                f"ver_000000000000{suffix}00", seeded["artifact_id"],
                object_name, settings.gcs_bucket, AGENT,
                90 + ord(suffix[1]),
            )
        signer_calls_before = len(fake_signer)
        resp = await _mint(
            http, seeded["drive_id"],
            version_target(seeded["artifact_id"], f"ver_000000000000{suffix}00"),
        )
        assert resp.status_code == 503, object_name
        assert resp.json()["error"]["code"] == "DOWNLOAD_SIGNING_UNAVAILABLE"
        assert len(fake_signer) == signer_calls_before  # never reached signing


async def test_public_grant_widens_only_in_workspace_access(
    http, override_actor, seeded, fake_signer,
):
    """Pin the deliberate public-grant semantics for Packet 5: an explicit
    AgentDrive-local `public` viewer grant satisfies local viewer
    authorization for an authenticated SAME-workspace caller, but the
    workspace/drive boundary still runs first — it never becomes
    cross-workspace access."""
    resp = await http.post(
        f"/v0/drives/{seeded['drive_id']}/grants",
        json={
            "principal_type": "public",
            "resource_type": "artifact",
            "resource_id": seeded["artifact_id"],
            "role": "viewer",
        },
        headers={"Idempotency-Key": "mint-public-grant-1"},
    )
    assert resp.status_code == 201, resp.text
    # Same workspace, no explicit personal grant: the public grant funds
    # the mint.
    override_actor(make_actor(subject=OTHER_AGENT))
    same_ws = await _mint(
        http, seeded["drive_id"], artifact_target(seeded["artifact_id"])
    )
    assert same_ws.status_code == 200, same_ws.text
    # Foreign workspace: the boundary answers first — uniform 404, the
    # public grant never applies across workspaces.
    override_actor(make_actor(workspace=WS_B))
    foreign = await _mint(
        http, seeded["drive_id"], artifact_target(seeded["artifact_id"])
    )
    assert foreign.status_code == 404


async def test_signer_construction_failure_fails_closed(
    http, override_actor, seeded, monkeypatch,
):
    """A capability-signer that cannot even be CONSTRUCTED (bad configured
    namespace) is configuration unavailability — the typed 503, never an
    escaping ValueError/500."""
    from agentdrive.api import v0_download_capabilities as dl_api

    def broken_build():
        raise ValueError("bad namespace configuration")

    monkeypatch.setattr(dl_api, "capability_signer", broken_build)
    resp = await _mint(http, seeded["drive_id"], artifact_target(seeded["artifact_id"]))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "DOWNLOAD_SIGNING_UNAVAILABLE"


async def test_production_composition_mints_through_the_singleton(
    http, override_actor, enabled_transfer, monkeypatch,
):
    """`build_download_signer()` — the real production namespace composition
    (artifact CAS bucket + transfer immutable prefix) — and the cached
    `capability_signer()` singleton path must actually construct and sign,
    with only the blocking V4 callable faked."""
    from agentdrive import storage_transfers as st
    from agentdrive.api import v0_download_capabilities as dl_api

    override_actor(make_actor())
    drive = await _create_drive(http, "prod-signer-demo", "mint-drive-3")
    artifact = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "prod.txt", "mint-art-3"
    )

    def fake_production_signer(bucket, object_name, generation, ttl_seconds,
                               response_type, disposition):
        # The singleton path runs with the REAL clock, so the fake stamps
        # the actual current instant (and a matching credential scope).
        stamp = _datetime.now(_UTC).strftime("%Y%m%dT%H%M%SZ")
        query = _v4_query(
            generation, ttl_seconds, response_type, disposition,
            **{
                "X-Goog-Date": stamp,
                "X-Goog-Credential": (
                    f"signer-demo@example.test/{stamp[:8]}"
                    "/auto/storage/goog4_request"
                ),
            },
        )
        return f"{DL_ENDPOINT}/{bucket}/{quote(object_name, safe='/')}?{query}"

    monkeypatch.setattr(st, "_production_url_signer", fake_production_signer)
    dl_api.reset_capability_signer()
    try:
        resp = await _mint(http, drive["id"], artifact_target(artifact["id"]))
        assert resp.status_code == 200, resp.text
        parts = urlsplit(resp.json()["download"]["target"]["url"])
        assert parts.path.startswith(f"/{settings.gcs_bucket}/cas/")
        # A second mint rides the cached singleton and still re-signs.
        again = await _mint(http, drive["id"], artifact_target(artifact["id"]))
        assert again.status_code == 200, again.text
    finally:
        dl_api.reset_capability_signer()


# ─── secret hygiene and no-persistence ──────────────────────────────────────


async def test_no_signed_url_reaches_logs_or_durable_state(
    http, override_actor, seeded, fake_signer, caplog,
):
    import logging

    caplog.set_level(logging.DEBUG)
    resp = await _mint(http, seeded["drive_id"], artifact_target(seeded["artifact_id"]))
    assert resp.status_code == 200
    url = resp.json()["download"]["target"]["url"]
    signature = dict(parse_qsl(urlsplit(url).query))["X-Goog-Signature"]
    assert signature not in caplog.text
    assert "X-Goog-Signature" not in caplog.text
    row = await _version_row(seeded["v2"])
    object_name = row["storage_object"]
    assert object_name not in caplog.text
    async with conn() as c:
        idem = await c.fetchval(
            "SELECT count(*) FROM idempotency_records "
            "WHERE path LIKE '%download-capabilities%'"
        )
    assert idem == 0


async def test_mint_never_redirects_or_streams(http, override_actor, seeded, fake_signer):
    resp = await _mint(http, seeded["drive_id"], artifact_target(seeded["artifact_id"]))
    assert resp.status_code == 200  # never 3xx
    assert "location" not in {k.lower() for k in resp.headers}
    assert resp.headers["Content-Type"].startswith("application/json")
    assert resp.json()["download"]["target"]["url"] is not None


async def test_content_get_surface_is_unchanged(http, override_actor, seeded, fake_signer):
    """§12 compatibility: the mint does not replace or reroute the existing
    conditional content GET (which may stream inline bytes)."""
    resp = await http.get(
        f"/v0/drives/{seeded['drive_id']}/artifacts/{seeded['artifact_id']}/content"
    )
    assert resp.status_code == 200
    assert resp.content == b"goodbye now"
