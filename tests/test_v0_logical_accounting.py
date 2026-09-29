"""Logical version-byte accounting across EVERY version producer (B3 §7/§9).

The committed quota unit is logical version bytes: every committed version
row counts its content size even when several rows share one physical
object. These tests prove:

  * inline artifact create, inline version append, artifact copy, every
    artifact materialized by folder copy, and version restore each increase
    the locked `drives.storage_bytes` counter and the additive
    `workspace_storage.committed_bytes` row EXACTLY once, through the one
    shared seam in `core/v0_content_commit.py`;
  * drive/folder/artifact lifecycle restore changes no logical byte count;
  * a same-key idempotent replay does not reserve or commit twice;
  * a released reservation releases exactly once (double release is a no-op
    at the counters);
  * a DB failure cannot leave committed accounting without a version row or
    vice versa (single-transaction atomicity);
  * concurrent reservations cannot overshoot a configured hard ceiling;
  * the pre-B8 compatibility boundary: with no entitlement ceiling
    configured, every producer preserves its shipped behavior and the
    15 MiB inline limit, direct transfer stays disabled, and a PARTIAL
    enabled configuration fails at boot rather than weakening policy.

All fixtures are synthetic.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"


def make_actor(*, scopes: set[str] | None = None) -> V0ActorContext:
    return V0ActorContext(
        subject=AGENT,
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(
            scopes
            if scopes is not None
            else {"content:read", "content:write", "drives:read", "drives:write",
                  "usage:read"}
        ),
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


@pytest_asyncio.fixture
async def override_actor(app_with_lifespan):
    def _set(actor: V0ActorContext) -> None:
        app.dependency_overrides[v0_actor] = lambda: actor

    yield _set
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
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


async def _create_drive(http, name: str, key: str) -> dict:
    app.dependency_overrides[v0_actor] = lambda: make_actor()
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


_MULTIPART_BOUNDARY = "----AccountingBoundaryXyZ"
_CT_MULTIPART = f"multipart/form-data; boundary={_MULTIPART_BOUNDARY}"


def _multipart(fields: dict[str, str], content: bytes) -> bytes:
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{_MULTIPART_BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f"--{_MULTIPART_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="content"; filename="t.bin"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{_MULTIPART_BOUNDARY}--\r\n".encode())
    return b"".join(parts)


async def _create_artifact(http, drive: dict, name: str, content: bytes, key: str):
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart({"parent_id": drive["root_folder_id"], "name": name}, content),
        headers={"Idempotency-Key": key, "Content-Type": _CT_MULTIPART},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _counters(drive_id: str) -> tuple[int, int, int, int]:
    """(drives.storage_bytes, sum(size_bytes), ws committed, ws reserved)."""
    async with conn() as c:
        counter = await c.fetchval(
            "SELECT storage_bytes FROM drives WHERE id = $1", drive_id
        )
        live_sum = await c.fetchval(
            "SELECT COALESCE(sum(v.size_bytes), 0) FROM artifact_versions v "
            "JOIN artifacts a ON a.id = v.artifact_id WHERE a.drive_id = $1",
            drive_id,
        )
        ws = await c.fetchrow(
            "SELECT committed_bytes, reserved_bytes FROM workspace_storage "
            "WHERE workspace_id = $1",
            WS_A,
        )
    return (
        int(counter),
        int(live_sum),
        int(ws["committed_bytes"]) if ws else 0,
        int(ws["reserved_bytes"]) if ws else 0,
    )


async def _live_reservations() -> int:
    async with conn() as c:
        return int(
            await c.fetchval(
                "SELECT count(*) FROM storage_reservations WHERE released_at IS NULL"
            )
        )


async def _seed_core_drive(c, drive_id: str, root_id: str) -> None:
    await c.execute(
        "INSERT INTO drives (id, workspace_id, name, revision) "
        "VALUES ($1, $2, 'core-fixture', 'rev_00000000000000c1')",
        drive_id, WS_A,
    )
    await c.execute(
        "INSERT INTO folders (id, drive_id, parent_id, name, revision) "
        "VALUES ($1, $2, NULL, NULL, 'rev_00000000000000c2')",
        root_id, drive_id,
    )
    await c.execute(
        "UPDATE drives SET root_folder_id = $2 WHERE id = $1", drive_id, root_id
    )


# ---------------------------------------------------------------------------
# every version producer crosses the one seam exactly once
# ---------------------------------------------------------------------------


async def test_inline_create_and_append_commit_exactly_once(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "acct-a", "kacct-1")
    assert await _counters(drive["id"]) == (0, 0, 0, 0)

    art = await _create_artifact(http, drive, "one.bin", b"hello world", "kacct-2")
    assert await _counters(drive["id"]) == (11, 11, 11, 0)

    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions",
        content=_multipart({}, b"12345"),
        headers={
            "Idempotency-Key": "kacct-3",
            "Content-Type": _CT_MULTIPART,
            "If-Match": f'"{art["revision"]}"',
        },
    )
    assert resp.status_code == 201, resp.text
    assert await _counters(drive["id"]) == (16, 16, 16, 0)
    assert await _live_reservations() == 0

    # the usage read reports the LOCKED counter, and it agrees with the sum
    usage = await http.get(f"/v0/drives/{drive['id']}/usage")
    assert usage.status_code == 200, usage.text
    assert usage.json()["storage_bytes"] == 16


async def test_same_key_replay_neither_reserves_nor_commits_twice(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "acct-replay", "krep-1")
    await _create_artifact(http, drive, "r.bin", b"0123456789", "krep-2")
    assert await _counters(drive["id"]) == (10, 10, 10, 0)

    # byte-identical replay: same key, same request
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart(
            {"parent_id": drive["root_folder_id"], "name": "r.bin"}, b"0123456789"
        ),
        headers={"Idempotency-Key": "krep-2", "Content-Type": _CT_MULTIPART},
    )
    assert resp.status_code == 201, resp.text  # stored original response
    assert await _counters(drive["id"]) == (10, 10, 10, 0)
    assert await _live_reservations() == 0


async def test_artifact_copy_commits_logical_bytes_without_byte_copy(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "acct-copy", "kcopy-1")
    art = await _create_artifact(http, drive, "src.bin", b"abcdefgh", "kcopy-2")
    assert await _counters(drive["id"]) == (8, 8, 8, 0)

    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/copy",
        json={
            "destination_drive_id": drive["id"],
            "destination_parent_id": drive["root_folder_id"],
            "destination_name": "dst.bin",
        },
        headers={"Idempotency-Key": "kcopy-3"},
    )
    assert resp.status_code == 201, resp.text
    # physical object is shared; LOGICAL bytes count for both version rows
    assert await _counters(drive["id"]) == (16, 16, 16, 0)


async def test_folder_copy_commits_every_materialized_artifact(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "acct-fcopy", "kf-1")
    sub = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        json={"parent_id": drive["root_folder_id"], "name": "src"},
        headers={"Idempotency-Key": "kf-2"},
    )
    assert sub.status_code == 201, sub.text
    folder = sub.json()
    for i, content in enumerate((b"aaa", b"bbbbb")):
        resp = await http.post(
            f"/v0/drives/{drive['id']}/artifacts",
            content=_multipart(
                {"parent_id": folder["id"], "name": f"f{i}.bin"}, content
            ),
            headers={"Idempotency-Key": f"kf-3-{i}", "Content-Type": _CT_MULTIPART},
        )
        assert resp.status_code == 201, resp.text
    assert await _counters(drive["id"]) == (8, 8, 8, 0)

    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders/{folder['id']}/copy",
        json={
            "destination_parent_id": drive["root_folder_id"],
            "destination_name": "dst",
        },
        headers={"Idempotency-Key": "kf-4"},
    )
    assert resp.status_code in (200, 201, 202), resp.text
    assert await _counters(drive["id"]) == (16, 16, 16, 0)
    assert await _live_reservations() == 0


async def test_version_restore_commits_logical_bytes(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "acct-restore", "kr-1")
    art = await _create_artifact(http, drive, "v.bin", b"first", "kr-2")

    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions",
        content=_multipart({}, b"second!"),
        headers={
            "Idempotency-Key": "kr-3",
            "Content-Type": _CT_MULTIPART,
            "If-Match": f'"{art["revision"]}"',
        },
    )
    assert resp.status_code == 201, resp.text
    new_revision = resp.json()["artifact_revision"]
    assert await _counters(drive["id"]) == (12, 12, 12, 0)

    versions = await http.get(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions"
    )
    first_version = versions.json()["items"][-1]["id"]
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions/"
        f"{first_version}/restore",
        headers={"Idempotency-Key": "kr-4", "If-Match": f'"{new_revision}"'},
    )
    assert resp.status_code == 201, resp.text
    # the restored head is a NEW version row referencing the old bytes:
    # +5 logical bytes even though no physical byte moved
    assert await _counters(drive["id"]) == (17, 17, 17, 0)


# ---------------------------------------------------------------------------
# lifecycle restore is NOT a version producer
# ---------------------------------------------------------------------------


async def test_lifecycle_delete_and_restore_change_no_logical_bytes(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "acct-life", "kl-1")
    art = await _create_artifact(http, drive, "life.bin", b"stay", "kl-2")
    baseline = await _counters(drive["id"])
    assert baseline == (4, 4, 4, 0)

    deleted = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}",
        headers={"Idempotency-Key": "kl-3", "If-Match": f'"{art["revision"]}"'},
    )
    assert deleted.status_code == 200, deleted.text
    assert await _counters(drive["id"]) == baseline

    restored = await http.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/restore",
        headers={
            "Idempotency-Key": "kl-4",
            "If-Match": f'"{deleted.json()["revision"]}"',
        },
    )
    assert restored.status_code == 200, restored.text
    assert await _counters(drive["id"]) == baseline
    assert await _live_reservations() == 0


# ---------------------------------------------------------------------------
# reservation ledger: exactly-once release, atomicity, concurrency
# ---------------------------------------------------------------------------


async def test_release_is_exactly_once(app_with_lifespan):
    from agentdrive.core import v0_content_commit as acct

    async with conn() as c:
        await _seed_core_drive(c, "drv_00000000000000c1", "fld_0000000000000c01")
        async with c.transaction():
            rsv = await acct.reserve_version_bytes(
                c, workspace_id=WS_A, drive_id="drv_00000000000000c1",
                principal_id=AGENT, upload_id=None, size_bytes=50,
            )
        live = await c.fetchval(
            "SELECT COALESCE(sum(size_bytes), 0) FROM storage_reservations "
            "WHERE workspace_id=$1 AND released_at IS NULL",
            WS_A,
        )
        assert int(live) == 50

        first = await acct.release_version_reservation(c, reservation_id=rsv)
        assert first is True
        second = await acct.release_version_reservation(c, reservation_id=rsv)
        assert second is False  # double release is a detected no-op
        live = await c.fetchval(
            "SELECT COALESCE(sum(size_bytes), 0) FROM storage_reservations "
            "WHERE workspace_id=$1 AND released_at IS NULL",
            WS_A,
        )
        assert int(live) == 0
        committed = await c.fetchval(
            "SELECT COALESCE(committed_bytes, 0) FROM workspace_storage "
            "WHERE workspace_id=$1",
            WS_A,
        )
        assert int(committed or 0) == 0


async def test_inline_reservations_never_lock_the_workspace_row(app_with_lifespan):
    """Security review I5: the disabled-path (shipped-behavior) reserve must
    not write or lock `workspace_storage` — that row lock was being held
    across the GCS upload, serializing every write in a workspace. Inline
    reservations live and die inside their own transaction; the workspace
    row moves only at commit time (after the object write) and for
    direct-upload sessions."""
    from agentdrive.core import v0_content_commit as acct

    async with conn() as c:
        await _seed_core_drive(c, "drv_00000000000000c5", "fld_0000000000000c05")

    async with conn() as holder, conn() as prober, holder.transaction():
        await acct.reserve_version_bytes(
            holder, workspace_id=WS_A, drive_id="drv_00000000000000c5",
            principal_id=AGENT, upload_id=None, size_bytes=5,
        )
        # while the reserving transaction is still open (the window that
        # used to span storage.put), the workspace row is untouched and
        # another session can write it without blocking
        row = await prober.fetchrow(
            "SELECT reserved_bytes FROM workspace_storage "
            "WHERE workspace_id = $1",
            WS_A,
        )
        assert row is None or row["reserved_bytes"] == 0
        async with prober.transaction():
            await prober.execute("SET LOCAL statement_timeout = '1000ms'")
            await prober.execute(
                "INSERT INTO workspace_storage (workspace_id) VALUES ($1) "
                "ON CONFLICT (workspace_id) DO UPDATE SET updated_at = now()",
                WS_A,
            )  # a lock held by `holder` would time this out


async def test_db_failure_cannot_split_accounting_from_version_row(app_with_lifespan):
    from agentdrive.core import v0_content_commit as acct

    async with conn() as c:
        await _seed_core_drive(c, "drv_00000000000000c2", "fld_0000000000000c02")
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000c02', 'drv_00000000000000c2', "
            "'fld_0000000000000c02', 'atomic.bin', 'rev_0000000000000c02')"
        )
        with pytest.raises(RuntimeError, match="injected"):
            async with c.transaction():
                rsv = await acct.reserve_version_bytes(
                    c, workspace_id=WS_A, drive_id="drv_00000000000000c2",
                    principal_id=AGENT, upload_id=None, size_bytes=6,
                )
                await acct.commit_immutable_version(
                    c,
                    acct.ImmutableVersionCommit(
                        drive_id="drv_00000000000000c2",
                        workspace_id=WS_A,
                        artifact_id="art_0000000000000c02",
                        version_id="ver_0000000000000c02",
                        parent_version_id=None,
                        ordinal=1,
                        checksum="sha256:0c02",
                        content_type="text/plain",
                        size_bytes=6,
                        storage_object="cas/drv_0c2/0c02",
                        storage_bucket="bucket-demo",
                        storage_generation=7,
                        actor_type="agent",
                        actor_id=AGENT,
                        reservation_id=rsv,
                    ),
                )
                raise RuntimeError("injected failure after commit seam")
        # everything rolled back together: no version, no counters, no ledger
        counters = await _counters("drv_00000000000000c2")
        assert counters == (0, 0, 0, 0)
        versions = await c.fetchval(
            "SELECT count(*) FROM artifact_versions WHERE id='ver_0000000000000c02'"
        )
        assert versions == 0
        assert await _live_reservations() == 0


async def test_concurrent_reservations_cannot_overshoot_hard_ceiling(
    app_with_lifespan,
):
    from agentdrive.core import v0_content_commit as acct

    async with conn() as c:
        await _seed_core_drive(c, "drv_00000000000000c3", "fld_0000000000000c03")

    async def try_reserve() -> str | None:
        async with conn() as c:
            try:
                async with c.transaction():
                    return await acct.reserve_version_bytes(
                        c, workspace_id=WS_A, drive_id="drv_00000000000000c3",
                        principal_id=AGENT, upload_id=None, size_bytes=60,
                        workspace_limit_bytes=100, drive_limit_bytes=100,
                    )
            except acct.QuotaExceededError:
                return None

    results = await asyncio.gather(try_reserve(), try_reserve())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, (
        f"exactly one of two 60-byte reservations may fit a 100-byte "
        f"ceiling; got {results}"
    )
    async with conn() as c:
        live = await c.fetchval(
            "SELECT COALESCE(sum(size_bytes), 0) FROM storage_reservations "
            "WHERE workspace_id=$1 AND released_at IS NULL",
            WS_A,
        )
        assert int(live) == 60


async def test_reservation_size_must_match_commit_size(app_with_lifespan):
    from agentdrive.core import v0_content_commit as acct

    async with conn() as c:
        await _seed_core_drive(c, "drv_00000000000000c4", "fld_0000000000000c04")
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000c04', 'drv_00000000000000c4', "
            "'fld_0000000000000c04', 'mismatch.bin', 'rev_0000000000000c04')"
        )
        with pytest.raises(acct.AccountingError):
            async with c.transaction():
                rsv = await acct.reserve_version_bytes(
                    c, workspace_id=WS_A, drive_id="drv_00000000000000c4",
                    principal_id=AGENT, upload_id=None, size_bytes=10,
                )
                await acct.commit_immutable_version(
                    c,
                    acct.ImmutableVersionCommit(
                        drive_id="drv_00000000000000c4",
                        workspace_id=WS_A,
                        artifact_id="art_0000000000000c04",
                        version_id="ver_0000000000000c04",
                        parent_version_id=None,
                        ordinal=1,
                        checksum="sha256:0c04",
                        content_type="text/plain",
                        size_bytes=99,  # != reserved 10
                        storage_object="cas/drv_0c4/0c04",
                        storage_bucket=None,
                        storage_generation=None,
                        actor_type="agent",
                        actor_id=AGENT,
                        reservation_id=rsv,
                    ),
                )


async def test_no_direct_artifact_version_insert_outside_the_seam():
    """Structural guard: `INSERT INTO artifact_versions` exists ONLY inside
    core/v0_content_commit.py. Any producer bypassing the seam is exactly the
    accounting drift B3 forbids."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parent.parent / "src"
    pattern = re.compile(r"insert\s+into\s+artifact_versions", re.IGNORECASE)
    offenders = []
    for path in src.rglob("*.py"):
        if pattern.search(path.read_text()) and path.name != "v0_content_commit.py":
            offenders.append(str(path.relative_to(src)))
    assert offenders == [], (
        f"version rows must be inserted only through the shared commit seam; "
        f"found direct inserts in {offenders}"
    )


