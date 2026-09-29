"""Direct-upload session state machine and recovery (B3, no mounted route).

Governing contract: TokenCanopy
``docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md``
§5/§6. This module owns the durable, credential-free transitions:

  publication:  preparing → active → completing → completed
                                   └→ rejected / cancelled / expired
  cleanup:      none → pending → quarantined → deleting → cleaned / blocked

Packet 1 rules encoded here:

  * Enumerations FAIL CLOSED — `decode_publication_state` /
    `decode_cleanup_state` reject unknown values; a row a decoder cannot
    read is never treated as active or terminal success.
  * The begin saga is crash-exact: `provider_attempted_at` commits BEFORE
    the one credential DISCLOSURE (since the 2026-08-20 amendment there is
    no server-side provider initiation — begin signs an initiation URL the
    client POSTs itself; signing mints no provider state, so it happens
    before this marker and its failures stay retryable); once the marker
    is set, this session can never disclose again
    (`acquire_initiation_lease` CASes on it). `preparing` recovery
    therefore never discloses a second credential — an uncertain
    disclosure becomes terminal `rejected` and moves toward cleanup.
  * One transition owner at a time: `acquire_transition` takes the
    completing/cancelling fence with a durable action + bounded lease; a
    same-action caller attaches to a STALE lease, a different action stays
    `UploadBusyError`. The publication deadline is fenced both when taking
    the completion fence and again inside `complete_publication`.
  * Reservation release is exactly-once, delegated to the shared
    `v0_content_commit` conditional release; every terminal path may call
    it and only one succeeds.
  * Cleanup state never changes a terminal publication outcome.

Provider work stays behind the injected ``TransferStorage`` protocol so the
transactional core is testable with fakes; packet 2 supplies the real XML
adapter. No function here returns, stores, or logs a resumable URI.

Layer rule: never import ``agentdrive.api``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..identity.actor import WORKSPACE_ADMIN_ROLES
from . import v0_content_commit as content_commit
from .ids import new_id

PUBLICATION_STATES = frozenset(
    {
        "preparing", "active", "completing", "cancelling",
        "completed", "cancelled", "expired", "rejected",
    }
)
TERMINAL_PUBLICATION_STATES = frozenset(
    {"completed", "cancelled", "expired", "rejected"}
)
CLEANUP_STATES = frozenset(
    {"none", "pending", "quarantined", "deleting", "cleaned", "blocked"}
)

_SESSION_COLUMNS = (
    "id, workspace_id, drive_id, principal_type, principal_id, "
    "principal_workspace_role, target_kind, "
    "parent_folder_id, artifact_name, artifact_id, expected_artifact_revision, "
    "declared_size_bytes, declared_media_type, declared_crc32c, "
    "adoption_marker, scratch_object, final_object, expires_at, state, "
    "cleanup_state, session_revision, transition_action, transition_lease_id, "
    "transition_lease_expires_at, provider_attempted_at, target_disclosed, "
    "observed_scratch_generation, observed_scratch_size, "
    "observed_scratch_crc32c, adopted_generation, adopted_size, "
    "adopted_crc32c, adopted_content_type, failure_code, "
    "result_artifact_id, result_version_id, result_revision, "
    "cleanup_next_attempt_at, cleanup_attempts, cleanup_failure_class, "
    "terminal_at, retention_until, created_at, updated_at"
)


class UnknownUploadStateError(ValueError):
    """A persisted or wire state value outside the closed enumeration.

    Fail closed: the caller must reject the row, emit no target, and page
    the operator rather than treating it as active or terminal success."""


class UploadSessionNotFoundError(LookupError):
    """No such upload session (maps to the anti-enumerating 404)."""


class UploadBusyError(RuntimeError):
    """Another completion/cancel owns the transition fence (409 UPLOAD_BUSY)."""


class StaleSessionRevisionError(RuntimeError):
    """The caller's captured session revision no longer matches at the
    fence CAS (412 PRECONDITION_FAILED, no current ETag disclosed)."""


class LeaseLostError(RuntimeError):
    """The worker's transition lease is no longer current: another owner
    re-leased the durable action (or the session left the fenced state).
    The loser must STOP immediately — it may not persist observations,
    continuations, terminal state, or a publication (§6 one-owner rule)."""


class UploadExpiredError(RuntimeError):
    """The publication deadline elapsed; the session is now terminal
    `expired` (422 UPLOAD_EXPIRED at the wire)."""


class InvalidUploadTransitionError(RuntimeError):
    """The requested transition is not legal from the current state.
    Carries the current state so the route layer can classify exactly
    (e.g. cancel-after-completed → 409 UPLOAD_ALREADY_COMPLETED)."""

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


def decode_publication_state(value: Any) -> str:
    if value not in PUBLICATION_STATES:
        raise UnknownUploadStateError(
            f"unrecognized upload publication state {value!r}"
        )
    return value


def decode_cleanup_state(value: Any) -> str:
    if value not in CLEANUP_STATES:
        raise UnknownUploadStateError(f"unrecognized upload cleanup state {value!r}")
    return value


def is_terminal(state: Any) -> bool:
    return decode_publication_state(state) in TERMINAL_PUBLICATION_STATES


def session_etag(row: Any) -> str:
    """Strong ETag: upload id + monotonically bumped session revision.
    Never contains the target; durable through terminal retention."""
    return f'"{row["id"]}.{row["session_revision"]}"'


def adoption_fingerprint(
    *, scratch_object: str, scratch_generation: int, upload_id: str
) -> str:
    """The canonical server-owned source fingerprint (§6): written into the
    adopted object's metadata by the fenced rewrite (packet 2/3) and required
    to match before any ambiguous destination is adopted. Binds the exact
    scratch KEY + GENERATION + upload id; the source BUCKET is bound by the
    adapter itself — a TransferStorage implementation is scoped to the one
    configured transfer bucket, so an observation can only ever come from it.
    """
    return f"src={scratch_object}@{scratch_generation};upld={upload_id}"


@dataclass(frozen=True)
class ObjectObservation:
    """A non-secret stat of one provider object: exact coordinates and the
    server-owned adoption marker read from object metadata."""

    object_name: str
    generation: int | None
    size: int | None
    crc32c: str | None
    content_type: str | None
    adoption_marker: str | None
    # The server-owned source fingerprint read from object metadata: binds
    # the adopted object to its exact scratch key/generation and upload id
    # (see `adoption_fingerprint`). None = the adapter could not observe it,
    # which is always AMBIGUOUS for adoption decisions.
    source_fingerprint: str | None = None


class TransferStorage(Protocol):
    """The injected provider seam (packet 2 supplies the real adapter)."""

    async def stat_object(self, object_name: str) -> ObjectObservation | None: ...

    async def delete_generation(self, object_name: str, generation: int) -> None: ...


def _decoded(row: Any) -> Any:
    """Fail-closed read: decode both state enumerations before use."""
    if row is None:
        raise UploadSessionNotFoundError("upload session not found")
    decode_publication_state(row["state"])
    decode_cleanup_state(row["cleanup_state"])
    return row


async def _fetch_locked(c: Any, upload_id: str) -> Any:
    return _decoded(
        await c.fetchrow(
            f"SELECT {_SESSION_COLUMNS} FROM upload_sessions "
            "WHERE id = $1 FOR UPDATE",
            upload_id,
        )
    )


async def create_session(
    c: Any,
    *,
    upload_id: str | None = None,
    workspace_id: str,
    drive_id: str,
    principal_type: str,
    principal_id: str,
    principal_workspace_role: str | None = None,
    target_kind: str,
    parent_folder_id: str | None,
    artifact_name: str | None,
    artifact_id: str | None,
    expected_artifact_revision: str | None,
    declared_size_bytes: int,
    declared_media_type: str,
    declared_crc32c: str,
    adoption_marker: str,
    scratch_object: str,
    final_object: str,
    expires_in_seconds: int,
    workspace_limit_bytes: int | None = None,
    drive_limit_bytes: int | None = None,
) -> Any:
    """The begin saga's first transaction: insert the `preparing` session and
    acquire its ONE linked reservation. Must run inside the caller's
    transaction, which also claims the idempotency key (packet 3) — commit
    happens BEFORE any provider contact."""
    upload_id = upload_id or new_id("upld")
    row = await c.fetchrow(
        "INSERT INTO upload_sessions "
        "(id, workspace_id, drive_id, principal_type, principal_id, "
        " principal_workspace_role, "
        " target_kind, parent_folder_id, artifact_name, artifact_id, "
        " expected_artifact_revision, declared_size_bytes, "
        " declared_media_type, declared_crc32c, adoption_marker, "
        " scratch_object, final_object, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, "
        "$14, $15, $16, $17, now() + make_interval(secs => $18)) "
        f"RETURNING {_SESSION_COLUMNS}",
        upload_id, workspace_id, drive_id, principal_type, principal_id,
        principal_workspace_role,
        target_kind, parent_folder_id, artifact_name, artifact_id,
        expected_artifact_revision, declared_size_bytes, declared_media_type,
        declared_crc32c, adoption_marker, scratch_object, final_object,
        float(expires_in_seconds),
    )
    await content_commit.reserve_version_bytes(
        c, workspace_id=workspace_id, drive_id=drive_id,
        principal_id=principal_id, upload_id=upload_id,
        size_bytes=declared_size_bytes,
        workspace_limit_bytes=workspace_limit_bytes,
        drive_limit_bytes=drive_limit_bytes,
    )
    return _decoded(row)


async def acquire_initiation_lease(
    c: Any, *, upload_id: str, lease_seconds: int
) -> Any | None:
    """Durably mark the ONE credential disclosure before it happens.

    CASes on ``state = 'preparing' AND provider_attempted_at IS NULL``: the
    winner gets the row back and must commit before disclosing the signed
    initiation target; everyone else — retries, other workers, recovery —
    gets None and can never cause a second disclosure for this session.
    (Pre-amendment this fenced the one server-side provider initiation;
    what it protects is unchanged: at most one credential per session.)"""
    row = await c.fetchrow(
        "UPDATE upload_sessions SET "
        "  provider_attempted_at = now(), "
        "  transition_action = 'initiate', "
        "  transition_lease_id = $2, "
        "  transition_lease_expires_at = now() + make_interval(secs => $3), "
        "  session_revision = session_revision + 1, updated_at = now() "
        "WHERE id = $1 AND state = 'preparing' AND provider_attempted_at IS NULL "
        f"RETURNING {_SESSION_COLUMNS}",
        upload_id, new_id("rev"), float(lease_seconds),
    )
    return _decoded(row) if row is not None else None


async def activate_session(c: Any, *, upload_id: str) -> Any:
    """CAS `preparing → active` after a successful initiation; sets
    ``target_disclosed`` (the service attempted its one allowed disclosure)
    and clears the initiation lease. Committed BEFORE the response that
    carries the URI is written."""
    row = await c.fetchrow(
        "UPDATE upload_sessions SET "
        "  state = 'active', target_disclosed = true, "
        "  transition_action = NULL, transition_lease_id = NULL, "
        "  transition_lease_expires_at = NULL, "
        "  session_revision = session_revision + 1, updated_at = now() "
        "WHERE id = $1 AND state = 'preparing' AND provider_attempted_at IS NOT NULL "
        f"RETURNING {_SESSION_COLUMNS}",
        upload_id,
    )
    if row is None:
        current = await c.fetchrow(
            "SELECT state FROM upload_sessions WHERE id = $1", upload_id
        )
        if current is None:
            raise UploadSessionNotFoundError("upload session not found")
        raise InvalidUploadTransitionError(
            "session cannot activate", state=decode_publication_state(current["state"]),
        )
    return _decoded(row)


async def _terminalize(
    c: Any,
    upload_id: str,
    *,
    from_states: tuple[str, ...],
    to_state: str,
    failure_code: str | None,
    cleanup_state: str,
    lease_id: str | None = None,
) -> Any | None:
    """One terminal transition + exactly-once reservation release.

    The two statements run inside their own (sub)transaction so a crash can
    never persist a terminal state with a live reservation — the leak class
    GC's reclaim pass exists to mop up, not to depend on."""
    async with c.transaction():
        row = await c.fetchrow(
            "UPDATE upload_sessions SET "
            "  state = $2, failure_code = $3, cleanup_state = $4, "
            "  cleanup_next_attempt_at = now(), "
            "  transition_action = NULL, transition_lease_id = NULL, "
            "  transition_lease_expires_at = NULL, "
            "  terminal_at = now(), "
            "  session_revision = session_revision + 1, updated_at = now() "
            "WHERE id = $1 AND state = ANY($5::text[]) "
            "  AND ($6::text IS NULL OR transition_lease_id = $6::text) "
            f"RETURNING {_SESSION_COLUMNS}",
            upload_id, to_state, failure_code, cleanup_state, list(from_states),
            lease_id,
        )
        if row is not None:
            await content_commit.release_version_reservation(c, upload_id=upload_id)
    return row


