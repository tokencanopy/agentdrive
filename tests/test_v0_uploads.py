"""B3 packet 3: the four raw direct-upload session controls (§5/§6/§7).

Governing contract: TokenCanopy
``docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md``.

Everything here runs the REAL mounted routes over real Postgres, with the
provider behind an in-memory ``FakeTransferStorage`` implementing packet 2's
adapter interface — the injected seam the design mandates so the
transactional core is provable without a live bucket. All identifiers,
hosts, and byte strings are synthetic.

Sections:
  * contract statics (manifest rows, idempotency classes)
  * disabled-first gate (503 TRANSFER_DISABLED, no fallback)
  * auth / scope / anti-enumeration
  * begin: strict schema, CRC32C canonicalization, size window, target
    union, saga crash points, one-provider-credential, replay semantics
  * status: non-secret shape, ETag/304
  * cancel: If-Match fencing, exactly-once release, terminal semantics
  * complete: adoption, integrity classification, races, durable replay
  * concurrency + secret-hygiene proofs
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from dataclasses import replace

import pytest
import pytest_asyncio

from agentdrive.api import v0_uploads as uploads_api
from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.core import v0_uploads as uploads_core
from agentdrive.core.v0_uploads import ObjectObservation
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext
from agentdrive.storage_transfers import (
    InitiationSigningUnavailableError,
    RewriteResult,
    SignedInitiation,
    TransferPreconditionFailedError,
    TransferProviderUnavailableError,
)

pytestmark = [
    pytest.mark.asyncio,
    # The direct-transfer surface is GCS-only; the filesystem store refuses it.
    pytest.mark.skipif(settings.storage_backend != "gcs", reason="direct transfer is GCS-only"),
]

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
OTHER_AGENT = "tcagt_0000000000000002"
WS_A = "tcws_0000000000000001"
WS_B = "tcws_0000000000000002"

FAKE_UPLOAD_ENDPOINT = "https://storage.example"
FAKE_BUCKET = "transfer-bucket-demo"


def crc32c_of(data: bytes) -> str:
    """Canonical padded base64 CRC32C of ``data`` (test-local, pure zlib is
    NOT crc32c — we only need a deterministic canonical 4-byte value)."""
    value = zlib.crc32(data) & 0xFFFFFFFF
    return base64.b64encode(struct.pack(">I", value)).decode("ascii")


CRC_A = crc32c_of(b"hello there")  # canonical 4-byte padded base64
SIZE_A = 11


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
            "TRUNCATE idempotency_records, drives, workspace_storage "
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
    """Flip the runtime settings to a complete, bounded §9 policy.

    Boot-time validation is exercised by tests/test_transfer_config.py; here
    we patch the already-validated runtime attributes, exactly as the packet
    1 accounting tests do."""
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
        settings, "direct_transfer_upload_endpoint", FAKE_UPLOAD_ENDPOINT
    )
    monkeypatch.setattr(
        settings, "direct_transfer_download_endpoint", "https://storage-dl.example"
    )
    monkeypatch.setattr(settings, "direct_transfer_bucket", FAKE_BUCKET)
    monkeypatch.setattr(settings, "direct_transfer_scratch_prefix", "scratch/")
    monkeypatch.setattr(settings, "direct_transfer_immutable_prefix", "immutable/")
    monkeypatch.setattr(settings, "direct_download_capability_ttl_seconds", 300)
    return settings


class FakeObject:
    def __init__(self, *, generation, size, crc32c, content_type, metadata):
        self.generation = generation
        self.size = size
        self.crc32c = crc32c
        self.content_type = content_type
        self.metadata = dict(metadata)


class FakeTransferStorage:
    """In-memory stand-in for packet 2's XmlTransferStorage, same call
    surface, plus fault-injection hooks and a provider-credential meter."""

    def __init__(self):
        self.initiations: list = []
        self.pending: dict[str, dict] = {}
        self.objects: dict[str, FakeObject] = {}
        self._generation = 100
        self.rewrite_calls = 0
        self.stat_calls = 0
        # fault hooks: exceptions raised on the NEXT matching call
        self.fail_initiate: Exception | None = None
        self.fail_stat: Exception | None = None
        self.fail_stat_always: Exception | None = None
        self.fail_rewrite: Exception | None = None
        # concurrency hooks: when set, the NEXT stat blocks on the gate
        # after signalling entry — lets a test hold a worker mid-provider.
        self.stall_next_stat: asyncio.Event | None = None
        self.stat_entered = asyncio.Event()
        # when True, the rewrite performs its side effect (dest object
        # exists) and THEN raises — the ambiguous-success shape
        self.rewrite_succeeds_then_fails = False
        self.rewrite_continuation_steps = 0  # >0: emit N not-done steps first
        self._continuations_seen: list[str | None] = []

    def _next_generation(self) -> int:
        self._generation += 1
        return self._generation

    async def sign_resumable_initiation(self, request) -> SignedInitiation:
        """One SIGNED initiation bundle per disclosure. No provider call
        happens here (2026-08-20 amendment); the meter below still counts
        credentials minted, which is what the one-disclosure tests pin."""
        if self.fail_initiate is not None:
            exc, self.fail_initiate = self.fail_initiate, None
            raise exc
        self.initiations.append(request)
        self.pending[request.object_name] = {
            "content_type": request.content_type,
            "adoption_marker": request.adoption_marker,
        }
        from datetime import UTC, datetime, timedelta

        return SignedInitiation(
            url=(
                f"{FAKE_UPLOAD_ENDPOINT}/{FAKE_BUCKET}/{request.object_name}"
                f"?X-Goog-Algorithm=GOOG4-RSA-SHA256"
                f"&X-Goog-Signature=fake-signed-{len(self.initiations)}"
            ),
            required_headers={
                "x-goog-resumable": "start",
                "Content-Type": request.content_type,
                "x-goog-meta-adoption-marker": request.adoption_marker,
            },
            expires_at=datetime.now(UTC) + timedelta(seconds=600),
        )

    def finalize_scratch(
        self, object_name: str, *, size: int, crc32c: str,
        content_type: str | None = None, marker: str | None = None,
    ) -> FakeObject:
        """Simulate the external client finishing its resumable PUTs."""
        pending = self.pending.get(object_name, {})
        obj = FakeObject(
            generation=self._next_generation(),
            size=size,
            crc32c=crc32c,
            content_type=content_type or pending.get("content_type", "text/plain"),
            metadata={
                "adoption-marker": (
                    marker if marker is not None else pending.get("adoption_marker")
                ),
            },
        )
        self.objects[object_name] = obj
        return obj

    def _observe(self, object_name: str) -> ObjectObservation | None:
        obj = self.objects.get(object_name)
        if obj is None:
            return None
        return ObjectObservation(
            object_name=object_name,
            generation=obj.generation,
            size=obj.size,
            crc32c=obj.crc32c,
            content_type=obj.content_type,
            adoption_marker=obj.metadata.get("adoption-marker"),
            source_fingerprint=obj.metadata.get("source-fingerprint"),
        )

    async def stat_generation(self, object_name, generation=None):
        self.stat_calls += 1
        if self.stall_next_stat is not None:
            gate, self.stall_next_stat = self.stall_next_stat, None
            self.stat_entered.set()
            await gate.wait()
        if self.fail_stat_always is not None:
            raise self.fail_stat_always
        if self.fail_stat is not None:
            exc, self.fail_stat = self.fail_stat, None
            raise exc
        observation = self._observe(object_name)
        if observation is None:
            return None
        if generation is not None and observation.generation != generation:
            raise TransferProviderUnavailableError("stat_mismatch")
        return observation

    async def stat_object(self, object_name):
        return await self.stat_generation(object_name, None)

    async def rewrite_generation_create_only(
        self, source, destination_name, *, content_type, adoption_marker,
        source_fingerprint, continuation=None,
    ):
        self.rewrite_calls += 1
        self._continuations_seen.append(continuation)
        if self.fail_rewrite is not None:
            exc, self.fail_rewrite = self.fail_rewrite, None
            if self.rewrite_succeeds_then_fails:
                self._do_rewrite(
                    source, destination_name, content_type=content_type,
                    adoption_marker=adoption_marker,
                    source_fingerprint=source_fingerprint,
                )
            raise exc
        if self.rewrite_continuation_steps > 0:
            self.rewrite_continuation_steps -= 1
            return RewriteResult(
                done=False,
                continuation=f"cont-{self.rewrite_continuation_steps}",
            )
        return RewriteResult(
            done=True,
            generation=self._do_rewrite(
                source, destination_name, content_type=content_type,
                adoption_marker=adoption_marker,
                source_fingerprint=source_fingerprint,
            ),
        )

    def _do_rewrite(
        self, source, destination_name, *, content_type, adoption_marker,
        source_fingerprint,
    ) -> int:
        src = self.objects.get(source.object_name)
        if src is None or src.generation != source.generation:
            raise TransferPreconditionFailedError("rewrite_precondition", status=404)
        if destination_name in self.objects:
            raise TransferPreconditionFailedError("rewrite_precondition", status=412)
        obj = FakeObject(
            generation=self._next_generation(),
            size=src.size,
            crc32c=src.crc32c,
            content_type=content_type,
            metadata={
                "adoption-marker": adoption_marker,
                "source-fingerprint": source_fingerprint,
            },
        )
        self.objects[destination_name] = obj
        return obj.generation

    async def delete_generation(self, object_name, generation):
        obj = self.objects.get(object_name)
        if obj is None:
            return
        if obj.generation != generation:
            raise TransferPreconditionFailedError("delete_precondition", status=412)
        del self.objects[object_name]


@pytest.fixture
def fake_storage(monkeypatch):
    from agentdrive.api import v0_uploads as uploads_api

    fake = FakeTransferStorage()
    monkeypatch.setattr(uploads_api, "transfer_storage", lambda: fake)
    return fake


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


def artifact_body(
    parent: str, name: str = "notes.txt", *, size: int = SIZE_A,
    crc: str = CRC_A, media: str = "text/plain",
) -> dict:
    return {
        "target": {"kind": "artifact", "parent_folder_id": parent, "name": name},
        "content": {
            "size_bytes": size,
            "media_type": media,
            "checksum": {"algorithm": "crc32c", "value": crc},
        },
    }


def version_body(artifact_id: str, *, size: int = SIZE_A, crc: str = CRC_A) -> dict:
    return {
        "target": {"kind": "version", "artifact_id": artifact_id},
        "content": {
            "size_bytes": size,
            "media_type": "text/plain",
            "checksum": {"algorithm": "crc32c", "value": crc},
        },
    }


async def _begin(http, drive_id: str, body: dict, key: str, **headers):
    return await http.post(
        f"/v0/drives/{drive_id}/uploads",
        content=json.dumps(body),
        headers={
            "Idempotency-Key": key,
            "Content-Type": "application/json; charset=utf-8",
            **headers,
        },
    )


async def _status(http, drive_id: str, upload_id: str, **headers):
    return await http.get(
        f"/v0/drives/{drive_id}/uploads/{upload_id}", headers=headers
    )


async def _cancel(http, drive_id, upload_id, key, if_match=None, **headers):
    hdrs = {"Idempotency-Key": key, **headers}
    if if_match is not None:
        hdrs["If-Match"] = if_match
    return await http.delete(
        f"/v0/drives/{drive_id}/uploads/{upload_id}", headers=hdrs
    )


async def _complete(http, drive_id, upload_id, key, **headers):
    return await http.post(
        f"/v0/drives/{drive_id}/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": key, **headers},
    )


async def _session_row(upload_id: str):
    async with conn() as c:
        return await c.fetchrow(
            "SELECT * FROM upload_sessions WHERE id = $1", upload_id
        )


async def _begin_active(
    http, drive_id: str, body: dict, key: str, **headers,
) -> dict:
    resp = await _begin(http, drive_id, body, key, **headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["upload"]


async def _create_artifact(http, drive_id: str, parent: str, name: str, key: str) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive_id}/artifacts",
        files={"content": (name, b"hello there", "text/plain")},
        data={"parent_id": parent, "name": name},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ─── contract statics ───────────────────────────────────────────────────────


async def test_manifest_pins_the_four_upload_operations():
    from agentdrive.api.v0_manifest import operations

    by_id = {o["operation_id"]: o for o in operations}
    expected = {
        "uploads_create": (
            "POST", "/v0/drives/{drive_id}/uploads",
            "creation-flavored", [200, 201], "required",
        ),
        "uploads_read": (
            "GET", "/v0/drives/{drive_id}/uploads/{upload_id}",
            "read", [200, 304], "not_required",
        ),
        "uploads_delete": (
            "DELETE", "/v0/drives/{drive_id}/uploads/{upload_id}",
            "mutation-of-existing", [200], "required",
        ),
        "uploads_complete": (
            "POST", "/v0/drives/{drive_id}/uploads/{upload_id}/complete",
            "creation-flavored", [200, 201], "required",
        ),
    }
    for op_id, (method, path, pclass, statuses, iclass) in expected.items():
        op = by_id.get(op_id)
        assert op is not None, f"{op_id} missing from the manifest"
        assert op["method"] == method
        assert op["path"] == path
        assert op["precondition_class"] == pclass
        assert sorted(op["expected_statuses"]) == sorted(statuses)
        assert op["idempotency_class"] == iclass
        assert op["scopes"] == ["content:write"]
        assert op["domain"] == "uploads"
        assert op["shape_fixture"] == "upload-session"


async def test_every_manifest_operation_declares_a_closed_idempotency_class():
    from agentdrive.api.v0_manifest import operations

    for op in operations:
        assert op.get("idempotency_class") in ("required", "not_required", "forbidden"), op[
            "operation_id"
        ]
        if op["operation_id"] == "download_capabilities_create":
            # The one forbidden operation (packet 4 §5.7): a supplied key is
            # 400 INVALID_REQUEST and never stored.
            assert op["idempotency_class"] == "forbidden"
        elif op["precondition_class"] == "read":
            assert op["idempotency_class"] == "not_required", op["operation_id"]
        else:
            assert op["idempotency_class"] == "required", op["operation_id"]


async def test_new_error_codes_are_registered():
    from agentdrive.api.error_codes import ERROR_CODES

    for code in (
        "TRANSFER_DISABLED", "TRANSFER_UNAVAILABLE", "TRANSFER_LIMIT_EXCEEDED",
        "UPLOAD_BUSY", "UPLOAD_INCOMPLETE", "UPLOAD_NOT_COMPLETABLE",
        "UPLOAD_ALREADY_COMPLETED", "UPLOAD_EXPIRED", "NAME_CONFLICT",
        "PAYLOAD_TOO_LARGE", "OBJECT_SIZE_MISMATCH", "OBJECT_METADATA_MISMATCH",
        "NOT_ACCEPTABLE",
    ):
        assert code in ERROR_CODES, code


# ─── disabled-first gate ────────────────────────────────────────────────────


async def test_all_four_controls_return_transfer_disabled_by_default(
    http, override_actor,
):
    override_actor(make_actor())
    drive_id = "drv_00000000000000aa"
    upload_id = "upld_00000000000000bb"
    responses = [
        await http.post(f"/v0/drives/{drive_id}/uploads", json={}),
        await http.get(f"/v0/drives/{drive_id}/uploads/{upload_id}"),
        await http.delete(f"/v0/drives/{drive_id}/uploads/{upload_id}"),
        await http.post(f"/v0/drives/{drive_id}/uploads/{upload_id}/complete"),
    ]
    for resp in responses:
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"]["code"] == "TRANSFER_DISABLED"
        assert resp.headers["Cache-Control"] == "no-store"
        assert "Retry-After" not in resp.headers


async def test_unresolved_generation_rows_keep_transfer_disabled(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "ud", "k-ud")
    art = await _create_artifact(http, drive["id"], drive["root_folder_id"], "a.txt", "k-ud-a")
    async with conn() as c:
        # Simulate a legacy CAS row awaiting reconciliation (the trigger
        # permits NULL coordinates at insert; only NULL → value may change).
        await c.execute(
            "INSERT INTO artifact_versions "
            "(id, artifact_id, parent_version_id, checksum, content_type, "
            " size_bytes, storage_object, actor_type, actor_id, ordinal) "
            "VALUES ('ver_00000000000000fe', $1, $2, 'sha256:' || repeat('a', 64), "
            " 'text/plain', 11, 'legacy/object', 'agent', $3, 2)",
            art["id"], art["head_version_id"], AGENT,
        )
    resp = await _begin(http, drive["id"], artifact_body(drive["root_folder_id"]), "k-ud-b")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "TRANSFER_DISABLED"


# ─── auth / scope / anti-enumeration ────────────────────────────────────────


async def test_upload_controls_require_auth(http, enabled_transfer):
    resp = await http.get("/v0/drives/drv_00000000000000aa/uploads/upld_00000000000000bb")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_upload_controls_require_content_write_scope(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "sc", "k-sc")
    override_actor(make_actor(scopes={"content:read"}))
    for resp in (
        await _begin(http, drive["id"], artifact_body(drive["root_folder_id"]), "k-sc1"),
        await _status(http, drive["id"], "upld_00000000000000bb"),
        await _cancel(http, drive["id"], "upld_00000000000000bb", "k-sc2", '"x"'),
        await _complete(http, drive["id"], "upld_00000000000000bb", "k-sc3"),
    ):
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_cross_workspace_drive_is_absent(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "xw", "k-xw")
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp = await _begin(http, drive["id"], artifact_body("fld_0000000000000001"), "k-xw1")
    assert resp.status_code == 404
    resp = await _status(http, drive["id"], "upld_00000000000000bb")
    assert resp.status_code == 404


async def test_unknown_upload_is_the_same_404(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "uk", "k-uk")
    resp = await _status(http, drive["id"], "upld_00000000000000bb")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_session_is_bound_to_its_initiating_principal(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "pb", "k-pb")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-pb1"
    )
    # Another agent in the same workspace, even with a drive-wide grant,
    # gets the anti-enumerating 404 (session principal binding, §8).
    override_actor(make_actor())
    grant = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "resource_type": "drive", "resource_id": drive["id"],
            "principal_type": "agent", "principal_id": OTHER_AGENT,
            "role": "manager",
        },
        headers={"Idempotency-Key": "k-pb-grant"},
    )
    assert grant.status_code == 201, grant.text
    override_actor(make_actor(subject=OTHER_AGENT))
    resp = await _status(http, drive["id"], upload["id"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ─── begin: strict request validation ───────────────────────────────────────


async def test_begin_requires_idempotency_key(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "ik", "k-ik")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/uploads",
        json=artifact_body(drive["root_folder_id"]),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_begin_rejects_non_json_content_type(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "ct", "k-ct")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/uploads",
        content=b"parent=fld",
        headers={
            "Idempotency-Key": "k-ct1",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


async def test_begin_rejects_unacceptable_accept(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "ac", "k-ac")
    resp = await _begin(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-ac1",
        Accept="text/html",
    )
    assert resp.status_code == 406
    assert resp.json()["error"]["code"] == "NOT_ACCEPTABLE"


async def test_begin_rejects_duplicate_json_keys(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "dj", "k-dj")
    raw = (
        '{"target": {"kind": "artifact", '
        f'"parent_folder_id": "{drive["root_folder_id"]}", '
        '"name": "a.txt", "name": "b.txt"}, "content": {"size_bytes": 11, '
        '"media_type": "text/plain", '
        f'"checksum": {{"algorithm": "crc32c", "value": "{CRC_A}"}}}}'
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/uploads",
        content=raw.encode(),
        headers={"Idempotency-Key": "k-dj1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update({"extra": 1}),                       # unknown top field
        lambda b: b["target"].update({"kind": "folder"}),       # unknown discriminator
        lambda b: b["target"].update({"artifact_id": "art_0000000000000001"}),
        lambda b: b["content"].update({"sha256": "ab" * 32}),   # sha256 rejected
        lambda b: b["content"]["checksum"].update({"algorithm": "md5"}),
        lambda b: b["content"].update({"size_bytes": "11"}),    # non-integer size
        lambda b: b["content"].update({"media_type": ""}),
        lambda b: b["content"].update({"media_type": "text/plain; charset=utf-8"}),
        lambda b: b["target"].update({"parent_folder_id": "folder-1"}),
        lambda b: b.pop("content"),
    ],
)
async def test_begin_strict_schema_rejections(
    http, override_actor, enabled_transfer, fake_storage, mutate,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "ss", "k-ss")
    body = artifact_body(drive["root_folder_id"])
    mutate(body)
    resp = await _begin(http, drive["id"], body, "k-ss1")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "value",
    [
        "yZRlqg",        # unpadded
        "yZRlq-==",      # url-safe alphabet
        "yZRlqgAA==",    # overlong (>4 bytes shape)
        "yZRlqh==",      # non-canonical trailing bits (re-encode differs)
        "zzz",           # garbage
        "",
    ],
)
async def test_begin_rejects_non_canonical_crc32c(
    http, override_actor, enabled_transfer, fake_storage, value,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "cc", "k-cc")
    resp = await _begin(
        http, drive["id"],
        artifact_body(drive["root_folder_id"], crc=value), "k-cc1",
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


async def test_begin_size_window(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "sw", "k-sw")
    root = drive["root_folder_id"]
    over = await _begin(
        http, drive["id"], artifact_body(root, size=10 * 1024 * 1024 + 1), "k-sw1"
    )
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    zero = await _begin(http, drive["id"], artifact_body(root, size=0), "k-sw2")
    assert zero.status_code == 400
    assert zero.json()["error"]["code"] == "INVALID_REQUEST"
    negative = await _begin(http, drive["id"], artifact_body(root, size=-1), "k-sw3")
    assert negative.status_code == 400


async def test_beta_file_ceiling_refuses_before_target_issuance(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    override_actor(make_actor())
    monkeypatch.setattr(
        settings, "direct_transfer_max_bytes", settings.max_file_bytes
    )
    drive = await _create_drive(http, "beta-file", "k-beta-file")

    response = await _begin(
        http,
        drive["id"],
        artifact_body(
            drive["root_folder_id"],
            size=settings.max_file_bytes + 1,
        ),
        "k-beta-file-over",
    )

    assert response.status_code == 413
    assert response.json()["error"]["details"]["limit"] == 1024**3
    assert fake_storage.initiations == []


async def test_upload_hour_exhaustion_refuses_before_target_issuance(
    http, override_actor, enabled_transfer, fake_storage,
):
    actor = make_actor()
    override_actor(
        replace(
            actor,
            drive_limits=replace(
                actor.drive_limits,
                upload_bytes_hour_principal=10,
                upload_bytes_hour_workspace=10,
            ),
        )
    )
    drive = await _create_drive(http, "upload-hour", "k-upload-hour")

    response = await _begin(
        http,
        drive["id"],
        artifact_body(drive["root_folder_id"], size=11),
        "k-upload-hour-over",
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "BANDWIDTH_LIMIT_EXCEEDED"
    assert fake_storage.initiations == []
    async with conn() as connection:
        assert await connection.fetchval("SELECT count(*) FROM upload_sessions") == 0


async def test_begin_artifact_target_rejects_if_match(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "am", "k-am")
    resp = await _begin(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-am1",
        **{"If-Match": '"rev_0000000000000001"'},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


async def test_begin_version_target_requires_if_match(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "vm", "k-vm")
    art = await _create_artifact(http, drive["id"], drive["root_folder_id"], "v.txt", "k-vm-a")
    missing = await _begin(http, drive["id"], version_body(art["id"]), "k-vm1")
    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "PRECONDITION_REQUIRED"
    stale = await _begin(
        http, drive["id"], version_body(art["id"]), "k-vm2",
        **{"If-Match": '"rev_00000000000000ff"'},
    )
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "PRECONDITION_FAILED"
    # A precondition failure must not burn the key (§7.2).
    ok = await _begin(
        http, drive["id"], version_body(art["id"]), "k-vm2",
        **{"If-Match": f'"{art["revision"]}"'},
    )
    assert ok.status_code == 201, ok.text


async def test_begin_unknown_query_param_rejected(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "qp", "k-qp")
    resp = await http.post(
        f"/v0/drives/{drive['id']}/uploads?verbose=1",
        content=json.dumps(artifact_body(drive["root_folder_id"])),
        headers={"Idempotency-Key": "k-qp1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


async def test_malformed_ids_are_invalid_request(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    resp = await _status(http, "drv_zz", "upld_00000000000000bb")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"
    resp = await _status(http, "drv_00000000000000aa", "upld_nope")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


# ─── begin: success, disclosure, replay ─────────────────────────────────────


async def test_begin_artifact_success_shape(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "bs", "k-bs")
    resp = await _begin(http, drive["id"], artifact_body(drive["root_folder_id"]), "k-bs1")
    assert resp.status_code == 201, resp.text
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Location"].endswith(
        f"/v0/drives/{drive['id']}/uploads/" + resp.json()["upload"]["id"]
    )
    assert resp.headers["ETag"]
    upload = resp.json()["upload"]
    assert upload["state"] == "active"
    assert upload["drive_id"] == drive["id"]
    assert upload["target"] == {
        "kind": "artifact",
        "parent_folder_id": drive["root_folder_id"],
        "name": "notes.txt",
    }
    assert upload["content"]["checksum"] == {"algorithm": "crc32c", "value": CRC_A}
    assert upload["target_disclosed"] is True
    assert upload["restart_required"] is False
    assert upload["result"] is None
    transfer = upload["transfer"]
    assert transfer["chunk_protocol"] == "gcs-xml-resumable"
    initiation = transfer["initiation"]
    assert initiation["method"] == "POST"
    assert initiation["url"].startswith(FAKE_UPLOAD_ENDPOINT)
    assert initiation["expires_at"]
    assert transfer["chunks"] == {
        "method": "PUT",
        "required_headers": {"Content-Type": "text/plain"},
    }
    assert len(fake_storage.initiations) == 1
    # The signed target used the server-selected scratch key + marker, and
    # the disclosed header set is EXACTLY the signed one.
    row = await _session_row(upload["id"])
    assert row["state"] == "active"
    assert fake_storage.initiations[0].object_name == row["scratch_object"]
    assert fake_storage.initiations[0].adoption_marker == row["adoption_marker"]
    assert initiation["required_headers"] == {
        "x-goog-resumable": "start",
        "Content-Type": "text/plain",
        "x-goog-meta-adoption-marker": row["adoption_marker"],
    }
    assert row["scratch_object"].startswith("scratch/")
    assert row["final_object"].startswith("immutable/")
    # One linked live reservation for the declared bytes.
    async with conn() as c:
        reservation = await c.fetchrow(
            "SELECT size_bytes, released_at FROM storage_reservations "
            "WHERE upload_id = $1",
            upload["id"],
        )
    assert reservation["size_bytes"] == SIZE_A
    assert reservation["released_at"] is None


async def test_direct_upload_begin_stores_canonical_name(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "direct", "k-direct-drive")
    begun = await _begin(
        http,
        drive["id"],
        artifact_body(drive["root_folder_id"], name="e\u0301" * 255),
        "k-direct-begin",
    )
    assert begun.status_code == 201, begun.text
    assert begun.json()["upload"]["target"]["name"] == "é" * 255


async def test_direct_upload_target_model_publishes_item_name_length_bounds() -> None:
    name_schema = uploads_api._TargetArtifactIn.model_json_schema()["properties"]["name"]

    assert name_schema["minLength"] == 1
    assert name_schema["maxLength"] == 255


async def test_begin_same_key_replay_never_reissues_the_target(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "rp", "k-rp")
    body = artifact_body(drive["root_folder_id"])
    first = await _begin(http, drive["id"], body, "k-rp1")
    assert first.status_code == 201
    replay = await _begin(http, drive["id"], body, "k-rp1")
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("Idempotent-Replay") == "true"
    upload = replay.json()["upload"]
    assert upload["id"] == first.json()["upload"]["id"]
    assert upload["target_disclosed"] is True
    assert upload["restart_required"] is True
    assert "transfer" not in upload
    assert len(fake_storage.initiations) == 1  # never a second credential


async def test_begin_same_key_different_body_conflicts(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "sk", "k-sk")
    root = drive["root_folder_id"]
    first = await _begin(http, drive["id"], artifact_body(root, name="a.txt"), "k-sk1")
    assert first.status_code == 201
    other = await _begin(http, drive["id"], artifact_body(root, name="b.txt"), "k-sk1")
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_begin_different_key_equivalent_target_conflicts(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "eq", "k-eq")
    body = artifact_body(drive["root_folder_id"])
    first = await _begin(http, drive["id"], body, "k-eq1")
    assert first.status_code == 201
    second = await _begin(http, drive["id"], body, "k-eq2")
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "CONFLICT"
    assert len(fake_storage.initiations) == 1
    # After the first session is terminal, a new key may begin fresh.
    upload_id = first.json()["upload"]["id"]
    row = await _session_row(upload_id)
    etag = f'"{upload_id}.{row["session_revision"]}"'
    cancelled = await _cancel(http, drive["id"], upload_id, "k-eq3", etag)
    assert cancelled.status_code == 200, cancelled.text
    third = await _begin(http, drive["id"], body, "k-eq4")
    assert third.status_code == 201, third.text


async def test_begin_active_session_ceiling(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    override_actor(make_actor())
    monkeypatch.setattr(settings, "direct_transfer_max_active_sessions_principal", 1)
    drive = await _create_drive(http, "ce", "k-ce")
    root = drive["root_folder_id"]
    first = await _begin(http, drive["id"], artifact_body(root, name="a.txt"), "k-ce1")
    assert first.status_code == 201
    second = await _begin(http, drive["id"], artifact_body(root, name="b.txt"), "k-ce2")
    assert second.status_code == 422, second.text
    assert second.json()["error"]["code"] == "TRANSFER_LIMIT_EXCEEDED"


async def _grant_service(drive_id: str, subject: str = "tcsvc_0000000000000001") -> None:
    """Grant the service principal manager on the drive, as Hub's attachment
    lane does when it provisions the managed drive."""
    from agentdrive.core.ids import new_id

    async with conn() as c:
        await c.execute(
            "INSERT INTO grants (id, drive_id, resource_type, resource_id, "
            "  principal_type, principal_id, role, revision) "
            "VALUES ($1, $2, 'drive', $2, 'service', $3, 'manager', $4)",
            new_id("grn"), drive_id, subject, new_id("rev"),
        )


def make_service_actor(subject: str = "tcsvc_0000000000000001") -> V0ActorContext:
    """A SERVICE principal in its real shape.

    `make_actor`'s non-agent branch describes a USER — it sets `membership_id`
    and a workspace role. Hub's token contract forbids `membership_id` on the
    service branch precisely because a service has no workspace membership, so
    building one that way produces an actor the authorization path does not
    recognise and every drive lookup answers 404."""
    return V0ActorContext(
        subject=subject,
        subject_type="service",
        workspace_id=WS_A,
        membership_id=None,
        token_id="tctok_0000000000000001",
        scopes=frozenset({
            "drives:read", "drives:write", "usage:read",
            "content:read", "content:write", "sharing:read", "sharing:write",
        }),
        credential_id=None,
        runtime_id=None,
        sponsor_id=None,
        workspace_role=None,
    )


async def test_service_principal_is_exempt_from_the_per_principal_ceiling(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """A SERVICE principal fronts a whole workspace under one subject, so the
    per-principal bound would cap every member at one person's budget.

    Chat's attachment lane hit exactly that in staging: three concurrent
    uploads for the entire workspace, and 422 TRANSFER_LIMIT_EXCEEDED on the
    fourth — including sessions stranded by an unrelated failure, which hold
    a slot for the whole TTL. The drive and workspace bounds still apply; this
    is an exemption from ONE axis, not from all of them."""
    override_actor(make_actor())
    drive = await _create_drive(http, "sv", "k-sv")
    root = drive["root_folder_id"]
    await _grant_service(drive["id"])
    override_actor(make_service_actor())
    monkeypatch.setattr(settings, "direct_transfer_max_active_sessions_principal", 1)
    first = await _begin(http, drive["id"], artifact_body(root, name="a.txt"), "k-sv1")
    assert first.status_code == 201, first.text
    # An agent would be refused here. A service is not.
    second = await _begin(http, drive["id"], artifact_body(root, name="b.txt"), "k-sv2")
    assert second.status_code == 201, second.text


async def test_service_principal_still_obeys_the_drive_ceiling(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Exempt from one axis, not unbounded. The drive bound is what actually
    governs a shared caller, so it must still stop it."""
    override_actor(make_actor())
    drive = await _create_drive(http, "sd", "k-sd")
    root = drive["root_folder_id"]
    await _grant_service(drive["id"])
    override_actor(make_service_actor())
    monkeypatch.setattr(settings, "direct_transfer_max_active_sessions_principal", 1)
    monkeypatch.setattr(settings, "direct_transfer_max_active_sessions_drive", 1)
    first = await _begin(http, drive["id"], artifact_body(root, name="a.txt"), "k-sd1")
    assert first.status_code == 201, first.text
    blocked = await _begin(http, drive["id"], artifact_body(root, name="b.txt"), "k-sd2")
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "TRANSFER_LIMIT_EXCEEDED"


