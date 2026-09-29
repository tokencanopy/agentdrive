"""Upload-session durable state: schema shape + publication/cleanup machine.

Two layers, per the B3 direct-transfer design (§6 of the governing spec,
`docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md`
in the TokenCanopy repo):

  * Schema constraints — migration 0049's `upload_sessions` /
    `storage_reservations` / `workspace_storage` tables and the
    `artifact_versions.storage_bucket` / `storage_generation` expansion,
    proven to BITE against a real database (same philosophy as
    `test_fresh_db_schema_apply._assert_invariants`).
  * The explicit state machine in `core/v0_uploads.py` — strict fail-closed
    decoding, the begin saga's crash rules, transition fences/leases,
    exact-once reservation release, and complete/cancel serialization.

Everything here drives the transactional core directly (no mounted route —
B3 packet 1 lands no HTTP surface). Provider work is behind an injected
protocol; these tests use fakes. All fixtures are synthetic.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import asyncpg
import pytest
import pytest_asyncio

from agentdrive.db import conn

pytestmark = pytest.mark.asyncio

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

WS = "tcws_0000000000000001"
AGENT = "tcagt_0000000000000001"

DRIVE = "drv_00000000000000e1"
ROOT = "fld_0000000000000e01"
ART = "art_0000000000000e01"
VER = "ver_0000000000000e01"

CRC = "yZRlqg=="  # canonical padded 4-byte base64 (synthetic)


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


async def _seed_drive(c) -> None:
    # Idempotent: several sessions in one test share the fixture drive.
    await c.execute(
        "INSERT INTO drives (id, workspace_id, name, revision) VALUES "
        "($1, $2, 'transfer-fixture', 'rev_00000000000000e1') "
        "ON CONFLICT (id) DO NOTHING",
        DRIVE, WS,
    )
    await c.execute(
        "INSERT INTO folders (id, drive_id, parent_id, name, revision) VALUES "
        "($1, $2, NULL, NULL, 'rev_00000000000000e2') "
        "ON CONFLICT (id) DO NOTHING",
        ROOT, DRIVE,
    )
    await c.execute(
        "UPDATE drives SET root_folder_id = $2 WHERE id = $1", DRIVE, ROOT,
    )


def _session_row(**over) -> dict:
    row = {
        "id": "upld_00000000000000e1",
        "workspace_id": WS,
        "drive_id": DRIVE,
        "principal_type": "agent",
        "principal_id": AGENT,
        "target_kind": "artifact",
        "parent_folder_id": ROOT,
        "artifact_name": "notes.txt",
        "artifact_id": None,
        "expected_artifact_revision": None,
        "declared_size_bytes": 11,
        "declared_media_type": "text/plain",
        "declared_crc32c": CRC,
        "adoption_marker": "mark_00000000000000e1",
        "scratch_object": "transfer-scratch/upld_00000000000000e1",
        "final_object": "transfer-immutable/ver_00000000000000e1",
        "expires_at": dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        "state": "preparing",
    }
    row.update(over)
    return row


async def _insert_session(c, **over):
    row = _session_row(**over)
    cols = ", ".join(row)
    params = ", ".join(f"${i + 1}" for i in range(len(row)))
    return await c.execute(
        f"INSERT INTO upload_sessions ({cols}) VALUES ({params})",
        *row.values(),
    )


async def _rejects(c, label: str, coro) -> None:
    try:
        async with c.transaction():
            await coro
    except asyncpg.PostgresError:
        return
    raise AssertionError(f"schema permits {label} — invariant is not enforced")


# ---------------------------------------------------------------------------
# migration artefacts exist
# ---------------------------------------------------------------------------


async def test_migration_0049_exists_and_is_folded_into_baseline():
    migration = REPO_ROOT / "migrations" / "0049_direct_transfer_sessions.sql"
    assert migration.is_file(), "migration 0049_direct_transfer_sessions.sql is missing"
    baseline = (REPO_ROOT / "schema.sql").read_text()
    for token in (
        "upload_sessions",
        "storage_reservations",
        "workspace_storage",
        "storage_generation",
        "storage_bucket",
    ):
        assert token in baseline, f"schema.sql baseline is missing {token!r}"


async def test_artifact_versions_gained_nullable_generation_columns(app_with_lifespan):
    async with conn() as c:
        rows = await c.fetch(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'artifact_versions' "
            "AND column_name IN ('storage_bucket', 'storage_generation')"
        )
    got = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
    assert got.get("storage_bucket") == ("text", "YES")
    assert got.get("storage_generation") == ("bigint", "YES")


# ---------------------------------------------------------------------------
# upload_sessions constraints bite
# ---------------------------------------------------------------------------


async def test_session_row_with_valid_shape_inserts(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        await _insert_session(c)
        state = await c.fetchval(
            "SELECT state FROM upload_sessions WHERE id = $1",
            "upld_00000000000000e1",
        )
        assert state == "preparing"
        cleanup = await c.fetchval(
            "SELECT cleanup_state FROM upload_sessions WHERE id = $1",
            "upld_00000000000000e1",
        )
        assert cleanup == "none"


async def test_unknown_publication_state_is_rejected(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        await _rejects(
            c, "an unknown publication state",
            _insert_session(c, state="uploading"),
        )
        await _rejects(
            c, "a legacy v0_uploads state name",
            _insert_session(c, state="unknown"),
        )


async def test_unknown_cleanup_state_is_rejected(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        await _insert_session(c)
        await _rejects(
            c, "an unknown cleanup state",
            c.execute(
                "UPDATE upload_sessions SET cleanup_state = 'sweeping' "
                "WHERE id = $1",
                "upld_00000000000000e1",
            ),
        )


async def test_negative_counts_are_rejected(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        await _rejects(
            c, "a negative declared size",
            _insert_session(c, declared_size_bytes=-1),
        )
        await _rejects(
            c, "a zero session revision",
            c.execute(
                "INSERT INTO upload_sessions (id, workspace_id, drive_id, "
                "principal_type, principal_id, target_kind, parent_folder_id, "
                "artifact_name, declared_size_bytes, declared_media_type, "
                "declared_crc32c, adoption_marker, scratch_object, final_object, "
                "expires_at, session_revision) "
                "VALUES ('upld_00000000000000e2', $1, $2, 'agent', $3, "
                "'artifact', $4, 'x.txt', 1, 'text/plain', $5, 'm', 's', 'f', "
                "now() + interval '1 hour', 0)",
                WS, DRIVE, AGENT, ROOT, CRC,
            ),
        )


async def test_invalid_target_combinations_are_unrepresentable(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        # artifact target must NOT carry version-target fields.
        await _rejects(
            c, "an artifact target carrying an artifact_id",
            _insert_session(c, artifact_id=ART),
        )
        await _rejects(
            c, "an artifact target with no parent folder",
            _insert_session(c, parent_folder_id=None),
        )
        await _rejects(
            c, "an artifact target with no name",
            _insert_session(c, artifact_name=None),
        )
        # version target must carry artifact id + captured revision, and no
        # namespace intent.
        await _rejects(
            c, "a version target with no expected artifact revision",
            _insert_session(
                c, target_kind="version", artifact_id=ART,
                parent_folder_id=None, artifact_name=None,
                expected_artifact_revision=None,
            ),
        )
        await _rejects(
            c, "a version target carrying a parent folder",
            _insert_session(
                c, target_kind="version", artifact_id=ART,
                expected_artifact_revision="rev_00000000000000e9",
                artifact_name=None,
            ),
        )
        await _rejects(
            c, "an unknown target kind",
            _insert_session(c, target_kind="folder"),
        )


async def test_completed_without_result_coordinates_is_rejected(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        await _rejects(
            c, "a completed session without an immutable result",
            _insert_session(
                c, state="completed",
            ),
        )
        # ... and a non-terminal session may not carry a terminal timestamp.
        await _rejects(
            c, "an active session with terminal_at",
            c.execute(
                "INSERT INTO upload_sessions (id, workspace_id, drive_id, "
                "principal_type, principal_id, target_kind, parent_folder_id, "
                "artifact_name, declared_size_bytes, declared_media_type, "
                "declared_crc32c, adoption_marker, scratch_object, final_object, "
                "expires_at, state, terminal_at) "
                "VALUES ('upld_00000000000000e3', $1, $2, 'agent', $3, "
                "'artifact', $4, 'y.txt', 1, 'text/plain', $5, 'm', 's', 'f', "
                "now() + interval '1 hour', 'active', now())",
                WS, DRIVE, AGENT, ROOT, CRC,
            ),
        )


async def test_lease_fields_are_all_or_none(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        await _insert_session(c)
        await _rejects(
            c, "a transition action without a lease",
            c.execute(
                "UPDATE upload_sessions SET transition_action = 'complete' "
                "WHERE id = $1",
                "upld_00000000000000e1",
            ),
        )


async def test_non_canonical_crc32c_is_rejected(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        for bad in ("yZRlqg", "yZRlqg==extra", "yZRl-g==", ""):
            await _rejects(
                c, f"a non-canonical declared crc32c {bad!r}",
                _insert_session(c, declared_crc32c=bad),
            )


# ---------------------------------------------------------------------------
# reservation ledger + workspace accounting row constraints
# ---------------------------------------------------------------------------


async def test_reservation_release_shape_and_one_live_per_upload(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        await _insert_session(c)
        await c.execute(
            "INSERT INTO storage_reservations "
            "(id, workspace_id, drive_id, principal_id, upload_id, size_bytes) "
            "VALUES ('rsv_00000000000000e1', $1, $2, $3, "
            "'upld_00000000000000e1', 11)",
            WS, DRIVE, AGENT,
        )
        # a second LIVE reservation for the same upload is unrepresentable
        await _rejects(
            c, "two live reservations for one upload session",
            c.execute(
                "INSERT INTO storage_reservations "
                "(id, workspace_id, drive_id, principal_id, upload_id, size_bytes) "
                "VALUES ('rsv_00000000000000e2', $1, $2, $3, "
                "'upld_00000000000000e1', 11)",
                WS, DRIVE, AGENT,
            ),
        )
        # released_at and release_kind travel together
        await _rejects(
            c, "a released_at without a release kind",
            c.execute(
                "UPDATE storage_reservations SET released_at = now() "
                "WHERE id = 'rsv_00000000000000e1'",
            ),
        )
        await _rejects(
            c, "a negative reservation size",
            c.execute(
                "INSERT INTO storage_reservations "
                "(id, workspace_id, drive_id, principal_id, size_bytes) "
                "VALUES ('rsv_00000000000000e3', $1, $2, $3, -5)",
                WS, DRIVE, AGENT,
            ),
        )


async def test_workspace_storage_counters_reject_negatives(app_with_lifespan):
    async with conn() as c:
        await c.execute(
            "INSERT INTO workspace_storage (workspace_id) VALUES ($1)", WS,
        )
        await _rejects(
            c, "a negative committed counter",
            c.execute(
                "UPDATE workspace_storage SET committed_bytes = -1 "
                "WHERE workspace_id = $1", WS,
            ),
        )
        await _rejects(
            c, "a negative reserved counter",
            c.execute(
                "UPDATE workspace_storage SET reserved_bytes = -1 "
                "WHERE workspace_id = $1", WS,
            ),
        )


# ---------------------------------------------------------------------------
# artifact_versions generation immutability
# ---------------------------------------------------------------------------


async def test_generation_permits_only_null_to_value_once(app_with_lifespan):
    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ($1, $2, $3, 'gen.txt', 'rev_00000000000000e3')",
            ART, DRIVE, ROOT,
        )
        await c.execute(
            "INSERT INTO artifact_versions (id, artifact_id, checksum, "
            "content_type, size_bytes, storage_object, actor_type, actor_id, "
            "ordinal) VALUES ($1, $2, 'sha256:ab', 'text/plain', 3, "
            "'cas/x/ab', 'agent', $3, 1)",
            VER, ART, AGENT,
        )
        # NULL -> observed value: the one permitted reconciliation transition.
        await c.execute(
            "UPDATE artifact_versions SET storage_bucket = 'bucket-demo', "
            "storage_generation = 42 WHERE id = $1",
            VER,
        )
        got = await c.fetchrow(
            "SELECT storage_bucket, storage_generation "
            "FROM artifact_versions WHERE id = $1", VER,
        )
        assert dict(got) == {"storage_bucket": "bucket-demo", "storage_generation": 42}
        # value -> different value: history rewriting, rejected.
        await _rejects(
            c, "repointing a persisted object generation",
            c.execute(
                "UPDATE artifact_versions SET storage_generation = 43 "
                "WHERE id = $1", VER,
            ),
        )
        # value -> NULL: also rejected (a resolved row never un-resolves).
        await _rejects(
            c, "clearing a persisted object generation",
            c.execute(
                "UPDATE artifact_versions SET storage_generation = NULL "
                "WHERE id = $1", VER,
            ),
        )
        await _rejects(
            c, "repointing a persisted storage bucket",
            c.execute(
                "UPDATE artifact_versions SET storage_bucket = 'other-demo' "
                "WHERE id = $1", VER,
            ),
        )


# ---------------------------------------------------------------------------
# strict fail-closed state decoding
# ---------------------------------------------------------------------------


async def test_state_decoder_fails_closed(app_with_lifespan):
    from agentdrive.core import v0_uploads

    for known in (
        "preparing", "active", "completing", "cancelling",
        "completed", "cancelled", "expired", "rejected",
    ):
        assert v0_uploads.decode_publication_state(known) == known
    for known in ("none", "pending", "quarantined", "deleting", "cleaned", "blocked"):
        assert v0_uploads.decode_cleanup_state(known) == known
    for bad in ("unknown", "ACTIVE", "", "done", None):
        with pytest.raises(v0_uploads.UnknownUploadStateError):
            v0_uploads.decode_publication_state(bad)  # type: ignore[arg-type]
        with pytest.raises(v0_uploads.UnknownUploadStateError):
            v0_uploads.decode_cleanup_state(bad)  # type: ignore[arg-type]
    # Terminal classification is part of the decoder contract: an
    # unrecognized value must never read as terminal success.
    assert v0_uploads.is_terminal("completed")
    assert v0_uploads.is_terminal("expired")
    assert not v0_uploads.is_terminal("completing")
    with pytest.raises(v0_uploads.UnknownUploadStateError):
        v0_uploads.is_terminal("finished")


# ---------------------------------------------------------------------------
# state machine + recovery (core/v0_uploads.py, provider behind a fake)
# ---------------------------------------------------------------------------


def _fake_observation(**over):
    from agentdrive.core.v0_uploads import ObjectObservation

    fields = {
        "object_name": "transfer-immutable/ver_00000000000000e1",
        "generation": 4242,
        "size": 11,
        "crc32c": CRC,
        "content_type": "text/plain",
        "adoption_marker": "mark_00000000000000e1",
    }
    fields.update(over)
    return ObjectObservation(**fields)


class _FakeTransferStorage:
    """Injected provider seam: canned stat responses, recorded deletes."""

    def __init__(self, observations=None):
        self.observations = observations or {}
        self.deleted: list[tuple[str, int]] = []

    async def stat_object(self, object_name):
        return self.observations.get(object_name)

    async def delete_generation(self, object_name, generation):
        self.deleted.append((object_name, generation))


async def _begin_session(c, *, upload_id="upld_00000000000000f1", expires_in_s=3600, size=11):
    """The begin saga's first transaction: preparing row + linked reservation."""
    from agentdrive.core import v0_uploads as uploads

    await _seed_drive(c)
    async with c.transaction():
        row = await uploads.create_session(
            c,
            upload_id=upload_id,
            workspace_id=WS,
            drive_id=DRIVE,
            principal_type="agent",
            principal_id=AGENT,
            target_kind="artifact",
            parent_folder_id=ROOT,
            artifact_name="notes.txt",
            artifact_id=None,
            expected_artifact_revision=None,
            declared_size_bytes=size,
            declared_media_type="text/plain",
            declared_crc32c=CRC,
            adoption_marker="mark_00000000000000e1",
            scratch_object="transfer-scratch/" + upload_id,
            final_object="transfer-immutable/ver_00000000000000e1",
            expires_in_seconds=expires_in_s,
        )
    return row


