"""Generation reconciliation for legacy CAS version rows (B3 §7).

`python -m agentdrive.jobs.reconcile_generations` scans every version row
whose object coordinates are incomplete (`storage_generation IS NULL`),
verifies the CAS object's identity (size against the row, the CAS key's
SHA-256 digest against the stored algorithm-qualified checksum), and fills
`storage_bucket`/`storage_generation` only while the locked row still holds
the same object/checksum/size. It supports `--dry-run`, never deletes or
rewrites content, is idempotent, and leaves mismatches unresolved with safe
counts/ids for operator repair.

Also proven here:
  * new inline writes persist their exact uploaded generation + bucket in
    the same publication flow — reconciliation is for LEGACY rows only;
  * the immutable trigger permits only the guarded NULL → observed-value
    transition (rechecked at the job level);
  * download/transfer readiness stays false while any live version row is
    unresolved (and while the feature flag is off).
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio

from agentdrive import storage
from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

WS = "tcws_0000000000000001"
AGENT = "tcagt_0000000000000001"
DRIVE = "drv_00000000000000b1"
ROOT = "fld_0000000000000b01"


@pytest_asyncio.fixture(autouse=True)
async def _clean(app_with_lifespan):
    # Clean BEFORE too: suites that predate B3 truncate drives but not the
    # workspace accounting row, so committed bytes would leak in.
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, storage_reservations, "
            "upload_sessions, workspace_storage, drives "
            "RESTART IDENTITY CASCADE"
        )
    yield
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, storage_reservations, "
            "upload_sessions, workspace_storage, drives "
            "RESTART IDENTITY CASCADE"
        )


async def _seed(c) -> None:
    await c.execute(
        "INSERT INTO drives (id, workspace_id, name, revision) VALUES "
        "($1, $2, 'reconcile-fixture', 'rev_00000000000000b1') "
        "ON CONFLICT (id) DO NOTHING",
        DRIVE, WS,
    )
    await c.execute(
        "INSERT INTO folders (id, drive_id, parent_id, name, revision) VALUES "
        "($1, $2, NULL, NULL, 'rev_00000000000000b2') "
        "ON CONFLICT (id) DO NOTHING",
        ROOT, DRIVE,
    )
    await c.execute(
        "UPDATE drives SET root_folder_id = $2 WHERE id = $1", DRIVE, ROOT
    )
    await c.execute(
        "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
        "VALUES ('art_0000000000000b01', $1, $2, 'legacy.bin', "
        "'rev_0000000000000b03') ON CONFLICT (id) DO NOTHING",
        DRIVE, ROOT,
    )


async def _legacy_row(
    c, version_id: str, content: bytes, *, ordinal: int,
    checksum: str | None = None, size: int | None = None,
    put_object: bool = True,
) -> str:
    """A pre-0049 CAS version row: NULL coordinates, object (usually) real."""
    from agentdrive import storage

    digest = hashlib.sha256(content).hexdigest()
    object_name = f"cas/{DRIVE}/{digest}"
    if put_object:
        await storage.put(object_name, content, "application/octet-stream")
    await c.execute(
        "INSERT INTO artifact_versions (id, artifact_id, checksum, "
        "content_type, size_bytes, storage_object, actor_type, actor_id, "
        "ordinal) VALUES ($1, 'art_0000000000000b01', $2, "
        "'application/octet-stream', $3, $4, 'agent', $5, $6)",
        version_id, checksum or f"sha256:{digest}",
        size if size is not None else len(content), object_name, AGENT, ordinal,
    )
    return object_name


async def test_dry_run_reports_without_writing(app_with_lifespan):
    from agentdrive.jobs.reconcile_generations import reconcile_all

    async with conn() as c:
        await _seed(c)
        await _legacy_row(c, "ver_0000000000000b01", b"legacy-one", ordinal=1)

    report = await reconcile_all(dry_run=True)
    assert report.scanned == 1
    assert report.resolved == 1  # resolvable, but…
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT storage_bucket, storage_generation FROM artifact_versions "
            "WHERE id = 'ver_0000000000000b01'"
        )
    assert dict(row) == {"storage_bucket": None, "storage_generation": None}


async def test_reconcile_fills_verified_rows_and_is_idempotent(app_with_lifespan):
    from agentdrive import storage
    from agentdrive.jobs.reconcile_generations import reconcile_all

    async with conn() as c:
        await _seed(c)
        name = await _legacy_row(c, "ver_0000000000000b02", b"legacy-two", ordinal=1)

    report = await reconcile_all(dry_run=False)
    assert (report.scanned, report.resolved, report.unresolved) == (1, 1, 0)
    stat = await storage.stat(name)
    async with conn() as c:
        row = await c.fetchrow(
            "SELECT storage_bucket, storage_generation FROM artifact_versions "
            "WHERE id = 'ver_0000000000000b02'"
        )
    assert row["storage_bucket"] == storage.store_id()
    assert row["storage_generation"] == stat.generation

    # idempotent: nothing left to scan, nothing rewritten
    report = await reconcile_all(dry_run=False)
    assert (report.scanned, report.resolved) == (0, 0)


async def test_mismatches_stay_unresolved_with_safe_ids(app_with_lifespan):
    from agentdrive.jobs.reconcile_generations import reconcile_all

    async with conn() as c:
        await _seed(c)
        # (a) object missing entirely
        await _legacy_row(
            c, "ver_0000000000000b03", b"absent", ordinal=1, put_object=False
        )
        # (b) recorded size disagrees with the landed object
        await _legacy_row(
            c, "ver_0000000000000b04", b"size-mismatch", ordinal=2, size=999
        )
        # (c) stored checksum disagrees with the CAS key's digest identity
        await _legacy_row(
            c, "ver_0000000000000b05", b"checksum-mismatch", ordinal=3,
            checksum="sha256:" + "0" * 64,
        )

    report = await reconcile_all(dry_run=False)
    assert report.scanned == 3
    assert report.resolved == 0
    assert report.unresolved == 3
    assert set(report.unresolved_version_ids) == {
        "ver_0000000000000b03", "ver_0000000000000b04", "ver_0000000000000b05",
    }
    async with conn() as c:
        unresolved = await c.fetchval(
            "SELECT count(*) FROM artifact_versions "
            "WHERE storage_generation IS NULL"
        )
    assert unresolved == 3
    # the job never deletes or rewrites content: rows and objects intact
    async with conn() as c:
        assert await c.fetchval("SELECT count(*) FROM artifact_versions") == 3


async def test_cli_supports_dry_run(app_with_lifespan):
    from agentdrive.jobs.reconcile_generations import build_parser

    parser = build_parser()
    assert parser.parse_args([]).dry_run is False
    assert parser.parse_args(["--dry-run"]).dry_run is True


async def test_new_inline_writes_persist_generation(app_with_lifespan):
    """Fresh writes carry their exact uploaded coordinates — reconciliation
    is only for pre-0049 rows."""
    from httpx import ASGITransport, AsyncClient


    actor = V0ActorContext(
        subject=AGENT, subject_type="agent", workspace_id=WS,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({"content:write", "drives:read", "drives:write"}),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id=None, workspace_role=None,
    )
    app.dependency_overrides[v0_actor] = lambda: actor
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            drive = await http.post(
                "/v0/drives", json={"name": "gen"},
                headers={"Idempotency-Key": "kgen-1"},
            )
            assert drive.status_code == 201, drive.text
            body = drive.json()
            boundary = "----GenBoundaryXyZ"
            content = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="parent_id"\r\n\r\n'
                f"{body['root_folder_id']}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="name"\r\n\r\nfresh.bin\r\n'
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="content"; '
                f'filename="fresh.bin"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + b"fresh bytes" + f"\r\n--{boundary}--\r\n".encode()
            created = await http.post(
                f"/v0/drives/{body['id']}/artifacts",
                content=content,
                headers={
                    "Idempotency-Key": "kgen-2",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            assert created.status_code == 201, created.text
    finally:
        app.dependency_overrides.clear()

    async with conn() as c:
        row = await c.fetchrow(
            "SELECT v.storage_bucket, v.storage_generation "
            "FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id "
            "WHERE a.name = 'fresh.bin'"
        )
    assert row["storage_bucket"] == storage.store_id()
    assert row["storage_generation"] is not None
    assert row["storage_generation"] > 0


async def test_transfer_readiness_requires_flag_and_zero_unresolved(
    app_with_lifespan, monkeypatch
):
    from agentdrive.config import settings
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        # default: flag off → not ready
        ready, reasons = await uploads.transfer_readiness(c)
        assert ready is False
        assert any("direct_transfer_enabled" in r for r in reasons)

        # flag on but a live unresolved legacy row → still not ready
        monkeypatch.setattr(settings, "direct_transfer_enabled", True)
        monkeypatch.setattr(
            settings, "direct_transfer_hard_logical_version_bytes_workspace", 1000
        )
        monkeypatch.setattr(
            settings, "direct_transfer_hard_logical_version_bytes_drive", 1000
        )
        await _seed(c)
        await _legacy_row(c, "ver_0000000000000b06", b"unresolved", ordinal=1)
        ready, reasons = await uploads.transfer_readiness(c)
        assert ready is False
        assert any("unresolved" in r for r in reasons)

        # resolve it → ready
        from agentdrive.jobs.reconcile_generations import reconcile_all

        await reconcile_all(dry_run=False)
        ready, reasons = await uploads.transfer_readiness(c)
        assert (ready, reasons) == (True, [])