async def test_past_deadline_session_stops_holding_its_ceiling_slot(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """An abandoned upload must stop occupying a slot at its DEADLINE, not
    at the next GC sweep. Nothing terminalizes an overdue session on the
    read path, and the sweeper runs on a schedule, so counting by state
    alone held the slot for the better part of a day."""
    override_actor(make_actor())
    monkeypatch.setattr(settings, "direct_transfer_max_active_sessions_principal", 1)
    drive = await _create_drive(http, "xp", "k-xp")
    root = drive["root_folder_id"]
    first = await _begin(http, drive["id"], artifact_body(root, name="a.txt"), "k-xp1")
    assert first.status_code == 201
    # The slot is genuinely held while the session is live.
    blocked = await _begin(http, drive["id"], artifact_body(root, name="b.txt"), "k-xp2")
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "TRANSFER_LIMIT_EXCEEDED"

    async with conn() as c:
        await c.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            first.json()["upload"]["id"],
        )

    # Past its deadline the abandoned session frees the slot, with no sweep.
    after = await _begin(http, drive["id"], artifact_body(root, name="b.txt"), "k-xp3")
    assert after.status_code == 201, after.text
    # The row itself is untouched — releasing its reservation and scratch
    # object stays GC's job; only the concurrency bound moved.
    row = await _session_row(first.json()["upload"]["id"])
    assert row["state"] not in ("completed", "cancelled", "expired", "rejected")