# ---------------------------------------------------------------------------
# pre-B8 compatibility boundary
# ---------------------------------------------------------------------------


async def test_default_configuration_keeps_shipped_behavior(http, override_actor):
    from agentdrive.config import settings
    from agentdrive.core.v0_artifacts import MAX_BUFFERED_UPLOAD_BYTES

    # direct transfer is disabled by default and the shipped inline ceiling
    # is exactly 15 MiB
    assert settings.direct_transfer_enabled is False
    assert MAX_BUFFERED_UPLOAD_BYTES == 15 * 1024 * 1024

    override_actor(make_actor())
    drive = await _create_drive(http, "acct-compat", "kc-1")
    # no ceiling configured: a normal write succeeds with exact accounting
    await _create_artifact(http, drive, "ok.bin", b"x" * 32, "kc-2")
    assert await _counters(drive["id"]) == (32, 32, 32, 0)
    # the shipped 15 MiB inline limit still rejects oversize content
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart(
            {"parent_id": drive["root_folder_id"], "name": "big.bin"},
            b"y" * (MAX_BUFFERED_UPLOAD_BYTES + 1),
        ),
        headers={"Idempotency-Key": "kc-3", "Content-Type": _CT_MULTIPART},
    )
    assert resp.status_code == 413, resp.text
    assert await _counters(drive["id"]) == (32, 32, 32, 0)


