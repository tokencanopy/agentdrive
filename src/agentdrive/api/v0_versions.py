"""Version vertical's HTTP surface (slice 6): 5 operations.

Versions are the immutable byte trail under an artifact. Appends and
restores require ``If-Match`` on the artifact revision (the artifact rotates
its head on each version change, so the ETag tracks it). Version content
reads follow the same stream-or-307 rule as head content.

Shared handlers (envelope, idempotency, multipart parsing, content boundary)
are imported from ``v0_artifacts`` — one source of truth for the reset
surface's cross-cutting behavior.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response

from ..config import settings
from ..core import ids
from ..core import v0_artifacts as core
from ..db import conn
from ..identity.actor import V0ActorContext
from .cursors import clamp_limit, cursor_int, cursor_str
from .v0_artifacts import (
    _body_hash,
    _bytes_response,
    _check_artifact_id,
    _check_drive_id,
    _check_multipart_content_length,
    _etag,
    _etag_matches,
    _mapping_error,
    _multipart_hash,
    _parse_multipart_create,
    _require_scope,
    _run_mutation,
)
from .v0_authz import require_local
from .v0_cursors import seal as _seal_cursor
from .v0_cursors import unseal as _unseal_cursor
from .v0_deps import known_params, v0_actor
from .v0_errors import V0ApiError
from .v0_models import VersionCreatedOut, VersionListOut, VersionOut
from .v0_rate_limit import enforce_v0_rate_limit

router = APIRouter(
    prefix="/v0", tags=["versions"], dependencies=[Depends(enforce_v0_rate_limit)]
)

_SCOPE_READ = "content:read"
_SCOPE_WRITE = "content:write"


def _check_version_id(version_id: str) -> None:
    if not ids.is_valid(version_id, "ver"):
        raise V0ApiError(400, "INVALID_ARGUMENT", "malformed version id")


def _version_location(drive_id: str, artifact_id: str, version_id: str) -> str:
    origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    return (
        f"{origin}/v0/drives/{drive_id}/artifacts/{artifact_id}/versions/{version_id}"
    )


@router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/versions",
    response_model=VersionListOut,
    operation_id="versions_list",
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
        Depends(known_params("limit", "cursor")),
    ],
)
async def list_versions(
    drive_id: str,
    artifact_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    limit: int | None = None,
    cursor: str | None = None,
    response: Response = ...,
) -> VersionListOut:
    """List the artifact's version trail, newest first (ordinal DESC)."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    page_size = clamp_limit(limit)

    # A versions cursor must not be replayable against another artifact's
    # version list, so the artifact id is the bound context.
    bound = {"artifact_id": artifact_id}
    position = _unseal_cursor("versions", drive_id, cursor, bound=bound)
    after_ordinal = cursor_int(position, "ordinal") if position else None
    after_id = cursor_str(position, "id") if position else None

    async with conn() as c:
        page = await core.list_versions(
            c, actor, drive_id, artifact_id,
            limit=page_size, after_ordinal=after_ordinal, after_id=after_id,
        )
    response.headers["Cache-Control"] = "private"
    return {
        "items": page["items"],
        "next_cursor": _seal_cursor(
            "versions", drive_id, page["next_cursor"], bound=bound
        ),
    }


