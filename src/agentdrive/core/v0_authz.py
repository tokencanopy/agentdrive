"""Local-capability authorization: the single authority primitive (§7.1, §8).

Authorization on the reset surface is the intersection of TWO halves:

  1. **Token scope** (`v0_actor.can`) — what the Hub-issued token permits,
     enforced at the route layer (`api/v0_authz.require_scope`).
  2. **Local capability** (this module) — what the principal holds in the
     `grants` table on a specific drive, folder, or artifact, resolved by
     `effective_role` below.

This module is the one place role resolution lives. The local half is a
grant row UNION one ambient rule: the **workspace-admin overlay** (contract
§8, ratified 2026-08-28) — a human actor whose verified token carries
`workspace_role` owner or admin holds implicit `manager` on every drive in
their own workspace, grants or no grants. It exists for visibility (owners
and admins see every drive their workspace pays for, including drives
created by members or agents) and closes the orphan-drive gap (a departed
creator's permanent grant no longer strands a drive). Members stay
grant-only, and agents NEVER receive the overlay — `workspace_role` is a
human-only claim the token verifier rejects on an agent token. The overlay
grants LOCAL capability only; token scope checks are unchanged and still
intersect. Inheritance is ADDITIVE-ONLY: a drive grant is authoritative
throughout the drive, a folder
grant applies down that folder's whole ancestry, and a direct artifact grant
applies to that artifact; `workspace` and `public` principals are handled.
If a principal can see a folder, they can see everything under it — there is
no subtree boundary that subtracts reach (folder sealing was removed;
restricting a subtree means not granting the ancestor). Every surface that
asks "may this principal act here?" — grants, shares, folder/artifact writes,
and a future read-visibility layer — calls `effective_role` / `has_role`
rather than re-implementing the resolution.

The module also answers the AGGREGATE form of the same question —
`artifact_visibility`, "who else can reach this artifact?", the
`effective_visibility` field on `artifacts_read`. It is here, next to
`effective_role` and `visibility_lateral`, so the reachability walk has
exactly one definition; a second grant-walker living in `v0_artifacts` would
be free to disagree with the one that actually gates access.
"""

from __future__ import annotations

from typing import Any

from .v0_drives import DriveNotFoundError

# Role → numeric level for comparison (viewer < editor < manager).
ROLE_LEVEL = {"viewer": 1, "editor": 2, "manager": 3}
RESOURCE_TYPES = ("drive", "folder", "artifact")


class NotAuthorizedError(LookupError):
    """The principal has no local capability on the resource.

    Treated as 404 at the boundary (as-if-absent) so a probe reveals nothing
    about the resource's existence.
    """


def workspace_admin_overlay(actor: Any) -> bool:
    """Does this actor carry the workspace-admin manager overlay AT ALL?

    True only for an actor shape that exposes a truthy ``is_workspace_admin``
    — `V0ActorContext` built from a verified human token whose
    `workspace_role` is owner or admin, or a stored-principal snapshot that
    recorded that standing at mint time. Every other actor shape (agents,
    the anonymous public principal, snapshots without the property) gets
    False via the ``getattr`` default, so the overlay fails closed.

    This answers only "is the actor an administrator of THEIR workspace";
    the caller must still pin the overlay to a drive in that same workspace
    (``effective_role`` compares the drive's ``workspace_id``; list queries
    are already workspace-filtered before they consult the flag).
    """
    return bool(getattr(actor, "is_workspace_admin", False))


async def _ensure_drive(c: Any, drive_id: str, *, include_deleted: bool = False) -> str:
    """The drive must exist (404 otherwise); returns its ``workspace_id``.

    By default it must be live; ``include_deleted=True`` admits a soft-deleted
    drive so a manager can restore it (the drive's manager grant survives
    soft-delete, and the route's core logic judges lifecycle rules).

    Workspace scoping is a ROUTE-boundary concern (the drive must be in the
    caller's workspace), not a property of GRANT resolution — a `public`
    grant is workspace-agnostic. The returned ``workspace_id`` exists for
    the one rule that IS workspace-shaped: the workspace-admin overlay,
    which `effective_role` pins to drives in the actor's own workspace.
    """
    workspace_id = await c.fetchval(
        "SELECT workspace_id FROM drives WHERE id=$1"
        + ("" if include_deleted else " AND deleted_at IS NULL"),
        drive_id,
    )
    if workspace_id is None:
        raise DriveNotFoundError(drive_id)
    return workspace_id


