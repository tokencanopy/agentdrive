"""The live GC command: legacy sweep semantics + B3 transfer cleanup.

`python -m agentdrive.jobs.gc` is a launch precondition (B3 §9): Terraform
schedules it daily (purge + CAS mark-sweep + scratch sweep + session
reconciliation) and weekly with `--orphan-sweep`. These tests prove:

  * the scheduled CLI contract survives — normal, `--dry-run`, and
    `--orphan-sweep` behavior, and the Terraform/Makefile callers still name
    the same command;
  * legacy semantics are retained on the v0 schema: soft-delete purge (with
    the B3 counter decrements), per-drive CAS mark/sweep with generation
    pinned deletes, scratch sweep, and the hard-purged-drive orphan sweep;
  * B3 transfer cleanup is generation-safe: incomplete provider sessions are
    invisible, a late finalized object is quarantined then collected by
    exact generation, live transition leases are never collected, grace is
    honored, a failed generation precondition retries rather than deleting
    a changed object, an adopted-but-uncommitted final object is an orphan,
    a COMMITTED generation is never selected, publication state stays
    terminal while cleanup progresses, and a retention/hold rejection parks
    cleanup as `blocked`.

All fixtures are synthetic; provider behavior is faked through the injected
TransferStorage seam except where the fake-GCS emulator is the honest test
substrate (CAS/scratch/orphan sweeps).
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
import pytest_asyncio

from agentdrive.db import conn

pytestmark = pytest.mark.asyncio

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

WS = "tcws_0000000000000001"
AGENT = "tcagt_0000000000000001"
DRIVE = "drv_00000000000000d1"
ROOT = "fld_0000000000000d01"
CRC = "yZRlqg=="


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


async def _seed_drive(c, drive_id=DRIVE, root_id=ROOT) -> None:
    await c.execute(
        "INSERT INTO drives (id, workspace_id, name, revision) VALUES "
        "($1, $2, 'gc-fixture', 'rev_00000000000000d1') "
        "ON CONFLICT (id) DO NOTHING",
        drive_id, WS,
    )
    await c.execute(
        "INSERT INTO folders (id, drive_id, parent_id, name, revision) VALUES "
        "($1, $2, NULL, NULL, 'rev_00000000000000d2') "
        "ON CONFLICT (id) DO NOTHING",
        root_id, drive_id,
    )
    await c.execute(
        "UPDATE drives SET root_folder_id = $2 WHERE id = $1", drive_id, root_id
    )


async def _make_session(c, upload_id: str, **over):
    """A direct-transfer session fixture in an arbitrary state."""
    from agentdrive.core import v0_uploads as uploads

    await _seed_drive(c)
    async with c.transaction():
        row = await uploads.create_session(
            c, upload_id=upload_id, workspace_id=WS, drive_id=DRIVE,
            principal_type="agent", principal_id=AGENT,
            target_kind="artifact", parent_folder_id=ROOT,
            artifact_name=f"{upload_id}.bin", artifact_id=None,
            expected_artifact_revision=None,
            declared_size_bytes=11, declared_media_type="text/plain",
            declared_crc32c=CRC, adoption_marker=f"mark_{upload_id}",
            scratch_object=f"transfer-scratch/{upload_id}",
            final_object=f"transfer-immutable/{upload_id}",
            expires_in_seconds=over.pop("expires_in_seconds", 3600),
        )
    if over:
        # One statement: the terminal/lease shape CHECKs demand that related
        # columns move together.
        sets = ", ".join(
            f"{column} = ${i + 2}" for i, column in enumerate(over)
        )
        await c.execute(
            f"UPDATE upload_sessions SET {sets} WHERE id = $1",
            upload_id, *over.values(),
        )
    return row


class _FakeTransferStorage:
    """Canned observations + scripted delete outcomes."""

    def __init__(self, observations=None, delete_errors=None):
        self.observations = dict(observations or {})
        self.delete_errors = dict(delete_errors or {})
        self.deleted: list[tuple[str, int]] = []

    async def stat_object(self, object_name):
        return self.observations.get(object_name)

    async def delete_generation(self, object_name, generation):
        err = self.delete_errors.get(object_name)
        if err is not None:
            raise err
        self.deleted.append((object_name, generation))
        self.observations.pop(object_name, None)


def _observation(object_name, *, generation=99, size=11, marker=None):
    from agentdrive.core.v0_uploads import ObjectObservation

    return ObjectObservation(
        object_name=object_name, generation=generation, size=size,
        crc32c=CRC, content_type="text/plain",
        adoption_marker=marker,
    )


def _sweeper(transfer_storage=None, **class_over):
    """A GCSweeper with test-friendly zeroed age gates."""
    from agentdrive.core.gc import GCSweeper

    attrs = {
        "TRANSFER_GRACE": dt.timedelta(0),
        "PURGE_RETENTION": dt.timedelta(0),
        "MARK_SWEEP_AGE": dt.timedelta(0),
        "SCRATCH_SWEEP_AGE": dt.timedelta(0),
        "ORPHAN_SWEEP_AGE": dt.timedelta(0),
        "LATE_FINALIZATION_WINDOW": dt.timedelta(0),
    }
    attrs.update(class_over)
    cls = type("TestSweeper", (GCSweeper,), attrs)
    return cls(transfer_storage=transfer_storage)


async def _session_row(upload_id: str):
    async with conn() as c:
        return await c.fetchrow(
            "SELECT state, cleanup_state, cleanup_failure_class "
            "FROM upload_sessions WHERE id = $1",
            upload_id,
        )


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


async def test_cli_contract_normal_dry_run_orphan_sweep():
    from agentdrive.jobs.gc import build_parser, make_sweeper

    parser = build_parser()
    args = parser.parse_args([])
    assert args.dry_run is False and args.orphan_sweep is False
    args = parser.parse_args(["--dry-run"])
    assert args.dry_run is True
    args = parser.parse_args(["--orphan-sweep"])
    assert args.orphan_sweep is True
    sweeper = make_sweeper(parser.parse_args(["--orphan-sweep"]))
    assert sweeper.include_orphan_sweep is True
    args = parser.parse_args(["--sessions-only"])
    assert args.sessions_only is True
    assert make_sweeper(args).sessions_only is True
    # Default off: the daily and weekly schedules pass no such flag.
    assert make_sweeper(parser.parse_args([])).sessions_only is False


async def test_sessions_only_runs_the_session_phases_and_no_listing_phase():
    """The hourly pass releases slots, reservations and scratch, and must
    NOT pay the daily's per-drive `cas/<drive_id>/` listing — that cost
    grows with stored objects and is why the full sweep stays daily."""
    from agentdrive.core.gc import GCSweeper, SweepResult

    session_phases = [
        "_reconcile_sessions",
        "_transfer_cleanup",
        "_reclaim_leaked_reservations",
        "_remove_expired_session_rows",
    ]
    listing_phases = ["_purge", "_mark_sweep", "_scratch_sweep", "_orphan_sweep"]

    async def _run(sessions_only: bool) -> list[str]:
        ran: list[str] = []
        sweeper = GCSweeper(sessions_only=sessions_only, include_orphan_sweep=True)
        for name in session_phases + listing_phases:
            def _phase(conn, result, _name=name):
                async def _inner():
                    ran.append(_name)
                return _inner()
            setattr(sweeper, name, _phase)
        await sweeper._phases(None, SweepResult(dry_run=False))
        return ran

    assert await _run(True) == session_phases
    # The full sweep is unchanged, orphan sweep included when asked for.
    assert await _run(False) == session_phases + listing_phases


async def test_make_sweeper_tolerates_a_namespace_without_the_new_flag():
    """Operators and older callers hand-build Namespaces; a flag added to
    the parser must not turn those into AttributeErrors."""
    import argparse

    from agentdrive.jobs.gc import make_sweeper

    sweeper = make_sweeper(argparse.Namespace(dry_run=False, orphan_sweep=False))
    assert sweeper.sessions_only is False


async def test_scheduled_callers_still_name_the_command():
    """Terraform and the Makefile are contract tests for the CLI (B3 §9):
    the scheduled command and its argument meanings must not change.

    Terraform is the hosted deployment's and is absent from a standalone
    checkout; the test then pins the Makefile half alone."""
    mk = (REPO_ROOT / "Makefile").read_text()
    assert "--dry-run" in mk
    # the module the scheduler names actually exists and is runnable
    from agentdrive.jobs import gc as gc_job

    tf_path = REPO_ROOT / "infra/terraform/modules/agentdrive-environment/main.tf"
    if tf_path.exists():
        tf = tf_path.read_text()
        assert '"python", "-m", "agentdrive.jobs.gc"' in tf
        assert '"--orphan-sweep"' in tf
        # The hourly session pass is what keeps an abandoned upload's slot,
        # reservation and scratch from being held until the next daily sweep.
        assert '"--sessions-only"' in tf
        assert 'schedule    = "0 * * * *"' in tf
    assert callable(gc_job.main)


# ---------------------------------------------------------------------------
# transfer-session reconciliation + cleanup
# ---------------------------------------------------------------------------


async def test_incomplete_provider_session_is_invisible(app_with_lifespan):
    """An unfinalized resumable session produces no object: GC must neither
    crash nor delete anything, and the live session is untouched."""
    async with conn() as c:
        await _make_session(c, "upld_00000000000000d1", state="active",
                            target_disclosed=True)
    storage = _FakeTransferStorage({})  # nothing stat-able anywhere
    result = await _sweeper(storage).run()
    assert storage.deleted == []
    row = await _session_row("upld_00000000000000d1")
    assert row["state"] == "active"
    assert result.transfer_deleted == 0


async def test_overdue_sessions_expire_exactly_once_with_release(app_with_lifespan):
    async with conn() as c:
        await _make_session(
            c, "upld_00000000000000d2", expires_in_seconds=-5,
            state="active", target_disclosed=True,
        )
    storage = _FakeTransferStorage({})
    result = await _sweeper(storage).run()
    row = await _session_row("upld_00000000000000d2")
    assert row["state"] == "expired"
    assert result.sessions_expired == 1
    async with conn() as c:
        reserved = await c.fetchval(
            "SELECT reserved_bytes FROM workspace_storage WHERE workspace_id=$1",
            WS,
        )
        live = await c.fetchval(
            "SELECT count(*) FROM storage_reservations WHERE released_at IS NULL"
        )
    assert (int(reserved), int(live)) == (0, 0)
    # a second sweep neither re-expires nor double-releases
    result = await _sweeper(storage).run()
    assert result.sessions_expired == 0
    async with conn() as c:
        reserved = await c.fetchval(
            "SELECT reserved_bytes FROM workspace_storage WHERE workspace_id=$1",
            WS,
        )
    assert int(reserved) == 0


async def test_late_finalized_scratch_is_quarantined_then_generation_deleted(
    app_with_lifespan,
):
    """GCS accepted an old URI after product expiry: publication stays
    `expired`, the object is quarantined, then deleted by EXACT generation."""
    async with conn() as c:
        await _make_session(
            c, "upld_00000000000000d3", expires_in_seconds=-5,
            state="active", target_disclosed=True,
        )
    scratch = "transfer-scratch/upld_00000000000000d3"
    storage = _FakeTransferStorage({scratch: _observation(scratch, generation=77)})
    first = await _sweeper(storage).run()  # expires the session…
    second = await _sweeper(storage).run()  # …and collection converges
    assert (scratch, 77) in storage.deleted
    row = await _session_row("upld_00000000000000d3")
    assert row["state"] == "expired"  # cleanup NEVER changes publication
    assert row["cleanup_state"] == "cleaned"
    assert first.transfer_deleted + second.transfer_deleted == 1


async def test_live_transition_lease_is_never_collected(app_with_lifespan):
    async with conn() as c:
        await _make_session(
            c, "upld_00000000000000d4", state="completing",
            target_disclosed=True, transition_action="complete",
            transition_lease_id="rev_00000000000000d4",
            transition_lease_expires_at=dt.datetime.now(dt.UTC)
            + dt.timedelta(minutes=10),
        )
    scratch = "transfer-scratch/upld_00000000000000d4"
    storage = _FakeTransferStorage({scratch: _observation(scratch)})
    await _sweeper(storage).run()
    assert storage.deleted == []
    row = await _session_row("upld_00000000000000d4")
    assert row["state"] == "completing"


async def test_grace_defers_collection(app_with_lifespan):
    async with conn() as c:
        await _make_session(
            c, "upld_00000000000000d5", expires_in_seconds=-5,
            state="active", target_disclosed=True,
        )
    scratch = "transfer-scratch/upld_00000000000000d5"
    storage = _FakeTransferStorage({scratch: _observation(scratch)})
    graceful = _sweeper(storage, TRANSFER_GRACE=dt.timedelta(hours=1))
    await graceful.run()  # expire
    await graceful.run()  # within grace: nothing collected yet
    assert storage.deleted == []
    row = await _session_row("upld_00000000000000d5")
    assert row["state"] == "expired"


async def test_generation_precondition_failure_retries_not_deletes(app_with_lifespan):
    """A failed generation precondition means the object CHANGED (possibly a
    late finalization): do not delete the new generation; restat and retry."""
    from agentdrive import storage as real_storage

    async with conn() as c:
        await _make_session(
            c, "upld_00000000000000d6", expires_in_seconds=-5,
            state="active", target_disclosed=True,
        )
    scratch = "transfer-scratch/upld_00000000000000d6"
    storage = _FakeTransferStorage(
        {scratch: _observation(scratch, generation=5)},
        delete_errors={scratch: real_storage.PreconditionFailed("generation moved")},
    )
    await _sweeper(storage).run()  # expire
    await _sweeper(storage).run()  # delete fails on the precondition
    assert storage.deleted == []
    row = await _session_row("upld_00000000000000d6")
    assert row["state"] == "expired"
    assert row["cleanup_state"] in ("pending", "quarantined")  # retryable, not cleaned
    # the object settles; the next sweep collects the newly observed generation
    storage.delete_errors.clear()
    storage.observations[scratch] = _observation(scratch, generation=6)
    await _sweeper(storage).run()
    assert (scratch, 6) in storage.deleted
    row = await _session_row("upld_00000000000000d6")
    assert row["cleanup_state"] == "cleaned"


async def test_adopted_but_uncommitted_final_is_an_orphan(app_with_lifespan):
    async with conn() as c:
        await _make_session(
            c, "upld_00000000000000d7", state="rejected",
            target_disclosed=True, failure_code="PRECONDITION_FAILED",
            cleanup_state="pending", adopted_generation=88,
            adopted_size=11, adopted_crc32c=CRC,
            adopted_content_type="text/plain",
            terminal_at=dt.datetime.now(dt.UTC),
            cleanup_next_attempt_at=dt.datetime.now(dt.UTC),
        )
        # its reservation was already released on the rejection path
        from agentdrive.core import v0_content_commit as acct

        await acct.release_version_reservation(c, upload_id="upld_00000000000000d7")
    final = "transfer-immutable/upld_00000000000000d7"
    storage = _FakeTransferStorage({final: _observation(final, generation=88)})
    await _sweeper(storage).run()
    assert (final, 88) in storage.deleted
    row = await _session_row("upld_00000000000000d7")
    assert row["state"] == "rejected"
    assert row["cleanup_state"] == "cleaned"


async def test_committed_generation_is_never_selected(app_with_lifespan):
    """The committed-version live set is authoritative: a COMPLETED session's
    final object/generation is content and must never be deleted; only its
    scratch object is collectable."""
    from agentdrive.core import v0_content_commit as acct

    final = "transfer-immutable/upld_00000000000000d8"
    scratch = "transfer-scratch/upld_00000000000000d8"
    async with conn() as c:
        await _make_session(c, "upld_00000000000000d8", target_disclosed=True)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000d08', $1, $2, 'done.bin', "
            "'rev_0000000000000d08')",
            DRIVE, ROOT,
        )
        async with c.transaction():
            reservation_id = await c.fetchval(
                "SELECT id FROM storage_reservations WHERE upload_id = $1 "
                "AND released_at IS NULL",
                "upld_00000000000000d8",
            )
            await acct.commit_immutable_version(
                c,
                acct.ImmutableVersionCommit(
                    drive_id=DRIVE, workspace_id=WS,
                    artifact_id="art_0000000000000d08",
                    version_id="ver_0000000000000d08",
                    parent_version_id=None, ordinal=1,
                    checksum=f"crc32c:{CRC}", content_type="text/plain",
                    size_bytes=11, storage_object=final,
                    storage_bucket="transfer-bucket-demo",
                    storage_generation=88,
                    actor_type="agent", actor_id=AGENT,
                    reservation_id=reservation_id,
                ),
            )
            await c.execute(
                "UPDATE upload_sessions SET state = 'completing', "
                "adopted_generation = 88, adopted_size = 11, "
                "adopted_crc32c = $2, adopted_content_type = 'text/plain' "
                "WHERE id = $1",
                "upld_00000000000000d8", CRC,
            )
            await c.execute(
                "UPDATE upload_sessions SET state = 'completed', "
                "result_artifact_id = 'art_0000000000000d08', "
                "result_version_id = 'ver_0000000000000d08', "
                "result_revision = 'rev_0000000000000d08', "
                "terminal_at = now(), cleanup_state = 'pending', "
                "cleanup_next_attempt_at = now() WHERE id = $1",
                "upld_00000000000000d8",
            )
    storage = _FakeTransferStorage(
        {
            final: _observation(final, generation=88),
            scratch: _observation(scratch, generation=44),
        }
    )
    await _sweeper(storage).run()
    assert (scratch, 44) in storage.deleted
    assert all(name != final for name, _ in storage.deleted), (
        "GC deleted a committed generation"
    )
    row = await _session_row("upld_00000000000000d8")
    assert row["state"] == "completed"
    assert row["cleanup_state"] == "cleaned"


async def test_retention_hold_blocks_cleanup_visibly(app_with_lifespan):
    async with conn() as c:
        await _make_session(
            c, "upld_00000000000000d9", expires_in_seconds=-5,
            state="active", target_disclosed=True,
        )
    scratch = "transfer-scratch/upld_00000000000000d9"
    storage = _FakeTransferStorage(
        {scratch: _observation(scratch)},
        delete_errors={scratch: RuntimeError("retention policy rejected delete")},
    )
    await _sweeper(storage).run()  # expire
    await _sweeper(storage).run()  # delete blocked
    row = await _session_row("upld_00000000000000d9")
    assert row["state"] == "expired"
    assert row["cleanup_state"] == "blocked"
    assert row["cleanup_failure_class"] == "RuntimeError"


async def test_dry_run_reconciles_nothing_and_deletes_nothing(app_with_lifespan):
    async with conn() as c:
        await _make_session(
            c, "upld_0000000000000d10", expires_in_seconds=-5,
            state="active", target_disclosed=True,
        )
    scratch = "transfer-scratch/upld_0000000000000d10"
    storage = _FakeTransferStorage({scratch: _observation(scratch)})
    result = await _sweeper(storage).run(dry_run=True)
    assert result.dry_run is True
    assert storage.deleted == []
    row = await _session_row("upld_0000000000000d10")
    assert row["state"] == "active"  # even terminalization is previewed only


# ---------------------------------------------------------------------------
# retained legacy sweeps on the v0 schema
# ---------------------------------------------------------------------------


async def test_soft_delete_purge_hard_deletes_and_decrements_counters(
    app_with_lifespan,
):
    from agentdrive.core import v0_content_commit as acct

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision, "
            "deleted_at) VALUES ('art_0000000000000d11', $1, $2, 'gone.bin', "
            "'rev_0000000000000d11', now() - interval '90 days')",
            DRIVE, ROOT,
        )
        async with c.transaction():
            rsv = await acct.reserve_version_bytes(
                c, workspace_id=WS, drive_id=DRIVE, principal_id=AGENT,
                upload_id=None, size_bytes=10,
            )
            await acct.commit_immutable_version(
                c,
                acct.ImmutableVersionCommit(
                    drive_id=DRIVE, workspace_id=WS,
                    artifact_id="art_0000000000000d11",
                    version_id="ver_0000000000000d11",
                    parent_version_id=None, ordinal=1,
                    checksum="sha256:d11", content_type="text/plain",
                    size_bytes=10, storage_object="cas/gone",
                    storage_bucket=None, storage_generation=None,
                    actor_type="agent", actor_id=AGENT, reservation_id=rsv,
                ),
            )

    # dry run first: previewed, rolled back
    result = await _sweeper(_FakeTransferStorage()).run(dry_run=True)
    assert result.purged_artifacts == 1
    async with conn() as c:
        remaining = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE id='art_0000000000000d11'"
        )
    assert remaining == 1

    result = await _sweeper(_FakeTransferStorage()).run()
    assert result.purged_artifacts == 1
    async with conn() as c:
        remaining = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE id='art_0000000000000d11'"
        )
        counter = await c.fetchval(
            "SELECT storage_bytes FROM drives WHERE id=$1", DRIVE
        )
        committed = await c.fetchval(
            "SELECT committed_bytes FROM workspace_storage WHERE workspace_id=$1",
            WS,
        )
    assert remaining == 0
    assert int(counter) == 0  # the purge keeps the locked counter exact
    assert int(committed) == 0
    # a fresh soft-delete is NOT purged under the real retention default
    async with conn() as c:
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision, "
            "deleted_at) VALUES ('art_0000000000000d12', $1, $2, 'young.bin', "
            "'rev_0000000000000d12', now())",
            DRIVE, ROOT,
        )
    retained = _sweeper(
        _FakeTransferStorage(), PURGE_RETENTION=dt.timedelta(days=30)
    )
    result = await retained.run()
    assert result.purged_artifacts == 0


async def test_cas_mark_sweep_deletes_orphans_keeps_referenced(app_with_lifespan):
    from agentdrive import storage as real_storage
    from agentdrive.core import v0_content_commit as acct

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000d13', $1, $2, 'kept.bin', "
            "'rev_0000000000000d13')",
            DRIVE, ROOT,
        )
    referenced = f"cas/{DRIVE}/referenced-demo"
    orphan = f"cas/{DRIVE}/orphan-demo"
    await real_storage.put(referenced, b"keep me", "text/plain")
    await real_storage.put(orphan, b"sweep me", "text/plain")
    async with conn() as c, c.transaction():
        rsv = await acct.reserve_version_bytes(
            c, workspace_id=WS, drive_id=DRIVE, principal_id=AGENT,
            upload_id=None, size_bytes=7,
        )
        await acct.commit_immutable_version(
            c,
            acct.ImmutableVersionCommit(
                drive_id=DRIVE, workspace_id=WS,
                artifact_id="art_0000000000000d13",
                version_id="ver_0000000000000d13",
                parent_version_id=None, ordinal=1,
                checksum="sha256:d13", content_type="text/plain",
                size_bytes=7, storage_object=referenced,
                storage_bucket=None, storage_generation=None,
                actor_type="agent", actor_id=AGENT, reservation_id=rsv,
            ),
        )

    result = await _sweeper(_FakeTransferStorage()).run()
    assert await real_storage.stat(referenced) is not None
    assert await real_storage.stat(orphan) is None
    assert result.cas_deleted >= 1
    assert result.parity_mismatches == 0


async def test_scratch_sweep_and_orphan_prefix_sweep(app_with_lifespan):
    from agentdrive import storage as real_storage

    scratch = "embed-scratch/drv_00000000000000d1/leftover-demo"
    orphan_prefix_blob = "cas/drv_000000000000gone/orphan-demo"
    await real_storage.put(scratch, b"scratch", "application/octet-stream")
    await real_storage.put(orphan_prefix_blob, b"orphaned", "application/octet-stream")
    async with conn() as c:
        await _seed_drive(c)

    # daily shape: scratch swept, orphan prefixes untouched
    await _sweeper(_FakeTransferStorage()).run()
    assert await real_storage.stat(scratch) is None
    assert await real_storage.stat(orphan_prefix_blob) is not None

    # weekly shape: --orphan-sweep collects prefixes whose drive is gone
    from agentdrive.core.gc import GCSweeper

    cls = type(
        "TestSweeper", (GCSweeper,),
        {
            "TRANSFER_GRACE": dt.timedelta(0),
            "PURGE_RETENTION": dt.timedelta(0),
            "MARK_SWEEP_AGE": dt.timedelta(0),
            "SCRATCH_SWEEP_AGE": dt.timedelta(0),
            "ORPHAN_SWEEP_AGE": dt.timedelta(0),
        },
    )
    await cls(
        include_orphan_sweep=True, transfer_storage=_FakeTransferStorage()
    ).run()
    assert await real_storage.stat(orphan_prefix_blob) is None


async def test_terminal_session_rows_are_retained_then_removed(app_with_lifespan):
    """Terminal non-secret state is retained for the recovery window, then
    the row is deleted only after release + cleaned (§9 step 6)."""
    async with conn() as c:
        await _make_session(
            c, "upld_0000000000000d14", expires_in_seconds=-5,
            state="active", target_disclosed=True,
        )
    storage = _FakeTransferStorage({})
    await _sweeper(storage).run()  # expire (cleanup finds nothing → cleaned)
    await _sweeper(storage).run()
    # within retention the row survives
    retained = _sweeper(storage, TERMINAL_RETENTION=dt.timedelta(days=7))
    await retained.run()
    async with conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM upload_sessions WHERE id='upld_0000000000000d14'"
        ) == 1
    # past retention it is removed
    zero = _sweeper(storage, TERMINAL_RETENTION=dt.timedelta(0))
    await zero.run()
    async with conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM upload_sessions WHERE id='upld_0000000000000d14'"
        ) == 0


# ---------------------------------------------------------------------------
# review findings (packet-1 adversarial + contract review), pinned as tests
# ---------------------------------------------------------------------------


async def test_purge_never_deletes_a_concurrently_restored_artifact(
    app_with_lifespan,
):
    """Security review C1: the purge candidate SELECT and the DELETE were two
    statements; a restore landing between them was hard-deleted anyway. The
    DELETE must re-check `deleted_at` so a restored row survives."""
    from agentdrive.core import v0_content_commit as acct
    from agentdrive.core.gc import GCSweeper

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision, "
            "deleted_at) VALUES ('art_0000000000000d20', $1, $2, 'race.bin', "
            "'rev_0000000000000d20', now() - interval '90 days')",
            DRIVE, ROOT,
        )
        async with c.transaction():
            rsv = await acct.reserve_version_bytes(
                c, workspace_id=WS, drive_id=DRIVE, principal_id=AGENT,
                upload_id=None, size_bytes=10,
            )
            await acct.commit_immutable_version(
                c,
                acct.ImmutableVersionCommit(
                    drive_id=DRIVE, workspace_id=WS,
                    artifact_id="art_0000000000000d20",
                    version_id="ver_0000000000000d20",
                    parent_version_id=None, ordinal=1,
                    checksum="sha256:d20", content_type="text/plain",
                    size_bytes=10, storage_object="cas/race",
                    storage_bucket=None, storage_generation=None,
                    actor_type="agent", actor_id=AGENT, reservation_id=rsv,
                ),
            )

    class RestoreBetweenSelectAndDelete(GCSweeper):
        TRANSFER_GRACE = dt.timedelta(0)
        PURGE_RETENTION = dt.timedelta(0)
        MARK_SWEEP_AGE = dt.timedelta(0)
        SCRATCH_SWEEP_AGE = dt.timedelta(0)
        ORPHAN_SWEEP_AGE = dt.timedelta(0)

        async def _purge_artifact_candidates(self, c, cutoff):
            rows = await super()._purge_artifact_candidates(c, cutoff)
            if rows:
                # a caller restores the artifact inside the race window
                async with conn() as other:
                    await other.execute(
                        "UPDATE artifacts SET deleted_at = NULL "
                        "WHERE id = 'art_0000000000000d20'"
                    )
            return rows

    await RestoreBetweenSelectAndDelete(
        transfer_storage=_FakeTransferStorage()
    ).run()
    async with conn() as c:
        survived = await c.fetchval(
            "SELECT count(*) FROM artifacts WHERE id='art_0000000000000d20' "
            "AND deleted_at IS NULL"
        )
        counter = await c.fetchval(
            "SELECT storage_bytes FROM drives WHERE id=$1", DRIVE
        )
        committed = await c.fetchval(
            "SELECT committed_bytes FROM workspace_storage WHERE workspace_id=$1",
            WS,
        )
    assert survived == 1, "purge hard-deleted a concurrently restored artifact"
    assert int(counter) == 10, "purge decremented the counter for a surviving row"
    assert int(committed or 0) == 10


async def test_purge_never_deletes_a_concurrently_restored_drive(app_with_lifespan):
    """Security review C1 (drive half): same race, CASCADE stakes."""
    from agentdrive.core.gc import GCSweeper

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "UPDATE drives SET deleted_at = now() - interval '90 days' "
            "WHERE id = $1",
            DRIVE,
        )

    class RestoreBetweenSelectAndDelete(GCSweeper):
        TRANSFER_GRACE = dt.timedelta(0)
        PURGE_RETENTION = dt.timedelta(0)
        MARK_SWEEP_AGE = dt.timedelta(0)
        SCRATCH_SWEEP_AGE = dt.timedelta(0)
        ORPHAN_SWEEP_AGE = dt.timedelta(0)

        async def _purge_drive_candidates(self, c, cutoff):
            rows = await super()._purge_drive_candidates(c, cutoff)
            if rows:
                async with conn() as other:
                    await other.execute(
                        "UPDATE drives SET deleted_at = NULL WHERE id = $1",
                        DRIVE,
                    )
            return rows

    await RestoreBetweenSelectAndDelete(
        transfer_storage=_FakeTransferStorage()
    ).run()
    async with conn() as c:
        survived = await c.fetchval(
            "SELECT count(*) FROM drives WHERE id=$1 AND deleted_at IS NULL",
            DRIVE,
        )
    assert survived == 1, "purge hard-deleted a concurrently restored drive"


async def test_terminal_session_with_leaked_reservation_is_reclaimed(
    app_with_lifespan,
):
    """Security review I1: a crash between the terminal transition and the
    release used to strand the reservation forever (nothing re-selected
    terminal sessions). GC must reclaim it exactly once."""
    async with conn() as c:
        await _make_session(
            c, "upld_0000000000000d21", state="expired",
            failure_code="UPLOAD_EXPIRED", cleanup_state="cleaned",
            terminal_at=dt.datetime.now(dt.UTC),
        )
        # simulate the crash: terminal state persisted, release never ran —
        # the reservation row is still live
        live = await c.fetchval(
            "SELECT count(*) FROM storage_reservations "
            "WHERE upload_id = 'upld_0000000000000d21' AND released_at IS NULL"
        )
        assert live == 1  # fixture sanity: the leak exists
    result = await _sweeper(
        _FakeTransferStorage(), TERMINAL_RETENTION=dt.timedelta(days=7)
    ).run()
    async with conn() as c:
        live = await c.fetchval(
            "SELECT count(*) FROM storage_reservations "
            "WHERE upload_id = 'upld_0000000000000d21' AND released_at IS NULL"
        )
    assert live == 0, "GC left a terminal session's reservation live"
    assert result.reservations_reclaimed == 1
    # second sweep: nothing left to reclaim (exactly-once)
    result = await _sweeper(
        _FakeTransferStorage(), TERMINAL_RETENTION=dt.timedelta(days=7)
    ).run()
    assert result.reservations_reclaimed == 0


async def test_overdue_preparing_session_expires_and_releases(app_with_lifespan):
    """Contract review finding 1: a session that crashed before its one
    initiation attempt and then passed its deadline stayed `preparing`
    forever, holding its reservation. GC must terminalize it as expired."""
    async with conn() as c:
        await _make_session(
            c, "upld_0000000000000d22", expires_in_seconds=-5,
        )  # preparing, provider never attempted, deadline passed
    result = await _sweeper(_FakeTransferStorage()).run()
    row = await _session_row("upld_0000000000000d22")
    assert row["state"] == "expired", (
        "an overdue preparing session must terminalize, not stay retryable"
    )
    assert result.sessions_expired == 1
    async with conn() as c:
        live = await c.fetchval(
            "SELECT count(*) FROM storage_reservations WHERE released_at IS NULL"
        )
    assert live == 0


async def test_missed_object_is_watched_until_late_finalization_window_ends(
    app_with_lifespan,
):
    """Contract review finding 3: a stat-miss on the first post-grace sweep
    used to mark cleanup `cleaned`, so an object the old URI finalized LATER
    (provider expiry is ~a week) was never collected. The session must stay
    quarantined until the late-finalization window has elapsed."""
    async with conn() as c:
        await _make_session(
            c, "upld_0000000000000d23", expires_in_seconds=-5,
            state="active", target_disclosed=True,
            provider_attempted_at=dt.datetime.now(dt.UTC),  # a URI EXISTS
        )
    scratch = "transfer-scratch/upld_0000000000000d23"
    storage = _FakeTransferStorage({})  # nothing observable yet
    watching = _sweeper(
        storage, LATE_FINALIZATION_WINDOW=dt.timedelta(days=8)
    )
    await watching.run()  # expire
    await watching.run()  # miss — must keep watching, NOT clean
    row = await _session_row("upld_0000000000000d23")
    assert row["state"] == "expired"
    assert row["cleanup_state"] != "cleaned", (
        "cleanup closed before the provider's late-finalization window ended"
    )
    # the old URI finalizes the object days later…
    storage.observations[scratch] = _observation(scratch, generation=31)
    await watching.run()
    assert (scratch, 31) in storage.deleted
    row = await _session_row("upld_0000000000000d23")
    assert row["cleanup_state"] == "cleaned"
    # …and once the window HAS elapsed with nothing observed, cleanup closes
    async with conn() as c:
        await _make_session(
            c, "upld_0000000000000d24", expires_in_seconds=-5,
            state="active", target_disclosed=True,
            provider_attempted_at=dt.datetime.now(dt.UTC),
        )
    elapsed = _sweeper(
        _FakeTransferStorage(), LATE_FINALIZATION_WINDOW=dt.timedelta(0)
    )
    await elapsed.run()
    await elapsed.run()
    row = await _session_row("upld_0000000000000d24")
    assert row["cleanup_state"] == "cleaned"


async def test_default_transfer_storage_refuses_until_packet_2(app_with_lifespan):
    """Security review I2: the default adapter pointed at the ARTIFACT bucket
    with no adoption-marker read — misclassifying reconciliation and marking
    unreachable objects cleaned. Until packet 2 supplies the transfer-bucket
    adapter it must refuse loudly, and a refusal must surface as a failed
    (non-zero-exit) sweep rather than silent success."""
    from agentdrive.core import gc as gc_core

    sweeper = gc_core.GCSweeper()
    with pytest.raises(gc_core.TransferStorageUnavailableError):
        await sweeper.transfer.stat_object("transfer-scratch/upld_demo")
    with pytest.raises(gc_core.TransferStorageUnavailableError):
        await sweeper.transfer.delete_generation("transfer-scratch/upld_demo", 1)

    # with a session present, a default-adapter sweep records the failure
    async with conn() as c:
        await _make_session(
            c, "upld_0000000000000d25", expires_in_seconds=-5,
            state="active", target_disclosed=True,
        )

    class Zeroed(gc_core.GCSweeper):
        TRANSFER_GRACE = dt.timedelta(0)
        PURGE_RETENTION = dt.timedelta(0)
        MARK_SWEEP_AGE = dt.timedelta(0)
        SCRATCH_SWEEP_AGE = dt.timedelta(0)
        ORPHAN_SWEEP_AGE = dt.timedelta(0)
        LATE_FINALIZATION_WINDOW = dt.timedelta(0)

    first = await Zeroed().run()   # expiry needs no provider stat
    second = await Zeroed().run()  # cleanup does — and must FAIL, not clean
    row = await _session_row("upld_0000000000000d25")
    assert row["cleanup_state"] != "cleaned"
    assert (first.errors + second.errors) >= 1
    assert (first.failed or second.failed) is True


async def test_reconcile_never_adopts_when_marker_is_unobservable(
    app_with_lifespan,
):
    """Security review I2 (reconcile half): an observation whose adoption
    marker cannot be read is AMBIGUOUS — never adopted, never rejected."""
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _make_session(c, "upld_0000000000000d26",
                                  target_disclosed=True)
        await c.execute(
            "UPDATE upload_sessions SET state='completing', "
            "transition_action='complete', "
            "transition_lease_id='rev_0000000000000d26', "
            "transition_lease_expires_at = now() - interval '1 second' "
            "WHERE id=$1",
            row["id"],
        )
        final = "transfer-immutable/upld_0000000000000d26"
        storage = _FakeTransferStorage(
            {final: _observation(final, generation=61, marker=None)}
        )
        async with c.transaction():
            outcome = await uploads.reconcile_upload_session(
                c, upload_id=row["id"], storage=storage,
            )
        assert outcome == "retry_rewrite", (
            "an unverifiable marker must be ambiguous, not adopted/rejected"
        )
        got = await c.fetchrow(
            "SELECT state, adopted_generation FROM upload_sessions WHERE id=$1",
            row["id"],
        )
        assert dict(got) == {"state": "completing", "adopted_generation": None}


async def test_sweep_failures_and_parity_mismatches_fail_the_job(app_with_lifespan):
    """Security review I3: phase errors and parity mismatches used to exit 0
    (no page). The job must exit non-zero for either."""
    import argparse

    from agentdrive.core.gc import SweepResult
    from agentdrive.jobs import gc as gc_job

    class StubSweeper:
        def __init__(self, result):
            self._result = result

        async def run(self, *, dry_run=False):
            return self._result

    args = argparse.Namespace(dry_run=False, orphan_sweep=False)

    async def run_with(result):
        original = gc_job.make_sweeper
        gc_job.make_sweeper = lambda _args: StubSweeper(result)
        try:
            return await gc_job._run(args)
        finally:
            gc_job.make_sweeper = original

    assert await run_with(SweepResult()) == 0
    assert await run_with(SweepResult(errors=1)) != 0
    assert await run_with(SweepResult(parity_mismatches=1)) != 0
    assert SweepResult(errors=1).failed is True
    assert SweepResult(parity_mismatches=2).failed is True
    assert SweepResult().failed is False


async def test_sweeper_honors_its_hard_time_bound(app_with_lifespan):
    """Contract review finding 7: the sweeper must bound itself INSIDE the
    Cloud Run timeout so an overrun is stopped by the app, not by SIGKILL
    (which strands the advisory lock until connection teardown)."""
    from agentdrive.core.gc import GCSweeper

    class InstantDeadline(GCSweeper):
        HARD_TIMEOUT_S = 0  # already out of time at start

    result = await InstantDeadline(transfer_storage=_FakeTransferStorage()).run()
    assert result.capped is True


# ---------------------------------------------------------------------------
# packet-1 correction pass (PR #456 review comment), pinned as tests
# ---------------------------------------------------------------------------


async def test_drive_purge_defers_until_sessions_are_cleaned_and_retired(
    app_with_lifespan,
):
    """Correction blocker 2: hard drive purge used to CASCADE away session
    rows whose cleanup was unfinished — erasing the only scratch/final
    coordinates. A drive is purge-eligible only when NO upload_sessions rows
    remain (nonterminal sessions terminalize by deadline; cleaned rows retire
    after retention; blocked rows hold the purge for the operator)."""
    async with conn() as c:
        await _make_session(
            c, "upld_0000000000000d30", expires_in_seconds=-5,
            state="expired", failure_code="UPLOAD_EXPIRED",
            cleanup_state="blocked", cleanup_failure_class="RuntimeError",
            terminal_at=dt.datetime.now(dt.UTC),
            cleanup_next_attempt_at=dt.datetime.now(dt.UTC)
            + dt.timedelta(days=365),  # parked far out: sweeps won't touch it
        )
        from agentdrive.core import v0_content_commit as acct

        await acct.release_version_reservation(c, upload_id="upld_0000000000000d30")
        await c.execute(
            "UPDATE drives SET deleted_at = now() - interval '90 days' "
            "WHERE id = $1",
            DRIVE,
        )
    # (a) blocked cleanup: the drive must NOT purge
    result = await _sweeper(_FakeTransferStorage()).run()
    async with conn() as c:
        drives = await c.fetchval(
            "SELECT count(*) FROM drives WHERE id = $1", DRIVE
        )
        sessions = await c.fetchval(
            "SELECT count(*) FROM upload_sessions WHERE id='upld_0000000000000d30'"
        )
    assert drives == 1, "purge discarded a drive with blocked session cleanup"
    assert sessions == 1
    assert result.purged_drives == 0

    # (b) a NONTERMINAL session (future deadline) also defers the purge
    async with conn() as c:
        await c.execute(
            "UPDATE upload_sessions SET state='active', failure_code=NULL, "
            "terminal_at=NULL, cleanup_state='none', target_disclosed=true, "
            "expires_at = now() + interval '1 hour' "
            "WHERE id='upld_0000000000000d30'"
        )
    result = await _sweeper(_FakeTransferStorage()).run()
    async with conn() as c:
        drives = await c.fetchval(
            "SELECT count(*) FROM drives WHERE id = $1", DRIVE
        )
    assert drives == 1, "purge discarded a drive with a nonterminal session"

    # (c) cleaned + retired rows: NOW the drive purges
    async with conn() as c:
        await c.execute(
            "UPDATE upload_sessions SET state='expired', "
            "failure_code='UPLOAD_EXPIRED', terminal_at = now(), "
            "cleanup_state='cleaned', "
            "expires_at = now() - interval '1 hour' "
            "WHERE id='upld_0000000000000d30'"
        )
    zero_retention = _sweeper(
        _FakeTransferStorage(), TERMINAL_RETENTION=dt.timedelta(0)
    )
    first = await zero_retention.run()   # retires the cleaned session row…
    second = await zero_retention.run()  # …then the drive is eligible
    async with conn() as c:
        drives = await c.fetchval(
            "SELECT count(*) FROM drives WHERE id = $1", DRIVE
        )
    assert drives == 0, "cleaned+retired drive was never purged"
    assert first.session_rows_removed + second.session_rows_removed == 1
    assert first.purged_drives + second.purged_drives == 1


async def test_stale_cancellation_is_not_stolen_by_expiry(app_with_lifespan):
    """Correction blocker 4: a stale past-deadline `cancelling` session used
    to reconcile as `expired`. The durable cancel action owns the outcome —
    reconciliation finalizes it as `cancelled`."""
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _make_session(
            c, "upld_0000000000000d31", expires_in_seconds=-5,
            state="cancelling", target_disclosed=True,
            transition_action="cancel",
            transition_lease_id="rev_0000000000000d31",
            transition_lease_expires_at=dt.datetime.now(dt.UTC)
            - dt.timedelta(seconds=1),
        )
        async with c.transaction():
            outcome = await uploads.reconcile_upload_session(
                c, upload_id=row["id"], storage=_FakeTransferStorage(),
            )
        assert outcome == "cancelled", (
            f"expiry stole a durable cancellation (got {outcome!r})"
        )
        got = await c.fetchrow(
            "SELECT state, cleanup_state FROM upload_sessions WHERE id=$1",
            row["id"],
        )
        assert got["state"] == "cancelled"
        assert got["cleanup_state"] == "pending"
        live = await c.fetchval(
            "SELECT count(*) FROM storage_reservations WHERE released_at IS NULL"
        )
        assert live == 0


async def test_blocked_cleanup_retries_after_policy_fix(app_with_lifespan):
    """Correction blocker 5: `blocked` rows were excluded from the due query,
    so a fixed retention/hold policy never led to collection. blocked →
    policy fixed → exact-generation delete → cleaned; publication state
    untouched throughout."""
    async with conn() as c:
        await _make_session(
            c, "upld_0000000000000d32", expires_in_seconds=-5,
            state="active", target_disclosed=True,
        )
    scratch = "transfer-scratch/upld_0000000000000d32"
    storage = _FakeTransferStorage(
        {scratch: _observation(scratch, generation=91)},
        delete_errors={scratch: RuntimeError("retention policy rejected delete")},
    )
    await _sweeper(storage).run()  # expire
    await _sweeper(storage).run()  # delete blocked
    row = await _session_row("upld_0000000000000d32")
    assert row["cleanup_state"] == "blocked"
    # operator fixes the bucket policy…
    storage.delete_errors.clear()
    await _sweeper(storage).run()
    assert (scratch, 91) in storage.deleted, (
        "a blocked session was never retried after the policy fix"
    )
    row = await _session_row("upld_0000000000000d32")
    assert row["state"] == "expired"  # publication unchanged
    assert row["cleanup_state"] == "cleaned"


async def test_never_attempted_session_skips_the_finalization_watch(
    app_with_lifespan,
):
    """Round-2 review nit, folded into the correction pass: a session whose
    `provider_attempted_at IS NULL` never had a resumable URI, so no object
    can EVER appear — cleanup closes on the first sweep instead of watching
    the full late-finalization window."""
    async with conn() as c:
        await _make_session(c, "upld_0000000000000d33", expires_in_seconds=-5)
        # preparing, never attempted, overdue
    watching = _sweeper(
        _FakeTransferStorage(), LATE_FINALIZATION_WINDOW=dt.timedelta(days=8)
    )
    await watching.run()  # expires it
    await watching.run()  # cleanup: nothing can appear → cleaned immediately
    row = await _session_row("upld_0000000000000d33")
    assert row["state"] == "expired"
    assert row["cleanup_state"] == "cleaned", (
        "a never-attempted session was watched although no URI ever existed"
    )


async def test_adopting_an_aged_orphan_survives_a_concurrent_mark_sweep(
    app_with_lifespan, monkeypatch,
):
    """Adversarial round-3 I-1: adopting an AGED, UNREFERENCED CAS object at
    its old generation G1 let a mark-sweep — whose listing predated the
    commit — delete G1 with a MATCHING generation pin, destroying a freshly
    committed version. Adoption of an aged orphan must refresh the
    generation (conditional same-content re-upload), so the stale sweep's
    pin misses and the committed object survives."""
    import hashlib

    from agentdrive import storage as real_storage
    from agentdrive.core import v0_content_commit as acct

    content = b"aged orphan bytes"
    digest = hashlib.sha256(content).hexdigest()
    orphan = f"cas/{DRIVE}/{digest}"

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000d40', $1, $2, 'raced.bin', "
            "'rev_0000000000000d40')",
            DRIVE, ROOT,
        )
    # step 1: the attacker's orphan blob — landed, never committed
    first = await real_storage.put(orphan, content, "application/octet-stream")
    assert first.generation is not None

    # every object is "aged" for both the sweep and the refresh threshold
    # (negative so container-vs-host clock skew cannot flip the comparison)
    monkeypatch.setattr(real_storage, "CAS_REFRESH_AGE", dt.timedelta(hours=-1))

    # step 2: the sweep's listing happens BEFORE the adoption commit; the
    # deletes run AFTER it — the exact interleaving of the attack. We stage
    # it by wrapping list_blobs: after the stale listing is materialized,
    # the duplicate-content write adopts the orphan and commits a version
    # row, then the sweep proceeds against its stale snapshot.
    original_list_blobs = real_storage.list_blobs
    committed: dict = {}

    def interleaved_list_blobs(prefix: str):
        async def _gen():
            blobs = [b async for b in original_list_blobs(prefix)]
            if prefix == f"cas/{DRIVE}/" and not committed:
                # the adoption + commit lands inside the race window,
                # through the seam's CAS landing helper (the producer path)
                async with conn() as c:
                    write = await acct.store_cas_object(
                        c, object_name=orphan, data=content,
                        content_type="application/octet-stream",
                    )
                    async with c.transaction():
                        rsv = await acct.reserve_version_bytes(
                            c, workspace_id=WS, drive_id=DRIVE,
                            principal_id=AGENT, upload_id=None,
                            size_bytes=len(content),
                        )
                        await acct.commit_immutable_version(
                            c,
                            acct.ImmutableVersionCommit(
                                drive_id=DRIVE, workspace_id=WS,
                                artifact_id="art_0000000000000d40",
                                version_id="ver_0000000000000d40",
                                parent_version_id=None, ordinal=1,
                                checksum=f"sha256:{digest}",
                                content_type="application/octet-stream",
                                size_bytes=len(content),
                                storage_object=orphan,
                                storage_bucket=write.bucket,
                                storage_generation=write.generation,
                                actor_type="agent", actor_id=AGENT,
                                reservation_id=rsv,
                            ),
                        )
                committed["generation"] = write.generation
            for b in blobs:  # the STALE listing (old generations)
                yield b
        return _gen()

    monkeypatch.setattr(real_storage, "list_blobs", interleaved_list_blobs)
    try:
        await _sweeper(_FakeTransferStorage()).run()
    finally:
        monkeypatch.setattr(real_storage, "list_blobs", original_list_blobs)

    assert committed, "the interleaving hook never fired"
    stat = await real_storage.stat(orphan)
    assert stat is not None, (
        "mark-sweep deleted the object behind a freshly committed version"
    )
    assert stat.generation == committed["generation"], (
        "the committed row's generation no longer matches the landed object"
    )
    # the adoption must have REFRESHED the aged orphan's generation, which is
    # exactly why the stale sweep's pin missed
    assert committed["generation"] != first.generation, (
        "an aged orphan was adopted at its old generation — the stale "
        "sweep's generation pin would have matched"
    )
    async with conn() as c:
        row = await c.fetchval(
            "SELECT count(*) FROM artifact_versions "
            "WHERE id='ver_0000000000000d40'"
        )
    assert row == 1


async def test_adopting_a_referenced_duplicate_never_refreshes(app_with_lifespan):
    """The refresh applies ONLY to unreferenced orphans: a duplicate-content
    write whose CAS object is already referenced by committed rows must
    adopt the EXISTING generation unchanged — refreshing it would break the
    generation those rows persist (the original blocker-1 bug)."""
    import hashlib

    from agentdrive import storage as real_storage
    from agentdrive.core import v0_content_commit as acct

    content = b"referenced duplicate bytes"
    digest = hashlib.sha256(content).hexdigest()
    name = f"cas/{DRIVE}/{digest}"

    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000d41', $1, $2, 'dup.bin', "
            "'rev_0000000000000d41')",
            DRIVE, ROOT,
        )
        first = await real_storage.put(name, content, "application/octet-stream")
        async with c.transaction():
            rsv = await acct.reserve_version_bytes(
                c, workspace_id=WS, drive_id=DRIVE, principal_id=AGENT,
                upload_id=None, size_bytes=len(content),
            )
            await acct.commit_immutable_version(
                c,
                acct.ImmutableVersionCommit(
                    drive_id=DRIVE, workspace_id=WS,
                    artifact_id="art_0000000000000d41",
                    version_id="ver_0000000000000d41",
                    parent_version_id=None, ordinal=1,
                    checksum=f"sha256:{digest}",
                    content_type="application/octet-stream",
                    size_bytes=len(content), storage_object=name,
                    storage_bucket=first.bucket,
                    storage_generation=first.generation,
                    actor_type="agent", actor_id=AGENT, reservation_id=rsv,
                ),
            )
    # a later duplicate write — even "aged" — adopts the SAME generation
    async with conn() as c:
        import unittest.mock

        with unittest.mock.patch.object(
            real_storage, "CAS_REFRESH_AGE", dt.timedelta(hours=-1)
        ):
            second = await acct.store_cas_object(
                c, object_name=name, data=content,
                content_type="application/octet-stream",
            )
    assert second.generation == first.generation
    stat = await real_storage.stat(name)
    assert stat.generation == first.generation


async def test_cas_refresh_age_sits_inside_the_mark_sweep_window():
    """Round-4 review M-1: the aged-orphan protection depends on the STRICT
    inequality CAS_REFRESH_AGE < MARK_SWEEP_AGE — an adoption below the
    refresh threshold must commit with margin before its object becomes
    sweep-eligible. Both are tunable constants (B8 revisits tuning), so the
    relation is pinned here on the PRODUCTION classes."""
    from agentdrive import storage as real_storage
    from agentdrive.core.gc import GCSweeper

    assert real_storage.CAS_REFRESH_AGE < GCSweeper.MARK_SWEEP_AGE, (
        "CAS_REFRESH_AGE must stay strictly below MARK_SWEEP_AGE or "
        "unrefreshed adoptions become sweep-eligible (review I-1 reopens)"
    )
    # keep a real margin, not a hair's width
    assert dt.timedelta(minutes=30) <= (
        GCSweeper.MARK_SWEEP_AGE - real_storage.CAS_REFRESH_AGE
    )


async def test_cas_bytes_land_only_through_the_seam_helper():
    """Round-4 review nit: like the INSERT guard, the byte-landing seam is
    structural — `storage.put(` may be called only by
    `v0_content_commit.store_cas_object`, so no producer can bypass the
    reference-aware generation-refresh rule."""
    import re

    src = REPO_ROOT / "src"
    pattern = re.compile(r"storage\.put\(")
    offenders = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if pattern.search(path.read_text())
        and path.name != "v0_content_commit.py"
    ]
    assert offenders == [], (
        f"CAS bytes must land through store_cas_object only; direct "
        f"storage.put calls in {offenders}"
    )