async def test_partial_enabled_configuration_fails_at_boot(monkeypatch):
    """`direct_transfer_enabled=true` without its complete bounded numeric
    policy must prevent readiness, never silently weaken enforcement."""
    from agentdrive.config import Settings

    base = {
        "database_url": "postgresql://t:t@localhost:5432/x",
        "gcs_bucket": "bucket-demo",
        "session_secret": "s" * 48,
    }
    # disabled: ceilings may be absent
    Settings(**base, direct_transfer_enabled=False)
    # enabled but partial or non-positive: fail closed
    with pytest.raises(ValueError):
        Settings(**base, direct_transfer_enabled=True)
    with pytest.raises(ValueError):
        Settings(
            **base,
            direct_transfer_enabled=True,
            direct_transfer_hard_logical_version_bytes_workspace=10_000,
        )
    with pytest.raises(ValueError):
        Settings(
            **base,
            direct_transfer_enabled=True,
            direct_transfer_hard_logical_version_bytes_workspace=0,
            direct_transfer_hard_logical_version_bytes_drive=5_000,
        )
    # §9 requires the COMPLETE bounded configuration surface (origin, exact
    # bucket/prefixes, endpoints, TTLs, session/rate limits, GC schedule)
    # before enablement. Packet 1 supplies only the ceilings, so even a
    # ceilings-complete enablement must refuse to boot until packets 2–3
    # land the rest of the surface (security review I4).
    with pytest.raises(ValueError, match="packet|incomplete|surface"):
        Settings(
            **base,
            direct_transfer_enabled=True,
            direct_transfer_hard_logical_version_bytes_workspace=10_000,
            direct_transfer_hard_logical_version_bytes_drive=5_000,
        )