async def effective_role(
    c: Any,
    *,
    actor: Any,
    drive_id: str,
    resource_type: str,
    resource_id: str,
    include_deleted: bool = False,
    include_public: bool = True,
) -> str | None:
    """The highest role `actor` holds on `resource` in `drive`, or None.

    Resolution (contract §8):

      * a `drive` grant applies to the whole drive;
      * a `folder` grant applies down that folder's WHOLE ancestry — every
        descendant folder and artifact, at any depth (additive-only);
      * a direct `artifact` grant applies to that artifact.

    `public` principal caps at viewer (schema-enforced anyway); `workspace`
    grants cover members. A human workspace owner/admin holds `manager` on
    every drive in their own workspace with no grant row at all (the
    workspace-admin overlay, §8). Returns None when the actor holds nothing.
    ``include_deleted`` admits a soft-deleted drive (restore);
    ``include_public`` (default True) admits ``public`` grants — set False for
    surfaces a public share must not reach (e.g. the change feed, §9).
    """
    if resource_type not in RESOURCE_TYPES:
        raise ValueError("resource_type must be drive, folder, or artifact")

    drive_workspace = await _ensure_drive(c, drive_id, include_deleted=include_deleted)

    # Workspace-admin overlay (§8): a human owner/admin holds manager on
    # every drive in THEIR OWN workspace — and transitively on its folders
    # and artifacts, since manager is the ceiling role and inheritance is
    # additive-only. The workspace comparison is load-bearing: the overlay
    # never crosses workspaces, whatever the route layer checked. `manager`
    # is the maximum role, so no grant lookup can improve on it.
    if workspace_admin_overlay(actor) and drive_workspace == actor.workspace_id:
        return "manager"

    if resource_type == "drive":
        return await _drive_role(
            c, actor, drive_id, include_public=include_public,
        )

    drive_level = _role_level(
        await _drive_role(c, actor, drive_id, include_public=include_public)
    )
    if resource_type == "folder":
        folder_level = _role_level(
            await _folder_role(
                c, actor, drive_id, resource_id, include_public=include_public,
            )
        )
        return _level_to_role(_max_level(drive_level, folder_level))

    artifact_level = _role_level(
        await _artifact_role(
            c, actor, drive_id, resource_id, include_public=include_public,
        )
    )
    return _level_to_role(_max_level(drive_level, artifact_level))


async def has_role(
    c: Any,
    *,
    actor: Any,
    drive_id: str,
    resource_type: str,
    resource_id: str,
    minimum: str,
    include_deleted: bool = False,
    include_public: bool = True,
) -> bool:
    """True when `actor` holds at least `minimum` role on the resource."""
    role = await effective_role(
        c, actor=actor, drive_id=drive_id,
        resource_type=resource_type, resource_id=resource_id,
        include_deleted=include_deleted, include_public=include_public,
    )
    return role is not None and ROLE_LEVEL[role] >= ROLE_LEVEL[minimum]


async def require(
    c: Any,
    *,
    actor: Any,
    drive_id: str,
    resource_type: str,
    resource_id: str,
    minimum: str,
    include_deleted: bool = False,
    include_public: bool = True,
) -> None:
    """Raise `NotAuthorizedError` unless `actor` holds at least `minimum`.

    The boundary maps `NotAuthorizedError` to a 404 so the resource's
    existence is not disclosed to a caller without local capability.
    """
    if not await has_role(
        c, actor=actor, drive_id=drive_id,
        resource_type=resource_type, resource_id=resource_id, minimum=minimum,
        include_deleted=include_deleted, include_public=include_public,
    ):
        raise NotAuthorizedError("not authorized on this resource")


async def _drive_role(
    c: Any, actor: Any, drive_id: str, *, include_public: bool = True
) -> str | None:
    """Role from a drive-level grant (the whole drive)."""
    public_filter = "" if include_public else " AND principal_type <> 'public'"
    row = await c.fetchrow(
        "SELECT role FROM grants "
        "WHERE drive_id=$1 AND resource_type='drive' AND resource_id=$2 "
        "AND (_principal_matches($3, $4, $5, principal_type, principal_id)) "
        "AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > clock_timestamp()) "
        + public_filter + " "
        "ORDER BY CASE role WHEN 'manager' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END DESC "
        "LIMIT 1",
        drive_id, drive_id, actor.subject_type, actor.subject, actor.workspace_id,
    )
    return row["role"] if row else None