async def fail_initiation(c: Any, *, upload_id: str, failure_code: str) -> Any:
    """`preparing → rejected` after a deterministic provider refusal or an
    uncertain initiation outcome. Releases the reservation exactly once and
    schedules cleanup (a URI-created-but-unrecorded session may later
    finalize an object; GC collects it generation-safely)."""
    row = await _terminalize(
        c, upload_id,
        from_states=("preparing",), to_state="rejected",
        failure_code=failure_code, cleanup_state="pending",
    )
    if row is None:
        raise InvalidUploadTransitionError(
            "session is not preparing",
            state=decode_publication_state(
                await c.fetchval(
                    "SELECT state FROM upload_sessions WHERE id = $1", upload_id
                )
            ),
        )
    return _decoded(row)


async def recover_stale_preparing(c: Any, *, upload_id: str) -> str:
    """Reconcile a `preparing` row found by GC/recovery.

    Never initiates and never discloses: failure BEFORE the durable
    ``provider_attempted_at`` marker proves no outbound attempt (the row is
    retryable by a same-key begin); once the marker is set and its lease is
    stale, the outbound outcome is uncertain — terminal `rejected` with the
    safe ``UPLOAD_INITIATION_UNCERTAIN`` classification, exactly-once
    release, and cleanup scheduled."""
    row = _decoded(
        await c.fetchrow(
            f"SELECT {_SESSION_COLUMNS} FROM upload_sessions "
            "WHERE id = $1 FOR UPDATE",
            upload_id,
        )
    )
    if row["state"] != "preparing":
        return "terminal" if is_terminal(row["state"]) else row["state"]
    if row["provider_attempted_at"] is None:
        return "retryable"
    lease_live = (
        row["transition_lease_expires_at"] is not None
        and await c.fetchval(
            "SELECT transition_lease_expires_at > now() FROM upload_sessions "
            "WHERE id = $1",
            upload_id,
        )
    )
    if lease_live:
        return "leased"
    await _terminalize(
        c, upload_id,
        from_states=("preparing",), to_state="rejected",
        failure_code="UPLOAD_INITIATION_UNCERTAIN", cleanup_state="pending",
    )
    return "rejected"