# ---------------------------------------------------------------------------
# packet-1 correction pass (PR #456 review comment)
# ---------------------------------------------------------------------------


async def test_duplicate_cas_content_keeps_a_stable_generation(http, override_actor):
    """Correction blocker 1: `storage.put` used to overwrite an existing CAS
    key, replacing the generation an earlier immutable version row points at
    (artifact-bucket versioning is OFF — the old generation would be gone).
    CAS creation must be create-only; a duplicate reuses the EXISTING
    object's generation."""
    from agentdrive import storage

    override_actor(make_actor())
    drive = await _create_drive(http, "acct-dup", "kdup-1")
    content = b"identical bytes"
    await _create_artifact(http, drive, "first.bin", content, "kdup-2")
    await _create_artifact(http, drive, "second.bin", content, "kdup-3")

    async with conn() as c:
        rows = await c.fetch(
            "SELECT v.storage_object, v.storage_generation "
            "FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id "
            "WHERE a.drive_id = $1 ORDER BY v.created_at",
            drive["id"],
        )
    assert len(rows) == 2
    assert rows[0]["storage_object"] == rows[1]["storage_object"]  # same CAS key
    assert rows[0]["storage_generation"] is not None
    assert rows[0]["storage_generation"] == rows[1]["storage_generation"], (
        "a duplicate-content write replaced the generation an earlier "
        "version row points at"
    )
    stat = await storage.stat(rows[0]["storage_object"])
    assert stat.generation == rows[0]["storage_generation"], (
        "the landed object's generation no longer matches the version rows"
    )