async def _folder_role(
    c: Any,
    actor: Any,
    drive_id: str,
    folder_id: str,
    *,
    include_public: bool = True,
) -> str | None:
    """Role from grants anywhere on the folder's ancestry. Thin wrapper over
    the shared ancestry-level SQL."""
    level = await _folder_level_only(
        c, drive_id, folder_id,
        actor.subject_type, actor.subject, actor.workspace_id,
        include_public=include_public,
    )
    return _level_to_role(level)


async def _artifact_role(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    include_public: bool = True,
) -> str | None:
    """Role from a direct artifact grant OR the parent folder's ancestry."""
    public_filter = "" if include_public else " AND principal_type <> 'public'"
    direct = await c.fetchval(
        "SELECT max(CASE role WHEN 'manager' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END) "
        "FROM grants "
        "WHERE drive_id=$1 AND resource_type='artifact' AND resource_id=$2 "
        "AND (_principal_matches($3, $4, $5, principal_type, principal_id)) "
        "AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > clock_timestamp()) "
        + public_filter,
        drive_id, artifact_id, actor.subject_type, actor.subject, actor.workspace_id,
    )
    parent_id = await c.fetchval(
        "SELECT parent_id FROM artifacts WHERE drive_id=$1 AND id=$2",
        drive_id, artifact_id,
    )
    parent_level = None
    if parent_id is not None:
        parent_level = await _folder_level_only(
            c, drive_id, parent_id,
            actor.subject_type, actor.subject, actor.workspace_id,
            include_public=include_public,
        )
    level = _max_level(direct, parent_level)
    return _level_to_role(level) if level is not None else None


async def _folder_level_only(
    c: Any, drive_id: str, folder_id: str,
    principal_type: str, principal_id: str, workspace_id: str,
    *,
    include_public: bool = True,
) -> int | None:
    """Numeric role level from the folder's ancestry (shared SQL body with
    `_folder_role`, minus the drive-grant path — drive grants are folded in by
    the artifact's direct-vs-parent max).

    Every ancestor counts: a grant anywhere above the folder applies, at any
    depth. Nothing subtracts reach on the way down."""
    public_filter = (
        " AND g.principal_type <> 'public'" if not include_public else ""
    )
    row = await c.fetchrow(
        """
        WITH RECURSIVE ancestry AS (
          SELECT f.id, f.parent_id
            FROM folders f
           WHERE f.drive_id = $1 AND f.id = $2
          UNION ALL
          SELECT parent.id, parent.parent_id
            FROM folders parent
            JOIN ancestry ON parent.id = ancestry.parent_id
           WHERE parent.drive_id = $1
        )
        SELECT max(
                 CASE g.role WHEN 'manager' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END
               ) AS level
          FROM grants g
          JOIN ancestry a
            ON g.resource_type = 'folder' AND g.resource_id = a.id
         WHERE g.drive_id = $1
           AND g.revoked_at IS NULL
           AND (g.expires_at IS NULL OR g.expires_at > clock_timestamp())
           AND (_principal_matches($3, $4, $5, g.principal_type, g.principal_id))
           """ + public_filter + """
        """,
        drive_id, folder_id, principal_type, principal_id, workspace_id,
    )
    return row["level"] if row else None


def _level_to_role(level: int | None) -> str | None:
    if level is None:
        return None
    if level >= 3:
        return "manager"
    if level == 2:
        return "editor"
    return "viewer"