async def acquire_transition(
    c: Any, *, upload_id: str, action: str, lease_seconds: int,
    expected_revision: int | None = None,
) -> Any:
    """Take the completing/cancelling fence.

    Serialization rules (§6): only one owner at a time; a live lease answers
    `UploadBusyError` to everyone; a STALE lease admits only the SAME durable
    action (the reconciler/retry attaches — it never starts independent
    provider work); terminal states raise `InvalidUploadTransitionError`
    carrying the current state. The publication deadline is fenced here for
    `complete`: past-deadline sessions become terminal `expired` first."""
    if action not in ("complete", "cancel"):
        raise ValueError(f"unknown transition action {action!r}")
    row = await _fetch_locked(c, upload_id)
    state = row["state"]

    if is_terminal(state):
        raise InvalidUploadTransitionError(
            f"session is terminal ({state})", state=state,
        )
    if (
        expected_revision is not None
        and row["session_revision"] != expected_revision
    ):
        raise StaleSessionRevisionError(
            "the session changed after its ETag was read"
        )

    # Deadline fence BEFORE provider inspection: at/after expires_at the
    # session is no longer publication-eligible (database clock). The fence
    # deliberately excludes `cancelling` — the durable cancel action owns
    # that session and finalizes as `cancelled` regardless of the deadline
    # (a phantom expire attempt here would raise without terminalizing or
    # releasing anything).
    past_deadline = await c.fetchval(
        "SELECT expires_at <= now() FROM upload_sessions WHERE id = $1", upload_id
    )
    if (
        past_deadline
        and action == "complete"
        and state in ("preparing", "active", "completing")
    ):
        await _terminalize(
            c, upload_id,
            from_states=("preparing", "active", "completing"),
            to_state="expired", failure_code="UPLOAD_EXPIRED",
            cleanup_state="pending",
        )
        raise UploadExpiredError("publication deadline elapsed")

    fenced_state = "completing" if action == "complete" else "cancelling"

    if state == "active":
        # `expected_revision` (cancel's If-Match) is enforced INSIDE the CAS
        # itself: the ETag comparison and the transition are one atomic
        # statement, so a session mutated after the caller's read can never
        # be fenced by the stale revision (review round 3, blocker 2).
        row = await c.fetchrow(
            "UPDATE upload_sessions SET "
            "  state = $2, transition_action = $3, transition_lease_id = $4, "
            "  transition_lease_expires_at = now() + make_interval(secs => $5), "
            "  session_revision = session_revision + 1, updated_at = now() "
            "WHERE id = $1 AND state = 'active' "
            "  AND ($6::bigint IS NULL OR session_revision = $6::bigint) "
            f"RETURNING {_SESSION_COLUMNS}",
            upload_id, fenced_state, action, new_id("rev"), float(lease_seconds),
            expected_revision,
        )
        if row is None:
            # The CAS lost: another request took the fence (or a
            # terminalizer won) between our read and this UPDATE — callers
            # on autocommit connections hold no row lock across statements,
            # so this race is ordinary, not exceptional (§7: the loser is
            # UPLOAD_BUSY, never a 500; a pinned stale revision is 412).
            await _raise_lost_fence(
                c, upload_id, expected_revision=expected_revision
            )
        return _decoded(row)

    if state in ("completing", "cancelling"):
        lease_live = await c.fetchval(
            "SELECT transition_lease_expires_at > now() FROM upload_sessions "
            "WHERE id = $1",
            upload_id,
        )
        if lease_live:
            raise UploadBusyError("another request owns the transition fence")
        if row["transition_action"] != action:
            # The durable action still owns the fence until reconciled; a
            # different verb cannot steal it.
            raise UploadBusyError(
                f"the durable {row['transition_action']} action owns the fence"
            )
        # Guarded re-lease: only the SAME fenced state + durable action with
        # a STALE lease may be re-leased. A row that was terminalized (or
        # re-leased by a rival attacher) between the read and this UPDATE
        # matches zero rows and is classified, never silently re-lit.
        row = await c.fetchrow(
            "UPDATE upload_sessions SET "
            "  transition_lease_id = $2, "
            "  transition_lease_expires_at = now() + make_interval(secs => $3), "
            "  session_revision = session_revision + 1, updated_at = now() "
            "WHERE id = $1 AND state = $4 AND transition_action = $5 "
            "  AND transition_lease_expires_at <= now() "
            f"RETURNING {_SESSION_COLUMNS}",
            upload_id, new_id("rev"), float(lease_seconds), state, action,
        )
        if row is None:
            await _raise_lost_fence(c, upload_id)
        return _decoded(row)

    # preparing (or any other non-terminal state): the begin saga still owns
    # the session.
    raise UploadBusyError(f"session is {state}; the begin saga owns it")


async def _raise_lost_fence(
    c: Any, upload_id: str, *, expected_revision: int | None = None
) -> None:
    """Classify a lost transition CAS: terminal → InvalidUploadTransition
    (carrying the current state for exact route mapping), gone → not found,
    a revision the caller pinned that no longer matches → stale (412),
    anything else → UploadBusyError."""
    current = await c.fetchrow(
        "SELECT state, session_revision FROM upload_sessions WHERE id = $1",
        upload_id,
    )
    if current is None:
        raise UploadSessionNotFoundError("upload session not found")
    state = decode_publication_state(current["state"])
    if state in TERMINAL_PUBLICATION_STATES:
        raise InvalidUploadTransitionError(
            f"session is terminal ({state})", state=state
        )
    if (
        expected_revision is not None
        and current["session_revision"] != expected_revision
    ):
        raise StaleSessionRevisionError(
            "the session changed after its ETag was read"
        )
    raise UploadBusyError("lost the transition fence race")


async def _raise_fence_write_lost(c: Any, upload_id: str, lease_id: str) -> None:
    """Classify a fenced write whose CAS matched zero rows: the session is
    gone, terminal, or owned by a different lease — the caller must stop."""
    current = await c.fetchrow(
        "SELECT state, transition_lease_id FROM upload_sessions WHERE id = $1",
        upload_id,
    )
    if current is None:
        raise UploadSessionNotFoundError("upload session not found")
    state = decode_publication_state(current["state"])
    if state in TERMINAL_PUBLICATION_STATES:
        raise InvalidUploadTransitionError(
            f"session is terminal ({state})", state=state
        )
    if current["transition_lease_id"] != lease_id:
        raise LeaseLostError("the transition lease is no longer current")
    raise InvalidUploadTransitionError(
        f"session is not completing ({state})", state=state
    )


async def renew_transition_lease(
    c: Any, *, upload_id: str, lease_id: str, lease_seconds: int
) -> None:
    """Extend the CURRENT owner's lease before a bounded provider call.

    CASes on the exact lease token: an owner that lost the lease learns it
    here — BEFORE issuing further provider work — and must stop
    (`LeaseLostError`). This is what makes a multi-call rewrite saga safe
    under the bounded lease: the fence is ownership, not a timestamp."""
    updated = await c.execute(
        "UPDATE upload_sessions SET "
        "  transition_lease_expires_at = now() + make_interval(secs => $3), "
        "  updated_at = now() "
        "WHERE id = $1 AND transition_lease_id = $2 "
        "  AND state IN ('completing', 'cancelling')",
        upload_id, lease_id, float(lease_seconds),
    )
    if updated != "UPDATE 1":
        await _raise_fence_write_lost(c, upload_id, lease_id)


async def record_scratch_observation(
    c: Any, *, upload_id: str, lease_id: str, generation: int, size: int,
    crc32c: str,
) -> None:
    updated = await c.execute(
        "UPDATE upload_sessions SET observed_scratch_generation = $2, "
        "observed_scratch_size = $3, observed_scratch_crc32c = $4, "
        "updated_at = now() WHERE id = $1 AND state = 'completing' "
        "AND transition_lease_id = $5",
        upload_id, generation, size, crc32c, lease_id,
    )
    if updated != "UPDATE 1":
        await _raise_fence_write_lost(c, upload_id, lease_id)


async def record_adopted_observation(
    c: Any,
    *,
    upload_id: str,
    lease_id: str,
    generation: int,
    size: int,
    crc32c: str,
    content_type: str,
) -> None:
    """Persist the FULL durable adoption proof (§6): the observed immutable
    object's generation, size, CRC32C, and content type together — a bare
    generation is not proof (packet-1 correction, blocker 7). The observation
    must EQUAL the session's declaration; a disagreement is refused here and
    classified by the caller as a deterministic rejection. Lease-fenced: only
    the current transition owner may persist adoption proof."""
    row = await c.fetchrow(
        "SELECT declared_size_bytes, declared_crc32c, declared_media_type "
        "FROM upload_sessions WHERE id = $1 AND state = 'completing' "
        "AND transition_lease_id = $2",
        upload_id, lease_id,
    )
    if row is None:
        await _raise_fence_write_lost(c, upload_id, lease_id)
    if (
        size != row["declared_size_bytes"]
        or crc32c != row["declared_crc32c"]
        or content_type != row["declared_media_type"]
        or generation <= 0
    ):
        raise InvalidUploadTransitionError(
            "adopted observation disagrees with the session declaration",
            state="completing",
        )
    updated = await c.execute(
        "UPDATE upload_sessions SET adopted_generation = $2, "
        "adopted_size = $3, adopted_crc32c = $4, adopted_content_type = $5, "
        "updated_at = now() WHERE id = $1 AND state = 'completing' "
        "AND transition_lease_id = $6",
        upload_id, generation, size, crc32c, content_type, lease_id,
    )
    if updated != "UPDATE 1":
        await _raise_fence_write_lost(c, upload_id, lease_id)


async def record_rewrite_continuation(
    c: Any, *, upload_id: str, lease_id: str, continuation: str | None
) -> None:
    """Durable server-only rewrite recovery state (never wire-exposed).
    Committed before each continuation call so only the CURRENT fenced
    owner can resume the SAME rewrite — a lease loser stops here."""
    updated = await c.execute(
        "UPDATE upload_sessions SET rewrite_continuation = $2, updated_at = now() "
        "WHERE id = $1 AND state = 'completing' AND transition_lease_id = $3",
        upload_id, continuation, lease_id,
    )
    if updated != "UPDATE 1":
        await _raise_fence_write_lost(c, upload_id, lease_id)


async def return_to_active(c: Any, *, upload_id: str, lease_id: str) -> Any:
    """`completing → active`: the resumable upload is incomplete, not
    corrupt. The reservation and deadline stay; the completion idempotency
    claim is abandoned by the caller (no product mutation executed).
    Lease-fenced: only the current owner may release the fence this way."""
    row = await c.fetchrow(
        "UPDATE upload_sessions SET "
        "  state = 'active', transition_action = NULL, "
        "  transition_lease_id = NULL, transition_lease_expires_at = NULL, "
        "  session_revision = session_revision + 1, updated_at = now() "
        "WHERE id = $1 AND state = 'completing' AND transition_lease_id = $2 "
        f"RETURNING {_SESSION_COLUMNS}",
        upload_id, lease_id,
    )
    if row is None:
        await _raise_fence_write_lost(c, upload_id, lease_id)
    return _decoded(row)