async def test_concurrent_duplicate_cas_writes_converge_on_one_generation(
    app_with_lifespan,
):
    """Correction blocker 1 (concurrent half): two racing writers of the
    same content must converge on ONE object generation — the loser of the
    create-only race adopts the winner's coordinates."""
    from agentdrive import storage

    name = "cas/drv_00000000000000c9/concurrent-demo"
    results = await asyncio.gather(
        storage.put(name, b"raced bytes", "application/octet-stream"),
        storage.put(name, b"raced bytes", "application/octet-stream"),
    )
    assert results[0].generation is not None
    assert results[0].generation == results[1].generation, (
        f"racing duplicate writes produced two generations: {results}"
    )
    stat = await storage.stat(name)
    assert stat.generation == results[0].generation


async def test_release_helper_is_atomic_under_failure_injection(app_with_lifespan):
    """Correction blocker 3: the ledger release and the workspace gauge
    decrement are two statements; on GC's autocommit connection a crash
    between them left permanent gauge drift. The helper must be atomic —
    an injected failure between the two effects rolls BOTH back, and a
    retry completes both."""
    from agentdrive.core import v0_content_commit as acct
    from agentdrive.core import v0_uploads as uploads

    class FailOnGaugeDecrement:
        """Wraps a real connection; raises on the reserved-gauge decrement."""

        def __init__(self, real):
            self._real = real
            self.armed = True

        def __getattr__(self, name):
            return getattr(self._real, name)

        async def execute(self, sql, *args):
            if self.armed and "reserved_bytes = reserved_bytes - " in sql:
                raise RuntimeError("injected crash between release effects")
            return await self._real.execute(sql, *args)

    async with conn() as c:
        await _seed_core_drive(c, "drv_00000000000000c6", "fld_0000000000000c06")
        await c.execute(
            "INSERT INTO folders (id, drive_id, parent_id, name, revision) "
            "VALUES ('fld_0000000000000c07', 'drv_00000000000000c6', "
            "'fld_0000000000000c06', 'sub', 'rev_0000000000000c07')"
        )
        async with c.transaction():
            await uploads.create_session(
                c, upload_id="upld_00000000000000c6", workspace_id=WS_A,
                drive_id="drv_00000000000000c6",
                principal_type="agent", principal_id=AGENT,
                target_kind="artifact",
                parent_folder_id="fld_0000000000000c06",
                artifact_name="atomic.bin", artifact_id=None,
                expected_artifact_revision=None,
                declared_size_bytes=25, declared_media_type="text/plain",
                declared_crc32c="yZRlqg==",
                adoption_marker="mark_00000000000000c6",
                scratch_object="transfer-scratch/upld_00000000000000c6",
                final_object="transfer-immutable/upld_00000000000000c6",
                expires_in_seconds=3600,
            )
        reserved = await c.fetchval(
            "SELECT reserved_bytes FROM workspace_storage WHERE workspace_id=$1",
            WS_A,
        )
        assert int(reserved) == 25

        # autocommit connection + injected failure between the two effects
        wrapper = FailOnGaugeDecrement(c)
        with pytest.raises(RuntimeError, match="injected crash"):
            await acct.release_version_reservation(
                wrapper, upload_id="upld_00000000000000c6"
            )
        row = await c.fetchrow(
            "SELECT released_at FROM storage_reservations "
            "WHERE upload_id = 'upld_00000000000000c6'"
        )
        reserved = await c.fetchval(
            "SELECT reserved_bytes FROM workspace_storage WHERE workspace_id=$1",
            WS_A,
        )
        assert row["released_at"] is None, (
            "the ledger release survived a crash that lost the gauge decrement"
        )
        assert int(reserved) == 25

        # retry on the healthy connection performs BOTH effects exactly once
        wrapper.armed = False
        assert await acct.release_version_reservation(
            c, upload_id="upld_00000000000000c6"
        ) is True
        reserved = await c.fetchval(
            "SELECT reserved_bytes FROM workspace_storage WHERE workspace_id=$1",
            WS_A,
        )
        assert int(reserved) == 0