def visibility_lateral(
    row_alias: str,
    *,
    start_parent_expr: str | None = None,
    include_direct_artifact: bool = False,
    principal_type_param: int,
    principal_id_param: int,
    workspace_param: int,
    overlay_param: int | None = None,
) -> str:
    """A ``JOIN LATERAL`` fragment that computes a row's grant visibility.

    Mirrors ``effective_role``'s resolution as a per-row predicate so list
    queries filter to rows the actor can see (the same rule search enforces):

      * a drive grant makes every row visible;
      * a folder grant makes that folder's WHOLE subtree visible, at any
        depth (additive-only inheritance);
      * ``include_direct_artifact`` additionally admits a direct grant on the
        row itself (artifacts).

    ``row_alias`` names the list's base table (``folders``/``artifacts``);
    ``start_parent_expr`` is the SQL expression for the folder whose ancestry
    grants apply (the row's own ``id`` for a folder, its ``parent_id`` for an
    artifact). ``principal_type_param``/``principal_id_param``/``workspace_param``
    are the ``$n`` placeholders for the actor's ``subject_type``/``subject``/
    ``workspace_id``. The caller appends ``ON <alias>_vis.level >= 1``.

    ``overlay_param`` is the ``$n`` placeholder for a boolean
    ``workspace_admin_overlay(actor)`` flag: when true, every row passes —
    mirroring the manager the overlay confers in ``effective_role``, so an
    owner/admin's listing shows exactly what their by-id reads admit. The
    caller must bind it only for a drive already confirmed to be in the
    actor's own workspace (all these list queries check that first), and
    omit it entirely on surfaces the overlay must never reach (the anonymous
    public read path).
    """

    direct_artifact = (
        f"OR g.resource_type = 'artifact' AND g.resource_id = {row_alias}.id "
        if include_direct_artifact
        else ""
    )
    p, pid, ws = f"${principal_type_param}", f"${principal_id_param}", f"${workspace_param}"
    vis_alias = f"{row_alias}_vis"
    overlay_or = f" OR ${overlay_param}::boolean" if overlay_param is not None else ""
    return f"""
        JOIN LATERAL (
          WITH RECURSIVE ancestry AS (
            SELECT parent.id,
                   parent.parent_id
              FROM folders AS parent
             WHERE parent.drive_id = {row_alias}.drive_id
               AND parent.id = {start_parent_expr}
            UNION ALL
            SELECT parent.id,
                   parent.parent_id
              FROM folders AS parent
              JOIN ancestry ON parent.id = ancestry.parent_id
             WHERE parent.drive_id = {row_alias}.drive_id
          )
          SELECT max(
                   CASE g.role WHEN 'manager' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END
                 ) AS role_level
            FROM grants g
            LEFT JOIN ancestry a
              ON g.resource_type = 'folder' AND g.resource_id = a.id
           WHERE g.drive_id = {row_alias}.drive_id
             AND g.revoked_at IS NULL
             AND (g.expires_at IS NULL OR g.expires_at > clock_timestamp())
             AND (_principal_matches({p}, {pid}, {ws}, g.principal_type, g.principal_id))
             AND (
               g.resource_type = 'drive' AND g.resource_id = {row_alias}.drive_id
               {direct_artifact}
               OR a.id IS NOT NULL
             )
         ) AS {vis_alias} ON ({vis_alias}.role_level >= 1{overlay_or})
    """