async def _reserved(c) -> int:
    row = await c.fetchrow(
        "SELECT reserved_bytes FROM workspace_storage WHERE workspace_id=$1", WS
    )
    return int(row["reserved_bytes"]) if row else 0


async def test_begin_saga_happy_path_to_completed(app_with_lifespan):
    from agentdrive.core import v0_content_commit as acct
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _begin_session(c)
        assert row["state"] == "preparing"
        assert row["target_disclosed"] is False
        assert await _reserved(c) == 11
        etag_0 = uploads.session_etag(row)

        # one initiation lease; provider_attempted_at is durable BEFORE the
        # (faked) outbound call
        leased = await uploads.acquire_initiation_lease(
            c, upload_id=row["id"], lease_seconds=60,
        )
        assert leased is not None
        assert leased["provider_attempted_at"] is not None

        active = await uploads.activate_session(c, upload_id=row["id"])
        assert active["state"] == "active"
        assert active["target_disclosed"] is True
        assert uploads.session_etag(active) != etag_0

        fenced = await uploads.acquire_transition(
            c, upload_id=row["id"], action="complete", lease_seconds=60,
        )
        assert fenced["state"] == "completing"
        lease = fenced["transition_lease_id"]
        await uploads.record_scratch_observation(
            c, upload_id=row["id"], lease_id=lease,
            generation=41, size=11, crc32c=CRC,
        )
        await uploads.record_adopted_observation(
            c, upload_id=row["id"], lease_id=lease, generation=4242, size=11,
            crc32c=CRC, content_type="text/plain",
        )

        # publication + immutable commit in ONE transaction
        async with c.transaction():
            await c.execute(
                "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
                "VALUES ($1, $2, $3, 'notes.txt', 'rev_00000000000000f1')",
                ART, DRIVE, ROOT,
            )
            reservation_id = await c.fetchval(
                "SELECT id FROM storage_reservations WHERE upload_id = $1 "
                "AND released_at IS NULL",
                row["id"],
            )
            await acct.commit_immutable_version(
                c,
                acct.ImmutableVersionCommit(
                    drive_id=DRIVE, workspace_id=WS, artifact_id=ART,
                    version_id=VER, parent_version_id=None, ordinal=1,
                    checksum=f"crc32c:{CRC}", content_type="text/plain",
                    size_bytes=11,
                    storage_object="transfer-immutable/ver_00000000000000e1",
                    storage_bucket="transfer-bucket-demo",
                    storage_generation=4242,
                    actor_type="agent", actor_id=AGENT,
                    reservation_id=reservation_id,
                ),
            )
            done = await uploads.complete_publication(
                c, upload_id=row["id"], lease_id=lease,
                result_artifact_id=ART, result_version_id=VER,
                result_revision="rev_00000000000000f2",
            )
        assert done["state"] == "completed"
        assert done["result_version_id"] == VER
        # §6 completion step 5: scratch deletion is SCHEDULED by the commit —
        # a completed session enters cleanup so GC can collect its scratch
        # object and later retire the row (contract review finding 2).
        assert done["cleanup_state"] == "pending"
        assert done["cleanup_next_attempt_at"] is not None
        assert await _reserved(c) == 0
        # converted exactly once: a later release attempt is a detected no-op
        assert await acct.release_version_reservation(c, upload_id=row["id"]) is False
        # terminal states accept no further fence
        with pytest.raises(uploads.InvalidUploadTransitionError):
            await uploads.acquire_transition(
                c, upload_id=row["id"], action="cancel", lease_seconds=60,
            )