async def test_commit_seam_rejects_inconsistent_coordinate_pairs(app_with_lifespan):
    """Correction blocker 6 (seam half): bucket and generation are all-or-
    none; the seam refuses a half-pair before the schema even sees it."""
    from agentdrive.core import v0_content_commit as acct

    async with conn() as c:
        await _seed_core_drive(c, "drv_00000000000000c7", "fld_0000000000000c08")
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000c07', 'drv_00000000000000c7', "
            "'fld_0000000000000c08', 'pair.bin', 'rev_0000000000000c08')"
        )
        for bucket, generation in (("bucket-demo", None), (None, 7), ("", 7)):
            with pytest.raises(acct.AccountingError):
                async with c.transaction():
                    rsv = await acct.reserve_version_bytes(
                        c, workspace_id=WS_A, drive_id="drv_00000000000000c7",
                        principal_id=AGENT, upload_id=None, size_bytes=4,
                    )
                    await acct.commit_immutable_version(
                        c,
                        acct.ImmutableVersionCommit(
                            drive_id="drv_00000000000000c7",
                            workspace_id=WS_A,
                            artifact_id="art_0000000000000c07",
                            version_id="ver_0000000000000c07",
                            parent_version_id=None, ordinal=1,
                            checksum="sha256:c07", content_type="text/plain",
                            size_bytes=4, storage_object="cas/pair",
                            storage_bucket=bucket,
                            storage_generation=generation,
                            actor_type="agent", actor_id=AGENT,
                            reservation_id=rsv,
                        ),
                    )


async def test_reserve_verifies_the_drive_belongs_to_the_workspace(
    app_with_lifespan,
):
    """Correction 'important' item: ownership is derived from the drive row,
    not trusted from duplicated caller inputs — a reservation naming the
    wrong workspace for a drive is refused."""
    from agentdrive.core import v0_content_commit as acct

    async with conn() as c:
        await _seed_core_drive(c, "drv_00000000000000c8", "fld_0000000000000c09")
        with pytest.raises(acct.AccountingError):
            await acct.reserve_version_bytes(
                c, workspace_id="tcws_0000000000000009",  # not the drive's
                drive_id="drv_00000000000000c8",
                principal_id=AGENT, upload_id=None, size_bytes=1,
            )