# Server-computed exposure summary for an artifact (§8, artifacts_read).
#
# The AGGREGATE question ("who else can reach this?"), as opposed to
# `effective_role`'s per-principal question ("may I reach this?"). Both walk
# the SAME reachability rule, which is why this lives here rather than in
# `v0_artifacts`: one grant-walker, one definition, so the badge a client
# renders and the decision the server enforces cannot drift apart.
#
# Reachability, per artifact: a drive-level grant (authoritative throughout
# the drive), a direct artifact grant, or a folder grant ANYWHERE on the
# artifact's ancestry — inheritance is additive-only, so an ancestor grant at
# any depth reaches down. Only LIVE grants count (`revoked_at IS NULL`,
# unexpired at `clock_timestamp()`).
#
# Classification, in order:
#   public  — some reachable grant has principal_type 'public';
#   shared  — some reachable grant names a principal outside the drive's
#             FOUNDING set;
#   private — otherwise.
#
# The founding set is `drives.created_by_principal_id` plus the principals of
# the drive-level manager grants minted WITH the drive. It is not just the
# creator: `v0_drives.create_drive` mints manager for an agent creator AND
# for that agent's sponsoring human in the same transaction, so a brand-new,
# never-shared drive already carries two grants. Comparing against the
# creator alone would report every artifact in every agent-created drive as
# "shared" from the moment it existed — the badge would carry no information.
#
# Founding rows are identified by `grants.created_at <= drives.created_at`
# AND still being live. `now()` is TRANSACTION time, so every row written by
# the drive-creating transaction shares one timestamp and any later grant has
# a strictly greater one — a manager added later is correctly "shared",
# because handing someone administration of the drive is a sharing event.
# The liveness filter is what stops a REMOVED owner from staying exempt: once
# a founder's grant is revoked or expires they leave the founding set, so
# handing them access back to a single artifact reads as "shared" like any
# other outsider. (The timestamp test is not attacker-reachable through the
# API — `create_grant` always uses transaction `now()` — but it is inference
# from a timestamp rather than a recorded fact. If founding membership ever
# needs to survive a restore, an import, or a backwards clock step, record it
# explicitly at drive creation instead of widening this predicate.)
#
# SHARE LINKS COUNT. `shares` is a second, independent read path — possession
# of the secret IS the credential, with no principal behind it — so a walk
# over `grants` alone would report `private` for an artifact anyone holding a
# link can read, which is the one direction this field must never fail in. A
# live share therefore classifies as `public`: reach that is not limited to
# any principal is exactly what `public` denotes here. It stays a separate
# arm below because a share is a raw capability over its target and consults
# no grant at all. Folder shares serve no bytes today (there is no folder
# viewer — `/s/{key}/` returns a marker), so counting them over-reports
# slightly; that is the safe direction, and it means this stays correct on
# the day a folder viewer ships.
#
# Batched over a list of artifact ids so a page costs ONE statement, never
# one lookup per row.
_ARTIFACT_VISIBILITY_SQL = """
WITH RECURSIVE target AS (
  SELECT a.id, a.parent_id
    FROM artifacts a
   WHERE a.drive_id = $1 AND a.id = ANY($2::text[])
),
ancestry AS (
  SELECT t.id AS artifact_id,
         f.id,
         f.parent_id,
         0::integer AS depth
    FROM target t
    JOIN folders f ON f.drive_id = $1 AND f.id = t.parent_id
  UNION ALL
  SELECT anc.artifact_id,
         parent.id,
         parent.parent_id,
         anc.depth + 1
    FROM folders parent
    JOIN ancestry anc ON parent.id = anc.parent_id
   WHERE parent.drive_id = $1
     -- Depth cap: a parent cycle would otherwise spin forever, and this walk
     -- now runs inside artifact WRITE transactions, not just reads. `folders`
     -- mutators forbid creating a cycle; this is the belt to that suspenders,
     -- and the bound is far above any real nesting.
     AND anc.depth < 256
),
founding AS (
  SELECT g.principal_id
    FROM grants g
    JOIN drives d ON d.id = g.drive_id
   WHERE g.drive_id = $1
     AND g.resource_type = 'drive'
     AND g.resource_id = $1
     AND g.role = 'manager'
     AND g.principal_id IS NOT NULL
     AND g.created_at <= d.created_at
     AND g.revoked_at IS NULL
     AND (g.expires_at IS NULL OR g.expires_at > clock_timestamp())
  UNION
  SELECT d.created_by_principal_id
    FROM drives d
   WHERE d.id = $1 AND d.created_by_principal_id IS NOT NULL
),
live_grants AS (
  SELECT g.resource_type, g.resource_id, g.principal_type, g.principal_id
    FROM grants g
   WHERE g.drive_id = $1
     AND g.revoked_at IS NULL
     AND (g.expires_at IS NULL OR g.expires_at > clock_timestamp())
),
reachable AS (
  -- Drive grant: authoritative throughout the drive.
  SELECT t.id AS artifact_id, g.principal_type, g.principal_id
    FROM target t
    JOIN live_grants g
      ON g.resource_type = 'drive' AND g.resource_id = $1
  UNION ALL
  -- Direct artifact grant.
  SELECT t.id, g.principal_type, g.principal_id
    FROM target t
    JOIN live_grants g
      ON g.resource_type = 'artifact' AND g.resource_id = t.id
  UNION ALL
  -- Folder grant anywhere on the ancestry (additive-only inheritance).
  SELECT a.artifact_id, g.principal_type, g.principal_id
    FROM ancestry a
    JOIN live_grants g
      ON g.resource_type = 'folder' AND g.resource_id = a.id
),
-- Share links: anonymous bearer reach (a share consults no grant). A live
-- share on the artifact head, on any of its versions, or on any ancestor
-- folder.
live_shares AS (
  SELECT s.resource_type, s.resource_id
    FROM shares s
   WHERE s.drive_id = $1
     AND s.revoked_at IS NULL
     AND (s.expires_at IS NULL OR s.expires_at > clock_timestamp())
),
shared_by_link AS (
  SELECT t.id AS artifact_id
    FROM target t
    JOIN live_shares s
      ON s.resource_type = 'artifact' AND s.resource_id = t.id
  UNION
  SELECT t.id
    FROM target t
    JOIN artifact_versions v ON v.artifact_id = t.id
    JOIN live_shares s
      ON s.resource_type = 'artifact_version' AND s.resource_id = v.id
  UNION
  SELECT a.artifact_id
    FROM ancestry a
    JOIN live_shares s
      ON s.resource_type = 'folder' AND s.resource_id = a.id
)
SELECT t.id,
       CASE
         WHEN EXISTS (
                SELECT 1 FROM shared_by_link l WHERE l.artifact_id = t.id
              ) THEN 'public'
         WHEN bool_or(r.principal_type = 'public') THEN 'public'
         WHEN bool_or(
                r.artifact_id IS NOT NULL
                AND r.principal_id IS NOT NULL
                AND NOT EXISTS (
                  SELECT 1 FROM founding f
                   WHERE f.principal_id = r.principal_id
                )
              ) THEN 'shared'
         ELSE 'private'
       END AS effective_visibility
  FROM target t
  LEFT JOIN reachable r ON r.artifact_id = t.id
 GROUP BY t.id
"""

