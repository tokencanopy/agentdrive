"""The live GC sweeper, rebuilt for the v0 schema at B3 packet 1.

Terraform schedules ``python -m agentdrive.jobs.gc`` daily at 03:00 UTC and
weekly with ``--orphan-sweep``; the Makefile's ``gc-now``/``gc-now-dry``
targets invoke the same Cloud Run Job. That CLI contract — normal,
``--dry-run``, ``--orphan-sweep`` — is preserved from the archived sweeper;
the phases are re-derived from the day-0 schema plus the B3 direct-transfer
design (TokenCanopy
``docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md``
§9):

  1. **Session reconciliation** — lease-aware `reconcile_upload_session`
     over every non-terminal upload session: stale ``preparing`` recovery,
     deadline terminalization (a publication can never commit at/after its
     deadline), stale cancel finalization. Exactly-once reservation release
     is owned by the shared accounting seam.
  2. **Transfer cleanup** — generation-safe collection of uncommitted
     scratch/final objects for terminal sessions, after grace: quarantine,
     verify the candidate generation is absent from the committed-version
     live set, then delete with ``ifGenerationMatch=<observed>``. A failed
     precondition means the object CHANGED (possibly a late resumable
     finalization): restat later, never delete the new generation. A
     retention/hold rejection parks cleanup as ``blocked`` (operator
     visible). Cleanup NEVER changes a terminal publication outcome.
  3. **Purge** — hard-delete soft-deleted artifacts/folders/drives past
     retention, keeping the B3 logical counters exact (`drives.storage_bytes`
     and `workspace_storage.committed_bytes` decrement with the purged
     version rows).
  4. **CAS mark-sweep** — per live drive: delete `cas/{drive}/` blobs not in
     the live version set, age-gated, generation-pinned. Also asserts
     counter/live-sum parity per drive (reported, never silently "fixed").
  5. **Scratch sweep** — age-only sweep of the legacy scratch prefix.
  6. **Orphan sweep** (weekly flag) — `cas/{drive}/` prefixes whose drive
     row no longer exists (hard-purged), age-gated.

``--dry-run`` wraps ALL database work in one rolled-back transaction and
suppresses every object-store delete: counters preview the work, nothing
persists. Provider access goes through the injected ``TransferStorage``
protocol (`core.v0_uploads`) for transfer objects and the ``storage`` module
for CAS/scratch blobs, so the decision logic tests with fakes.

Never logged or persisted here: resumable URIs, signed URLs, tokens, or
provider response bodies — only object keys, generations, ids, and outcome
classes.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from .. import storage
from . import v0_changes as changes
from . import v0_content_commit as content_commit
from . import v0_uploads as uploads

log = logging.getLogger(__name__)

# Session-scoped advisory lock so two sweeps never interleave. Constant,
# derived from the ASCII of "gc_sweep" — stable across releases.
GC_SWEEP_LOCK = 0x67635F7377656570

_NON_TERMINAL = ("preparing", "active", "completing", "cancelling")
_TERMINAL = ("completed", "cancelled", "expired", "rejected")


@dataclass(frozen=True)
class _SystemActor:
    """Minimal actor exposing subject_type and subject for server-initiated changes."""

    subject_type: str = "system"
    subject: str | None = None


_SYSTEM_ACTOR = _SystemActor()


@dataclass
class SweepResult:
    """Counters + outcome for one sweep run (JSON-printed by the CLI)."""

    dry_run: bool = False
    skipped: bool = False
    skipped_reason: str | None = None
    capped: bool = False
    sessions_reconciled: int = 0
    sessions_expired: int = 0
    sessions_rejected: int = 0
    completions_resumed: int = 0
    session_rows_removed: int = 0
    reservations_reclaimed: int = 0
    transfer_deleted: int = 0
    transfer_blocked: int = 0
    purged_drives: int = 0
    purged_artifacts: int = 0
    purged_folders: int = 0
    cas_scanned: int = 0
    cas_deleted: int = 0
    scratch_deleted: int = 0
    orphan_deleted: int = 0
    sheet_sessions_expired: int = 0
    sheet_sessions_removed: int = 0
    parity_mismatches: int = 0
    errors: int = 0
    duration_ms: int = 0
    error_classes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """A sweep with phase errors or a counter/live-sum parity mismatch
        must FAIL the job (non-zero exit) so the operator is paged — silent
        success on either was the fail-open the security review flagged."""
        return self.errors > 0 or self.parity_mismatches > 0

    def as_dict(self) -> dict[str, Any]:
        return {**dict(self.__dict__), "failed": self.failed}


class TransferStorageUnavailableError(RuntimeError):
    """The dedicated transfer-bucket adapter has not landed (B3 packet 2).

    The pre-packet-2 default REFUSES rather than answering from the artifact
    bucket with no adoption-marker read — a wrong-bucket stat would
    misclassify reconciliation and mark unreachable objects `cleaned`
    (security review I2). A refusal surfaces as a failed phase → non-zero
    job exit, never as silent progress."""


class _UnavailableTransferStorage:
    """Fail-loud default until packet 2 supplies the real adapter."""

    async def stat_object(self, object_name: str) -> uploads.ObjectObservation | None:
        raise TransferStorageUnavailableError(
            "no transfer-bucket adapter: B3 packet 2 supplies it; refusing "
            "to stat transfer objects against the artifact bucket"
        )

    async def delete_generation(self, object_name: str, generation: int) -> None:
        raise TransferStorageUnavailableError(
            "no transfer-bucket adapter: B3 packet 2 supplies it; refusing "
            "to delete transfer objects against the artifact bucket"
        )


class GCSweeper:
    """The sweep orchestrator. Constants live on the class so tests can
    subclass with tightened ages/retention; production values are the
    defaults here (B8 revisits alerting/tuning, not these safety shapes)."""

    NAME = "gc"
    LOCK_ID = GC_SWEEP_LOCK

    # Soft-delete retention before hard purge. Operators can widen by
    # subclass/config later; purge is leaf-first and resumes across sweeps.
    PURGE_RETENTION = timedelta(days=30)
    # Age gates: young blobs may belong to an uncommitted in-flight write.
    MARK_SWEEP_AGE = timedelta(hours=24)
    SCRATCH_SWEEP_AGE = timedelta(hours=24)
    ORPHAN_SWEEP_AGE = timedelta(hours=24)
    # Terminal sessions wait this long before object collection…
    TRANSFER_GRACE = timedelta(hours=1)
    # …their scratch key stays WATCHED (quarantined, restatted each sweep)
    # for this long after terminal_at, because the never-persisted resumable
    # URI can finalize an object until the provider's ~one-week expiry — a
    # first-sweep stat miss must not close cleanup (contract review
    # finding 3)…
    LATE_FINALIZATION_WINDOW = timedelta(days=8)
    # …and their non-secret rows are retained this long for recovery/replay.
    TERMINAL_RETENTION = timedelta(days=7)
    # An expired or discarded sheet session stays readable this long, so an
    # agent that comes back can see WHY its work did not land rather than
    # getting an indistinguishable 404. Completed sessions are never swept
    # here: their edit log is the change feed's cell-level provenance and
    # lives exactly as long as the version it produced.
    SHEET_SESSION_RETENTION = timedelta(days=1)
    # The sweeper bounds itself INSIDE Cloud Run's 3600 s job timeout so an
    # overrun is stopped by the app (lock released, summary emitted), never
    # by SIGKILL stranding the advisory lock until connection teardown.
    HARD_TIMEOUT_S = 50 * 60
    # Bounded per-sweep work.
    MAX_TRANSFER_SESSIONS = 500
    PURGE_BATCH = 200

    CAS_PREFIX = storage.CAS_PREFIX
    # NOTE: packet 2's direct-transfer scratch prefix must NOT nest under
    # this legacy prefix — `_scratch_sweep` is age-only and would collect an
    # in-flight transfer's scratch object; transfer objects are collected
    # exclusively through their session rows above.
    SCRATCH_PREFIX = storage.SCRATCH_PREFIX

    def __init__(
        self,
        *,
        include_orphan_sweep: bool = False,
        sessions_only: bool = False,
        transfer_storage: uploads.TransferStorage | None = None,
    ) -> None:
        self.include_orphan_sweep = include_orphan_sweep
        self.sessions_only = sessions_only
        self.transfer = transfer_storage or _UnavailableTransferStorage()
        self._deadline: float | None = None

    # ─── public entry ──────────────────────────────────────────────

    async def run(self, *, dry_run: bool = False) -> SweepResult:
        from ..config import settings

        started = time.monotonic()
        self._deadline = started + self.HARD_TIMEOUT_S
        result = SweepResult(dry_run=dry_run)
        conn = await asyncpg.connect(settings.database_url)
        try:
            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1::bigint)", self.LOCK_ID
            )
            if not got:
                log.info("at=gc.lock_held")
                result.skipped = True
                result.skipped_reason = "lock_held"
                return result

            try:
                if dry_run:
                    # One rolled-back transaction previews ALL database work;
                    # object-store deletes are suppressed inside the phases.
                    tr = conn.transaction()
                    await tr.start()
                    try:
                        await self._phases(conn, result)
                    finally:
                        await tr.rollback()
                        log.info("at=gc.dry_run_rolled_back")
                else:
                    await self._phases(conn, result)
            finally:
                with contextlib.suppress(Exception):
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1::bigint)", self.LOCK_ID
                    )
            result.duration_ms = int((time.monotonic() - started) * 1000)
            log.info("at=gc.sweep_completed %s", result.as_dict())
            return result
        finally:
            await conn.close()

    def _time_up(self, result: SweepResult) -> bool:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            if not result.capped:
                log.warning("at=gc.deadline_hit timeout_s=%s", self.HARD_TIMEOUT_S)
                result.capped = True
            return True
        return False

    async def _phases(self, conn: Any, result: SweepResult) -> None:
        """Phase order matters: sessions terminalize before their objects
        collect; artifacts purge before folders; purge precedes mark-sweep so
        a purged artifact's blobs are unprotected in the same run.

        `sessions_only` stops after the four session phases — the FREQUENT
        pass. Those four are database work plus per-session object deletes,
        each bounded by MAX_TRANSFER_SESSIONS, so they are cheap enough to
        run hourly; that is what releases an abandoned upload's ceiling slot,
        its byte reservation, and its scratch object rather than leaving them
        held until the next daily sweep. Everything below LISTS an
        object-store prefix per drive — `_mark_sweep` walks every
        `cas/<drive_id>/` — which is unbounded in stored objects and is
        exactly the cost that must NOT be multiplied by 24. `_scratch_sweep`
        stays daily with them: it is the age-only leftover catcher, while a
        live transfer's scratch is collected through its session row in
        `_transfer_cleanup`, which the frequent pass does run."""
        phases = [
            self._reconcile_sessions,
            self._transfer_cleanup,
            self._reclaim_leaked_reservations,
            self._remove_expired_session_rows,
            self._sweep_sheet_sessions,
        ]
        if not self.sessions_only:
            phases += [self._purge, self._mark_sweep, self._scratch_sweep]
            if self.include_orphan_sweep:
                phases.append(self._orphan_sweep)
        for phase in phases:
            if self._time_up(result):
                return
            try:
                await phase(conn, result)
            except Exception as e:  # phase isolation: the next sweep retries
                log.error(
                    "at=gc.phase_failed phase=%s error_class=%s",
                    phase.__name__, type(e).__name__,
                )
                result.errors += 1
                result.error_classes.append(f"{phase.__name__}:{type(e).__name__}")

    # ─── phase 1: session reconciliation ───────────────────────────

    async def _reconcile_sessions(self, conn: Any, result: SweepResult) -> None:
        rows = await conn.fetch(
            "SELECT id FROM upload_sessions WHERE state = ANY($1::text[]) "
            "ORDER BY expires_at LIMIT $2",
            list(_NON_TERMINAL), self.MAX_TRANSFER_SESSIONS,
        )
        for row in rows:
            if self._time_up(result):
                return
            try:
                async with conn.transaction():
                    outcome = await uploads.reconcile_upload_session(
                        conn, upload_id=row["id"], storage=self.transfer,
                    )
            except Exception as e:
                log.error(
                    "at=gc.reconcile_failed upload_id=%s error_class=%s",
                    row["id"], type(e).__name__,
                )
                result.errors += 1
                result.error_classes.append(f"reconcile:{type(e).__name__}")
                continue
            result.sessions_reconciled += 1
            if outcome == "expired":
                result.sessions_expired += 1
            elif outcome == "rejected":
                result.sessions_rejected += 1
            elif (
                outcome in ("retry_rewrite", "resume_commit")
                and not result.dry_run
            ):
                # The EXECUTABLE reconciler (review round 3): a stale durable
                # completion action is resumed here, not discarded — one
                # bounded attempt per sweep, so the schedule is the retry
                # cadence and phase errors are the alert signal. Skipped in
                # dry-run: resuming performs real provider rewrites.
                await self._resume_completion(conn, row["id"], result)

    async def _resume_completion(self, conn: Any, upload_id: str, result: SweepResult) -> None:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _borrow():
            yield conn

        try:
            outcome = await uploads.resume_stale_completion(
                self.transfer, upload_id=upload_id, connection_factory=_borrow,
            )
        except Exception as e:
            log.error(
                "at=gc.resume_failed upload_id=%s error_class=%s",
                upload_id, type(e).__name__,
            )
            result.errors += 1
            result.error_classes.append(f"resume:{type(e).__name__}")
            return
        result.completions_resumed += 1
        log.info(
            "at=gc.completion_resumed upload_id=%s outcome=%s",
            upload_id, outcome,
        )

    # ─── phase 2: generation-safe transfer cleanup ─────────────────

    async def _transfer_cleanup(self, conn: Any, result: SweepResult) -> None:
        rows = await conn.fetch(
            "SELECT id, state, cleanup_state, scratch_object, final_object, "
            "  observed_scratch_generation, adopted_generation, "
            "  provider_attempted_at "
            "FROM upload_sessions "
            "WHERE state = ANY($1::text[]) "
            # `blocked` IS due once its next-attempt time passes: a fixed
            # retention/hold policy must lead to collection (packet-1
            # correction, blocker 5).
            "  AND cleanup_state IN ('pending', 'quarantined', 'deleting', "
            "                        'blocked') "
            "  AND (cleanup_next_attempt_at IS NULL "
            "       OR cleanup_next_attempt_at <= now()) "
            "  AND terminal_at <= now() - $2::interval "
            "ORDER BY terminal_at LIMIT $3",
            list(_TERMINAL), self.TRANSFER_GRACE, self.MAX_TRANSFER_SESSIONS,
        )
        for row in rows:
            if self._time_up(result):
                return
            await self._cleanup_one_session(conn, result, row)

    async def _cleanup_one_session(
        self, conn: Any, result: SweepResult, row: Any
    ) -> None:
        # A COMPLETED session's final object IS the committed content; only
        # its scratch object is ever a candidate. The committed live set is
        # rechecked per object below as defense in depth.
        candidates = [row["scratch_object"]]
        if row["state"] != "completed":
            candidates.append(row["final_object"])

        retry = False
        scratch_unobserved = False
        for object_name in candidates:
            committed = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM artifact_versions "
                "WHERE storage_object = $1)",
                object_name,
            )
            if committed:
                # Never select a committed object. Name-only on purpose: it
                # protects EVERY generation of a committed key, which is
                # strictly safer than a generation-aware check. Packet 2 may
                # narrow it to (object, generation) once the dedicated
                # transfer bucket separates namespaces — never the reverse.
                continue
            observation = await self.transfer.stat_object(object_name)
            if observation is None:
                # An incomplete provider session produces no object — but the
                # never-persisted URI can still finalize one until provider
                # expiry, so a scratch-key miss means WATCH, not done.
                if object_name == row["scratch_object"]:
                    scratch_unobserved = True
                continue
            if observation.generation is None:
                retry = True  # cannot delete generation-safely yet
                continue
            # Quarantine + persist the exact observed generation before any
            # delete (publication state is untouched throughout).
            await uploads.set_cleanup_state(
                conn, upload_id=row["id"], cleanup_state="quarantined",
                next_attempt_in_seconds=None,
            )
            if object_name == row["scratch_object"] and (
                row["observed_scratch_generation"] is None
            ):
                await conn.execute(
                    "UPDATE upload_sessions SET observed_scratch_generation = $2 "
                    "WHERE id = $1 AND observed_scratch_generation IS NULL",
                    row["id"], observation.generation,
                )
            if result.dry_run:
                retry = True  # preview only; nothing was actually collected
                continue
            try:
                await self.transfer.delete_generation(
                    object_name, observation.generation
                )
            except storage.NotFound:
                continue
            except storage.PreconditionFailed:
                # The object changed under us (possibly a late resumable
                # finalization) — restat after grace, never delete the new
                # generation blind.
                retry = True
                continue
            except Exception as e:
                await uploads.set_cleanup_state(
                    conn, upload_id=row["id"], cleanup_state="blocked",
                    failure_class=type(e).__name__,
                    next_attempt_in_seconds=self.TRANSFER_GRACE.total_seconds(),
                )
                result.transfer_blocked += 1
                return
            result.transfer_deleted += 1

        if (
            not retry
            and scratch_unobserved
            and row["provider_attempted_at"] is not None
        ):
            # Keep watching the scratch key until the provider's late-
            # finalization window has passed (contract review finding 3):
            # only then can "nothing observed" honestly mean "nothing will
            # ever exist". The final key needs no watch — only the fenced
            # server-side rewrite can create it, and this session is done.
            # A session that never reached its ONE credential disclosure
            # (provider_attempted_at IS NULL) has no signed initiation
            # target in the world, so no object can ever appear — cleanup
            # closes immediately. A DISCLOSED-but-never-initiated session
            # (the client never POSTed, or the minutes-scale signature
            # expired unused — 2026-08-20 amendment) looks exactly like the
            # old "URI disclosed, no bytes ever PUT" case and takes the
            # same watch-then-close path.
            still_in_window = await conn.fetchval(
                "SELECT terminal_at > now() - $2::interval "
                "FROM upload_sessions WHERE id = $1",
                row["id"], self.LATE_FINALIZATION_WINDOW,
            )
            retry = bool(still_in_window)

        if retry:
            await uploads.set_cleanup_state(
                conn, upload_id=row["id"], cleanup_state="quarantined",
                next_attempt_in_seconds=self.TRANSFER_GRACE.total_seconds(),
            )
        else:
            await uploads.set_cleanup_state(
                conn, upload_id=row["id"], cleanup_state="cleaned",
                next_attempt_in_seconds=None,
            )

    async def _reclaim_leaked_reservations(
        self, conn: Any, result: SweepResult
    ) -> None:
        """Exactly-once repair for the crash window the atomic `_terminalize`
        makes rare but history may contain: a TERMINAL session whose
        reservation is still live would otherwise hold workspace/drive
        logical bytes forever (security review I1)."""
        rows = await conn.fetch(
            "SELECT r.id FROM storage_reservations r "
            "JOIN upload_sessions u ON u.id = r.upload_id "
            "WHERE r.released_at IS NULL AND u.state = ANY($1::text[]) "
            "LIMIT $2",
            list(_TERMINAL), self.MAX_TRANSFER_SESSIONS,
        )
        for row in rows:
            released = await content_commit.release_version_reservation(
                conn, reservation_id=row["id"]
            )
            if released:
                result.reservations_reclaimed += 1

    async def _remove_expired_session_rows(
        self, conn: Any, result: SweepResult
    ) -> None:
        """§9 step 6: terminal non-secret rows are retained for the recovery
        window, then removed only after their reservation is released and
        cleanup is `cleaned`."""
        removed = await conn.fetch(
            "DELETE FROM upload_sessions "
            "WHERE state = ANY($1::text[]) AND cleanup_state = 'cleaned' "
            "  AND terminal_at <= now() - $2::interval "
            "  AND NOT EXISTS (SELECT 1 FROM storage_reservations r "
            "                  WHERE r.upload_id = upload_sessions.id "
            "                    AND r.released_at IS NULL) "
            "RETURNING id",
            list(_TERMINAL), self.TERMINAL_RETENTION,
        )
        result.session_rows_removed += len(removed)

    async def _sweep_sheet_sessions(self, conn: Any, result: SweepResult) -> None:
        """Terminalize overdue sessions, then collect the terminal ones.

        Two steps, deliberately separate. Expiry also happens at USE — the
        next call touching an overdue session transitions it — so this is the
        backstop for sessions nobody ever comes back to, not the primary
        mechanism. Doing it here as well means an abandoned session stops
        counting against the per-drive open cap without waiting for a caller
        that will never arrive.

        Collection is `expired` and `discarded` only. `completed` sessions are
        RETAINED: their edit log is what lets the change feed say
        `Q3!C3: 1,200 -> 1,610` months later, and it is bounded by the version
        pruner rather than by age (design §6).

        Edits go with the session by ON DELETE CASCADE, so there is no second
        sweep to forget.
        """
        expired = await conn.fetch(
            "UPDATE sheet_sessions SET state = 'expired', updated_at = now() "
            "WHERE state = 'open' AND lease_expires_at <= now() "
            "RETURNING id",
        )
        result.sheet_sessions_expired += len(expired)

        removed = await conn.fetch(
            "DELETE FROM sheet_sessions "
            "WHERE state IN ('expired', 'discarded') "
            "  AND updated_at <= now() - $1::interval "
            "RETURNING id",
            self.SHEET_SESSION_RETENTION,
        )
        result.sheet_sessions_removed += len(removed)

    # ─── phase 3: soft-delete purge ────────────────────────────────

    async def _purge_artifact_candidates(self, conn: Any, cutoff: timedelta) -> list:
        """Candidate ids only — every predicate is RE-CHECKED under lock in
        the per-row transaction, so a restore landing after this SELECT is
        never harmed (security review C1). Split out as a seam so the race
        window is testable."""
        return await conn.fetch(
            "SELECT a.id FROM artifacts a "
            "JOIN drives d ON d.id = a.drive_id AND d.deleted_at IS NULL "
            "WHERE a.deleted_at IS NOT NULL "
            "  AND a.deleted_at <= now() - $1::interval "
            "LIMIT $2",
            cutoff, self.PURGE_BATCH,
        )

    async def _purge_drive_candidates(self, conn: Any, cutoff: timedelta) -> list:
        """Same candidate/re-check split for drives.

        A drive with ANY remaining upload_sessions row is not purge-eligible
        (packet-1 correction, blocker 2): the CASCADE would erase the only
        scratch/final coordinates of unfinished transfer cleanup. Nonterminal
        sessions terminalize by deadline, cleaned rows retire after
        retention, and `blocked` rows hold the purge for the operator."""
        return await conn.fetch(
            "SELECT id FROM drives d "
            "WHERE d.deleted_at IS NOT NULL "
            "  AND d.deleted_at <= now() - $1::interval "
            "  AND NOT EXISTS (SELECT 1 FROM upload_sessions u "
            "                  WHERE u.drive_id = d.id) "
            "LIMIT $2",
            cutoff, self.PURGE_BATCH,
        )

    async def _purge(self, conn: Any, result: SweepResult) -> None:
        cutoff = self.PURGE_RETENTION

        # Artifacts on live drives, batched; counters stay exact. Lock order
        # inside the row transaction is workspace → drive → artifact-delete,
        # matching the accounting seam so a concurrent producer cannot
        # deadlock a sweep.
        while not self._time_up(result):
            rows = await self._purge_artifact_candidates(conn, cutoff)
            if not rows:
                break
            for row in rows:
                async with conn.transaction():
                    # Re-check + lock: only a row STILL soft-deleted past
                    # retention is purged; a concurrent restore survives.
                    locked = await conn.fetchrow(
                        "SELECT a.id, a.drive_id, a.name, d.workspace_id "
                        "FROM artifacts a JOIN drives d ON d.id = a.drive_id "
                        "WHERE a.id = $1 AND a.deleted_at IS NOT NULL "
                        "  AND a.deleted_at <= now() - $2::interval "
                        "  AND d.deleted_at IS NULL "
                        "FOR UPDATE OF a",
                        row["id"], cutoff,
                    )
                    if locked is None:
                        continue  # restored (or already purged) — skip
                    bytes_ = await conn.fetchval(
                        "SELECT COALESCE(sum(size_bytes), 0) "
                        "FROM artifact_versions WHERE artifact_id = $1",
                        locked["id"],
                    )
                    if bytes_:
                        await conn.execute(
                            "UPDATE workspace_storage "
                            "SET committed_bytes = committed_bytes - $2, "
                            "    updated_at = now() WHERE workspace_id = $1",
                            locked["workspace_id"], bytes_,
                        )
                        await conn.execute(
                            "UPDATE drives SET storage_bytes = storage_bytes - $2 "
                            "WHERE id = $1",
                            locked["drive_id"], bytes_,
                        )
                    await changes.append(
                        conn,
                        drive_id=locked["drive_id"],
                        actor=_SYSTEM_ACTOR,
                        type="artifact.purged",
                        resource_type="artifact",
                        resource_id=locked["id"],
                        data={"name": locked["name"]},
                    )
                    await conn.execute(
                        "DELETE FROM artifacts WHERE id = $1", locked["id"]
                    )
                    result.purged_artifacts += 1
            if len(rows) < self.PURGE_BATCH:
                break

        # Folders: leaf-first (child folders/artifacts must be gone); the
        # structural root is FK-protected and never soft-deleted anyway.
        while True:
            removed = await conn.fetch(
                "DELETE FROM folders f "
                "WHERE f.deleted_at IS NOT NULL "
                "  AND f.deleted_at <= now() - $1::interval "
                "  AND f.parent_id IS NOT NULL "
                "  AND EXISTS (SELECT 1 FROM drives d WHERE d.id = f.drive_id "
                "              AND d.deleted_at IS NULL) "
                "  AND NOT EXISTS (SELECT 1 FROM folders ch "
                "                  WHERE ch.parent_id = f.id) "
                "  AND NOT EXISTS (SELECT 1 FROM artifacts a "
                "                  WHERE a.parent_id = f.id) "
                "RETURNING f.id",
                cutoff,
            )
            result.purged_folders += len(removed)
            if not removed:
                break

        # Drives past retention: re-check + lock in the row transaction
        # (security review C1), with true workspace → drive lock ACQUISITION
        # order (the workspace accounting row is locked before the drive row,
        # matching the seam), a session-eligibility re-check (blocker 2),
        # straggler reservation release, the committed-counter decrement, and
        # only then the CASCADE delete.
        #
        # NOTE: Do NOT attempt a drive.purged change event here.
        # drive_changes.drive_id is declared REFERENCES drives(id) ON DELETE CASCADE.
        # When the sweeper hard-deletes a DRIVE row, that drive's entire change
        # feed is cascaded away in the same statement — so an event recording the
        # purge would delete itself. It is unrepresentable in a per-drive feed.
        rows = await self._purge_drive_candidates(conn, cutoff)
        for row in rows:
            if self._time_up(result):
                return
            async with conn.transaction():
                # plain read for the workspace id — no drive lock yet
                workspace_id = await conn.fetchval(
                    "SELECT workspace_id FROM drives WHERE id = $1", row["id"]
                )
                if workspace_id is None:
                    continue  # already purged
                await conn.execute(
                    "INSERT INTO workspace_storage (workspace_id) VALUES ($1) "
                    "ON CONFLICT (workspace_id) DO NOTHING",
                    workspace_id,
                )
                await conn.execute(
                    "SELECT 1 FROM workspace_storage "
                    "WHERE workspace_id = $1 FOR UPDATE",
                    workspace_id,
                )
                locked = await conn.fetchrow(
                    "SELECT id, workspace_id, storage_bytes FROM drives "
                    "WHERE id = $1 AND deleted_at IS NOT NULL "
                    "  AND deleted_at <= now() - $2::interval "
                    "  AND NOT EXISTS (SELECT 1 FROM upload_sessions u "
                    "                  WHERE u.drive_id = drives.id) "
                    "FOR UPDATE",
                    row["id"], cutoff,
                )
                if locked is None:
                    continue  # restored / newly ineligible — skip
                live = await conn.fetch(
                    "SELECT id FROM storage_reservations "
                    "WHERE drive_id = $1 AND released_at IS NULL",
                    locked["id"],
                )
                for reservation in live:
                    await content_commit.release_version_reservation(
                        conn, reservation_id=reservation["id"]
                    )
                if locked["storage_bytes"]:
                    await conn.execute(
                        "UPDATE workspace_storage "
                        "SET committed_bytes = committed_bytes - $2, "
                        "    updated_at = now() WHERE workspace_id = $1",
                        locked["workspace_id"], locked["storage_bytes"],
                    )
                await conn.execute(
                    "DELETE FROM drives WHERE id = $1", locked["id"]
                )
                result.purged_drives += 1

    # ─── phases 4-6: object-store sweeps ───────────────────────────

    def _old_enough(self, blob: storage.Blob, age: timedelta) -> bool:
        return datetime.now(UTC) - blob.time_created >= age

    async def _mark_sweep(self, conn: Any, result: SweepResult) -> None:
        if not storage.capabilities().generation_pinned_delete:
            # The pin is what makes a listing-then-delete safe against a
            # concurrent same-key write; without it, refuse rather than
            # sweep blind (ArtifactGC.tla assumes the pin holds).
            raise RuntimeError(
                "the object store cannot enforce generation-pinned deletes; "
                "refusing the mark-sweep"
            )
        drives = await conn.fetch(
            "SELECT id FROM drives WHERE deleted_at IS NULL"
        )
        for drive in drives:
            drive_id = drive["id"]
            live_set = {
                r["storage_object"]
                for r in await conn.fetch(
                    "SELECT DISTINCT v.storage_object FROM artifact_versions v "
                    "JOIN artifacts a ON a.id = v.artifact_id "
                    "WHERE a.drive_id = $1",
                    drive_id,
                )
            }
            async for blob in storage.list_blobs(f"{self.CAS_PREFIX}{drive_id}/"):
                result.cas_scanned += 1
                if blob.name in live_set:
                    continue
                if not self._old_enough(blob, self.MARK_SWEEP_AGE):
                    continue
                if result.dry_run:
                    result.cas_deleted += 1
                    continue
                # Membership re-check IMMEDIATELY before the delete
                # (adversarial review I-1): a duplicate-content write that
                # adopted this aged object and committed AFTER our live-set
                # snapshot is visible to this fresh statement. Belt to the
                # producer-side generation refresh (store_cas_object), whose
                # pin-miss protection real GCS enforces but the emulator's
                # delete path does not.
                referenced_now = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM artifact_versions "
                    "WHERE storage_object = $1)",
                    blob.name,
                )
                if referenced_now:
                    continue
                try:
                    await storage.delete(
                        blob.name, if_generation_match=blob.generation
                    )
                    result.cas_deleted += 1
                except (storage.NotFound, storage.PreconditionFailed):
                    continue
            counter, live_sum = await content_commit.storage_bytes_parity(
                conn, drive_id
            )
            if counter != live_sum:
                result.parity_mismatches += 1
                log.warning(
                    "at=gc.parity_mismatch drive_id=%s counter=%s live_sum=%s",
                    drive_id, counter, live_sum,
                )

    async def _scratch_sweep(self, conn: Any, result: SweepResult) -> None:
        async for blob in storage.list_blobs(self.SCRATCH_PREFIX):
            if not self._old_enough(blob, self.SCRATCH_SWEEP_AGE):
                continue
            if result.dry_run:
                result.scratch_deleted += 1
                continue
            try:
                await storage.delete(blob.name, if_generation_match=blob.generation)
                result.scratch_deleted += 1
            except (storage.NotFound, storage.PreconditionFailed):
                continue

    async def _orphan_sweep(self, conn: Any, result: SweepResult) -> None:
        prefixes = await storage.list_prefixes(self.CAS_PREFIX)
        for prefix in prefixes:
            drive_id = prefix.removeprefix(self.CAS_PREFIX).rstrip("/")
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM drives WHERE id = $1)", drive_id
            )
            if exists:
                continue
            async for blob in storage.list_blobs(prefix):
                if not self._old_enough(blob, self.ORPHAN_SWEEP_AGE):
                    continue
                if result.dry_run:
                    result.orphan_deleted += 1
                    continue
                try:
                    await storage.delete(
                        blob.name, if_generation_match=blob.generation
                    )
                    result.orphan_deleted += 1
                except (storage.NotFound, storage.PreconditionFailed):
                    continue
