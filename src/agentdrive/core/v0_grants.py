"""Grants vertical (slice 8): local capability, intersected with token scope.

A grant gives one principal (``agent`` / ``user`` / ``workspace`` / ``public``)
a role (``viewer`` / ``editor`` / ``manager``) on a drive, folder, or artifact.
The schema already enforces the hard parts (§6.8 via §12A):

  * `public` grants are viewer-only and carry no principal id;
  * at most one LIVE grant per (resource, principal) — role changes are
    UPDATE/PATCH, not a second row;
  * grants are drive-scoped: a grant's resource must belong to the same
    drive (``reject_out_of_drive_resource``).

**Authorization model.** A grant operation requires the caller to be a
*manager* on the grant's target resource, resolved by the shared
``core.v0_authz`` primitive (drive grants authoritative throughout the drive;
a human workspace owner/admin is a manager on every drive in their own
workspace via the workspace-admin overlay, §8).
(Day-0 authz elsewhere is scope-only;
grant-resolution into folder/artifact reads is not layered in this slice.)

**Reading grants is narrower than reading the drive.** ``list_grants`` and
``get_grant`` are Google-Drive-shaped: a drive ``manager`` enumerates the
whole access graph; every other caller sees only the grants that name THEM
(``_principal_matches``, the same rule authorization uses). Neither
operation is refused outright — seeing your own access is not a privilege —
but the drive's roster of principals, roles and expiries is manager-only.

**The creator's grant is permanent.** The drive-level manager grant
``create_drive`` mints for ``drives.created_by_principal_id`` cannot be
revoked, demoted, or given an expiry (``GrantPermanentError`` →
``409 GRANT_PERMANENT``); see ``_is_owner_grant`` for why it is the drive's
rule rather than a self-revoke rule. Every live drive therefore has at
least one live manager GRANT — which is not the same as a reachable
manager: a departed creator's permanent grant still counts as live even
though no token will ever exercise it again. The workspace-admin overlay
(§8, `core.v0_authz`) is what closes that gap: a human workspace
owner/admin holds manager on every drive in their workspace with no grant
row at all, so no drive in a workspace with a live owner/admin is ever
unreachable.

**Recovery invariant (break-glass).** While a drive has zero active
(unexpired, unrevoked) manager grants, a workspace owner or admin (human
token, ``is_workspace_admin``) may mint exactly their own drive-level
manager grant, under ``FOR UPDATE`` on the drive row so concurrent admins
serialize to one winner. Under the overlay this path is a RESIDUAL safety
net rather than the answer to anything routine: the same owner/admin
already passes the ordinary manager check via the overlay, so the
break-glass branch below is reached only if the overlay is ever narrowed.
It is kept — and kept correct — precisely for that day.

Scope note: the route layer requires the ``sharing:write`` scope; this
module is the local-capability half.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from . import v0_authz as authz
from . import v0_changes as changes
from .ids import new_id
from .timestamps import to_rfc3339
from .v0_drives import DriveNotFoundError, precondition

ROLES = ("viewer", "editor", "manager")
# Every principal kind a grant ROW may hold. The schema accepts all of these.
PRINCIPAL_TYPES = ("agent", "user", "service", "workspace", "public")

# The kinds the CREATE API accepts. Equal to `PRINCIPAL_TYPES` again as of
# this slice.
#
# `service` was briefly excluded on purpose: the previous slice widened the
# readers and constraints so a Service row could serialize, and letting a
# human manager actually CREATE one before the rules governing that access
# existed would have made "reader-only" untrue. Those rules are here now —
# explicit grants only, no workspace-grant match, and the manager invariant —
# so §7.3's "active Service Accounts are selectable in Drive sharing" is
# expressible.
#
# The two names stay separate rather than collapsing back into one. They
# answer different questions — what a row may hold, versus what a caller may
# ask for — and the next principal kind will want the same staging.
CREATABLE_PRINCIPAL_TYPES = PRINCIPAL_TYPES
RESOURCE_TYPES = ("drive", "folder", "artifact")

_GRANT_COLUMNS = (
    "id, drive_id, resource_type, resource_id, principal_type, principal_id, "
    "role, revision, expires_at, created_at, revoked_at"
)


class GrantNotFoundError(LookupError):
    """The grant does not exist in the drive. Always 404 GRANT_NOT_FOUND."""


class BadGrantError(ValueError):
    """A grant shape or role is invalid. 400 INVALID_ARGUMENT."""


class GrantConflictError(ValueError):
    """A live grant for the same (resource, principal) already exists, or a
    duplicate raced through the checks. 409 GRANT_CONFLICT."""


class GrantNotRevokedError(ValueError):
    """revoke was called on an already-revoked grant. 409 CONFLICT."""


class GrantPermanentError(ValueError):
    """The drive creator's own drive-level manager grant cannot be revoked,
    demoted, or given an expiry. 409 GRANT_PERMANENT."""


_NOT_FOUND_MESSAGE = "no such grant target resource or no manager authority"


def grant_payload(row: Any) -> dict[str, Any]:
    """The wire shape of one grant row."""
    expires_at = row["expires_at"]
    revoked_at = row["revoked_at"]
    return {
        "id": row["id"],
        "drive_id": row["drive_id"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "principal_type": row["principal_type"],
        "principal_id": row["principal_id"],
        "role": row["role"],
        "revision": row["revision"],
        "state": (
            "revoked" if revoked_at is not None
            else "expired" if expires_at is not None and expires_at <= _now()
            else "active"
        ),
        "expires_at": to_rfc3339(expires_at) if expires_at else None,
        "revoked_at": to_rfc3339(revoked_at) if revoked_at else None,
        "created_at": to_rfc3339(row["created_at"]),
    }


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def _require_manager(
    c: Any,
    actor: Any,
    drive_id: str,
    resource_type: str,
    resource_id: str,
) -> None:
    """Raise `GrantNotFoundError` unless `actor` holds manager on the target.

    The ONE place grant-administration authority is checked, shared by the
    first-execution core mutators AND the route's replay guard so the two
    cannot disagree. `GrantNotFoundError` is the uniform 404 the surface uses
    for a denial (no disclosure of whether the resource exists).
    """
    if not await authz.has_role(
        c, actor=actor, drive_id=drive_id,
        resource_type=resource_type, resource_id=resource_id,
        minimum="manager",
    ):
        raise GrantNotFoundError(_NOT_FOUND_MESSAGE)


async def _validate_resource(
    c: Any, drive_id: str, resource_type: str, resource_id: str
) -> None:
    """Confirm the target resource exists in the drive (404 semantics)."""
    if resource_type == "drive":
        if resource_id != drive_id:
            raise DriveNotFoundError(drive_id)
        return
    if resource_type == "folder":
        exists = await c.fetchval(
            "SELECT 1 FROM folders WHERE drive_id=$1 AND id=$2 LIMIT 1",
            drive_id, resource_id,
        )
    else:
        exists = await c.fetchval(
            "SELECT 1 FROM artifacts WHERE drive_id=$1 AND id=$2 LIMIT 1",
            drive_id, resource_id,
        )
    if not exists:
        raise GrantNotFoundError(_NOT_FOUND_MESSAGE)


def _grant_event_data(row: Any, *, previous: Any = None) -> dict[str, Any]:
    """The change ``data`` for a grant event: the GRANTEE principal, role, and
    expiry — the access-graph facts that make a permission event manager-only.
    ``previous`` (the pre-mutation row) adds a ``previous`` block of the
    role/expiry that changed. Grants carry no secret, so none is emitted."""
    data: dict[str, Any] = {
        "grant_id": row["id"],
        "principal_type": row["principal_type"],
        "principal_id": row["principal_id"],
        "role": row["role"],
        "expires_at": to_rfc3339(row["expires_at"]) if row["expires_at"] else None,
    }
    if previous is not None:
        data["previous"] = {
            "role": previous["role"],
            "expires_at": (
                to_rfc3339(previous["expires_at"]) if previous["expires_at"] else None
            ),
        }
    return data


async def append_grant_change(
    c: Any, actor: Any, row: Any, *, type: str, previous: Any = None
) -> None:
    """Append a grant change on the SAME transaction as the mutation (§6.7),
    on the grant's target resource. Lost event ↔ lost mutation is made
    unrepresentable: both commit together or neither does."""
    await changes.append(
        c,
        drive_id=row["drive_id"],
        actor=actor,
        type=type,
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        previous_revision=previous["revision"] if previous is not None else None,
        revision=row["revision"],
        data=_grant_event_data(row, previous=previous),
    )


async def create_grant(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    role: str,
    expires_at: Any,
) -> dict[str, Any]:
    """Grant one principal a role on a drive, folder, or artifact."""
    await _ensure_drive(c, actor, drive_id)
    if role not in ROLES:
        raise BadGrantError("role must be viewer, editor, or manager")
    if principal_type not in CREATABLE_PRINCIPAL_TYPES:
        raise BadGrantError(
            "principal_type must be agent, user, service, workspace, or public"
        )
    if principal_type == "public":
        if principal_id is not None:
            raise BadGrantError("public grants carry no principal_id")
        if role != "viewer":
            raise BadGrantError("public grants are always viewer")
    else:
        if not principal_id:
            raise BadGrantError("principal_id is required for a non-public grant")
    await _validate_resource(c, drive_id, resource_type, resource_id)

    if not await authz.has_role(
        c, actor=actor, drive_id=drive_id,
        resource_type=resource_type, resource_id=resource_id, minimum="manager",
    ):
        recovered = await _try_break_glass(
            c, actor, drive_id,
            principal_type=principal_type, principal_id=principal_id,
            resource_type=resource_type, resource_id=resource_id,
            role=role, expires_at=expires_at,
        )
        if recovered is None:
            raise GrantNotFoundError(_NOT_FOUND_MESSAGE)
        return recovered
    conflicting = await c.fetchrow(
        "SELECT id, expires_at, "
        "       (expires_at IS NOT NULL AND expires_at <= clock_timestamp()) AS expired "
        "FROM grants "
        "WHERE drive_id=$1 AND resource_type=$2 AND resource_id=$3 "
        "AND principal_type=$4 AND principal_id IS NOT DISTINCT FROM $5 "
        "AND revoked_at IS NULL FOR UPDATE",
        drive_id, resource_type, resource_id, principal_type, principal_id,
    )
    if conflicting is not None:
        if not conflicting["expired"]:
            # A genuinely live grant owns the (resource, principal) slot — role
            # changes are PATCH, not a second row (§6.8).
            raise GrantConflictError("an active grant for this principal already exists")
        # The conflicting grant is EXPIRED: authorization already treats it as
        # dead (§8 `expires_at <= clock_timestamp()`), but the partial unique
        # index `grants_one_live_per_principal` keys on `revoked_at IS NULL`
        # only and cannot reference the clock — so a stale, never-revoked
        # expired row would block a re-grant forever. Revoke it in the SAME
        # transaction (freeing the index slot) and fall through to the insert.
        # Emit the revoke too, so the feed stays a faithful ledger — the
        # displaced row leaves as a grant.revoked, not silently, and a manager
        # reconstructing the access graph from the feed sees no phantom.
        swept = await c.fetchrow(
            "UPDATE grants SET revoked_at = now(), revision = $2 WHERE id = $1 "
            "RETURNING " + _GRANT_COLUMNS,
            conflicting["id"], new_id("rev"),
        )
        await append_grant_change(c, actor, swept, type="grant.revoked")

    try:
        row = await c.fetchrow(
            "INSERT INTO grants "
            "(id, drive_id, resource_type, resource_id, principal_type, "
            "principal_id, role, revision, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
            "RETURNING " + _GRANT_COLUMNS,
            new_id("grn"), drive_id, resource_type, resource_id,
            principal_type, principal_id, role, new_id("rev"), expires_at,
        )
    except asyncpg.UniqueViolationError:
        raise GrantConflictError("an active grant for this principal already exists") from None
    await append_grant_change(c, actor, row, type="grant.created")
    return grant_payload(row)


async def _try_break_glass(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    role: str,
    expires_at: Any,
) -> dict[str, Any] | None:
    """Mint the caller's own drive-manager grant at zero active managers.

    Every condition must hold: a human workspace owner or admin
    (``is_workspace_admin`` — both roles, §8), targeting their own
    drive-level manager grant. The drive row is locked FOR UPDATE so two
    concurrent admins serialize; the loser sees the winner's grant and is
    denied. Anything else returns None → ordinary 404 (no disclosure).

    Residual under the workspace-admin overlay: an owner/admin passes
    ``create_grant``'s ordinary manager check via the overlay, so this
    branch no longer fires in the shipped configuration — it stays as the
    recovery floor should the overlay ever be narrowed."""
    if not (
        actor.is_workspace_admin
        and resource_type == "drive"
        and resource_id == drive_id
        and role == "manager"
        and principal_type == "user"
        and principal_id == actor.subject
    ):
        return None
    drive = await c.fetchrow(
        "SELECT id FROM drives WHERE id=$1 AND workspace_id=$2 AND deleted_at IS NULL FOR UPDATE",
        drive_id, actor.workspace_id,
    )
    if drive is None:
        return None
    managers = await c.fetchval(
        "SELECT count(*) FROM grants "
        "WHERE drive_id=$1 AND resource_type='drive' AND resource_id=$1 "
        "AND role='manager' AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > clock_timestamp())",
        drive_id,
    )
    if managers:
        return None
    try:
        row = await c.fetchrow(
            "INSERT INTO grants "
            "(id, drive_id, resource_type, resource_id, principal_type, "
            "principal_id, role, revision, expires_at) "
            "VALUES ($1, $2, 'drive', $3, 'user', $4, 'manager', $5, $6) "
            "RETURNING " + _GRANT_COLUMNS,
            new_id("grn"), drive_id, drive_id, actor.subject, new_id("rev"), expires_at,
        )
    except asyncpg.UniqueViolationError:
        return None
    await append_grant_change(c, actor, row, type="grant.created")
    return grant_payload(row)


async def _ensure_drive(c: Any, actor: Any, drive_id: str) -> None:
    row = await c.fetchrow(
        "SELECT workspace_id FROM drives WHERE id=$1 AND deleted_at IS NULL",
        drive_id,
    )
    if row is None or row["workspace_id"] != actor.workspace_id:
        raise DriveNotFoundError(drive_id)


async def is_drive_manager(c: Any, actor: Any, drive_id: str) -> bool:
    """Does the actor hold ``manager`` on the DRIVE itself?

    The one predicate that decides whether a grant listing enumerates the
    drive's whole access graph or only the caller's own rows. Drive-level,
    not per-resource: a folder manager administers that folder's grants
    (``_require_manager``) but does not get to read every principal in the
    drive.
    """
    return await authz.has_role(
        c, actor=actor, drive_id=drive_id,
        resource_type="drive", resource_id=drive_id, minimum="manager",
    )


async def list_grants(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    state: str,
    limit: int,
    after_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    principal_type: str | None = None,
) -> dict[str, Any]:
    """Explicit-grants-only listing with ``active|revoked|all`` filter and a
    stable (id) keyset anchor. Returns ``{"items", "next_cursor"}``.

    **Enumeration is manager-only.** A drive ``manager`` sees every grant in
    the drive. Everybody else sees ONLY the rows that grant access to THEM —
    matched by the same ``_principal_matches`` rule authorization uses, so a
    ``workspace`` grant covering the caller and a ``public`` grant (which
    already exposes the resource to them) are their own access too. The
    listing is refused only for a caller holding no live grant anywhere in
    the drive (the route's gate) — never for lack of manager, because a
    caller must always be able to see what they hold. Before this, any drive
    viewer could page out every principal id, role, and expiry in the drive —
    the access graph is not a read-only user's business.
    """
    await _ensure_drive(c, actor, drive_id)
    manager = await is_drive_manager(c, actor, drive_id)
    params: list[Any] = [drive_id]
    sql = (
        f"SELECT {_GRANT_COLUMNS} FROM grants WHERE drive_id = $1"
    )
    if not manager:
        params.extend([actor.subject_type, actor.subject, actor.workspace_id])
        sql += (
            " AND (_principal_matches("
            f"${len(params) - 2}, ${len(params) - 1}, ${len(params)}, "
            "principal_type, principal_id))"
        )
        # `active` means redeemable/effective, so it must exclude an expired
        # row as well as a revoked one — the authorization resolver has always
        # applied the same predicate (`v0_authz._folder_level_only`), so
        # without this the listing disagreed with the access it describes.
        # An expired-but-unrevoked row is neither `active` nor `revoked`; it
        # appears under `all`, matching its computed `state`.
    if state == "active":
        sql += (
            " AND revoked_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > clock_timestamp())"
        )
    elif state == "revoked":
        sql += " AND revoked_at IS NOT NULL"
    if resource_type is not None:
        params.append(resource_type)
        sql += " AND resource_type = $" + str(len(params))
    if resource_id is not None:
        params.append(resource_id)
        sql += " AND resource_id = $" + str(len(params))
    if principal_type is not None:
        params.append(principal_type)
        sql += " AND principal_type = $" + str(len(params))
    if after_id is not None:
        params.append(after_id)
        sql += " AND id > $" + str(len(params)) + " COLLATE \"C\""
    params.append(limit + 1)
    sql += " ORDER BY id COLLATE \"C\" LIMIT $" + str(len(params))
    rows = await c.fetch(sql, *params)

    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more and rows:
        next_cursor = {"id": rows[-1]["id"]}
    return {"items": [grant_payload(r) for r in rows], "next_cursor": next_cursor}


async def get_grant(
    c: Any, actor: Any, drive_id: str, grant_id: str
) -> dict[str, Any]:
    """Read one grant in the drive (404 for missing / other drive).

    Carries the SAME manager gate as ``list_grants`` — a non-manager may read
    only a grant that names them. Without this, the listing gate would be
    decoration: ``grants_read`` is the by-id sibling of the same enumeration,
    and "the id is 64 bits of entropy" is an obfuscation argument, not an
    authorization one. A hidden grant reads as absent (404), the surface's
    uniform denial, so the probe does not confirm the id exists.
    """
    await _ensure_drive(c, actor, drive_id)
    # The visibility test is folded INTO the row fetch rather than applied
    # afterwards. Checking a fetched row made "hidden" cost one extra
    # round-trip over "absent", so the two 404s were separable by response
    # time — a measurable existence oracle contradicting the property this
    # docstring claims. One statement on both paths, so timing carries no
    # signal.
    manager = await is_drive_manager(c, actor, drive_id)
    row = await c.fetchrow(
        f"SELECT {_GRANT_COLUMNS} FROM grants "
        "WHERE drive_id = $1 AND id = $2 "
        "AND ($3 OR (_principal_matches($4, $5, $6, principal_type, principal_id)))",
        drive_id, grant_id, manager,
        actor.subject_type, actor.subject, actor.workspace_id,
    )
    if row is None:
        raise GrantNotFoundError(_NOT_FOUND_MESSAGE)
    return grant_payload(row)


_PERMANENT_MESSAGE = (
    "this drive's creator keeps manager access for as long as the drive "
    "exists; delete the drive instead"
)


async def _is_owner_grant(c: Any, drive_id: str, row: Any) -> bool:
    """Is this the grant that makes the drive's creator its manager?

    The one grant v0 will not let go of. A drive's access is grants and
    nothing else — there is no owner column in `grants` — so before this
    the creator's own row was revocable like any other, and revoking it
    (or demoting it, or handing it an expiry) left a drive nobody could
    administer. `list_grants` would still answer for whoever held a
    viewer grant, and the only way back was the break-glass path at the
    top of this module: a workspace administrator minting themselves a
    manager grant on a drive with zero live ones.

    That recovery is a floor, not a design. `drives.created_by_principal_id`
    is immutable server-observed attribution recorded at creation, and
    `create_drive` mints the matching drive-level manager grant in the same
    transaction — so the pair is exactly "the person whose drive this is",
    and it stays true through every later grant change. Pinning it makes
    "every live drive has a live manager" an invariant the API enforces
    rather than a state an administrator can be called on to restore.

    Deliberately NOT the caller's own row: a rule that only stopped you
    revoking YOURSELF still lets a second manager revoke the creator and
    then leave, which is the same orphaned drive by a longer route.

    Scoped to the DRIVE-level grant. A creator's grant on one folder or
    artifact inside the drive is ordinary sharing and revokes normally;
    it is drive-level manager that administration depends on.

    Nullable `created_by_principal_id` (rows written before the column, or
    a creator without a subject) simply has no owner to protect — the
    comparison is false and the grant behaves as it did before.
    """
    if row["resource_type"] != "drive" or row["resource_id"] != drive_id:
        return False
    if row["role"] != "manager" or row["principal_id"] is None:
        return False
    creator = await c.fetchval(
        "SELECT created_by_principal_id FROM drives WHERE id = $1", drive_id
    )
    return creator is not None and creator == row["principal_id"]


async def update_grant(
    c: Any,
    actor: Any,
    drive_id: str,
    grant_id: str,
    *,
    role: str | None,
    expires_at: Any,
    clear_expires_at: bool,
    if_match: str | None,
) -> dict[str, Any]:
    """Change a grant's role or expiry. Requires If-Match on the grant's
    current state (428/412)."""
    await _ensure_drive(c, actor, drive_id)
    row = await c.fetchrow(
        f"SELECT {_GRANT_COLUMNS} FROM grants WHERE drive_id=$1 AND id=$2 FOR UPDATE",
        drive_id, grant_id,
    )
    if row is None:
        raise GrantNotFoundError(_NOT_FOUND_MESSAGE)
    await _require_manager(
        c, actor, drive_id, row["resource_type"], row["resource_id"]
    )
    precondition(if_match, row["revision"])

    target_role = role if role is not None else row["role"]
    if row["principal_type"] == "public" and target_role != "viewer":
        raise BadGrantError("public grants are always viewer")
    if clear_expires_at:
        target_expires = None
    elif expires_at is not None:
        target_expires = expires_at
    else:
        target_expires = row["expires_at"]

    # Demotion and expiry are the two ways an update reaches the same place
    # a revoke would: a drive whose creator can no longer administer it.
    # A no-op re-PATCH of `manager` with no expiry is still allowed, so a
    # client that submits the whole row back is not refused for it.
    if (target_role != "manager" or target_expires is not None) and (
        await _is_owner_grant(c, drive_id, row)
    ):
        raise GrantPermanentError(_PERMANENT_MESSAGE)

    revision = new_id("rev")
    await c.execute(
        "UPDATE grants SET role=$3, expires_at=$4, revision=$5 "
        "WHERE drive_id=$1 AND id=$2",
        drive_id, grant_id, target_role, target_expires, revision,
    )
    updated = await c.fetchrow(
        f"SELECT {_GRANT_COLUMNS} FROM grants WHERE drive_id=$1 AND id=$2",
        drive_id, grant_id,
    )
    await append_grant_change(c, actor, updated, type="grant.updated", previous=row)
    return grant_payload(updated)


async def revoke_grant(
    c: Any,
    actor: Any,
    drive_id: str,
    grant_id: str,
    *,
    if_match: str | None,
) -> dict[str, Any]:
    """Revoke a grant (sets revoked_at). Requires If-Match on the grant's
    current state (428/412)."""
    await _ensure_drive(c, actor, drive_id)
    row = await c.fetchrow(
        f"SELECT {_GRANT_COLUMNS} FROM grants WHERE drive_id=$1 AND id=$2 FOR UPDATE",
        drive_id, grant_id,
    )
    if row is None:
        raise GrantNotFoundError(_NOT_FOUND_MESSAGE)
    await _require_manager(
        c, actor, drive_id, row["resource_type"], row["resource_id"]
    )
    precondition(if_match, row["revision"])
    if row["revoked_at"] is not None:
        raise GrantNotRevokedError("the grant is already revoked")
    if await _is_owner_grant(c, drive_id, row):
        raise GrantPermanentError(_PERMANENT_MESSAGE)

    revision = new_id("rev")
    await c.execute(
        "UPDATE grants SET revoked_at=now(), revision=$3 WHERE drive_id=$1 AND id=$2",
        drive_id, grant_id, revision,
    )
    updated = await c.fetchrow(
        f"SELECT {_GRANT_COLUMNS} FROM grants WHERE drive_id=$1 AND id=$2",
        drive_id, grant_id,
    )
    await append_grant_change(c, actor, updated, type="grant.revoked", previous=row)
    return grant_payload(updated)