async def test_initiation_never_runs_twice(app_with_lifespan):
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _begin_session(c, upload_id="upld_00000000000000f2")
        first = await uploads.acquire_initiation_lease(
            c, upload_id=row["id"], lease_seconds=60,
        )
        assert first is not None
        # a second initiation attempt — same or another worker — finds
        # provider_attempted_at set and gets nothing back
        second = await uploads.acquire_initiation_lease(
            c, upload_id=row["id"], lease_seconds=60,
        )
        assert second is None


async def test_stale_preparing_recovery(app_with_lifespan):
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        # crash BEFORE the outbound marker: row stays preparing/retryable
        row = await _begin_session(c, upload_id="upld_00000000000000f3")
        outcome = await uploads.recover_stale_preparing(c, upload_id=row["id"])
        assert outcome == "retryable"
        state = await c.fetchval(
            "SELECT state FROM upload_sessions WHERE id=$1", row["id"]
        )
        assert state == "preparing"
        assert await _reserved(c) == 11

        # crash AFTER the outbound marker with an expired lease: uncertain —
        # terminal rejected, reservation released exactly once, cleanup pending
        await uploads.acquire_initiation_lease(
            c, upload_id=row["id"], lease_seconds=0,
        )
        outcome = await uploads.recover_stale_preparing(c, upload_id=row["id"])
        assert outcome == "rejected"
        got = await c.fetchrow(
            "SELECT state, failure_code, cleanup_state FROM upload_sessions "
            "WHERE id=$1",
            row["id"],
        )
        assert dict(got) == {
            "state": "rejected",
            "failure_code": "UPLOAD_INITIATION_UNCERTAIN",
            "cleanup_state": "pending",
        }
        assert await _reserved(c) == 0
        # recovery is idempotent at the reservation: run again, nothing double-releases
        outcome = await uploads.recover_stale_preparing(c, upload_id=row["id"])
        assert outcome == "terminal"
        assert await _reserved(c) == 0