@router.post(
    "/drives/{drive_id}/artifacts/{artifact_id}/versions",
    status_code=201,
    response_model=VersionCreatedOut,
    operation_id="versions_append",
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def append_version(
    drive_id: str,
    artifact_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> VersionCreatedOut:
    """Append one immutable version and rotate the artifact head."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    _check_multipart_content_length(request)
    fields, content_bytes, part_content_type = await _parse_multipart_create(request)

    ct = fields.get("content_type") or part_content_type or "application/octet-stream"
    sha256 = fields.get("sha256")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.append_version(
                c, actor, drive_id, artifact_id,
                content=content_bytes, content_type=ct, sha256=sha256,
                if_match=if_match,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            201,
            {"ETag": _etag(result["id"]),
             "Location": _version_location(drive_id, artifact_id, result["id"]),
             "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/artifacts/{artifact_id}/versions",
        request_hash=_multipart_hash(
            {"content_type": ct, "sha256": sha256},
            {"content_digest": hashlib.sha256(content_bytes).hexdigest()},
        ),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload


@router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/versions/{version_id}",
    response_model=VersionOut,
    responses={304: {"description": "If-None-Match matched."}},
    operation_id="versions_read",
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def read_version(
    drive_id: str,
    artifact_id: str,
    version_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    response: Response = ...,
) -> VersionOut:
    """Read one immutable version."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    _check_version_id(version_id)
    async with conn() as c:
        try:
            version = await core.get_version(c, actor, drive_id, artifact_id, version_id)
        except Exception as exc:
            raise _mapping_error(exc) from None
    etag = _etag(version["id"])
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private"})
    response.headers.update({"ETag": etag, "Cache-Control": "private"})
    return version


@router.get(
    "/drives/{drive_id}/artifacts/{artifact_id}/versions/{version_id}/content",
    responses={
        304: {"description": "If-None-Match matched."},
        307: {"description": "Redirect to a short-lived signed URL."},
    },
    operation_id="versions_content",
    dependencies=[
        Depends(require_local("viewer", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def read_version_content(
    drive_id: str,
    artifact_id: str,
    version_id: str,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    """Download one version's immutable bytes — stream or 307."""
    _require_scope(actor, _SCOPE_READ)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    _check_version_id(version_id)
    async with conn() as c:
        row = await core.version_content(c, actor, drive_id, artifact_id, version_id)
    if row is None:
        raise V0ApiError(404, "ARTIFACT_NOT_FOUND", "no such resource in this drive")
    etag = _etag(row["version_id"])
    if _etag_matches(if_none_match, etag):
        return Response(
            status_code=304,
            headers={"Cache-Control": "private, max-age=31536000, immutable",
                     "ETag": etag},
        )
    base_headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    }
    return await _bytes_response(
        gcs_object=row["storage_object"],
        gcs_bucket=row["storage_bucket"],
        gcs_generation=row["storage_generation"],
        size_bytes=row["size_bytes"],
        content_type=row["content_type"],
        filename=row["artifact_name"],
        etag=etag,
        base_headers=base_headers,
        actor=actor,
        drive_id=drive_id,
    )


@router.post(
    "/drives/{drive_id}/artifacts/{artifact_id}/versions/{version_id}/restore",
    status_code=201,
    response_model=VersionCreatedOut,
    operation_id="versions_restore",
    dependencies=[
        Depends(require_local("editor", "artifact", "artifact_id")),
        Depends(known_params()),
    ],
)
async def restore_version(
    drive_id: str,
    artifact_id: str,
    version_id: str,
    request: Request,
    actor: Annotated[V0ActorContext, Depends(v0_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    response: Response = ...,
) -> VersionCreatedOut:
    """Restore a historical version as a NEW head version (no byte copy)."""
    _require_scope(actor, _SCOPE_WRITE)
    _check_drive_id(drive_id)
    _check_artifact_id(artifact_id)
    _check_version_id(version_id)
    if (await request.body()).strip():
        raise V0ApiError(400, "INVALID_ARGUMENT", "this endpoint accepts no request body")

    async def execute(c: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
        try:
            result = await core.restore_version(
                c, actor, drive_id, artifact_id, version_id,
                if_match=if_match,
            )
        except Exception as exc:
            raise _mapping_error(exc) from None
        return (
            201,
            {"ETag": _etag(result["id"]),
             "Location": _version_location(drive_id, artifact_id, result["id"]),
             "Cache-Control": "private"},
            result,
        )

    status, headers, payload = await _run_mutation(
        actor,
        key=idempotency_key,
        method="POST",
        path=f"/v0/drives/{drive_id}/artifacts/{artifact_id}/versions/{version_id}/restore",
        request_hash=_body_hash(None),
        execute=execute,
    )
    response.status_code = status
    response.headers.update(headers)
    return payload