async def complete_publication(
    c: Any,
    *,
    upload_id: str,
    lease_id: str,
    result_artifact_id: str,
    result_version_id: str,
    result_revision: str,
) -> Any:
    """`completing → completed` inside the caller's publication transaction
    (which also runs `commit_immutable_version`). Rechecks the deadline —
    the second fence — so a late object is never published; a session whose
    deadline elapsed during completion becomes `expired` with cleanup
    `quarantined` (an adopted object may exist)."""
    row = await c.fetchrow(
        "UPDATE upload_sessions SET "
        "  state = 'completed', "
        "  result_artifact_id = $2, result_version_id = $3, "
        "  result_revision = $4, "
        "  transition_action = NULL, transition_lease_id = NULL, "
        "  transition_lease_expires_at = NULL, terminal_at = now(), "
        # §6 step 5: SCHEDULE generation-guarded scratch deletion with the
        # commit — GC collects the scratch object after grace and later
        # retires the row. Only the committed FINAL object is content.
        "  cleanup_state = 'pending', cleanup_next_attempt_at = now(), "
        "  session_revision = session_revision + 1, updated_at = now() "
        "WHERE id = $1 AND state = 'completing' AND expires_at > now() "
        "AND adopted_generation IS NOT NULL AND transition_lease_id = $5 "
        f"RETURNING {_SESSION_COLUMNS}",
        upload_id, result_artifact_id, result_version_id, result_revision,
        lease_id,
    )
    if row is not None:
        return _decoded(row)

    current = await c.fetchrow(
        "SELECT state, expires_at <= now() AS expired, adopted_generation, "
        "transition_lease_id FROM upload_sessions WHERE id = $1",
        upload_id,
    )
    if current is None:
        raise UploadSessionNotFoundError("upload session not found")
    state = decode_publication_state(current["state"])
    if state == "completing" and current["transition_lease_id"] != lease_id:
        raise LeaseLostError("the transition lease is no longer current")
    if state == "completing" and current["expired"]:
        await _terminalize(
            c, upload_id,
            from_states=("completing",), to_state="expired",
            failure_code="UPLOAD_EXPIRED", cleanup_state="quarantined",
        )
        raise UploadExpiredError("publication deadline elapsed during completion")
    if state == "completing":
        raise InvalidUploadTransitionError(
            "no adopted generation to publish", state=state,
        )
    raise InvalidUploadTransitionError(
        f"session is not completing ({state})", state=state,
    )


async def reject_session(
    c: Any, *, upload_id: str, failure_code: str | None,
    cleanup_state: str = "pending", lease_id: str | None = None,
) -> Any:
    """Deterministic terminal rejection from any non-terminal state.
    Publication becomes `rejected` durably; the reservation releases exactly
    once; cleanup proceeds independently and never revives publication.
    When ``lease_id`` is given the terminalization is fenced on it, so a
    lease loser cannot terminalize the new owner's session."""
    decode_cleanup_state(cleanup_state)
    row = await _terminalize(
        c, upload_id,
        from_states=("preparing", "active", "completing", "cancelling"),
        to_state="rejected", failure_code=failure_code,
        cleanup_state=cleanup_state, lease_id=lease_id,
    )
    if row is None:
        current = await c.fetchval(
            "SELECT state FROM upload_sessions WHERE id = $1", upload_id
        )
        if current is None:
            raise UploadSessionNotFoundError("upload session not found")
        raise InvalidUploadTransitionError(
            f"session is terminal ({current})",
            state=decode_publication_state(current),
        )
    return _decoded(row)


async def finalize_cancel(c: Any, *, upload_id: str) -> Any:
    """`cancelling → cancelled`: publication permanently closed, reservation
    released exactly once, uncommitted objects scheduled for safe deletion.
    The external bearer may remain provider-usable until GCS expiry — that
    never reopens publication."""
    row = await _terminalize(
        c, upload_id,
        from_states=("cancelling",), to_state="cancelled",
        failure_code=None, cleanup_state="pending",
    )
    if row is None:
        current = await c.fetchval(
            "SELECT state FROM upload_sessions WHERE id = $1", upload_id
        )
        if current is None:
            raise UploadSessionNotFoundError("upload session not found")
        raise InvalidUploadTransitionError(
            f"session is not cancelling ({current})",
            state=decode_publication_state(current),
        )
    return _decoded(row)


async def expire_session(c: Any, *, upload_id: str) -> Any:
    """Terminalize an overdue non-completed session as `expired`.

    Called by GC/recovery once ``expires_at`` has passed. A `completing`
    session quarantines (an object may exist to collect); earlier states go
    straight to `pending` cleanup. Exactly-once release either way."""
    row = await _fetch_locked(c, upload_id)
    if is_terminal(row["state"]):
        return row
    if row["state"] == "cancelling":
        # The durable cancel action owns this session's outcome: expiry may
        # never steal it into `expired` (packet-1 correction, blocker 4).
        # `finalize_cancel` is the only exit.
        raise InvalidUploadTransitionError(
            "a cancelling session finalizes as cancelled, never expired",
            state="cancelling",
        )
    cleanup = "quarantined" if row["state"] == "completing" else "pending"
    row = await _terminalize(
        c, upload_id,
        from_states=("preparing", "active", "completing"),
        to_state="expired", failure_code="UPLOAD_EXPIRED", cleanup_state=cleanup,
    )
    if row is None:
        # Another terminalizer won between our read and the CAS (possible
        # when the caller runs on an autocommit connection): return the
        # terminal row rather than erroring — expiry is idempotent.
        return _decoded(
            await c.fetchrow(
                f"SELECT {_SESSION_COLUMNS} FROM upload_sessions WHERE id = $1",
                upload_id,
            )
        )
    return _decoded(row)


async def set_cleanup_state(
    c: Any,
    *,
    upload_id: str,
    cleanup_state: str,
    failure_class: str | None = None,
    next_attempt_in_seconds: float | None = None,
) -> None:
    """Advance the cleanup machine WITHOUT touching publication state.

    ``cleanup_attempts`` counts bounded FAILURE/retry attempts (§6): it
    increments only when a failure class is recorded or a retry is
    scheduled, not on the normal quarantine → cleaned progression."""
    decode_cleanup_state(cleanup_state)
    is_retry = failure_class is not None or next_attempt_in_seconds is not None
    await c.execute(
        "UPDATE upload_sessions SET cleanup_state = $2, "
        "cleanup_failure_class = $3, "
        "cleanup_attempts = cleanup_attempts + CASE WHEN $5 THEN 1 ELSE 0 END, "
        "cleanup_next_attempt_at = CASE WHEN $4::float8 IS NULL THEN NULL "
        "  ELSE now() + make_interval(secs => $4::float8) END, "
        "updated_at = now() WHERE id = $1",
        upload_id, cleanup_state, failure_class, next_attempt_in_seconds,
        is_retry,
    )


def evaluate_destination_observation(row: Any, observation: ObjectObservation) -> str:
    """Classify one destination observation against the session's durable
    server-owned adoption identity (§6 ambiguous-adoption rule).

    Returns ``"mismatch"`` for an AFFIRMATIVE disagreement (a field the
    adapter DID observe that contradicts the expectation — deterministic
    rejection, the object is foreign and never adopted), ``"adopt"`` when
    every identity field is observed and matches, and ``"ambiguous"`` when
    anything is unobservable — never adopt, never reject; retry boundedly.

    Shared by GC reconciliation and the fenced completion saga (packet 3)
    so the two can never classify the same observation differently."""
    expected_fingerprint = (
        adoption_fingerprint(
            scratch_object=row["scratch_object"],
            scratch_generation=row["observed_scratch_generation"],
            upload_id=row["id"],
        )
        if row["observed_scratch_generation"] is not None
        else None
    )
    mismatched = (
        (
            observation.adoption_marker is not None
            and observation.adoption_marker != row["adoption_marker"]
        )
        or (
            observation.source_fingerprint is not None
            and expected_fingerprint is not None
            and observation.source_fingerprint != expected_fingerprint
        )
        or (
            observation.content_type is not None
            and observation.content_type != row["declared_media_type"]
        )
        or (
            observation.size is not None
            and observation.size != row["declared_size_bytes"]
        )
        or (
            observation.crc32c is not None
            and observation.crc32c != row["declared_crc32c"]
        )
        or (
            observation.generation is not None
            and row["adopted_generation"] is not None
            and observation.generation != row["adopted_generation"]
        )
    )
    if mismatched:
        return "mismatch"
    verifiable = (
        observation.adoption_marker is not None
        and observation.source_fingerprint is not None
        and expected_fingerprint is not None
        and observation.content_type is not None
        and observation.size is not None
        and observation.crc32c is not None
        and observation.generation is not None
    )
    return "adopt" if verifiable else "ambiguous"