VISIBILITY_LEVELS = ("public", "shared", "private")


async def artifact_visibility(
    c: Any, drive_id: str, artifact_ids: list[str]
) -> dict[str, str]:
    """Map each artifact id to ``public`` | ``shared`` | ``private``.

    ONE statement for the whole batch (see ``_ARTIFACT_VISIBILITY_SQL`` for
    the rule). Ids that do not resolve are absent from the mapping; callers
    fall back to ``private``, the safe answer — an artifact whose exposure we
    cannot establish is never advertised as reachable by anyone else.
    """
    if not artifact_ids:
        return {}
    rows = await c.fetch(_ARTIFACT_VISIBILITY_SQL, drive_id, list(artifact_ids))
    return {row["id"]: row["effective_visibility"] for row in rows}


async def artifact_visibility_one(c: Any, drive_id: str, artifact_id: str) -> str:
    """``artifact_visibility`` for a single artifact (defaults to private)."""
    found = await artifact_visibility(c, drive_id, [artifact_id])
    return found.get(artifact_id, "private")


def drive_visibility_exists(
    *,
    drive_id_expr: str,
    principal_type_param: int,
    principal_id_param: int,
    workspace_param: int,
    overlay_param: int | None = None,
) -> str:
    """An ``EXISTS`` fragment: is this drive visible to the actor?

    Mirrors `_drive_role` EXACTLY — a live (unrevoked, unexpired) drive-level
    grant matching the principal via `_principal_matches` (agents by id,
    `workspace` grants by workspace, `public` by anyone). Used by
    `list_drives` so the list shows precisely the drives the same caller's
    `drives_read` would return 200 for — one interpretation of grant
    matching, shared, so the two cannot disagree.

    ``drive_id_expr`` is the SQL expression naming the drive row (e.g.
    ``drives.id``); ``*_param`` are the ``$n`` placeholders for the actor's
    ``subject_type``/``subject``/``workspace_id``.

    ``overlay_param`` binds a boolean ``workspace_admin_overlay(actor)``
    flag that admits every drive — the listing face of the workspace-admin
    overlay, so an owner/admin's `list_drives` shows every drive the same
    caller's `drives_read` would return 200 for. The caller must already
    have workspace-filtered the drive rows to the actor's own workspace
    (`list_drives` selects on ``workspace_id`` first).
    """
    p, pid, ws = f"${principal_type_param}", f"${principal_id_param}", f"${workspace_param}"
    overlay_or = f"${overlay_param}::boolean OR " if overlay_param is not None else ""
    return f"""
        ({overlay_or}EXISTS (
          SELECT 1 FROM grants
           WHERE grants.drive_id = {drive_id_expr}
             AND grants.resource_type = 'drive'
             AND grants.resource_id = {drive_id_expr}
             AND (_principal_matches({p}, {pid}, {ws}, grants.principal_type, grants.principal_id))
             AND grants.revoked_at IS NULL
             AND (grants.expires_at IS NULL OR grants.expires_at > clock_timestamp())
        ))
    """


def _role_level(role: str | None) -> int | None:
    return ROLE_LEVEL[role] if role is not None else None


def _max_level(*levels: int | None) -> int | None:
    present = [lvl for lvl in levels if lvl is not None]
    return max(present) if present else None
