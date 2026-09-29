"""Route-layer authorization dependencies: the two halves, composed (§7.1).

  * ``require_scope(scope)`` — the TOKEN half: the bearer must carry the
    scope on its Hub-issued token (403 PERMISSION_DENIED otherwise).
  * ``require_local(minimum, resource_type, resource_param)`` — the LOCAL
    half: the caller must hold at least ``minimum`` role on the resource
    named by the route's ``{resource_param}`` path param, per the grants
    table (404 as-if-absent otherwise, so a probe reveals nothing).

Every v0 mutation is gated by BOTH; the module composes them so a route
cannot forget the scope half. ``require_scope`` is also usable alone for
read-only routes that need no local capability.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request

from ..core import ids
from ..core import v0_authz as authz
from ..db import conn
from ..identity.actor import V0ActorContext
from .v0_deps import v0_actor
from .v0_errors import V0ApiError


def require_scope(scope: str) -> Callable:
    """Token half: the bearer's token must carry ``scope`` (403 otherwise)."""

    async def _dep(
        actor: Annotated[V0ActorContext, Depends(v0_actor)],
    ) -> None:
        if not actor.can(scope):
            raise V0ApiError(
                403, "PERMISSION_DENIED", f"the token does not carry the {scope} scope"
            )

    return _dep


def require_any_grant_in_drive() -> Callable:
    """Admit any principal holding a live grant ANYWHERE in the drive.

    The gate for the grant-READING surface. ``require_local("viewer",
    "drive", ...)`` is the wrong shape there: it demands a DRIVE-level grant,
    so a folder-scoped principal — including a folder MANAGER — was refused a
    listing that would only ever have shown them their own access, while
    ``grants_create``'s ``409 GRANT_CONFLICT`` still disclosed that a grant
    existed on a resource they administer. A read surface refusing what a
    write surface reveals is not a boundary.

    Not an open door: a caller holding no grant anywhere in the drive still
    gets the uniform 404, and WHICH ROWS a caller sees is scoped separately
    in ``core.v0_grants`` — drive managers see the whole access graph,
    everyone else sees only the grants that name them.

    A workspace owner/admin passes without a grant row: the workspace-admin
    overlay (§8) makes them a drive manager, and a manager must be able to
    read the access graph they administer. The workspace check above the
    overlay test is what pins it to the actor's own workspace.
    """

    async def _dep(
        request: Request,
        actor: Annotated[V0ActorContext, Depends(v0_actor)],
    ) -> None:
        drive_id = request.path_params["drive_id"]
        if not ids.is_valid(drive_id, "drv"):
            raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")
        async with conn() as c:
            ws = await c.fetchval(
                "SELECT workspace_id FROM drives WHERE id=$1 AND deleted_at IS NULL",
                drive_id,
            )
            if ws is None or ws != actor.workspace_id:
                raise V0ApiError(
                    404, "DRIVE_NOT_FOUND", "no such drive in this workspace"
                )
            if authz.workspace_admin_overlay(actor):
                return
            holds = await c.fetchval(
                "SELECT 1 FROM grants "
                "WHERE drive_id = $1 "
                "AND (_principal_matches($2, $3, $4, principal_type, principal_id)) "
                "AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > clock_timestamp()) "
                "LIMIT 1",
                drive_id, actor.subject_type, actor.subject, actor.workspace_id,
            )
        if not holds:
            raise V0ApiError(404, "NOT_AUTHORIZED", "not authorized on this resource")

    return _dep


def require_local(
    minimum: str,
    resource_type: str,
    resource_param: str,
    *,
    scope: str | None = None,
    include_deleted: bool = False,
    include_public: bool = True,
) -> Callable:
    """Local half (optionally with the token scope, composed).

    Reads ``drive_id`` and ``resource_param`` from the request path params
    and requires the caller to hold at least ``minimum`` role on that
    resource in the drive (404 as-if-absent otherwise). When ``scope`` is
    given, the token scope is enforced first — the full §7.1 intersection.

    Runs on EVERY request, idempotent replays included, so a principal whose
    local grant was revoked after the original request receives the normal
    authorization failure rather than a replayed result (§6.2).
    ``include_deleted=True`` admits a soft-deleted drive (restore).

    ``include_public=False`` requires the caller to hold the role in their
    OWN right, ignoring any `public` grant on the resource. Almost every
    route wants the default: if an artifact is published, a workspace
    caller may certainly read it. It matters where a route mints something
    that outlives the request and is later re-authorized under stricter
    terms — a viewer session re-checks its principal's own standing, so
    admitting a public grant at mint would issue a credential that can
    never resolve.
    """

    async def _dep(
        request: Request,
        actor: Annotated[V0ActorContext, Depends(v0_actor)],
    ) -> None:
        if scope is not None and not actor.can(scope):
            raise V0ApiError(
                403, "PERMISSION_DENIED", f"the token does not carry the {scope} scope"
            )
        drive_id = request.path_params["drive_id"]
        resource_id = request.path_params[resource_param]
        # Malformed ids are 400 (invalid argument), not 404 — a well-formed
        # id on a resource you cannot see is what must read as absent.
        if not ids.is_valid(drive_id, "drv"):
            raise V0ApiError(400, "INVALID_ARGUMENT", "malformed drive id")
        prefix = {"drive": "drv", "folder": "fld", "artifact": "art"}[resource_type]
        if not ids.is_valid(resource_id, prefix):
            raise V0ApiError(
                400, "INVALID_ARGUMENT", f"malformed {resource_type} id"
            )
        try:
            async with conn() as c:
                # A drive in another workspace is absent (§6.1) — a caller
                # without a workspace-scoped drive gets the same 404 as a
                # caller whose drive never existed. The anti-enumeration rule
                # wins over any grant (public included) for cross-workspace
                # tokens; the possession-based /s/{share_key} surface is how
                # public access is exercised, not the authenticated API.
                ws = await c.fetchval(
                    "SELECT workspace_id FROM drives WHERE id=$1", drive_id
                )
                if ws is None or ws != actor.workspace_id:
                    raise V0ApiError(
                        404, "DRIVE_NOT_FOUND", "no such drive in this workspace"
                    )
                await authz.require(
                    c,
                    actor=actor,
                    drive_id=drive_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    minimum=minimum,
                    include_deleted=include_deleted,
                    include_public=include_public,
                )
        except authz.DriveNotFoundError:
            raise V0ApiError(
                404, "DRIVE_NOT_FOUND", "no such drive in this workspace"
            ) from None
        except authz.NotAuthorizedError:
            raise V0ApiError(
                404, "NOT_AUTHORIZED", "not authorized on this resource"
            ) from None

    return _dep