async def test_past_deadline_session_stops_blocking_its_own_target(
    http, override_actor, enabled_transfer, fake_storage,
):
    """The equivalence check is the same story: an overdue session cannot
    publish, so it must not make re-uploading that name impossible until a
    sweep runs."""
    override_actor(make_actor())
    drive = await _create_drive(http, "xq", "k-xq")
    body = artifact_body(drive["root_folder_id"], name="same.txt")
    first = await _begin(http, drive["id"], body, "k-xq1")
    assert first.status_code == 201
    conflict = await _begin(http, drive["id"], body, "k-xq2")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "CONFLICT"

    async with conn() as c:
        await c.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            first.json()["upload"]["id"],
        )

    again = await _begin(http, drive["id"], body, "k-xq3")
    assert again.status_code == 201, again.text
    assert again.json()["upload"]["id"] != first.json()["upload"]["id"]


async def test_begin_reservation_ceiling_maps_to_transfer_limit(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    override_actor(make_actor())
    monkeypatch.setattr(
        settings, "direct_transfer_hard_logical_version_bytes_drive", SIZE_A + 5
    )
    drive = await _create_drive(http, "qr", "k-qr")
    root = drive["root_folder_id"]
    resp = await _begin(http, drive["id"], artifact_body(root, name="big.txt"), "k-qr1")
    # The drive already carries 0 committed bytes; 11 fits under 16.
    assert resp.status_code == 201, resp.text
    resp2 = await _begin(http, drive["id"], artifact_body(root, name="big2.txt"), "k-qr2")
    assert resp2.status_code == 422
    assert resp2.json()["error"]["code"] == "TRANSFER_LIMIT_EXCEEDED"
    assert resp2.json()["error"]["details"] == {
        "limit_name": "storage_bytes_drive",
        "used": 0,
        "reserved": SIZE_A,
        "limit": SIZE_A + 5,
        "requested": SIZE_A,
        "remaining": 5,
        "reset_at": None,
    }


# ─── begin: crash points ────────────────────────────────────────────────────


async def test_begin_crash_before_initiation_lease_is_resumable(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Failure AFTER the preparing commit but BEFORE the one disclosure
    lease: retryable 503, no key burn, and the SAME key later takes a
    lease and receives the one 201 disclosure. (Signing runs BEFORE the
    lease — 2026-08-20 amendment — so an undisclosed signature may exist;
    it died with the frame and is NOT a credential disclosure.)"""
    override_actor(make_actor())
    drive = await _create_drive(http, "c1", "k-c1")
    body = artifact_body(drive["root_folder_id"])

    original = uploads_core.acquire_initiation_lease
    calls = {"n": 0}

    async def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash before initiation lease")
        return await original(*args, **kwargs)

    monkeypatch.setattr(uploads_core, "acquire_initiation_lease", boom)
    first = await _begin(http, drive["id"], body, "k-c1a")
    assert first.status_code == 503, first.text
    assert first.json()["error"]["code"] == "TRANSFER_UNAVAILABLE"
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT state, provider_attempted_at FROM upload_sessions LIMIT 1"
        )
    assert row["state"] == "preparing"
    assert row["provider_attempted_at"] is None
    # One signature was minted and never disclosed; the marker is unburned.
    assert len(fake_storage.initiations) == 1

    retry = await _begin(http, drive["id"], body, "k-c1a")
    assert retry.status_code == 201, retry.text
    assert retry.json()["upload"]["transfer"]["initiation"]["url"]
    assert len(fake_storage.initiations) == 2


async def test_begin_signing_failure_leaves_the_session_retryable(
    http, override_actor, enabled_transfer, fake_storage,
):
    """2026-08-20 amendment: begin makes NO provider call — signing is pure
    computation, so a signing failure provably leaves no credential in the
    world. The session stays preparing with the marker unburned and its
    reservation live, and the SAME key retries into the one disclosure."""
    override_actor(make_actor())
    drive = await _create_drive(http, "c2", "k-c2")
    body = artifact_body(drive["root_folder_id"])
    fake_storage.fail_initiate = InitiationSigningUnavailableError("signing_failed")
    resp = await _begin(http, drive["id"], body, "k-c2a")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "TRANSFER_UNAVAILABLE"
    assert resp.headers.get("Retry-After")
    async with conn() as c:
        row = await c.fetchrow("SELECT * FROM upload_sessions LIMIT 1")
    assert row["state"] == "preparing"
    assert row["provider_attempted_at"] is None
    async with conn() as c:
        released = await c.fetchval(
            "SELECT released_at IS NULL FROM storage_reservations "
            "WHERE upload_id = $1",
            row["id"],
        )
    assert released is True  # the reservation is still LIVE
    # Same-key retry resumes the saga and receives the one disclosure.
    retry = await _begin(http, drive["id"], body, "k-c2a")
    assert retry.status_code == 201, retry.text
    assert retry.json()["upload"]["transfer"]["initiation"]["url"]
    assert len(fake_storage.initiations) == 1


async def test_begin_crash_after_uri_before_active_never_discloses(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """A signed target exists and the disclosure marker is set, but the
    active CAS did not commit: the response is an error, the signed URL is
    never disclosed or persisted, and no later path can reissue it
    (uncertain-terminal; here the immediate wire outcome is pinned)."""
    override_actor(make_actor())
    drive = await _create_drive(http, "c4", "k-c4")
    body = artifact_body(drive["root_folder_id"])

    original = uploads_core.activate_session
    calls = {"n": 0}

    async def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash before active commit")
        return await original(*args, **kwargs)

    monkeypatch.setattr(uploads_core, "activate_session", boom)
    resp = await _begin(http, drive["id"], body, "k-c4a")
    assert resp.status_code == 503, resp.text
    text = resp.text
    assert "fake-signed" not in text
    assert len(fake_storage.initiations) == 1
    async with conn() as c:
        row = await c.fetchrow("SELECT * FROM upload_sessions LIMIT 1")
    assert row["provider_attempted_at"] is not None
    # Same-key replay NEVER re-discloses once the disclosure marker is set.
    replay = await _begin(http, drive["id"], body, "k-c4a")
    assert replay.status_code in (200, 503)
    assert len(fake_storage.initiations) == 1
    if replay.status_code == 200:
        assert "transfer" not in replay.json()["upload"]


# ─── status ─────────────────────────────────────────────────────────────────


async def test_status_shape_and_304(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "st", "k-st")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-st1"
    )
    resp = await _status(http, drive["id"], upload["id"])
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    etag = resp.headers["ETag"]
    body = resp.json()["upload"]
    assert body["state"] == "active"
    assert body["target_disclosed"] is True
    assert body["restart_required"] is True
    assert body["result"] is None
    assert "transfer" not in body
    text = resp.text
    for forbidden in ("scratch", "immutable/", "generation", "principal",
                      "reservation", "continuation", "fake-signed",
                      "X-Goog-Signature"):
        assert forbidden not in text, forbidden
    not_modified = await _status(
        http, drive["id"], upload["id"], **{"If-None-Match": etag}
    )
    assert not_modified.status_code == 304
    assert not_modified.headers["ETag"] == etag
    assert not_modified.headers["Cache-Control"] == "no-store"
    assert not_modified.content == b""


async def test_status_ignores_idempotency_key_header(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "si", "k-si")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-si1"
    )
    resp = await _status(
        http, drive["id"], upload["id"], **{"Idempotency-Key": "k-si-read"}
    )
    assert resp.status_code == 200


# ─── cancel ─────────────────────────────────────────────────────────────────


async def test_cancel_requires_if_match_and_key(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "cn", "k-cn")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-cn1"
    )
    no_key = await http.delete(f"/v0/drives/{drive['id']}/uploads/{upload['id']}")
    assert no_key.status_code == 400
    assert no_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    no_match = await _cancel(http, drive["id"], upload["id"], "k-cn2")
    assert no_match.status_code == 428
    assert no_match.json()["error"]["code"] == "PRECONDITION_REQUIRED"
    stale = await _cancel(http, drive["id"], upload["id"], "k-cn3", '"upld_x.99"')
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "PRECONDITION_FAILED"
    # 412 must not disclose the current ETag (§5.1).
    assert "ETag" not in stale.headers
    assert "current_revision" not in stale.text


async def test_cancel_success_releases_exactly_once(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "cx", "k-cx")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-cx1"
    )
    status = await _status(http, drive["id"], upload["id"])
    etag = status.headers["ETag"]
    resp = await _cancel(http, drive["id"], upload["id"], "k-cx2", etag)
    assert resp.status_code == 200, resp.text
    body = resp.json()["upload"]
    assert body["state"] == "cancelled"
    assert body["result"] is None
    assert body["restart_required"] is False
    assert resp.headers["ETag"] != etag
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT released_at, release_kind FROM storage_reservations "
            "WHERE upload_id = $1",
            upload["id"],
        )
    assert row["released_at"] is not None
    assert row["release_kind"] == "released"
    # Same-key replay returns the original response with the marker.
    replay = await _cancel(http, drive["id"], upload["id"], "k-cx2", etag)
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json()["upload"]["state"] == "cancelled"
    # A NEW key with the CURRENT (cancelled) ETag returns the same status.
    new_etag = resp.headers["ETag"]
    again = await _cancel(http, drive["id"], upload["id"], "k-cx3", new_etag)
    assert again.status_code == 200
    assert again.json()["upload"]["state"] == "cancelled"
    # Still exactly one release.
    async with conn() as c:
        releases = await c.fetchval(
            "SELECT count(*) FROM storage_reservations "
            "WHERE upload_id = $1 AND released_at IS NOT NULL",
            upload["id"],
        )
    assert releases == 1


async def test_cancel_of_preparing_session_is_busy(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "cp", "k-cp")
    body = artifact_body(drive["root_folder_id"])

    original = uploads_core.acquire_initiation_lease
    calls = {"n": 0}

    async def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("keep it preparing")
        return await original(*args, **kwargs)

    monkeypatch.setattr(uploads_core, "acquire_initiation_lease", boom)
    resp = await _begin(http, drive["id"], body, "k-cp1")
    assert resp.status_code == 503
    async with conn() as c:
        row = await c.fetchrow("SELECT id, session_revision FROM upload_sessions LIMIT 1")
    etag = f'"{row["id"]}.{row["session_revision"]}"'
    cancel = await _cancel(http, drive["id"], row["id"], "k-cp2", etag)
    assert cancel.status_code == 409
    assert cancel.json()["error"]["code"] == "UPLOAD_BUSY"


async def test_cancel_rejects_body_and_query(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "cb", "k-cb")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-cb1"
    )
    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/uploads/{upload['id']}",
        content=b'{"why": "no"}',
        headers={"Idempotency-Key": "k-cb2", "If-Match": '"x"'},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"
    resp = await http.delete(
        f"/v0/drives/{drive['id']}/uploads/{upload['id']}?force=1",
        headers={"Idempotency-Key": "k-cb3", "If-Match": '"x"'},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


# ─── complete: publication ──────────────────────────────────────────────────


async def _finalized_artifact_upload(
    http, drive, fake_storage, key="k-fa", *, name="notes.txt"
):
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"], name=name), key
    )
    row = await _session_row(upload["id"])
    fake_storage.finalize_scratch(row["scratch_object"], size=SIZE_A, crc32c=CRC_A)
    return upload


async def test_complete_publishes_exactly_one_artifact(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "pa", "k-pa")
    canonical_name = "Résumé 🚀.bin"
    upload = await _finalized_artifact_upload(
        http, drive, fake_storage, "k-pa1", name="Re\u0301sume\u0301 🚀.bin"
    )
    # Simulate a session row written before begin-time canonicalization. Publication
    # remains authoritative and must normalize the persisted target again.
    async with conn() as c:
        await c.execute(
            "UPDATE upload_sessions SET artifact_name = $2 WHERE id = $1",
            upload["id"],
            "Re\u0301sume\u0301 🚀.bin",
        )
    resp = await _complete(http, drive["id"], upload["id"], "k-pa2")
    assert resp.status_code == 201, resp.text
    body = resp.json()["upload"]
    assert body["state"] == "completed"
    assert body["restart_required"] is False
    assert body["target"]["name"] == canonical_name
    result = body["result"]
    assert result["kind"] == "artifact"
    assert result["artifact_id"].startswith("art_")
    assert result["version_id"].startswith("ver_")
    assert result["revision"].startswith("rev_")
    assert resp.headers["Location"].endswith(
        f"/v0/drives/{drive['id']}/artifacts/{result['artifact_id']}"
    )
    # The artifact is real, its version carries the adopted coordinates.
    art = await http.get(f"/v0/drives/{drive['id']}/artifacts/{result['artifact_id']}")
    assert art.status_code == 200
    assert art.json()["name"] == canonical_name
    async with conn() as c:
        session = await c.fetchrow(
            "SELECT artifact_name FROM upload_sessions WHERE id = $1",
            upload["id"],
        )
        version = await c.fetchrow(
            "SELECT checksum, size_bytes, storage_bucket, storage_generation, "
            "storage_object FROM artifact_versions WHERE id = $1",
            result["version_id"],
        )
        drive_bytes = await c.fetchval(
            "SELECT storage_bytes FROM drives WHERE id = $1", drive["id"]
        )
        reservation = await c.fetchrow(
            "SELECT release_kind FROM storage_reservations WHERE upload_id = $1",
            upload["id"],
        )
    assert session["artifact_name"] == canonical_name
    assert version["checksum"] == f"crc32c:{CRC_A}"
    assert version["size_bytes"] == SIZE_A
    assert version["storage_bucket"] == FAKE_BUCKET
    assert version["storage_generation"] is not None
    assert version["storage_object"].startswith("immutable/")
    assert drive_bytes == SIZE_A + len(b"nothing-counted-elsewhere") * 0
    assert reservation["release_kind"] == "converted"
    status = await _status(http, drive["id"], upload["id"])
    assert status.status_code == 200, status.text
    assert status.json()["upload"]["target"]["name"] == canonical_name
    # Same-key completed replay is 200 with the same result.
    replay = await _complete(http, drive["id"], upload["id"], "k-pa2")
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json()["upload"]["target"]["name"] == canonical_name
    assert replay.json()["upload"]["result"] == result
    # A DIFFERENT key after completed: 200, same result, no second publish.
    other = await _complete(http, drive["id"], upload["id"], "k-pa3")
    assert other.status_code == 200
    assert other.json()["upload"]["target"]["name"] == canonical_name
    assert other.json()["upload"]["result"] == result
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = $1", canonical_name
        )
    assert artifacts == 1


async def test_complete_publishes_exactly_one_version(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "pv", "k-pv")
    art = await _create_artifact(http, drive["id"], drive["root_folder_id"], "v.txt", "k-pv-a")
    upload_resp = await _begin(
        http, drive["id"], version_body(art["id"]), "k-pv1",
        **{"If-Match": f'"{art["revision"]}"'},
    )
    assert upload_resp.status_code == 201, upload_resp.text
    upload = upload_resp.json()["upload"]
    assert upload["target"] == {"kind": "version", "artifact_id": art["id"]}
    row = await _session_row(upload["id"])
    fake_storage.finalize_scratch(row["scratch_object"], size=SIZE_A, crc32c=CRC_A)
    resp = await _complete(http, drive["id"], upload["id"], "k-pv2")
    assert resp.status_code == 201, resp.text
    result = resp.json()["upload"]["result"]
    assert result["kind"] == "version"
    assert result["artifact_id"] == art["id"]
    assert resp.headers["Location"].endswith(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions/{result['version_id']}"
    )
    versions = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions")
    items = versions.json()["items"]
    assert len(items) == 2
    assert items[0]["id"] == result["version_id"]
    assert items[0]["hash"] == f"crc32c:{CRC_A}"
    fresh = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}")
    assert fresh.json()["head_version_id"] == result["version_id"]
    assert fresh.json()["revision"] == result["revision"]


async def test_complete_rejects_if_match_and_body(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "cm", "k-cm")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-cm1")
    with_match = await _complete(
        http, drive["id"], upload["id"], "k-cm2", **{"If-Match": '"x"'}
    )
    assert with_match.status_code == 400
    assert with_match.json()["error"]["code"] == "INVALID_REQUEST"
    with_body = await http.post(
        f"/v0/drives/{drive['id']}/uploads/{upload['id']}/complete",
        content=b'{"finish": true}',
        headers={"Idempotency-Key": "k-cm3", "Content-Type": "application/json"},
    )
    assert with_body.status_code == 400
    assert with_body.json()["error"]["code"] == "INVALID_REQUEST"
    no_key = await http.post(
        f"/v0/drives/{drive['id']}/uploads/{upload['id']}/complete"
    )
    assert no_key.status_code == 400
    assert no_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_complete_incomplete_returns_to_active_and_frees_the_key(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "ic", "k-ic")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-ic1"
    )
    resp = await _complete(http, drive["id"], upload["id"], "k-ic2")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "UPLOAD_INCOMPLETE"
    assert "Retry-After" in resp.headers
    row = await _session_row(upload["id"])
    assert row["state"] == "active"
    # The key was abandoned: the SAME key may execute a later, successful
    # completion once the object is finalized.
    fake_storage.finalize_scratch(row["scratch_object"], size=SIZE_A, crc32c=CRC_A)
    retry = await _complete(http, drive["id"], upload["id"], "k-ic2")
    assert retry.status_code == 201, retry.text


@pytest.mark.parametrize(
    ("finalize_kwargs", "expected_code"),
    [
        ({"size": SIZE_A + 3, "crc32c": CRC_A}, "OBJECT_SIZE_MISMATCH"),
        ({"size": SIZE_A, "crc32c": crc32c_of(b"other bytes")}, "CHECKSUM_MISMATCH"),
        ({"size": SIZE_A, "crc32c": CRC_A, "content_type": "text/html"},
         "OBJECT_METADATA_MISMATCH"),
        ({"size": SIZE_A, "crc32c": CRC_A, "marker": "foreign-marker"},
         "OBJECT_METADATA_MISMATCH"),
    ],
)
async def test_complete_deterministic_rejections_are_terminal_and_replayable(
    http, override_actor, enabled_transfer, fake_storage,
    finalize_kwargs, expected_code,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "dr", "k-dr")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-dr1"
    )
    row = await _session_row(upload["id"])
    fake_storage.finalize_scratch(row["scratch_object"], **finalize_kwargs)
    resp = await _complete(http, drive["id"], upload["id"], "k-dr2")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == expected_code
    row = await _session_row(upload["id"])
    assert row["state"] == "rejected"
    assert row["failure_code"] == expected_code
    assert row["cleanup_state"] in ("pending", "quarantined")
    async with conn() as c:
        released = await c.fetchval(
            "SELECT release_kind FROM storage_reservations WHERE upload_id = $1",
            upload["id"],
        )
    assert released == "released"
    # Same-key replay: the exact stored failure.
    replay = await _complete(http, drive["id"], upload["id"], "k-dr2")
    assert replay.status_code == 422
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json()["error"]["code"] == expected_code
    # Different key: no work, terminal classification.
    other = await _complete(http, drive["id"], upload["id"], "k-dr3")
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "UPLOAD_NOT_COMPLETABLE"
    assert other.json()["error"]["details"]["failure"]["code"] == expected_code
    # Status keeps reporting the safe terminal state.
    status = await _status(http, drive["id"], upload["id"])
    assert status.status_code == 200
    assert status.json()["upload"]["state"] == "rejected"
    assert status.json()["upload"]["failure"] == {"code": expected_code}


async def test_complete_name_collision_is_name_conflict(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "nc", "k-nc")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-nc1")
    # A rival inline create takes the name first.
    await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "notes.txt", "k-nc-rival"
    )
    resp = await _complete(http, drive["id"], upload["id"], "k-nc2")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "NAME_CONFLICT"
    row = await _session_row(upload["id"])
    assert row["state"] == "rejected"
    assert row["failure_code"] == "NAME_CONFLICT"


async def test_complete_artifact_head_race_is_412_terminal(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "hr", "k-hr")
    art = await _create_artifact(http, drive["id"], drive["root_folder_id"], "h.txt", "k-hr-a")
    begin = await _begin(
        http, drive["id"], version_body(art["id"]), "k-hr1",
        **{"If-Match": f'"{art["revision"]}"'},
    )
    assert begin.status_code == 201
    upload = begin.json()["upload"]
    row = await _session_row(upload["id"])
    fake_storage.finalize_scratch(row["scratch_object"], size=SIZE_A, crc32c=CRC_A)
    # The head moves via an inline append before completion.
    append = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions",
        files={"content": ("h.txt", b"replacement", "text/plain")},
        headers={"Idempotency-Key": "k-hr-app", "If-Match": f'"{art["revision"]}"'},
    )
    assert append.status_code == 201, append.text
    resp = await _complete(http, drive["id"], upload["id"], "k-hr2")
    assert resp.status_code == 412, resp.text
    assert resp.json()["error"]["code"] == "PRECONDITION_FAILED"
    row = await _session_row(upload["id"])
    assert row["state"] == "rejected"
    # No version row was created for the direct session's content.
    versions = await http.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions")
    assert all(item["hash"].startswith("sha256:") for item in versions.json()["items"])


async def test_complete_expiry_boundary(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "ex", "k-ex")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-ex1")
    async with conn() as c:
        await c.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            upload["id"],
        )
    resp = await _complete(http, drive["id"], upload["id"], "k-ex2")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "UPLOAD_EXPIRED"
    row = await _session_row(upload["id"])
    assert row["state"] == "expired"
    # Same key replays the exact 422.
    replay = await _complete(http, drive["id"], upload["id"], "k-ex2")
    assert replay.status_code == 422
    assert replay.headers.get("Idempotent-Replay") == "true"
    # Different key: 409 UPLOAD_NOT_COMPLETABLE with the safe failure code.
    other = await _complete(http, drive["id"], upload["id"], "k-ex3")
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "UPLOAD_NOT_COMPLETABLE"
    assert other.json()["error"]["details"]["failure"]["code"] == "UPLOAD_EXPIRED"
    # Status: 200, state expired, result null, safe failure code.
    status = await _status(http, drive["id"], upload["id"])
    assert status.status_code == 200
    upload_body = status.json()["upload"]
    assert upload_body["state"] == "expired"
    assert upload_body["result"] is None
    assert upload_body["failure"] == {"code": "UPLOAD_EXPIRED"}
    # A late finalized object can never publish (cancel also cannot revive).
    late = await _complete(http, drive["id"], upload["id"], "k-ex4")
    assert late.status_code == 409


async def test_complete_transient_provider_failure_keeps_completing(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "tp", "k-tp")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-tp1")
    fake_storage.fail_stat = TransferProviderUnavailableError("stat_unavailable")
    resp = await _complete(http, drive["id"], upload["id"], "k-tp2")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "TRANSFER_UNAVAILABLE"
    assert "Retry-After" in resp.headers
    row = await _session_row(upload["id"])
    assert row["state"] == "completing"
    assert row["transition_action"] == "complete"
    # Same key while the durable action holds: the current retryable answer.
    same = await _complete(http, drive["id"], upload["id"], "k-tp2")
    assert same.status_code == 503
    assert same.json()["error"]["code"] == "TRANSFER_UNAVAILABLE"
    # A different key receives UPLOAD_BUSY.
    other = await _complete(http, drive["id"], upload["id"], "k-tp3")
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "UPLOAD_BUSY"
    # After the leases go stale, the SAME key attaches through the real
    # idempotency crash lease and the same durable action, and finishes.
    from agentdrive.api import v0_uploads as uploads_api
    from agentdrive.core import idempotency as idem

    monkeypatch.setattr(uploads_api, "COMPLETE_LEASE_SECONDS", 0)
    monkeypatch.setattr(idem, "IN_FLIGHT_LEASE_SECONDS", 0)
    async with conn() as c:
        await c.execute(
            "UPDATE upload_sessions SET transition_lease_expires_at = now() "
            "WHERE id = $1", upload["id"],
        )
    done = await _complete(http, drive["id"], upload["id"], "k-tp2")
    assert done.status_code == 201, done.text


async def test_complete_ambiguous_rewrite_recovers_at_the_preselected_key(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Rewrite executed (final object exists) but the response was lost: the
    retry discovers the verified object at the preselected final key and
    resumes publication — never a duplicate or a new key."""
    override_actor(make_actor())
    drive = await _create_drive(http, "ar", "k-ar")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-ar1")
    fake_storage.fail_rewrite = TransferProviderUnavailableError("rewrite_unavailable")
    fake_storage.rewrite_succeeds_then_fails = True
    resp = await _complete(http, drive["id"], upload["id"], "k-ar2")
    assert resp.status_code == 503
    row = await _session_row(upload["id"])
    assert row["state"] == "completing"
    assert row["final_object"] in fake_storage.objects
    # Attach after the leases go stale (the real idempotency crash lease,
    # not ledger surgery); discovery must adopt, not rewrite.
    from agentdrive.api import v0_uploads as uploads_api
    from agentdrive.core import idempotency as idem

    monkeypatch.setattr(uploads_api, "COMPLETE_LEASE_SECONDS", 0)
    monkeypatch.setattr(idem, "IN_FLIGHT_LEASE_SECONDS", 0)
    async with conn() as c:
        await c.execute(
            "UPDATE upload_sessions SET transition_lease_expires_at = now() "
            "WHERE id = $1", upload["id"],
        )
    rewrites_before = fake_storage.rewrite_calls
    done = await _complete(http, drive["id"], upload["id"], "k-ar2")
    assert done.status_code == 201, done.text
    assert fake_storage.rewrite_calls == rewrites_before  # adopted, not rewritten
    async with conn() as c:
        count = await c.fetchval("SELECT count(*) FROM artifacts WHERE name = 'notes.txt'")
    assert count == 1


async def test_complete_persists_rewrite_continuation_between_steps(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "rc", "k-rc")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-rc1")
    fake_storage.rewrite_continuation_steps = 2
    resp = await _complete(http, drive["id"], upload["id"], "k-rc2")
    assert resp.status_code == 201, resp.text
    # Three calls: two continuations then done; each later call carried the
    # previously returned token.
    assert fake_storage.rewrite_calls == 3
    assert fake_storage._continuations_seen[0] is None
    assert fake_storage._continuations_seen[1] == "cont-1"
    assert fake_storage._continuations_seen[2] == "cont-0"


async def test_complete_vs_cancel_races(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "vc", "k-vc")
    # cancel first, then complete: never both succeed.
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-vc1")
    status = await _status(http, drive["id"], upload["id"])
    cancelled = await _cancel(
        http, drive["id"], upload["id"], "k-vc2", status.headers["ETag"]
    )
    assert cancelled.status_code == 200
    resp = await _complete(http, drive["id"], upload["id"], "k-vc3")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "UPLOAD_NOT_COMPLETABLE"
    # complete first, then cancel: UPLOAD_ALREADY_COMPLETED.
    upload2 = await _begin_active(
        http, drive["id"],
        artifact_body(drive["root_folder_id"], name="second.txt"), "k-vc4",
    )
    row2 = await _session_row(upload2["id"])
    fake_storage.finalize_scratch(row2["scratch_object"], size=SIZE_A, crc32c=CRC_A)
    done = await _complete(http, drive["id"], upload2["id"], "k-vc5")
    assert done.status_code == 201
    cancel_after = await _cancel(
        http, drive["id"], upload2["id"], "k-vc6", done.headers["ETag"]
    )
    assert cancel_after.status_code == 409
    assert cancel_after.json()["error"]["code"] == "UPLOAD_ALREADY_COMPLETED"
    # The completed artifact survives.
    art = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{done.json()['upload']['result']['artifact_id']}"
    )
    assert art.status_code == 200


async def test_cancel_while_completing_is_busy(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "cw", "k-cw")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-cw1")
    fake_storage.fail_stat = TransferProviderUnavailableError("stat_unavailable")
    resp = await _complete(http, drive["id"], upload["id"], "k-cw2")
    assert resp.status_code == 503
    status = await _status(http, drive["id"], upload["id"])
    cancel = await _cancel(
        http, drive["id"], upload["id"], "k-cw3", status.headers["ETag"]
    )
    assert cancel.status_code == 409
    assert cancel.json()["error"]["code"] == "UPLOAD_BUSY"
    assert "Retry-After" in cancel.headers


# ─── concurrency ────────────────────────────────────────────────────────────


async def test_concurrent_same_key_begin_creates_one_session(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "k1", "k-k1")
    body = artifact_body(drive["root_folder_id"])
    results = await asyncio.gather(
        *[_begin(http, drive["id"], body, "k-k1a") for _ in range(4)]
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(201) <= 1
    assert len(fake_storage.initiations) <= 1
    async with conn() as c:
        count = await c.fetchval("SELECT count(*) FROM upload_sessions")
    assert count == 1


async def test_concurrent_duplicate_complete_publishes_once(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "k2", "k-k2")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-k2a")
    results = await asyncio.gather(
        _complete(http, drive["id"], upload["id"], "k-k2b"),
        _complete(http, drive["id"], upload["id"], "k-k2c"),
        _complete(http, drive["id"], upload["id"], "k-k2b"),
    )
    # Every loser answers a contract status — never a 500 (review finding:
    # the fence-CAS loser must classify as UPLOAD_BUSY / retryable).
    assert all(r.status_code in (200, 201, 409, 503) for r in results), [
        (r.status_code, r.text) for r in results
    ]
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
        conversions = await c.fetchval(
            "SELECT count(*) FROM storage_reservations "
            "WHERE upload_id = $1 AND release_kind = 'converted'",
            upload["id"],
        )
    assert artifacts == 1
    assert conversions == 1
    assert any(r.status_code in (200, 201) for r in results)


# ─── secret hygiene ─────────────────────────────────────────────────────────


async def test_idempotency_ledger_never_stores_the_target(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "sh", "k-sh")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-sh1")
    done = await _complete(http, drive["id"], upload["id"], "k-sh2")
    assert done.status_code == 201
    async with conn() as c:
        rows = await c.fetch(
            "SELECT response_body, response_headers FROM idempotency_records"
        )
    for row in rows:
        text = (str(row["response_body"]) + str(row["response_headers"])).lower()
        assert "fake-signed" not in text
        assert "x-goog-signature" not in text
        assert '"transfer"' not in text
        assert '"initiation"' not in text
        assert "authorization" not in text
        assert FAKE_UPLOAD_ENDPOINT not in str(row["response_body"])


async def test_no_target_in_logs(
    http, override_actor, enabled_transfer, fake_storage, caplog,
):
    import logging

    override_actor(make_actor())
    drive = await _create_drive(http, "lg", "k-lg")
    with caplog.at_level(logging.DEBUG):
        upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-lg1")
        done = await _complete(http, drive["id"], upload["id"], "k-lg2")
        assert done.status_code == 201
    for record in caplog.records:
        message = record.getMessage()
        assert "fake-signed" not in message
        assert "X-Goog-Signature" not in message
        assert "scratch/" not in message
        assert "immutable/" not in message


async def test_error_bodies_never_name_object_coordinates(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "eb", "k-eb")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-eb1"
    )
    row = await _session_row(upload["id"])
    fake_storage.finalize_scratch(row["scratch_object"], size=99, crc32c=CRC_A)
    resp = await _complete(http, drive["id"], upload["id"], "k-eb2")
    assert resp.status_code == 422
    text = resp.text
    assert row["scratch_object"] not in text
    assert row["final_object"] not in text
    assert FAKE_BUCKET not in text


# ─── review round 2: fence races, terminal-under-fence, limits ──────────────


async def test_acquire_transition_cas_loser_is_busy(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Two requests read `active` before either takes the fence (autocommit
    callers hold no row lock between statements): the CAS loser must
    classify as 409 UPLOAD_BUSY, never surface UploadSessionNotFoundError."""
    override_actor(make_actor())
    drive = await _create_drive(http, "r1", "k-r1")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-r1a"
    )
    async with conn() as c:
        stale = await uploads_core._fetch_locked(c, upload["id"])  # active snapshot
    async with conn() as c:
        await uploads_core.acquire_transition(
            c, upload_id=upload["id"], action="complete", lease_seconds=60
        )

    async def stale_read(c, upload_id):
        return stale

    monkeypatch.setattr(uploads_core, "_fetch_locked", stale_read)
    async with conn() as c:
        with pytest.raises(uploads_core.UploadBusyError):
            await uploads_core.acquire_transition(
                c, upload_id=upload["id"], action="complete", lease_seconds=60
            )


async def test_stale_lease_reattach_never_relights_a_terminal_row(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """The stale-lease re-lease must be guarded: a session terminalized
    between the read and the UPDATE is never re-leased (terminal rows are
    immutable to the fence)."""
    override_actor(make_actor())
    drive = await _create_drive(http, "r2", "k-r2")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-r2a"
    )
    async with conn() as c:
        await uploads_core.acquire_transition(
            c, upload_id=upload["id"], action="complete", lease_seconds=60
        )
        await c.execute(
            "UPDATE upload_sessions SET transition_lease_expires_at = now() "
            "WHERE id = $1", upload["id"],
        )
        stale = await uploads_core._fetch_locked(c, upload["id"])  # completing
    async with conn() as c:
        # A rival terminalizer resolves the session (no deadline involved,
        # so the guarded re-lease UPDATE is the only thing that can refuse).
        await uploads_core.reject_session(
            c, upload_id=upload["id"], failure_code="OBJECT_METADATA_MISMATCH",
        )

    async def stale_read(c, upload_id):
        return stale

    monkeypatch.setattr(uploads_core, "_fetch_locked", stale_read)
    async with conn() as c:
        with pytest.raises(
            (uploads_core.UploadBusyError, uploads_core.InvalidUploadTransitionError)
        ):
            await uploads_core.acquire_transition(
                c, upload_id=upload["id"], action="complete", lease_seconds=60
            )
    row = await _session_row(upload["id"])
    assert row["state"] == "rejected"
    assert row["transition_lease_id"] is None  # never re-leased


async def test_complete_terminalized_under_fence_is_not_a_500(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """A reconciler expiring the session between adoption and publication
    must surface the deadline outcome (422 UPLOAD_EXPIRED), never a 500."""
    override_actor(make_actor())
    drive = await _create_drive(http, "r3", "k-r3")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-r3a")
    original = uploads_core.record_adopted_observation

    async def record_then_steal(c, **kwargs):
        await original(c, **kwargs)
        async with conn() as c2:
            await c2.execute(
                "UPDATE upload_sessions SET expires_at = now() - interval '1 second' "
                "WHERE id = $1", kwargs["upload_id"],
            )
            await uploads_core.expire_session(c2, upload_id=kwargs["upload_id"])

    monkeypatch.setattr(uploads_core, "record_adopted_observation", record_then_steal)
    resp = await _complete(http, drive["id"], upload["id"], "k-r3b")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "UPLOAD_EXPIRED"
    row = await _session_row(upload["id"])
    assert row["state"] == "expired"
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
    assert artifacts == 0


async def test_publication_crash_before_commit_republishes_once(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Plan Task 3 step 5: crash inside the publication transaction (after
    the version commit ran, before it committed). The retry attaches to the
    stale fence, discovers the persisted adoption, and publishes exactly
    once — no second rewrite, no duplicate artifact."""
    from agentdrive.api import v0_uploads as uploads_api

    override_actor(make_actor())
    monkeypatch.setattr(uploads_api, "COMPLETE_LEASE_SECONDS", 0)
    drive = await _create_drive(http, "r4", "k-r4")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-r4a")
    original = uploads_api._store_result
    calls = {"n": 0}

    async def crash_once(c, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash before publication commit")
        return await original(c, **kwargs)

    monkeypatch.setattr(uploads_api, "_store_result", crash_once)
    # ASGITransport re-raises the unhandled server exception into the test
    # after the central 500 handler ran; a wire client sees a plain 500.
    with pytest.raises(RuntimeError, match="injected crash"):
        await _complete(http, drive["id"], upload["id"], "k-r4b")
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
    assert artifacts == 0  # the publication rolled back whole
    rewrites = fake_storage.rewrite_calls
    retry = await _complete(http, drive["id"], upload["id"], "k-r4b")
    assert retry.status_code == 201, retry.text
    assert fake_storage.rewrite_calls == rewrites  # adopted, not re-rewritten
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
        conversions = await c.fetchval(
            "SELECT count(*) FROM storage_reservations "
            "WHERE upload_id = $1 AND release_kind = 'converted'",
            upload["id"],
        )
    assert artifacts == 1
    assert conversions == 1


async def test_transfer_control_rate_limit(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    override_actor(make_actor())
    monkeypatch.setattr(settings, "direct_transfer_rate_principal", 1)
    drive = await _create_drive(http, "r5", "k-r5")
    root = drive["root_folder_id"]
    first = await _begin(http, drive["id"], artifact_body(root, name="a.txt"), "k-r5a")
    assert first.status_code == 201, first.text
    second = await _begin(http, drive["id"], artifact_body(root, name="b.txt"), "k-r5b")
    assert second.status_code == 429, second.text
    assert second.json()["error"]["code"] == "RATE_LIMITED"
    assert second.json()["error"]["details"]["limit_name"] == (
        "direct_transfer_rate_principal"
    )
    assert "Retry-After" in second.headers


async def test_status_cancel_complete_reject_unacceptable_accept(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive_id = "drv_00000000000000aa"
    upload_id = "upld_00000000000000bb"
    for resp in (
        await _status(http, drive_id, upload_id, Accept="text/html"),
        await _cancel(http, drive_id, upload_id, "k-r6a", '"x"', Accept="text/html"),
        await _complete(http, drive_id, upload_id, "k-r6b", Accept="text/html"),
    ):
        assert resp.status_code == 406, resp.text
        assert resp.json()["error"]["code"] == "NOT_ACCEPTABLE"


async def test_begin_body_size_is_bounded(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "r7", "k-r7")
    raw = json.dumps(artifact_body(drive["root_folder_id"])).encode()
    padded = raw + b" " * (70 * 1024)
    resp = await http.post(
        f"/v0/drives/{drive['id']}/uploads",
        content=padded,
        headers={"Idempotency-Key": "k-r7a", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


async def test_concurrent_begins_across_drives_respect_workspace_ceiling(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """§9 count ceilings must hold across DRIVES: the check must serialize
    on a workspace-scoped lock, not only the per-drive namespace lock."""
    override_actor(make_actor())
    monkeypatch.setattr(settings, "direct_transfer_max_active_sessions_principal", 1)
    drive_a = await _create_drive(http, "r8a", "k-r8a")
    drive_b = await _create_drive(http, "r8b", "k-r8b")
    original = uploads_core.count_live_sessions

    async def slow_count(*args, **kwargs):
        result = await original(*args, **kwargs)
        await asyncio.sleep(0.15)  # widen the count→insert window
        return result

    monkeypatch.setattr(uploads_core, "count_live_sessions", slow_count)
    results = await asyncio.gather(
        _begin(http, drive_a["id"], artifact_body(drive_a["root_folder_id"]), "k-r8c"),
        _begin(http, drive_b["id"], artifact_body(drive_b["root_folder_id"]), "k-r8d"),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 422], statuses
    async with conn() as c:
        live = await c.fetchval(
            "SELECT count(*) FROM upload_sessions WHERE state IN "
            "('preparing', 'active', 'completing', 'cancelling')"
        )
    assert live == 1


# ─── review round 3 · commit 1: tenant isolation, uniform 404, hygiene ──────


async def test_foreign_workspace_cannot_poison_drive_rate_bucket(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Blocker: the drive rate window must not be chargeable by a caller who
    is not authorized on the drive. A foreign workspace hammering a guessed
    drive id gets 404s and the owner's window stays untouched."""
    override_actor(make_actor())
    monkeypatch.setattr(settings, "direct_transfer_rate_drive", 1)
    drive = await _create_drive(http, "rp1", "k-rp1")
    root = drive["root_folder_id"]

    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    for i in range(4):
        resp = await _begin(http, drive["id"], artifact_body(root), f"k-rp1-f{i}")
        assert resp.status_code == 404, resp.text
        resp = await _cancel(
            http, drive["id"], "upld_00000000000000bb", f"k-rp1-fc{i}", '"x.1"'
        )
        assert resp.status_code == 404, resp.text
        resp = await _complete(http, drive["id"], "upld_00000000000000bb", f"k-rp1-fx{i}")
        assert resp.status_code == 404, resp.text

    # The owner's first control on the drive succeeds — the window is intact.
    override_actor(make_actor())
    ok = await _begin(http, drive["id"], artifact_body(root), "k-rp1-a")
    assert ok.status_code == 201, ok.text
    # And the limit still bites for the OWNER's next drive-charged control.
    second = await _begin(http, drive["id"], artifact_body(root, name="b.txt"), "k-rp1-b")
    assert second.status_code == 429, second.text
    assert second.json()["error"]["details"]["limit_name"] == (
        "direct_transfer_rate_drive"
    )


async def test_upload_misses_share_one_identical_404(
    http, override_actor, enabled_transfer, fake_storage,
):
    """Blocker: wrong-workspace, wrong-drive, unknown upload, foreign
    principal, and local denial must collapse to byte-identical 404
    NOT_FOUND envelopes — no DRIVE_NOT_FOUND on this surface."""
    override_actor(make_actor())
    drive = await _create_drive(http, "u4", "k-u4")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-u4a"
    )
    grant = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "resource_type": "drive", "resource_id": drive["id"],
            "principal_type": "agent", "principal_id": OTHER_AGENT,
            "role": "manager",
        },
        headers={"Idempotency-Key": "k-u4-grant"},
    )
    assert grant.status_code == 201

    responses = []
    # 1. wrong workspace (existing drive, foreign workspace token)
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    responses.append(await _status(http, drive["id"], upload["id"]))
    # 2. begin against the foreign drive — same miss, same envelope
    responses.append(
        await _begin(http, drive["id"], artifact_body(drive["root_folder_id"]), "k-u4b")
    )
    override_actor(make_actor())
    # 3. unknown (well-formed) drive id
    responses.append(await _status(http, "drv_00000000000000ee", upload["id"]))
    # 4. unknown upload in the caller's own drive
    responses.append(await _status(http, drive["id"], "upld_00000000000000ee"))
    # 5. foreign principal, same workspace, even with a drive manager grant
    override_actor(make_actor(subject=OTHER_AGENT))
    responses.append(await _status(http, drive["id"], upload["id"]))
    # 6. local denial: the owner's grants revoked
    async with conn() as c:
        await c.execute("DELETE FROM grants WHERE drive_id = $1", drive["id"])
    override_actor(make_actor())
    responses.append(await _status(http, drive["id"], upload["id"]))

    bodies = {json.dumps(r.json(), sort_keys=True) for r in responses}
    statuses = {r.status_code for r in responses}
    assert statuses == {404}, [(r.status_code, r.text) for r in responses]
    assert len(bodies) == 1, bodies
    assert responses[0].json()["error"]["code"] == "NOT_FOUND"


async def test_hostile_exception_strings_never_reach_logs(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch, caplog,
):
    """Blocker: warning paths must log a stable classification, never the
    raw exception text (which can carry coordinates/markers/diagnostics)."""
    import logging

    hostile = RuntimeError(
        "boom https://storage.example/b/o?upload_id=fake-session "
        "scratch/evil-key immutable/evil-key marker-deadbeef "
        "Authorization: Bearer tok"
    )

    override_actor(make_actor())
    drive = await _create_drive(http, "hx", "k-hx")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-hx1"
    )

    async def bad_abandon(c, *, owner_id):
        raise hostile

    monkeypatch.setattr(
        "agentdrive.core.idempotency.abandon", bad_abandon
    )
    with caplog.at_level(logging.WARNING):
        stale = await _cancel(http, drive["id"], upload["id"], "k-hx2", '"zz.9"')
        assert stale.status_code == 412
    monkeypatch.undo()  # restore abandon for the next probe

    original_fail = uploads_core.fail_initiation

    async def bad_fail(c, **kwargs):
        raise hostile

    monkeypatch.setattr(uploads_core, "fail_initiation", bad_fail)
    # Signing failures no longer terminalize (2026-08-20 amendment); the
    # warning path under test now triggers from an activate crash, which
    # still routes through _fail_initiation.
    async def bad_activate(*args, **kwargs):
        raise RuntimeError("injected activate crash")

    monkeypatch.setattr(uploads_core, "activate_session", bad_activate)
    with caplog.at_level(logging.WARNING):
        resp = await _begin(
            http, drive["id"],
            artifact_body(drive["root_folder_id"], name="h2.txt"), "k-hx3",
        )
        assert resp.status_code == 503
    monkeypatch.setattr(uploads_core, "fail_initiation", original_fail)

    text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        "storage.example", "scratch/evil-key", "immutable/evil-key",
        "marker-deadbeef", "Bearer tok", "fake-signed",
    ):
        assert secret not in text, secret
    assert "RuntimeError" in text  # the stable classification survives


async def test_accept_quality_zero_is_not_acceptable(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "q0", "k-q0")
    refused = await _begin(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-q0a",
        Accept="application/json;q=0",
    )
    assert refused.status_code == 406, refused.text
    refused = await _begin(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-q0b",
        Accept="text/html, application/json;q=0.0",
    )
    assert refused.status_code == 406
    accepted = await _begin(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-q0c",
        Accept="application/json;q=0.5",
    )
    assert accepted.status_code == 201, accepted.text


async def test_upload_responses_carry_json_charset(
    http, override_actor, enabled_transfer, fake_storage,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "cs", "k-cs")
    begin = await _begin(http, drive["id"], artifact_body(drive["root_folder_id"]), "k-cs1")
    assert begin.status_code == 201
    assert begin.headers["content-type"] == "application/json; charset=utf-8"
    upload_id = begin.json()["upload"]["id"]
    status = await _status(http, drive["id"], upload_id)
    assert status.headers["content-type"] == "application/json; charset=utf-8"
    replay = await _begin(http, drive["id"], artifact_body(drive["root_folder_id"]), "k-cs1")
    assert replay.status_code == 200
    assert replay.headers["content-type"] == "application/json; charset=utf-8"
    miss = await _status(http, drive["id"], "upld_00000000000000ee")
    assert miss.headers["content-type"] == "application/json; charset=utf-8"


async def test_new_unresolved_generation_row_disables_transfer_immediately(
    http, override_actor, enabled_transfer, fake_storage,
):
    """Should-fix: a positive readiness probe must not be cached past a new
    unresolved row — the exact zero-row gate holds per request."""
    override_actor(make_actor())
    drive = await _create_drive(http, "rc2", "k-rc2")
    ok = await _begin(http, drive["id"], artifact_body(drive["root_folder_id"]), "k-rc2a")
    assert ok.status_code == 201  # readiness was just observed positive
    art = await _create_artifact(http, drive["id"], drive["root_folder_id"], "r.txt", "k-rc2b")
    async with conn() as c:
        await c.execute(
            "INSERT INTO artifact_versions "
            "(id, artifact_id, parent_version_id, checksum, content_type, "
            " size_bytes, storage_object, actor_type, actor_id, ordinal) "
            "VALUES ('ver_00000000000000fd', $1, $2, 'sha256:' || repeat('b', 64), "
            " 'text/plain', 11, 'legacy/object-2', 'agent', $3, 2)",
            art["id"], art["head_version_id"], AGENT,
        )
    blocked = await _begin(
        http, drive["id"], artifact_body(drive["root_folder_id"], name="n.txt"), "k-rc2c"
    )
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["error"]["code"] == "TRANSFER_DISABLED"


# ─── review round 3 · commit 2: cancel CAS atomicity + replay exemption ─────


async def test_cancel_replay_needs_no_if_match(
    http, override_actor, enabled_transfer, fake_storage,
):
    """Blocker: §5.1 exempts the exact same-key replay from the If-Match
    requirement — replay reauthorizes and returns the stored 200."""
    override_actor(make_actor())
    drive = await _create_drive(http, "n1", "k-n1")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-n1a"
    )
    status = await _status(http, drive["id"], upload["id"])
    first = await _cancel(http, drive["id"], upload["id"], "k-n1b", status.headers["ETag"])
    assert first.status_code == 200, first.text
    replay = await _cancel(http, drive["id"], upload["id"], "k-n1b")  # NO If-Match
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json()["upload"]["state"] == "cancelled"
    # A NEW key still requires the precondition.
    fresh = await _cancel(http, drive["id"], upload["id"], "k-n1c")
    assert fresh.status_code == 428
    assert fresh.json()["error"]["code"] == "PRECONDITION_REQUIRED"


async def test_cancel_if_match_is_atomic_with_the_transition(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Blocker: a session mutated between the ETag validation and the fence
    CAS must NOT be cancelled by the stale ETag — the revision is enforced
    inside the same CAS that transitions active → cancelling."""
    override_actor(make_actor())
    drive = await _create_drive(http, "n2", "k-n2")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-n2a"
    )
    status = await _status(http, drive["id"], upload["id"])
    etag = status.headers["ETag"]

    original = uploads_core.acquire_transition
    raced = {"n": 0}

    async def bump_then_acquire(c, **kwargs):
        # Simulate a concurrent transition landing between the route's
        # validation read and the fence CAS.
        if raced["n"] == 0:
            raced["n"] += 1
            async with conn() as c2:
                await c2.execute(
                    "UPDATE upload_sessions SET session_revision = "
                    "session_revision + 1, updated_at = now() WHERE id = $1",
                    kwargs["upload_id"],
                )
        return await original(c, **kwargs)

    monkeypatch.setattr(uploads_core, "acquire_transition", bump_then_acquire)
    resp = await _cancel(http, drive["id"], upload["id"], "k-n2b", etag)
    assert resp.status_code == 412, resp.text
    assert resp.json()["error"]["code"] == "PRECONDITION_FAILED"
    assert "ETag" not in resp.headers
    row = await _session_row(upload["id"])
    assert row["state"] == "active"  # never cancelled by the stale ETag
    # The key was not burned; the corrected retry succeeds.
    monkeypatch.setattr(uploads_core, "acquire_transition", original)
    fresh = await _status(http, drive["id"], upload["id"])
    done = await _cancel(http, drive["id"], upload["id"], "k-n2b", fresh.headers["ETag"])
    assert done.status_code == 200, done.text


async def test_cancel_rejects_star_and_multi_member_if_match(
    http, override_actor, enabled_transfer, fake_storage,
):
    """The cancel fence requires THE session's current strong ETag: `*`,
    weak tags, and ambiguous multi-member lists cannot fence a revision."""
    override_actor(make_actor())
    drive = await _create_drive(http, "n3", "k-n3")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-n3a"
    )
    star = await _cancel(http, drive["id"], upload["id"], "k-n3b", "*")
    assert star.status_code == 400
    assert star.json()["error"]["code"] == "INVALID_REQUEST"
    status = await _status(http, drive["id"], upload["id"])
    etag = status.headers["ETag"]
    multi = await _cancel(
        http, drive["id"], upload["id"], "k-n3c", f'{etag}, "upld_00000000000000aa.1"'
    )
    assert multi.status_code == 400
    weak = await _cancel(http, drive["id"], upload["id"], "k-n3d", f"W/{etag}")
    assert weak.status_code == 412
    row = await _session_row(upload["id"])
    assert row["state"] == "active"


# ─── review round 3 · commit 3: lease fence, reconciler, atomic terminals ───


def _gc_sweeper(fake):
    """A GCSweeper wired to the fake transfer adapter, age gates zeroed."""
    import datetime as dt

    from agentdrive.core.gc import GCSweeper

    cls = type("TestSweeper", (GCSweeper,), {
        "TRANSFER_GRACE": dt.timedelta(0),
        "PURGE_RETENTION": dt.timedelta(0),
        "MARK_SWEEP_AGE": dt.timedelta(days=365),
        "SCRATCH_SWEEP_AGE": dt.timedelta(days=365),
        "ORPHAN_SWEEP_AGE": dt.timedelta(days=365),
        "LATE_FINALIZATION_WINDOW": dt.timedelta(0),
    })
    return cls(transfer_storage=fake)


async def test_lease_lost_worker_cannot_alter_the_new_owners_state(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Blocker: the transition lease is an OWNERSHIP fence. A worker blocked
    past lease expiry while another key takes the lease must stop — it can
    never write observations, continuations, terminal state, or a second
    publication over the new owner."""
    from agentdrive.api import v0_uploads as uploads_api

    override_actor(make_actor())
    monkeypatch.setattr(uploads_api, "COMPLETE_LEASE_SECONDS", 0)
    drive = await _create_drive(http, "lf", "k-lf")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-lf1")

    gate = asyncio.Event()
    fake_storage.stall_next_stat = gate
    worker_a = asyncio.create_task(
        _complete(http, drive["id"], upload["id"], "k-lf-a")
    )
    await asyncio.wait_for(fake_storage.stat_entered.wait(), timeout=5)
    # A's zero-second lease is immediately stale; B (a different key)
    # attaches to the same durable action and finishes the completion.
    done = await _complete(http, drive["id"], upload["id"], "k-lf-b")
    assert done.status_code == 201, done.text
    result = done.json()["upload"]["result"]
    rewrites_after_b = fake_storage.rewrite_calls
    gate.set()
    resp_a = await asyncio.wait_for(worker_a, timeout=10)
    # A must not have performed provider work or altered B's outcome.
    assert fake_storage.rewrite_calls == rewrites_after_b
    assert resp_a.status_code in (200, 409), resp_a.text
    if resp_a.status_code == 200:
        assert resp_a.json()["upload"]["result"] == result
    row = await _session_row(upload["id"])
    assert row["state"] == "completed"
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
        conversions = await c.fetchval(
            "SELECT count(*) FROM storage_reservations "
            "WHERE upload_id = $1 AND release_kind = 'converted'",
            upload["id"],
        )
    assert artifacts == 1
    assert conversions == 1


async def test_concurrent_cancel_while_complete_holds_the_fence(
    http, override_actor, enabled_transfer, fake_storage,
):
    """Should-fix: a REAL concurrent complete-vs-cancel race — cancel runs
    while the completion worker is blocked mid-provider holding a LIVE
    lease, and must answer UPLOAD_BUSY without disturbing the publication."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cc2", "k-cc2")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-cc2a")
    status = await _status(http, drive["id"], upload["id"])

    gate = asyncio.Event()
    fake_storage.stall_next_stat = gate
    worker = asyncio.create_task(
        _complete(http, drive["id"], upload["id"], "k-cc2b")
    )
    await asyncio.wait_for(fake_storage.stat_entered.wait(), timeout=5)
    cancel = await _cancel(
        http, drive["id"], upload["id"], "k-cc2c", status.headers["ETag"]
    )
    assert cancel.status_code in (409, 412), cancel.text
    if cancel.status_code == 409:
        assert cancel.json()["error"]["code"] == "UPLOAD_BUSY"
    gate.set()
    done = await asyncio.wait_for(worker, timeout=10)
    assert done.status_code == 201, done.text
    row = await _session_row(upload["id"])
    assert row["state"] == "completed"


async def test_expiry_terminalization_and_replay_are_atomic(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Blocker: the deadline outcome and its exact replayable 422 commit
    together. A crash between deadline detection and result storage leaves
    the session UNTERMINALIZED, so the same key re-executes into the exact
    422 rather than degrading to the different-key 409."""
    from agentdrive.api import v0_uploads as uploads_api

    override_actor(make_actor())
    drive = await _create_drive(http, "ax", "k-ax")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-ax1")
    async with conn() as c:
        await c.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 second' "
            "WHERE id = $1", upload["id"],
        )
    original = uploads_api._store_result
    calls = {"n": 0}

    async def crash_once(c, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash between expiry and storage")
        return await original(c, **kwargs)

    monkeypatch.setattr(uploads_api, "_store_result", crash_once)
    with pytest.raises(RuntimeError, match="injected crash"):
        await _complete(http, drive["id"], upload["id"], "k-ax2")
    row = await _session_row(upload["id"])
    assert row["state"] == "active", row["state"]  # nothing half-committed
    # The SAME key re-executes into the exact first-completion 422 …
    retry = await _complete(http, drive["id"], upload["id"], "k-ax2")
    assert retry.status_code == 422, retry.text
    assert retry.json()["error"]["code"] == "UPLOAD_EXPIRED"
    replay = await _complete(http, drive["id"], upload["id"], "k-ax2")
    assert replay.status_code == 422
    assert replay.headers.get("Idempotent-Replay") == "true"
    # … while a different key receives the specified terminal 409.
    other = await _complete(http, drive["id"], upload["id"], "k-ax3")
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "UPLOAD_NOT_COMPLETABLE"
    assert other.json()["error"]["details"]["failure"]["code"] == "UPLOAD_EXPIRED"


async def test_post_adoption_revocation_terminalizes_rejected(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Blocker: once the final object is adopted, authorization revocation
    must terminalize as rejected (quarantined, released once) — never
    return the session to active for a later retry to publish."""
    override_actor(make_actor())
    drive = await _create_drive(http, "pr", "k-pr")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-pr1")
    original = uploads_core.record_adopted_observation

    async def record_then_revoke(c, **kwargs):
        await original(c, **kwargs)
        async with conn() as c2:
            await c2.execute(
                "UPDATE grants SET revoked_at = now() WHERE drive_id = $1",
                drive["id"],
            )

    monkeypatch.setattr(uploads_core, "record_adopted_observation", record_then_revoke)
    resp = await _complete(http, drive["id"], upload["id"], "k-pr2")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    monkeypatch.setattr(uploads_core, "record_adopted_observation", original)
    row = await _session_row(upload["id"])
    assert row["state"] == "rejected"
    assert row["cleanup_state"] == "quarantined"
    async with conn() as c:
        release = await c.fetchrow(
            "SELECT release_kind, count(*) OVER () AS releases "
            "FROM storage_reservations "
            "WHERE upload_id = $1 AND released_at IS NOT NULL",
            upload["id"],
        )
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
        # restore the grants: publication must STAY closed
        await c.execute(
            "UPDATE grants SET revoked_at = NULL WHERE drive_id = $1",
            drive["id"],
        )
    assert release["release_kind"] == "released"
    assert release["releases"] == 1
    assert artifacts == 0
    late = await _complete(http, drive["id"], upload["id"], "k-pr3")
    assert late.status_code == 409, late.text
    assert late.json()["error"]["code"] == "UPLOAD_NOT_COMPLETABLE"
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
    assert artifacts == 0


async def test_post_adoption_destination_gone_terminalizes_rejected(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Blocker: destination deletion after adoption is a terminal rejection
    with quarantine — restoration must not reopen publication."""
    override_actor(make_actor())
    drive = await _create_drive(http, "pd", "k-pd")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-pd1")
    original = uploads_core.record_adopted_observation

    async def record_then_delete_folder(c, **kwargs):
        await original(c, **kwargs)
        async with conn() as c2:
            await c2.execute(
                "UPDATE folders SET deleted_at = now() WHERE id = $1",
                drive["root_folder_id"],
            )

    monkeypatch.setattr(
        uploads_core, "record_adopted_observation", record_then_delete_folder
    )
    resp = await _complete(http, drive["id"], upload["id"], "k-pd2")
    assert resp.status_code == 404, resp.text
    monkeypatch.setattr(uploads_core, "record_adopted_observation", original)
    async with conn() as c:
        await c.execute(
            "UPDATE folders SET deleted_at = NULL WHERE id = $1",
            drive["root_folder_id"],
        )
    row = await _session_row(upload["id"])
    assert row["state"] == "rejected"
    assert row["cleanup_state"] == "quarantined"
    late = await _complete(http, drive["id"], upload["id"], "k-pd3")
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "UPLOAD_NOT_COMPLETABLE"
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
    assert artifacts == 0


async def test_reconciler_resumes_a_stale_completion_to_publication(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Blocker: transient completions are resumed by an EXECUTABLE bounded
    reconciler on the GC path — publication happens without any client
    retry, and the original key then receives its exact durable result via
    the real idempotency lease (no manual ledger surgery)."""
    from agentdrive.api import v0_uploads as uploads_api
    from agentdrive.core import idempotency

    override_actor(make_actor())
    monkeypatch.setattr(uploads_api, "COMPLETE_LEASE_SECONDS", 0)
    drive = await _create_drive(http, "rr", "k-rr")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-rr1")
    fake_storage.fail_stat = TransferProviderUnavailableError("stat_unavailable")
    resp = await _complete(http, drive["id"], upload["id"], "k-rr2")
    assert resp.status_code == 503
    row = await _session_row(upload["id"])
    assert row["state"] == "completing"

    result = await _gc_sweeper(fake_storage).run()
    assert result.completions_resumed >= 1, result.as_dict()
    row = await _session_row(upload["id"])
    assert row["state"] == "completed", dict(row)
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
        conversions = await c.fetchval(
            "SELECT count(*) FROM storage_reservations "
            "WHERE upload_id = $1 AND release_kind = 'converted'",
            upload["id"],
        )
    assert artifacts == 1
    assert conversions == 1
    # The same key attaches through the REAL idempotency crash lease and
    # receives the exact durable result.
    monkeypatch.setattr(idempotency, "IN_FLIGHT_LEASE_SECONDS", 0)
    same = await _complete(http, drive["id"], upload["id"], "k-rr2")
    assert same.status_code == 200, same.text
    assert same.json()["upload"]["result"]["artifact_id"].startswith("art_")


async def test_reconciler_is_bounded_on_persistent_provider_failure(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    from agentdrive.api import v0_uploads as uploads_api

    override_actor(make_actor())
    monkeypatch.setattr(uploads_api, "COMPLETE_LEASE_SECONDS", 0)
    drive = await _create_drive(http, "rb", "k-rb")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-rb1")
    fake_storage.fail_stat = TransferProviderUnavailableError("stat_unavailable")
    resp = await _complete(http, drive["id"], upload["id"], "k-rb2")
    assert resp.status_code == 503
    fake_storage.fail_stat_always = TransferProviderUnavailableError("stat_unavailable")
    # Two sweeps: each makes ONE bounded attempt and leaves the durable
    # action intact — no spin, no state corruption, no publication.
    for _ in range(2):
        result = await _gc_sweeper(fake_storage).run()
        assert result.completions_resumed >= 0  # sweep completes
        row = await _session_row(upload["id"])
        assert row["state"] == "completing"
    fake_storage.fail_stat_always = None
    result = await _gc_sweeper(fake_storage).run()
    row = await _session_row(upload["id"])
    assert row["state"] == "completed"


async def test_reconciler_terminalizes_a_deterministic_mismatch(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    from agentdrive.api import v0_uploads as uploads_api
    from agentdrive.core import idempotency

    override_actor(make_actor())
    monkeypatch.setattr(uploads_api, "COMPLETE_LEASE_SECONDS", 0)
    drive = await _create_drive(http, "rj", "k-rj")
    upload = await _begin_active(
        http, drive["id"], artifact_body(drive["root_folder_id"]), "k-rj1"
    )
    session = await _session_row(upload["id"])
    fake_storage.fail_stat = TransferProviderUnavailableError("stat_unavailable")
    resp = await _complete(http, drive["id"], upload["id"], "k-rj2")
    assert resp.status_code == 503
    # The object then finalizes with the WRONG checksum.
    fake_storage.finalize_scratch(
        session["scratch_object"], size=SIZE_A, crc32c=crc32c_of(b"tampered"),
    )
    await _gc_sweeper(fake_storage).run()
    row = await _session_row(upload["id"])
    assert row["state"] == "rejected", dict(row)
    assert row["failure_code"] == "CHECKSUM_MISMATCH"
    # A later completion key sees the safe terminal classification.
    monkeypatch.setattr(idempotency, "IN_FLIGHT_LEASE_SECONDS", 0)
    late = await _complete(http, drive["id"], upload["id"], "k-rj3")
    assert late.status_code == 409
    assert late.json()["error"]["details"]["failure"]["code"] == "CHECKSUM_MISMATCH"


async def test_begin_crash_after_active_commit_before_response(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    from agentdrive.api import v0_uploads as uploads_api

    override_actor(make_actor())
    drive = await _create_drive(http, "b9", "k-b9")
    original = uploads_api._session_payload
    calls = {"n": 0}

    def crash_on_disclosure(row, *, transfer=None):
        if transfer is not None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("injected crash after active commit")
        return original(row, transfer=transfer)

    monkeypatch.setattr(uploads_api, "_session_payload", crash_on_disclosure)
    with pytest.raises(RuntimeError, match="injected crash"):
        await _begin(http, drive["id"], artifact_body(drive["root_folder_id"]), "k-b9a")
    async with conn() as c:
        row = await c.fetchrow("SELECT state, target_disclosed FROM upload_sessions")
    assert row["state"] == "active"
    assert row["target_disclosed"] is True
    assert len(fake_storage.initiations) == 1
    replay = await _begin(http, drive["id"], artifact_body(drive["root_folder_id"]), "k-b9a")
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert "transfer" not in replay.json()["upload"]
    assert len(fake_storage.initiations) == 1  # never a second credential


async def test_complete_crash_after_commit_before_response(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    from agentdrive.api import v0_uploads as uploads_api

    override_actor(make_actor())
    drive = await _create_drive(http, "c9", "k-c9")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-c9a")
    original = uploads_api._json_response
    calls = {"n": 0}

    def crash_on_201(status, body, headers):
        if status == 201:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("injected crash after committed completion")
        return original(status, body, headers)

    monkeypatch.setattr(uploads_api, "_json_response", crash_on_201)
    with pytest.raises(RuntimeError, match="injected crash"):
        await _complete(http, drive["id"], upload["id"], "k-c9b")
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
    assert artifacts == 1  # the publication committed with its ledger record
    replay = await _complete(http, drive["id"], upload["id"], "k-c9b")
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json()["upload"]["result"]["artifact_id"].startswith("art_")


# ─── review round 4: drive liveness, ledger-takeover tolerance ──────────────


async def test_post_adoption_drive_soft_delete_terminalizes_rejected(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Round-4 should-fix: publication must never commit into a soft-deleted
    drive — the drive-liveness check is part of publication reauthorization,
    and restoration cannot reopen publication."""
    override_actor(make_actor())
    drive = await _create_drive(http, "dd", "k-dd")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-dd1")
    original = uploads_core.record_adopted_observation

    async def record_then_soft_delete_drive(c, **kwargs):
        await original(c, **kwargs)
        async with conn() as c2:
            await c2.execute(
                "UPDATE drives SET deleted_at = now() WHERE id = $1",
                drive["id"],
            )

    monkeypatch.setattr(
        uploads_core, "record_adopted_observation", record_then_soft_delete_drive
    )
    resp = await _complete(http, drive["id"], upload["id"], "k-dd2")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    monkeypatch.setattr(uploads_core, "record_adopted_observation", original)
    row = await _session_row(upload["id"])
    assert row["state"] == "rejected", dict(row)
    assert row["cleanup_state"] == "quarantined"
    async with conn() as c:
        released = await c.fetchval(
            "SELECT count(*) FROM storage_reservations "
            "WHERE upload_id = $1 AND release_kind = 'released'",
            upload["id"],
        )
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
        await c.execute(
            "UPDATE drives SET deleted_at = NULL WHERE id = $1", drive["id"]
        )
    assert released == 1
    assert artifacts == 0
    late = await _complete(http, drive["id"], upload["id"], "k-dd3")
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "UPLOAD_NOT_COMPLETABLE"
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
    assert artifacts == 0


async def test_post_adoption_drive_soft_delete_rejects_version_target(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    override_actor(make_actor())
    drive = await _create_drive(http, "dv", "k-dv")
    art = await _create_artifact(http, drive["id"], drive["root_folder_id"], "v.txt", "k-dv-a")
    begin = await _begin(
        http, drive["id"], version_body(art["id"]), "k-dv1",
        **{"If-Match": f'"{art["revision"]}"'},
    )
    assert begin.status_code == 201, begin.text
    upload = begin.json()["upload"]
    session = await _session_row(upload["id"])
    fake_storage.finalize_scratch(session["scratch_object"], size=SIZE_A, crc32c=CRC_A)
    original = uploads_core.record_adopted_observation

    async def record_then_soft_delete_drive(c, **kwargs):
        await original(c, **kwargs)
        async with conn() as c2:
            await c2.execute(
                "UPDATE drives SET deleted_at = now() WHERE id = $1",
                drive["id"],
            )

    monkeypatch.setattr(
        uploads_core, "record_adopted_observation", record_then_soft_delete_drive
    )
    resp = await _complete(http, drive["id"], upload["id"], "k-dv2")
    assert resp.status_code == 404, resp.text
    row = await _session_row(upload["id"])
    assert row["state"] == "rejected"
    async with conn() as c:
        direct_versions = await c.fetchval(
            "SELECT count(*) FROM artifact_versions WHERE artifact_id = $1 "
            "AND checksum LIKE 'crc32c:%'",
            art["id"],
        )
    assert direct_versions == 0


async def test_publication_survives_an_idempotency_claim_takeover(
    http, override_actor, enabled_transfer, fake_storage, monkeypatch,
):
    """Round-4 should-fix: a same-key retry that reaps the winner's claim
    row mid-saga (the 5-minute crash lease) must not roll back a correct
    publication — the commit proceeds, the store is skipped, and the key
    recovers its durable result from the terminal state."""
    from agentdrive.core import idempotency

    override_actor(make_actor())
    monkeypatch.setattr(idempotency, "IN_FLIGHT_LEASE_SECONDS", 0)
    drive = await _create_drive(http, "tk", "k-tk")
    upload = await _finalized_artifact_upload(http, drive, fake_storage, "k-tk1")

    gate = asyncio.Event()
    fake_storage.stall_next_stat = gate
    worker = asyncio.create_task(
        _complete(http, drive["id"], upload["id"], "k-tk2")
    )
    await asyncio.wait_for(fake_storage.stat_entered.wait(), timeout=5)
    # The same key retries: the zero-second crash lease reaps the winner's
    # claim row and this retry executes — the LIVE transition lease turns it
    # away without provider work.
    retry = await _complete(http, drive["id"], upload["id"], "k-tk2")
    assert retry.status_code in (409, 503), retry.text
    gate.set()
    done = await asyncio.wait_for(worker, timeout=10)
    # The winner's publication COMMITS even though its claim row is gone.
    assert done.status_code == 201, done.text
    async with conn() as c:
        artifacts = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE name = 'notes.txt'"
        )
        conversions = await c.fetchval(
            "SELECT count(*) FROM storage_reservations "
            "WHERE upload_id = $1 AND release_kind = 'converted'",
            upload["id"],
        )
    assert artifacts == 1
    assert conversions == 1
    # The key recovers the exact durable result from the terminal state.
    final = await _complete(http, drive["id"], upload["id"], "k-tk2")
    assert final.status_code == 200, final.text
    assert final.json()["upload"]["result"]["artifact_id"].startswith("art_")
