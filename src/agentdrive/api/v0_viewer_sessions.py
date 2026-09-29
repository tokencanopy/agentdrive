"""Viewer-sessions vertical: 1 operation (2026-08-09 private-viewer design).

``POST /v0/drives/{drive_id}/artifacts/{artifact_id}/viewer-sessions`` mints
a short-lived credential the console hands to the isolated viewer iframe.
Like the entire `/v0` REST surface, it is marked **beta** during private beta.

Wire semantics follow the other verticals with one deliberate deviation:
the success status is **200, not 201 + Location**. A viewer session is token
issuance (the Hub ``/v0/product-tokens`` precedent), not addressable-resource
creation — there is intentionally NO read/list/revoke surface for sessions,
so a ``Location`` would name a URL that answers nothing. ``Idempotency-Key``
is still required (every v0 mutation takes one), and the idempotency ledger
stores the response WITHOUT the plaintext credential, exactly as share
create/rotate strip their secret.

The mint requires the full §7.1 intersection: ``content:read`` on the token
AND a live local ``viewer`` grant on the artifact — both enforced by
``require_local`` before the handler runs, on replays too.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from ..config import settings
from ..core import idempotency, ids, v0_drives
from ..core import v0_viewer_sessions as core
from ..db import conn
from ..identity.actor import V0ActorContext
from .v0_authz import require_local
from .v0_deps import known_params, v0_actor
from .v0_errors import V0ApiError
from .v0_models import ViewerSessionCreateOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["viewer-sessions"],
    dependencies=[Depends(enforce_v0_rate_limit)],
)


class ViewerSessionCreateIn(BaseModel):
    """POST /v0/drives/{id}/artifacts/{id}/viewer-sessions body.

    ``version_id`` omitted (or null) pins the artifact's CURRENT head at
    mint time; the session never follows the head afterwards.
    """

    version_id: str | None = Field(default=None, pattern=r"^ver_[a-f0-9]{16}$")
    model_config = ConfigDict(extra="forbid")


def _body_hash(model: BaseModel | None) -> str:
    payload = model.model_dump(mode="json") if model is not None else {}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _mapping_error(exc: Exception) -> V0ApiError:
    if isinstance(exc, v0_drives.DriveNotFoundError):
        return V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive in this workspace")
    if isinstance(exc, core.ViewerVersionNotFoundError):
        return V0ApiError(404, "VERSION_NOT_FOUND", str(exc))
    if isinstance(exc, core.ViewerSessionNotFoundError):
        return V0ApiError(404, "ARTIFACT_NOT_FOUND", str(exc))
    raise exc


async def _run_mutation(
    actor: V0ActorContext,
    *,
    key: str | None,
    method: str,
    path: str,
    request_hash: str,
    execute: Any,
    store_body: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    from contextlib import suppress

    if not key:
        raise V0ApiError(
            400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required"
        )
    outcome = await idempotency.claim(
        principal_id=actor.subject,
        key=key,
        method=method,
        path=path,
        request_hash=request_hash,
    )
    if outcome.state == "replayed":
        stored = outcome.stored
        return (stored.status, stored.headers, stored.body)
    if outcome.state == "conflict":
        raise V0ApiError(
            409,
            "IDEMPOTENCY_CONFLICT",
            "idempotency key was already used for a different request",
        )
    if outcome.state == "in_flight":
        raise V0ApiError(
            409,
            "IDEMPOTENCY_IN_PROGRESS",
            "idempotency key is already being processed; retry",
            headers={"Retry-After": "5"},
        )
    owner_id = outcome.owner_id
    assert owner_id is not None

    async with conn() as c:
        try:
            async with c.transaction():
                status, headers, body = await execute(c)
                ledger_body = store_body(body) if store_body else body
                await idempotency.complete(
                    c, owner_id=owner_id, status=status, body=ledger_body,
                    headers=headers,
                )
        except Exception:
            with suppress(Exception):
                await idempotency.abandon(c, owner_id=owner_id)
            raise
    return (status, headers, body)


@router.post(
    "/drives/{drive_id}/artifacts/{artifact_id}/viewer-sessions",
    status_code=200,
    response_model=ViewerSessionCreateOut,
    operation_id="viewer_sessions_create",
    dependencies=[
        # The full §7.1 intersection, composed: content:read on the token AND
        # a live local viewer grant on the artifact. Re-runs on idempotent
        # replays, so a revoked grant refuses the stored result too.
        #
        # `include_public=False` matches what resolution enforces. Without
        # it the two halves disagree: a caller whose only access is a
        # `public` grant could mint (the default admits public grants),
        # while `resolve_credential` re-checks the principal's OWN standing
        # and would refuse the credential on first use — a 200 handing back
        # something that can never work. Strictness belongs on both sides of
        # a credential's life, and a published artifact is readable through
        # the permalink publishing already created.
        Depends(
            require_local(
                "viewer",
                "artifact",
                "artifact_id",
                scope="content:read",
                include_public=False,
            )
        ),
        Depends(known_params()),
    ],
)
async def create_viewer_session(
    drive_id: str,
    artifact_id: str,
    body: ViewerSessionCreateIn,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    response: Response = ...,
) -> ViewerSessionCreateOut:
    """Mint a viewer session pinned to one immutable version. The response
    carries the plaintext credential — the only response that does — and is
    never cacheable."""
    # Fail closed while no viewer host is bound. An unbound deployment has
    # nowhere a credential can be redeemed (`HostSurfaceMiddleware` serves
    # `/view/` only when `viewer_base_url` is set), so a 200 here would hand
    # back a real, short-lived credential that works nowhere. Same posture as
    # the transfer surface's TRANSFER_DISABLED: operator enablement, no
    # fallback, and no Retry-After (enablement has no honest client retry
    # time). Placed after the route's authz dependencies — a caller without
    # the scope/grant intersection still gets its 403/404 — and BEFORE the
    # idempotency claim, so a refused key stays reusable once the operator
    # binds the host.
    if not settings.viewer_base_url:
        raise V0ApiError(
            503, "VIEWER_DISABLED",
            "the private viewer is not enabled on this deployment",
        )
    if not ids.is_valid(drive_id, "drv") or not ids.is_valid(artifact_id, "art"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed id")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.create_session(
                c, actor, drive_id, artifact_id,
                version_id=body.version_id,
                ttl_seconds=settings.viewer_session_ttl_seconds,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (200, {"Cache-Control": "no-store"}, result)

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/artifacts/{artifact_id}/viewer-sessions",
        request_hash=_body_hash(body),
        execute=execute,
        store_body=lambda b: {k: v for k, v in b.items() if k != "credential"},
    )
    response.status_code = status
    response.headers.update(headers)
    return payload