async def get_session(c: Any, *, drive_id: str, upload_id: str) -> Any:
    """Fetch one session bound to its drive, states decoded fail-closed.
    Raises :class:`UploadSessionNotFoundError` on a miss — the route layer
    maps it to the anti-enumerating 404."""
    return _decoded(
        await c.fetchrow(
            f"SELECT {_SESSION_COLUMNS} FROM upload_sessions "
            "WHERE id = $1 AND drive_id = $2",
            upload_id, drive_id,
        )
    )


async def get_session_locked(c: Any, *, drive_id: str, upload_id: str) -> Any:
    return _decoded(
        await c.fetchrow(
            f"SELECT {_SESSION_COLUMNS} FROM upload_sessions "
            "WHERE id = $1 AND drive_id = $2 FOR UPDATE",
            upload_id, drive_id,
        )
    )


async def find_equivalent_live_session(
    c: Any,
    *,
    drive_id: str,
    principal_type: str,
    principal_id: str,
    target_kind: str,
    parent_folder_id: str | None,
    artifact_name: str | None,
    artifact_id: str | None,
) -> str | None:
    """The id of a live session already targeting the same destination for
    the same principal, if any (§6 begin recovery: a different key conflicts
    with the still-live equivalent target/session rather than creating a
    second provider credential). Callers hold the drive namespace lock so
    concurrent begins serialize against this check.

    "Live" is state AND deadline — see `count_live_sessions`. A past-deadline
    session is not a rival for the target: `complete` fences on `expires_at`
    and terminalizes it as `expired` first, so it can never publish. Blocking
    on one would make re-uploading the same name impossible until GC swept
    it, which is the same up-to-a-day wait the ceiling had."""
    return await c.fetchval(
        "SELECT id FROM upload_sessions "
        "WHERE drive_id = $1 AND principal_type = $2 AND principal_id = $3 "
        "AND target_kind = $4 "
        "AND parent_folder_id IS NOT DISTINCT FROM $5 "
        "AND artifact_name IS NOT DISTINCT FROM $6 "
        "AND artifact_id IS NOT DISTINCT FROM $7 "
        "AND state IN ('preparing', 'active', 'completing', 'cancelling') "
        "AND expires_at > now() "
        "LIMIT 1",
        drive_id, principal_type, principal_id, target_kind,
        parent_folder_id, artifact_name, artifact_id,
    )


async def count_live_sessions(
    c: Any, *, workspace_id: str, drive_id: str, principal_id: str
) -> tuple[int, int, int]:
    """(principal, workspace, drive) live-session counts for the §9
    active-session ceilings. Direct sessions only — these bounds never
    apply to inline producers.

    A session counts while its state is non-terminal AND its deadline has
    not passed. The deadline half is load-bearing: nothing terminalizes an
    overdue session on the read path — only the GC sweeper does, and it runs
    on a schedule. Counting by state alone therefore held a slot from the
    4h TTL until the next sweep, so an abandoned upload could occupy one of
    a principal's three slots for the better part of a day (2026-08-22: this
    is what refused a whole batch as "no room" on an all-but-empty drive).
    Past-deadline sessions cannot complete — `complete` expires them first —
    so excluding them refuses nothing that could still succeed. Their BYTE
    reservations and scratch objects are still GC's to release; this bound
    is only about concurrency."""
    row = await c.fetchrow(
        "SELECT "
        "  count(*) FILTER (WHERE workspace_id = $1 AND principal_id = $3) "
        "    AS by_principal, "
        "  count(*) FILTER (WHERE workspace_id = $1) AS by_workspace, "
        "  count(*) FILTER (WHERE drive_id = $2) AS by_drive "
        "FROM upload_sessions "
        "WHERE state IN ('preparing', 'active', 'completing', 'cancelling') "
        "AND expires_at > now() "
        "AND (workspace_id = $1 OR drive_id = $2)",
        workspace_id, drive_id, principal_id,
    )
    return (row["by_principal"], row["by_workspace"], row["by_drive"])


async def live_reservation_id(c: Any, upload_id: str) -> str | None:
    return await c.fetchval(
        "SELECT id FROM storage_reservations "
        "WHERE upload_id = $1 AND released_at IS NULL",
        upload_id,
    )


class DestinationGoneError(LookupError):
    """The publication destination (parent folder / target artifact) is no
    longer live. The session stays non-terminal — it may still expire —
    and the wire answer is the anti-enumerating 404."""


class UploadHeadChangedError(RuntimeError):
    """The captured artifact head revision no longer matches at publication
    (§7 version head race). Deterministic terminal rejection, 412."""


async def publish_completed_artifact(
    c: Any, *, actor: Any, session: Any, lease_id: str
) -> dict[str, Any]:
    """§6 completion step 4, artifact form: atomically recheck namespace
    vacancy, create the artifact + its first immutable version from the
    verified ADOPTED object (no bytes through this process), rotate the
    head, append change events, convert the session's reservation, and mark
    the session ``completed`` — all in the caller's transaction.

    Raises ``DestinationGoneError`` / ``ArtifactNameConflictError`` /
    ``UploadExpiredError``; on any raise the caller's transaction rolls
    back and the caller classifies the outcome."""
    from ..config import settings
    from . import v0_changes as changes
    from .v0_artifacts import ArtifactNameConflictError, _resolve_parent
    from .v0_drives import _lock_drive_namespace
    from .v0_folders import _name_is_occupied, validate_name

    drive_id = session["drive_id"]
    parent_id = session["parent_folder_id"]
    name = validate_name(session["artifact_name"])
    await _lock_drive_namespace(c, drive_id)
    # Legacy sessions can predate begin-time NFC normalization. Canonicalize
    # under this lease so the fresh row and stored completion response agree.
    updated = await c.execute(
        "UPDATE upload_sessions SET artifact_name = $2 "
        "WHERE id = $1 AND state = 'completing' "
        "AND transition_lease_id = $3",
        session["id"],
        name,
        lease_id,
    )
    if updated != "UPDATE 1":
        await _raise_fence_write_lost(c, session["id"], lease_id)
    if await _resolve_parent(c, drive_id, parent_id) is None:
        raise DestinationGoneError("the destination folder is no longer live")
    if await _name_is_occupied(c, drive_id, parent_id, name):
        raise ArtifactNameConflictError(
            f"a sibling under {parent_id} already uses the name {name!r}"
        )

    artifact_id = new_id("art")
    version_id = new_id("ver")
    revision = new_id("rev")
    try:
        await c.execute(
            "INSERT INTO artifacts "
            "(id, drive_id, parent_id, name, content_type, content_preview, "
            " metadata, labels, revision) "
            "VALUES ($1, $2, $3, $4, $5, NULL, '{}'::jsonb, "
            "        '{}'::text[], $6)",
            artifact_id, drive_id, parent_id, name,
            session["declared_media_type"], revision,
        )
    except Exception as exc:  # UniqueViolation under the cross-kind trigger
        import asyncpg

        if isinstance(exc, asyncpg.UniqueViolationError):
            raise ArtifactNameConflictError(
                f"a sibling under {parent_id} already uses the name {name!r}"
            ) from None
        raise

    await _commit_adopted_version(
        c, actor=actor, session=session,
        artifact_id=artifact_id, version_id=version_id,
        parent_version_id=None, ordinal=1,
        transfer_bucket=settings.direct_transfer_bucket,
    )
    await c.execute(
        "UPDATE artifacts SET head_version_id = $2 WHERE id = $1",
        artifact_id, version_id,
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.created", resource_type="artifact",
        resource_id=artifact_id, revision=revision,
        data={"name": name},
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.version.created", resource_type="artifact",
        resource_id=artifact_id, revision=revision,
        data={"name": name, "version_id": version_id, "upload_id": session["id"]},
    )
    await complete_publication(
        c, upload_id=session["id"], lease_id=lease_id,
        result_artifact_id=artifact_id, result_version_id=version_id,
        result_revision=revision,
    )
    return {
        "kind": "artifact", "artifact_id": artifact_id,
        "version_id": version_id, "revision": revision,
    }