async def test_transition_fence_busy_and_same_action_takeover(app_with_lifespan):
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _begin_session(c, upload_id="upld_00000000000000f4")
        await uploads.acquire_initiation_lease(c, upload_id=row["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row["id"])

        fenced = await uploads.acquire_transition(
            c, upload_id=row["id"], action="complete", lease_seconds=60,
        )
        assert fenced["state"] == "completing"
        # LIVE lease: both a competing complete and a cancel are busy
        with pytest.raises(uploads.UploadBusyError):
            await uploads.acquire_transition(
                c, upload_id=row["id"], action="complete", lease_seconds=60,
            )
        with pytest.raises(uploads.UploadBusyError):
            await uploads.acquire_transition(
                c, upload_id=row["id"], action="cancel", lease_seconds=60,
            )

        # stale lease: the SAME action attaches (new lease, retained
        # observations); a different action still cannot steal the fence
        await c.execute(
            "UPDATE upload_sessions SET transition_lease_expires_at = "
            "now() - interval '1 second' WHERE id = $1",
            row["id"],
        )
        with pytest.raises(uploads.UploadBusyError):
            await uploads.acquire_transition(
                c, upload_id=row["id"], action="cancel", lease_seconds=60,
            )
        taken = await uploads.acquire_transition(
            c, upload_id=row["id"], action="complete", lease_seconds=60,
        )
        assert taken["state"] == "completing"
        assert taken["transition_lease_expires_at"] is not None


async def test_incomplete_scratch_returns_to_active(app_with_lifespan):
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _begin_session(c, upload_id="upld_00000000000000f5")
        await uploads.acquire_initiation_lease(c, upload_id=row["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row["id"])
        fenced = await uploads.acquire_transition(
            c, upload_id=row["id"], action="complete", lease_seconds=60,
        )
        back = await uploads.return_to_active(
            c, upload_id=row["id"], lease_id=fenced["transition_lease_id"],
        )
        assert back["state"] == "active"
        assert back["transition_action"] is None
        # reservation and deadline unchanged; a later completion may run
        assert await _reserved(c) == 11
        again = await uploads.acquire_transition(
            c, upload_id=row["id"], action="complete", lease_seconds=60,
        )
        assert again["state"] == "completing"


async def test_deterministic_rejection_releases_exactly_once(app_with_lifespan):
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _begin_session(c, upload_id="upld_00000000000000f6")
        await uploads.acquire_initiation_lease(c, upload_id=row["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row["id"])
        await uploads.acquire_transition(
            c, upload_id=row["id"], action="complete", lease_seconds=60,
        )
        rejected = await uploads.reject_session(
            c, upload_id=row["id"], failure_code="CHECKSUM_MISMATCH",
        )
        assert rejected["state"] == "rejected"
        assert rejected["failure_code"] == "CHECKSUM_MISMATCH"
        assert rejected["cleanup_state"] == "pending"
        assert await _reserved(c) == 0
        # publication is terminal; cleanup retries never re-release
        with pytest.raises(uploads.InvalidUploadTransitionError):
            await uploads.reject_session(
                c, upload_id=row["id"], failure_code="CHECKSUM_MISMATCH",
            )
        assert await _reserved(c) == 0


async def test_cancel_serializes_against_complete(app_with_lifespan):
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _begin_session(c, upload_id="upld_00000000000000f7")
        await uploads.acquire_initiation_lease(c, upload_id=row["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row["id"])

        fenced = await uploads.acquire_transition(
            c, upload_id=row["id"], action="cancel", lease_seconds=60,
        )
        assert fenced["state"] == "cancelling"
        # complete cannot slip in behind the cancel fence
        with pytest.raises(uploads.UploadBusyError):
            await uploads.acquire_transition(
                c, upload_id=row["id"], action="complete", lease_seconds=60,
            )
        done = await uploads.finalize_cancel(c, upload_id=row["id"])
        assert done["state"] == "cancelled"
        assert done["cleanup_state"] == "pending"
        assert await _reserved(c) == 0
        # cancelled is terminal: no revival, no second release
        with pytest.raises(uploads.InvalidUploadTransitionError):
            await uploads.acquire_transition(
                c, upload_id=row["id"], action="complete", lease_seconds=60,
            )


async def test_deadline_fences_win_before_and_inside_publication(app_with_lifespan):
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        # fence 1: taking the completion fence past the deadline expires
        row = await _begin_session(c, upload_id="upld_00000000000000f8", expires_in_s=-1)
        await uploads.acquire_initiation_lease(c, upload_id=row["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row["id"])
        with pytest.raises(uploads.UploadExpiredError):
            await uploads.acquire_transition(
                c, upload_id=row["id"], action="complete", lease_seconds=60,
            )
        got = await c.fetchrow(
            "SELECT state, failure_code, cleanup_state FROM upload_sessions "
            "WHERE id=$1",
            row["id"],
        )
        assert dict(got) == {
            "state": "expired",
            "failure_code": "UPLOAD_EXPIRED",
            "cleanup_state": "pending",
        }
        assert await _reserved(c) == 0

        # fence 2: a deadline that elapses DURING completing blocks the
        # final publication transition — a late object is never published
        row2 = await _begin_session(c, upload_id="upld_00000000000000f9")
        await uploads.acquire_initiation_lease(c, upload_id=row2["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row2["id"])
        fenced2 = await uploads.acquire_transition(
            c, upload_id=row2["id"], action="complete", lease_seconds=60,
        )
        lease2 = fenced2["transition_lease_id"]
        await uploads.record_scratch_observation(
            c, upload_id=row2["id"], lease_id=lease2,
            generation=6, size=11, crc32c=CRC,
        )
        await uploads.record_adopted_observation(
            c, upload_id=row2["id"], lease_id=lease2, generation=7, size=11,
            crc32c=CRC, content_type="text/plain",
        )
        await c.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            row2["id"],
        )
        with pytest.raises(uploads.UploadExpiredError):
            await uploads.complete_publication(
                c, upload_id=row2["id"], lease_id=lease2,
                result_artifact_id=ART, result_version_id=VER,
                result_revision="rev_00000000000000f9",
            )
        state = await c.fetchval(
            "SELECT state FROM upload_sessions WHERE id=$1", row2["id"]
        )
        assert state == "expired"
        cleanup = await c.fetchval(
            "SELECT cleanup_state FROM upload_sessions WHERE id=$1", row2["id"]
        )
        assert cleanup == "quarantined"  # an object may exist; GC collects it
        assert await _reserved(c) == 0


async def test_cleanup_progress_never_changes_terminal_publication(app_with_lifespan):
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _begin_session(c, upload_id="upld_0000000000000f10", expires_in_s=-1)
        await uploads.expire_session(c, upload_id=row["id"])
        for cleanup in ("quarantined", "deleting", "cleaned"):
            await uploads.set_cleanup_state(c, upload_id=row["id"], cleanup_state=cleanup)
            got = await c.fetchrow(
                "SELECT state, cleanup_state FROM upload_sessions WHERE id=$1",
                row["id"],
            )
            assert got["state"] == "expired"
            assert got["cleanup_state"] == cleanup
        # blocked is representable and operator-visible, still not publication
        await uploads.set_cleanup_state(
            c, upload_id=row["id"], cleanup_state="blocked",
            failure_class="retention_policy",
        )
        got = await c.fetchrow(
            "SELECT state, cleanup_state, cleanup_failure_class "
            "FROM upload_sessions WHERE id=$1",
            row["id"],
        )
        assert dict(got) == {
            "state": "expired",
            "cleanup_state": "blocked",
            "cleanup_failure_class": "retention_policy",
        }


async def test_reconcile_ambiguous_adoption(app_with_lifespan):
    from agentdrive.core import v0_uploads as uploads

    async def _stale_completing(c, upload_id):
        row = await _begin_session(c, upload_id=upload_id)
        await uploads.acquire_initiation_lease(c, upload_id=row["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row["id"])
        fenced = await uploads.acquire_transition(
            c, upload_id=row["id"], action="complete", lease_seconds=60,
        )
        await c.execute(
            "UPDATE upload_sessions SET transition_lease_expires_at = "
            "now() - interval '1 second' WHERE id = $1",
            upload_id,
        )
        return fenced

    async with conn() as c:
        # (a) destination observation matches every server-owned invariant
        # (marker, source fingerprint, type, size, CRC): adopt it — persist
        # the full observed identity and resume the commit
        row = await _stale_completing(c, "upld_0000000000000f11")
        lease = row["transition_lease_id"]
        await uploads.record_scratch_observation(
            c, upload_id=row["id"], lease_id=lease,
            generation=41, size=11, crc32c=CRC,
        )
        fingerprint = uploads.adoption_fingerprint(
            scratch_object=row["scratch_object"],
            scratch_generation=41,
            upload_id=row["id"],
        )
        storage = _FakeTransferStorage(
            {
                "transfer-immutable/ver_00000000000000e1": _fake_observation(
                    source_fingerprint=fingerprint
                )
            }
        )
        outcome = await uploads.reconcile_upload_session(
            c, upload_id=row["id"], storage=storage,
        )
        assert outcome == "resume_commit"
        adopted = await c.fetchrow(
            "SELECT adopted_generation, adopted_size FROM upload_sessions "
            "WHERE id=$1", row["id"]
        )
        assert dict(adopted) == {"adopted_generation": 4242, "adopted_size": 11}
        await c.execute("TRUNCATE upload_sessions, storage_reservations CASCADE")
        await c.execute("DELETE FROM workspace_storage")
        await c.execute("TRUNCATE drives RESTART IDENTITY CASCADE")

        # (b) identity/content mismatch is NEVER adopted: terminal rejected +
        # quarantined, and the foreign generation is not deleted here
        row = await _stale_completing(c, "upld_0000000000000f12")
        storage = _FakeTransferStorage(
            {
                "transfer-immutable/ver_00000000000000e1": _fake_observation(
                    adoption_marker="mark_of_someone_else"
                )
            }
        )
        outcome = await uploads.reconcile_upload_session(
            c, upload_id=row["id"], storage=storage,
        )
        assert outcome == "rejected"
        got = await c.fetchrow(
            "SELECT state, failure_code, cleanup_state, adopted_generation "
            "FROM upload_sessions WHERE id=$1",
            row["id"],
        )
        assert dict(got) == {
            "state": "rejected",
            "failure_code": "OBJECT_METADATA_MISMATCH",
            "cleanup_state": "quarantined",
            "adopted_generation": None,
        }
        assert storage.deleted == []  # never deletes what it has not owned
        await c.execute("TRUNCATE upload_sessions, storage_reservations CASCADE")
        await c.execute("DELETE FROM workspace_storage")
        await c.execute("TRUNCATE drives RESTART IDENTITY CASCADE")

        # (c) destination absent: bounded retry of the same rewrite phase —
        # no new key, no rejection, publication still completing
        row = await _stale_completing(c, "upld_0000000000000f13")
        storage = _FakeTransferStorage({})
        outcome = await uploads.reconcile_upload_session(
            c, upload_id=row["id"], storage=storage,
        )
        assert outcome == "retry_rewrite"
        got = await c.fetchrow(
            "SELECT state, adopted_generation FROM upload_sessions WHERE id=$1",
            row["id"],
        )
        assert dict(got) == {"state": "completing", "adopted_generation": None}


async def test_deadline_fence_on_cancelling_session_stays_with_the_cancel(
    app_with_lifespan,
):
    """Security review M3: a past-deadline complete against a `cancelling`
    session must NOT phantom-expire (the old fence attempt matched no state
    and still raised). The durable cancel action owns the session; complete
    is busy, and reconciliation later finalizes the cancel."""
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _begin_session(c, upload_id="upld_0000000000000f14")
        await uploads.acquire_initiation_lease(c, upload_id=row["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row["id"])
        await uploads.acquire_transition(
            c, upload_id=row["id"], action="cancel", lease_seconds=60,
        )
        await c.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 second', "
            "transition_lease_expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            row["id"],
        )
        with pytest.raises(uploads.UploadBusyError):
            await uploads.acquire_transition(
                c, upload_id=row["id"], action="complete", lease_seconds=60,
            )
        got = await c.fetchrow(
            "SELECT state FROM upload_sessions WHERE id=$1", row["id"]
        )
        assert got["state"] == "cancelling"
        assert await _reserved(c) == 11  # untouched until the cancel finalizes
        done = await uploads.finalize_cancel(c, upload_id=row["id"])
        assert done["state"] == "cancelled"
        assert await _reserved(c) == 0


# ---------------------------------------------------------------------------
# packet-1 correction pass (PR #456 review comment)
# ---------------------------------------------------------------------------


async def test_version_coordinates_are_all_or_none(app_with_lifespan):
    """Correction blocker 6 (schema half): a generation without a bucket (or
    the reverse, or an empty bucket) is unrepresentable, and readiness
    treats EITHER missing coordinate as unresolved."""
    async with conn() as c:
        await _seed_drive(c)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000e02', $1, $2, 'pair.txt', "
            "'rev_0000000000000e12') ON CONFLICT (id) DO NOTHING",
            DRIVE, ROOT,
        )

        def _insert_version_pair(version_id, bucket, generation, ordinal):
            return c.execute(
                "INSERT INTO artifact_versions (id, artifact_id, checksum, "
                "content_type, size_bytes, storage_object, storage_bucket, "
                "storage_generation, actor_type, actor_id, ordinal) VALUES "
                "($1, 'art_0000000000000e02', 'sha256:pair', 'text/plain', 3, "
                "'cas/pair', $2, $3, 'agent', $4, $5)",
                version_id, bucket, generation, AGENT, ordinal,
            )

        await _rejects(
            c, "a generation without a bucket",
            _insert_version_pair("ver_0000000000000e02", None, 7, 1),
        )
        await _rejects(
            c, "a bucket without a generation",
            _insert_version_pair("ver_0000000000000e03", "bucket-demo", None, 2),
        )
        await _rejects(
            c, "an empty storage bucket",
            _insert_version_pair("ver_0000000000000e04", "", 7, 3),
        )
        # the valid complete pair still inserts
        await _insert_version_pair("ver_0000000000000e05", "bucket-demo", 7, 4)


async def test_adopted_observation_is_all_or_none_and_declared_equal(
    app_with_lifespan,
):
    """Correction blocker 7: the durable adoption proof is the FULL observed
    identity — generation, size, CRC32C, content type — not a bare
    generation. The schema rejects a partial proof; the recorder refuses an
    observation that disagrees with the session's declaration."""
    from agentdrive.core import v0_uploads as uploads

    async with conn() as c:
        row = await _begin_session(c, upload_id="upld_0000000000000f15")
        await uploads.acquire_initiation_lease(c, upload_id=row["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row["id"])
        fenced = await uploads.acquire_transition(
            c, upload_id=row["id"], action="complete", lease_seconds=60,
        )
        # (a) schema: a bare adopted_generation with no identity is rejected
        await _rejects(
            c, "an adopted generation without its full observed identity",
            c.execute(
                "UPDATE upload_sessions SET adopted_generation = 4242 "
                "WHERE id = $1",
                row["id"],
            ),
        )
        # (b) recorder: an observation disagreeing with the declaration is
        # refused — never persisted, session unchanged
        with pytest.raises(uploads.InvalidUploadTransitionError):
            await uploads.record_adopted_observation(
                c, upload_id=row["id"],
                lease_id=fenced["transition_lease_id"], generation=4242,
                size=999,  # != declared 11
                crc32c=CRC, content_type="text/plain",
            )
        got = await c.fetchrow(
            "SELECT adopted_generation, adopted_size FROM upload_sessions "
            "WHERE id = $1",
            row["id"],
        )
        assert dict(got) == {"adopted_generation": None, "adopted_size": None}
        # (c) the matching observation persists the complete proof
        await uploads.record_adopted_observation(
            c, upload_id=row["id"], lease_id=fenced["transition_lease_id"],
            generation=4242, size=11,
            crc32c=CRC, content_type="text/plain",
        )
        got = await c.fetchrow(
            "SELECT adopted_generation, adopted_size, adopted_crc32c, "
            "adopted_content_type FROM upload_sessions WHERE id = $1",
            row["id"],
        )
        assert dict(got) == {
            "adopted_generation": 4242,
            "adopted_size": 11,
            "adopted_crc32c": CRC,
            "adopted_content_type": "text/plain",
        }


async def test_reconcile_revalidates_a_persisted_adoption(app_with_lifespan):
    """Correction blocker 7 (recovery half): `resume_commit` must never be
    answered from a bare persisted generation. The reconciler re-stats the
    destination and requires the full identity — marker, source fingerprint,
    declared size/CRC/type, and the SAME generation — before resuming;
    a wrong or missing fingerprint is never adopted."""
    from agentdrive.core import v0_uploads as uploads

    async def _adopted_stale_session(c, upload_id):
        row = await _begin_session(c, upload_id=upload_id)
        await uploads.acquire_initiation_lease(c, upload_id=row["id"], lease_seconds=60)
        await uploads.activate_session(c, upload_id=row["id"])
        fenced = await uploads.acquire_transition(
            c, upload_id=row["id"], action="complete", lease_seconds=60,
        )
        await uploads.record_scratch_observation(
            c, upload_id=row["id"], lease_id=fenced["transition_lease_id"],
            generation=41, size=11, crc32c=CRC,
        )
        await uploads.record_adopted_observation(
            c, upload_id=row["id"], lease_id=fenced["transition_lease_id"],
            generation=4242, size=11,
            crc32c=CRC, content_type="text/plain",
        )
        await c.execute(
            "UPDATE upload_sessions SET transition_lease_expires_at = "
            "now() - interval '1 second' WHERE id = $1",
            row["id"],
        )
        return row

    final = "transfer-immutable/ver_00000000000000e1"

    async with conn() as c:
        # (a) full identity + correct fingerprint → resume_commit
        row = await _adopted_stale_session(c, "upld_0000000000000f16")
        fingerprint = uploads.adoption_fingerprint(
            scratch_object=row["scratch_object"],
            scratch_generation=41,
            upload_id=row["id"],
        )
        storage = _FakeTransferStorage(
            {final: _fake_observation(source_fingerprint=fingerprint)}
        )
        async with c.transaction():
            outcome = await uploads.reconcile_upload_session(
                c, upload_id=row["id"], storage=storage,
            )
        assert outcome == "resume_commit"
        await c.execute("TRUNCATE upload_sessions, storage_reservations CASCADE")
        await c.execute("DELETE FROM workspace_storage")
        await c.execute("TRUNCATE drives RESTART IDENTITY CASCADE")

        # (b) WRONG fingerprint: never adopted — terminal rejected + quarantined
        row = await _adopted_stale_session(c, "upld_0000000000000f17")
        storage = _FakeTransferStorage(
            {final: _fake_observation(source_fingerprint="src=elsewhere@1;upld=fake")}
        )
        async with c.transaction():
            outcome = await uploads.reconcile_upload_session(
                c, upload_id=row["id"], storage=storage,
            )
        assert outcome == "rejected", (
            "a wrong source fingerprint was accepted as adoption proof"
        )
        got = await c.fetchrow(
            "SELECT state, failure_code, cleanup_state FROM upload_sessions "
            "WHERE id=$1", row["id"],
        )
        assert dict(got) == {
            "state": "rejected",
            "failure_code": "OBJECT_METADATA_MISMATCH",
            "cleanup_state": "quarantined",
        }
        await c.execute("TRUNCATE upload_sessions, storage_reservations CASCADE")
        await c.execute("DELETE FROM workspace_storage")
        await c.execute("TRUNCATE drives RESTART IDENTITY CASCADE")

        # (c) MISSING fingerprint: unverifiable — ambiguous, never resumed
        row = await _adopted_stale_session(c, "upld_0000000000000f18")
        storage = _FakeTransferStorage(
            {final: _fake_observation(source_fingerprint=None)}
        )
        async with c.transaction():
            outcome = await uploads.reconcile_upload_session(
                c, upload_id=row["id"], storage=storage,
            )
        assert outcome == "retry_rewrite"
        await c.execute("TRUNCATE upload_sessions, storage_reservations CASCADE")
        await c.execute("DELETE FROM workspace_storage")
        await c.execute("TRUNCATE drives RESTART IDENTITY CASCADE")

        # (d) destination ABSENT despite a persisted adoption (response-loss
        # then object vanished): ambiguous — bounded retry, never blind resume
        row = await _adopted_stale_session(c, "upld_0000000000000f19")
        storage = _FakeTransferStorage({})
        async with c.transaction():
            outcome = await uploads.reconcile_upload_session(
                c, upload_id=row["id"], storage=storage,
            )
        assert outcome == "retry_rewrite"
        state = await c.fetchval(
            "SELECT state FROM upload_sessions WHERE id=$1", row["id"]
        )
        assert state == "completing"