async def publish_completed_version(
    c: Any, *, actor: Any, session: Any, lease_id: str
) -> dict[str, Any]:
    """§6 completion step 4, version form: lock the artifact, compare the
    CAPTURED head revision (never last-writer-wins), append one immutable
    version from the adopted object, rotate the head, and complete the
    session — all in the caller's transaction."""
    from ..config import settings
    from . import v0_changes as changes
    from .v0_drives import _lock_drive_namespace

    drive_id = session["drive_id"]
    artifact_id = session["artifact_id"]
    # Serialize with drive soft-delete (the same advisory lock every content
    # mutator takes), so the publication-time drive-liveness check cannot
    # race delete_drive.
    await _lock_drive_namespace(c, drive_id)
    artifact = await c.fetchrow(
        "SELECT id, name, revision, head_version_id FROM artifacts "
        "WHERE drive_id = $1 AND id = $2 AND deleted_at IS NULL FOR UPDATE",
        drive_id, artifact_id,
    )
    if artifact is None:
        raise DestinationGoneError("the target artifact is no longer live")
    if artifact["revision"] != session["expected_artifact_revision"]:
        raise UploadHeadChangedError(
            "the artifact head changed after the upload began"
        )

    version_id = new_id("ver")
    revision = new_id("rev")
    next_ordinal = await c.fetchval(
        "SELECT coalesce(max(ordinal), 0) + 1 FROM artifact_versions "
        "WHERE artifact_id = $1",
        artifact_id,
    )
    await _commit_adopted_version(
        c, actor=actor, session=session,
        artifact_id=artifact_id, version_id=version_id,
        parent_version_id=artifact["head_version_id"], ordinal=next_ordinal,
        transfer_bucket=settings.direct_transfer_bucket,
    )
    # The head rotates to the direct version. The denormalized preview is
    # cleared, not carried over: the bytes never crossed this process, so a
    # preview cannot honestly be derived.
    await c.execute(
        "UPDATE artifacts SET head_version_id = $3, revision = $4, "
        "content_type = $5, content_preview = NULL, updated_at = now() "
        "WHERE drive_id = $1 AND id = $2",
        drive_id, artifact_id, version_id, revision,
        session["declared_media_type"],
    )
    await changes.append(
        c, drive_id=drive_id, actor=actor,
        type="artifact.version.created", resource_type="artifact",
        resource_id=artifact_id,
        previous_revision=artifact["revision"], revision=revision,
        data={"name": artifact["name"], "version_id": version_id, "upload_id": session["id"]},
    )
    await complete_publication(
        c, upload_id=session["id"], lease_id=lease_id,
        result_artifact_id=artifact_id, result_version_id=version_id,
        result_revision=revision,
    )
    return {
        "kind": "version", "artifact_id": artifact_id,
        "version_id": version_id, "revision": revision,
    }


async def _commit_adopted_version(
    c: Any,
    *,
    actor: Any,
    session: Any,
    artifact_id: str,
    version_id: str,
    parent_version_id: str | None,
    ordinal: int,
    transfer_bucket: str,
) -> None:
    """Commit the verified adopted object as one immutable version through
    the shared seam — the ONLY version-row path (§7). Storage identity stays
    honest: the dedicated transfer bucket, the exact adopted generation, and
    an algorithm-qualified ``crc32c:`` checksum."""
    if session["adopted_generation"] is None:
        raise InvalidUploadTransitionError(
            "no verified adopted object to publish", state=session["state"],
        )
    reservation_id = await live_reservation_id(c, session["id"])
    if reservation_id is None:
        raise content_commit.AccountingError(
            f"upload session {session['id']} has no live reservation"
        )
    await content_commit.commit_immutable_version(
        c,
        content_commit.ImmutableVersionCommit(
            drive_id=session["drive_id"],
            workspace_id=session["workspace_id"],
            artifact_id=artifact_id,
            version_id=version_id,
            parent_version_id=parent_version_id,
            ordinal=ordinal,
            checksum=f"crc32c:{session['adopted_crc32c']}",
            content_type=session["adopted_content_type"],
            size_bytes=session["adopted_size"],
            storage_object=session["final_object"],
            storage_bucket=transfer_bucket,
            storage_generation=session["adopted_generation"],
            actor_type=actor.subject_type,
            actor_id=actor.subject,
            reservation_id=reservation_id,
        ),
    )


async def reconcile_upload_session(
    c: Any, *, upload_id: str, storage: TransferStorage
) -> str:
    """Lease-aware reconciliation of one session (GC's per-session step).

    Outcomes:
      ``terminal``       — already terminal; nothing to do.
      ``leased``         — a live lease owns the session; skip.
      ``retryable``      — preparing, no outbound attempt; a same-key begin
                           may proceed.
      ``rejected``       — uncertain initiation or non-adoptable destination;
                           terminal with safe classification.
      ``expired``        — deadline passed; terminalized.
      ``resume_commit``  — a verified adopted object exists; the fenced
                           completion (packet 3) resumes step 4.
      ``retry_rewrite``  — destination absent; retry the SAME persisted
                           rewrite phase, never a new key.
    """
    row = _decoded(
        await c.fetchrow(
            f"SELECT {_SESSION_COLUMNS} FROM upload_sessions "
            "WHERE id = $1 FOR UPDATE",
            upload_id,
        )
    )
    state = row["state"]
    if is_terminal(state):
        return "terminal"

    lease_live = row["transition_lease_expires_at"] is not None and await c.fetchval(
        "SELECT transition_lease_expires_at > now() FROM upload_sessions "
        "WHERE id = $1",
        upload_id,
    )
    if lease_live:
        return "leased"

    past_deadline = await c.fetchval(
        "SELECT expires_at <= now() FROM upload_sessions WHERE id = $1", upload_id
    )

    if state == "cancelling":
        # The durable cancel action owns the outcome even past the deadline
        # (packet-1 correction, blocker 4): finalize as `cancelled`, never
        # let expiry steal it into `expired`.
        await finalize_cancel(c, upload_id=upload_id)
        return "cancelled"

    if state == "preparing":
        # An overdue `preparing` session — including one that crashed BEFORE
        # its one initiation attempt — terminalizes as expired so its
        # reservation releases and its scratch key enters cleanup; it must
        # never stay "retryable" forever (contract review finding 1). The
        # deadline check runs first: `recover_stale_preparing` alone would
        # answer "retryable" for the never-attempted case indefinitely.
        if past_deadline:
            await expire_session(c, upload_id=upload_id)
            return "expired"
        return await recover_stale_preparing(c, upload_id=upload_id)

    if past_deadline:
        await expire_session(c, upload_id=upload_id)
        return "expired"

    if state == "active":
        return "leased" if lease_live else "active"

    # state == "completing" with a stale lease: the ambiguous-adoption rule.
    # Adoption — first-time OR resumption of a persisted proof — is decided
    # only from a fresh destination observation whose FULL server-owned
    # identity matches: adoption marker, source fingerprint (bound to the
    # observed scratch generation + upload id; the bucket is bound by the
    # adapter's scoping), declared type/size/CRC32C, and a real generation.
    observation = await storage.stat_object(row["final_object"])
    if observation is None:
        # Ambiguous success is not proof of failure: retry the same rewrite
        # phase boundedly; never select a new key — and never resume a
        # commit from a persisted generation without re-observing the object
        # (packet-1 correction, blocker 7).
        return "retry_rewrite"
    # AFFIRMATIVE mismatches — a field the adapter DID observe that
    # disagrees with the server-owned expectation — are deterministic
    # rejections: the object is foreign and is never adopted (nor deleted
    # here; the generation-guarded live-set policy owns any deletion).
    # Anything UNOBSERVABLE — a None field, or an expected fingerprint that
    # cannot be derived because the scratch observation is missing — is
    # AMBIGUOUS: never adopt, never reject; retry the same phase boundedly.
    # (Classification shared with the fenced completion saga.)
    verdict = evaluate_destination_observation(row, observation)
    if verdict == "mismatch":
        await reject_session(
            c, upload_id=upload_id,
            failure_code="OBJECT_METADATA_MISMATCH",
            cleanup_state="quarantined",
        )
        return "rejected"
    if verdict == "ambiguous":
        return "retry_rewrite"
    if row["adopted_generation"] is None:
        await record_adopted_observation(
            c, upload_id=upload_id, lease_id=row["transition_lease_id"],
            generation=observation.generation,
            size=observation.size, crc32c=observation.crc32c,
            content_type=observation.content_type,
        )
    return "resume_commit"


async def transfer_readiness(c: Any) -> tuple[bool, list[str]]:
    """Whether direct transfer may serve at all (B3 §7/§9 readiness).

    Fail closed on every axis: the feature flag must be on (its enabled
    numeric policy is boot-validated by `config.Settings`), and every
    version row must carry a resolved object generation — download minting
    and transfer stay disabled while any row is unresolved. Deliberately
    conservative: rows under soft-deleted artifacts count too (they resolve
    or purge before readiness flips). Returns ``(ready, reasons)``; reasons
    are operator-safe strings (counts and setting names, never coordinates
    or secrets)."""
    from ..config import settings

    reasons: list[str] = []
    if not settings.direct_transfer_enabled:
        reasons.append("direct_transfer_enabled=false")
    unresolved = await c.fetchval(
        # Either coordinate missing means unresolved (the schema makes a
        # half-pair unrepresentable; this is the belt over that suspender).
        "SELECT count(*) FROM artifact_versions "
        "WHERE storage_generation IS NULL OR storage_bucket IS NULL "
        "   OR storage_bucket = ''"
    )
    if unresolved:
        reasons.append(f"{unresolved} unresolved generation rows")
    return (not reasons, reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Fenced completion saga + reconciler executor (B3 packet 3, review round 3)
# ─────────────────────────────────────────────────────────────────────────────

# Bound on rewrite continuation steps in ONE completion attempt; a rewrite
# that has not converged by then surfaces as retryable, never as a spin.
MAX_REWRITE_STEPS = 32

# Lease taken by the background reconciler when it attaches to a stale
# durable completion action.
RECONCILE_LEASE_SECONDS = 60


@dataclass(frozen=True)
class SessionPrincipal:
    """The minimal actor view a reconciler derives from the session row —
    enough for local reauthorization (`authz.require`) and version-row
    attribution, never a token.

    `workspace_role` is the mint-time snapshot (0057): the reconciler's
    token-less re-authorization honors the workspace-admin overlay for a
    session an owner/admin opened on a drive they hold no grant row in —
    otherwise a completion that fell to the reconciler would be
    misclassified as unauthorized for exactly the access the mint verified.
    """

    subject: str
    subject_type: str
    workspace_id: str
    workspace_role: str | None = None

    @property
    def is_workspace_admin(self) -> bool:
        """Mint-time owner/admin standing, for the workspace-admin overlay."""
        return (
            self.subject_type == "user"
            and self.workspace_role in WORKSPACE_ADMIN_ROLES
        )


def session_principal(row: Any) -> SessionPrincipal:
    return SessionPrincipal(
        subject=row["principal_id"],
        subject_type=row["principal_type"],
        workspace_id=row["workspace_id"],
        workspace_role=row["principal_workspace_role"],
    )


@dataclass(frozen=True)
class CompletionOutcome:
    """One classified result of a fenced completion attempt (§5.5/§6).

    ``kind``:
      published        — one immutable result committed (``result`` set).
      incomplete       — no finalized scratch object; caller returns the
                         session to active (pre-adoption only).
      reject           — deterministic publication failure; caller
                         terminalizes as rejected with ``failure_code`` /
                         ``cleanup_state``. ``anti_enumerate`` marks the
                         post-adoption authorization/destination misses that
                         must answer the uniform 404 with no failure code.
      expired          — the deadline fence fired; caller terminalizes as
                         expired atomically with its stored 422.
      unavailable      — transient/ambiguous provider work; the durable
                         completing action and lease stay for reconciliation.
      lease_lost       — this worker no longer owns the fence; it performed
                         no further writes and must classify from a fresh
                         read.
    """

    kind: str
    result: dict[str, Any] | None = None
    failure_code: str | None = None
    cleanup_state: str | None = None
    anti_enumerate: bool = False


class _CompletionUnauthorizedError(RuntimeError):
    """Publication-time local reauthorization failed (post-adoption)."""


def _default_connection_factory():
    from ..db import conn

    return conn


async def run_fenced_completion(
    storage: Any,
    *,
    drive_id: str,
    upload_id: str,
    lease_id: str,
    actor: Any,
    connection_factory: Any = None,
    on_publish: Any = None,
    lease_seconds: int = RECONCILE_LEASE_SECONDS,
) -> CompletionOutcome:
    """§6 completion steps 2–4 under an OWNED lease.

    Every durable mutation CASes on ``lease_id`` and the lease is renewed
    before each bounded provider call, so a worker that loses the fence
    stops without altering the new owner's state. Deterministic and
    deadline outcomes are RETURNED, not terminalized here — the caller
    commits the terminal transition atomically with its own bookkeeping
    (the route stores the replayable wire result in the same transaction;
    the reconciler terminalizes alone). ``on_publish(c, fresh_row, result)``
    runs INSIDE the publication transaction.
    """
    from ..storage_transfers import (
        TransferProviderError,
    )

    factory = connection_factory or _default_connection_factory()

    async def _fetch_row() -> Any:
        async with factory() as c:
            return await get_session(c, drive_id=drive_id, upload_id=upload_id)

    try:
        row = await _fetch_row()
        if row["state"] != "completing" or row["transition_lease_id"] != lease_id:
            return CompletionOutcome(kind="lease_lost")

        # ── steps 2–3: verify + adopt (skipped when proof is durable) ──
        if row["adopted_generation"] is None:
            outcome = await _ensure_adoption_fenced(
                storage, factory, row,
                lease_id=lease_id, lease_seconds=lease_seconds,
            )
            if outcome is not None:
                return outcome
            row = await _fetch_row()

        # ── step 4: one publication transaction ──
        async with factory() as c:
            try:
                async with c.transaction():
                    session = _decoded(
                        await c.fetchrow(
                            f"SELECT {_SESSION_COLUMNS} FROM upload_sessions "
                            "WHERE id = $1 AND drive_id = $2 FOR UPDATE",
                            upload_id, drive_id,
                        )
                    )
                    if (
                        session["state"] != "completing"
                        or session["transition_lease_id"] != lease_id
                    ):
                        raise LeaseLostError("lost the completion fence")
                    # Lock-THEN-check ordering (mirrors the buffered
                    # producers): the drive namespace advisory lock is held
                    # BEFORE the liveness/authorization reads, so a
                    # concurrent delete_drive cannot commit in the gap
                    # between the reauthorization SELECT and publication.
                    from .v0_drives import _lock_drive_namespace

                    await _lock_drive_namespace(c, drive_id)
                    await _reauthorize_publication(c, actor, session)
                    if session["target_kind"] == "artifact":
                        result = await publish_completed_artifact(
                            c, actor=actor, session=session, lease_id=lease_id
                        )
                    else:
                        result = await publish_completed_version(
                            c, actor=actor, session=session, lease_id=lease_id
                        )
                    fresh = await get_session(
                        c, drive_id=drive_id, upload_id=upload_id
                    )
                    if on_publish is not None:
                        await on_publish(c, fresh, result)
                return CompletionOutcome(kind="published", result=result)
            except UploadExpiredError:
                # The in-transaction terminalization rolled back with the
                # aborted publication; the CALLER re-terminalizes atomically
                # with its stored 422.
                return CompletionOutcome(kind="expired")
            except _CompletionUnauthorizedError:
                # Post-adoption authorization miss: terminal rejection with
                # quarantine, surfaced anti-enumerating (§6/§8).
                return CompletionOutcome(
                    kind="reject", failure_code=None,
                    cleanup_state="quarantined", anti_enumerate=True,
                )
            except DestinationGoneError:
                return CompletionOutcome(
                    kind="reject", failure_code=None,
                    cleanup_state="quarantined", anti_enumerate=True,
                )
            except UploadHeadChangedError:
                return CompletionOutcome(
                    kind="reject", failure_code="PRECONDITION_FAILED",
                    cleanup_state="quarantined",
                )
            except Exception as exc:
                from .v0_artifacts import ArtifactNameConflictError

                if isinstance(exc, ArtifactNameConflictError):
                    return CompletionOutcome(
                        kind="reject", failure_code="NAME_CONFLICT",
                        cleanup_state="quarantined",
                    )
                raise
    except LeaseLostError:
        return CompletionOutcome(kind="lease_lost")
    except InvalidUploadTransitionError:
        # The session left `completing` under us (terminal or reopened):
        # classify from a fresh read like any lost fence.
        return CompletionOutcome(kind="lease_lost")
    except TransferProviderError:
        # Transient or ambiguous PROVIDER work only (§7): the durable
        # completing action and lease stay for reconciliation. A plain code
        # bug propagates — it must never masquerade as provider weather.
        return CompletionOutcome(kind="unavailable")


async def _reauthorize_publication(c: Any, actor: Any, session: Any) -> None:
    """Publication-time local reauthorization (§6 step 4/§8): workspace,
    principal binding, and current editor capability — every miss is the
    anti-enumerating unauthorized outcome."""
    from . import v0_authz as authz

    # Drive liveness is part of publication reauthorization: content is
    # never committed into a soft-deleted drive (its purge queue must not
    # acquire rows after the delete). The CALLER holds the drive namespace
    # advisory lock before invoking this check (lock-then-check), so it
    # serializes with delete_drive.
    workspace = await c.fetchval(
        "SELECT workspace_id FROM drives WHERE id = $1 AND deleted_at IS NULL",
        session["drive_id"],
    )
    if workspace is None or workspace != actor.workspace_id:
        raise _CompletionUnauthorizedError("drive is not visible")
    if (
        session["principal_id"] != actor.subject
        or session["principal_type"] != actor.subject_type
    ):
        raise _CompletionUnauthorizedError("session principal mismatch")
    try:
        if session["target_kind"] == "artifact":
            await authz.require(
                c, actor=actor, drive_id=session["drive_id"],
                resource_type="folder", resource_id=session["parent_folder_id"],
                minimum="editor", include_deleted=True,
            )
        else:
            await authz.require(
                c, actor=actor, drive_id=session["drive_id"],
                resource_type="artifact", resource_id=session["artifact_id"],
                minimum="editor", include_deleted=True,
            )
    except authz.NotAuthorizedError:
        raise _CompletionUnauthorizedError("local capability revoked") from None


async def _ensure_adoption_fenced(
    storage: Any, factory: Any, row: Any, *, lease_id: str, lease_seconds: int
) -> CompletionOutcome | None:
    """§6 steps 2–3: verify the finalized scratch object and adopt it at the
    preselected final key. Returns ``None`` when the adoption proof is
    durably persisted (proceed to publication) or a terminal-ish outcome for
    the caller to commit. Every provider call is preceded by a lease
    renewal; every durable write CASes on the lease."""
    from ..storage_transfers import ObjectGeneration

    upload_id = row["id"]

    async def _renew() -> None:
        async with factory() as c:
            await renew_transition_lease(
                c, upload_id=upload_id, lease_id=lease_id,
                lease_seconds=lease_seconds,
            )

    # A prior attempt may have already rewritten: discover at the ONE
    # preselected final key before touching scratch again.
    if row["observed_scratch_generation"] is not None:
        await _renew()
        observation = await storage.stat_object(row["final_object"])
        if observation is not None:
            verdict = evaluate_destination_observation(row, observation)
            if verdict == "adopt":
                async with factory() as c:
                    await record_adopted_observation(
                        c, upload_id=upload_id, lease_id=lease_id,
                        generation=observation.generation,
                        size=observation.size, crc32c=observation.crc32c,
                        content_type=observation.content_type,
                    )
                return None
            if verdict == "mismatch":
                return CompletionOutcome(
                    kind="reject", failure_code="OBJECT_METADATA_MISMATCH",
                    cleanup_state="quarantined",
                )
            from ..storage_transfers import TransferProviderUnavailableError

            raise TransferProviderUnavailableError("adoption_ambiguous")

    await _renew()
    scratch = await storage.stat_generation(
        row["scratch_object"], row["observed_scratch_generation"]
    )
    if scratch is None:
        # Incomplete, not corrupt (pre-adoption only): the caller returns
        # the session to active and frees the key.
        return CompletionOutcome(kind="incomplete")

    # §6 step 2: fail closed on ANY missing mandatory field, then require
    # exact equality with the durable declaration.
    if (
        scratch.generation is None
        or scratch.size is None
        or scratch.crc32c is None
        or scratch.content_type is None
        or scratch.adoption_marker is None
        or scratch.adoption_marker != row["adoption_marker"]
        or scratch.content_type != row["declared_media_type"]
    ):
        return CompletionOutcome(
            kind="reject", failure_code="OBJECT_METADATA_MISMATCH",
            cleanup_state="pending",
        )
    if scratch.size != row["declared_size_bytes"]:
        return CompletionOutcome(
            kind="reject", failure_code="OBJECT_SIZE_MISMATCH",
            cleanup_state="pending",
        )
    if scratch.crc32c != row["declared_crc32c"]:
        return CompletionOutcome(
            kind="reject", failure_code="CHECKSUM_MISMATCH",
            cleanup_state="pending",
        )

    async with factory() as c:
        await record_scratch_observation(
            c, upload_id=upload_id, lease_id=lease_id,
            generation=scratch.generation, size=scratch.size,
            crc32c=scratch.crc32c,
        )
        row = await get_session(c, drive_id=row["drive_id"], upload_id=upload_id)
        continuation = await c.fetchval(
            "SELECT rewrite_continuation FROM upload_sessions WHERE id = $1",
            upload_id,
        )

    fingerprint = adoption_fingerprint(
        scratch_object=row["scratch_object"],
        scratch_generation=scratch.generation,
        upload_id=upload_id,
    )
    adopted_generation: int | None = None
    for _ in range(MAX_REWRITE_STEPS):
        await _renew()
        step = await storage.rewrite_generation_create_only(
            ObjectGeneration(
                object_name=row["scratch_object"], generation=scratch.generation
            ),
            row["final_object"],
            content_type=row["declared_media_type"],
            adoption_marker=row["adoption_marker"],
            source_fingerprint=fingerprint,
            continuation=continuation,
        )
        if step.done:
            adopted_generation = step.generation
            break
        continuation = step.continuation
        # §6: the continuation commits BEFORE the next provider call, fenced
        # so only the current owner can resume the SAME rewrite.
        async with factory() as c:
            await record_rewrite_continuation(
                c, upload_id=upload_id, lease_id=lease_id,
                continuation=continuation,
            )
    if adopted_generation is None:
        from ..storage_transfers import TransferProviderUnavailableError

        raise TransferProviderUnavailableError("rewrite_unconverged")

    await _renew()
    final = await storage.stat_generation(row["final_object"], adopted_generation)
    if final is None:
        from ..storage_transfers import TransferProviderUnavailableError

        raise TransferProviderUnavailableError("adoption_ambiguous")
    verdict = evaluate_destination_observation(row, final)
    if verdict == "mismatch":
        return CompletionOutcome(
            kind="reject", failure_code="OBJECT_METADATA_MISMATCH",
            cleanup_state="quarantined",
        )
    if verdict == "ambiguous":
        from ..storage_transfers import TransferProviderUnavailableError

        raise TransferProviderUnavailableError("adoption_ambiguous")
    async with factory() as c:
        await record_adopted_observation(
            c, upload_id=upload_id, lease_id=lease_id,
            generation=final.generation, size=final.size,
            crc32c=final.crc32c, content_type=final.content_type,
        )
    return None


async def resume_stale_completion(
    storage: Any,
    *,
    upload_id: str,
    connection_factory: Any = None,
    lease_seconds: int = RECONCILE_LEASE_SECONDS,
) -> str:
    """The executable bounded reconciler for a stale `completing` action
    (§5.5 transient rule / review round 3 blocker): attach to the stale
    lease, resume the exact saved phase, reauthorize the initiating
    principal locally, then publish or terminalize. ONE bounded attempt per
    invocation — the scheduler (GC) provides the retry cadence and its
    alerting counts the failures.

    Returns one of ``published | rejected | expired | returned_to_active |
    unavailable | leased | lost | <state>`` (a terminal/foreign state name
    when there was nothing to do)."""
    factory = connection_factory or _default_connection_factory()

    async with factory() as c:
        try:
            row = await c.fetchrow(
                "SELECT state, transition_action, drive_id FROM upload_sessions "
                "WHERE id = $1",
                upload_id,
            )
            if row is None:
                return "gone"
            if row["state"] != "completing" or row["transition_action"] != "complete":
                return decode_publication_state(row["state"])
            fenced = await acquire_transition(
                c, upload_id=upload_id, action="complete",
                lease_seconds=lease_seconds,
            )
        except UploadBusyError:
            return "leased"
        except (InvalidUploadTransitionError, UploadExpiredError):
            # Terminal already, or the deadline fence fired and terminalized
            # (acquire's own atomic subtransaction on this connection).
            return "expired"

    actor = session_principal(fenced)
    lease_id = fenced["transition_lease_id"]
    outcome = await run_fenced_completion(
        storage, drive_id=fenced["drive_id"], upload_id=upload_id,
        lease_id=lease_id, actor=actor, connection_factory=factory,
        lease_seconds=lease_seconds,
    )
    if outcome.kind == "published":
        return "published"
    if outcome.kind == "incomplete":
        async with factory() as c:
            try:
                await return_to_active(c, upload_id=upload_id, lease_id=lease_id)
            except (LeaseLostError, InvalidUploadTransitionError):
                return "lost"
        return "returned_to_active"
    if outcome.kind == "reject":
        async with factory() as c:
            try:
                async with c.transaction():
                    await reject_session(
                        c, upload_id=upload_id,
                        failure_code=outcome.failure_code,
                        cleanup_state=outcome.cleanup_state or "pending",
                        lease_id=lease_id,
                    )
            except (LeaseLostError, InvalidUploadTransitionError):
                return "lost"
        return "rejected"
    if outcome.kind == "expired":
        async with factory() as c:
            await expire_session(c, upload_id=upload_id)
        return "expired"
    if outcome.kind == "lease_lost":
        return "lost"
    return "unavailable"
