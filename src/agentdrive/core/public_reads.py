"""Permalink reads, gated on a live `public` grant.

`/a/{art_id}`, `/v/{art_id}/{ver_id}` and `/f/{fld_id}` carry no credential:
the id is not a secret, it rides in API responses, logs and UIs. So the grant
is the entire authorization, and these functions are the only place the public
surface asks whether something is published.

**Anti-enumeration is the shape of the interface, not a rule layered on top.**
Every function returns `None` for "no such artifact", "no such version", "no
such folder", "soft-deleted", "drive gone" and "not published" alike, so the
caller has nothing to tell apart and cannot accidentally render a
distinguishable refusal. Every reason collapses before the value leaves this
module.

**One resolution, not two.** Whether a grant covers a resource — directly, or
anywhere up its folder ancestry, or via a drive-wide grant — is
`core.v0_authz`'s job, and it is asked here with an anonymous principal rather
than re-expressed as a second SQL predicate. A private copy would drift: it
would keep serving a grant the authenticated surface has stopped honouring.

**The folder listing is the one place this surface discloses by inclusion.**
`public_artifact` answers a question about a resource the caller already
named; `public_folder` hands back resources the caller did *not* name. So the
listing filter has to be the same resolution the serve path uses, or the page
becomes an oracle over names no grant covers. It is `v0_authz`'s own
`visibility_lateral` — the fragment `list_folders` / `list_artifacts` already
filter with — driven by the same anonymous principal. The invariant that buys:
**every entry in a listing is one `/a/` or `/f/` would answer 200 for**, and
therefore a soft-deleted child is absent from the listing entirely — its name
withheld, not shown-but-unlinked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import paths
from . import v0_authz as authz
from .kinds import chip_label, kind_for
from .v0_drives import DriveNotFoundError
from .version_reads import read_coordinates

# A folder page is a listing, and a listing is unbounded work on an
# unauthenticated route: one `mkdir` plus 100k uploads would otherwise mint a
# page nobody can serve and every crawler will ask for. 500 immediate children
# is far more than a reader scans and far less than a folder can hold; past it
# the page says so and the reader narrows the link.
MAX_LISTING_ENTRIES = 500

# Every artifact-shaped descriptor on the public surface has these columns, in
# the shape `v0_shares.resolve_secret` returns, so `_render_artifact` and
# `_serve_bytes` cannot tell a permalink from a share link. `drive_id` is the
# one extra: authorization needs it, the descriptor does not carry it on.
_COLUMNS = (
    "v.storage_object, v.storage_bucket, v.storage_generation, "
    "v.size_bytes, v.content_type, v.id AS version_id, "
    "a.name, a.id AS artifact_id, a.drive_id, a.updated_at, "
    "(SELECT workspace_id FROM drives WHERE id=a.drive_id) AS workspace_id"
)


@dataclass(frozen=True)
class _Anonymous:
    """The `public` principal, wearing the actor shape `v0_authz` expects.

    `_principal_matches` (schema.sql) tests three disjuncts; with a null
    subject and a null workspace, only `principal_type = 'public' AND
    principal_id IS NULL` can be true — an agent, user or workspace grant
    compares against NULL and yields NULL, which a WHERE clause drops. So this
    principal sees public grants and nothing else, by construction rather than
    by a filter someone must remember to add.
    """

    subject_type: str = "public"
    subject: str | None = None
    workspace_id: str | None = None


ANONYMOUS = _Anonymous()


async def public_artifact(c: Any, artifact_id: str) -> dict[str, Any] | None:
    """The artifact's current head, if a live public grant covers it."""
    row = await c.fetchrow(
        f"SELECT {_COLUMNS} FROM artifacts a "
        "JOIN artifact_versions v ON v.id = a.head_version_id "
        "WHERE a.id = $1 AND a.deleted_at IS NULL",
        artifact_id,
    )
    return await _published_descriptor(c, row)


async def public_version(
    c: Any, artifact_id: str, version_id: str
) -> dict[str, Any] | None:
    """One immutable version, if a live public grant covers its artifact.

    The artifact id is not decoration: a version id alone would let a caller
    who learned one version id read it without knowing which artifact it
    belongs to, and would make `/v/{A}/{V}` serve bytes for an artifact other
    than `A`. Both must match the same row.
    """
    row = await c.fetchrow(
        f"SELECT {_COLUMNS} FROM artifacts a "
        "JOIN artifact_versions v ON v.artifact_id = a.id "
        "WHERE a.id = $1 AND v.id = $2 AND a.deleted_at IS NULL",
        artifact_id,
        version_id,
    )
    return await _published_descriptor(c, row)


async def public_folder(c: Any, folder_id: str) -> dict[str, Any] | None:
    """A folder and the children a public reader may actually reach.

    The folder itself resolves exactly as an artifact does — a live `public`
    grant on it, on any ancestor, or on the drive.
    The children are then filtered by the SAME resolution, per child, so the
    listing can only name rows `/a/` and `/f/` would serve.
    """
    row = await c.fetchrow(
        "SELECT id, name, drive_id FROM folders "
        "WHERE id = $1 AND deleted_at IS NULL",
        folder_id,
    )
    if row is None or not await _is_published(
        c, row["drive_id"], "folder", row["id"]
    ):
        return None
    entries = await _visible_children(c, row["drive_id"], row["id"])
    return {
        "folder_id": row["id"],
        # A drive's root folder is a real folder with a NULL name (§4.1); it is
        # publishable via a drive grant, and "/" is what it is called.
        "name": row["name"] or "/",
        "path": await paths.folder_path(c, row["id"]) or "",
        "entries": entries[:MAX_LISTING_ENTRIES],
        "truncated": len(entries) > MAX_LISTING_ENTRIES,
    }


# The anonymous principal as positional query args, in the order
# `visibility_lateral` names them ($3, $4, $5 below).
_ANON_ARGS = (ANONYMOUS.subject_type, ANONYMOUS.subject, ANONYMOUS.workspace_id)

_VISIBLE_SUBFOLDERS = (
    "SELECT fld.id, fld.name FROM folders AS fld"
    + authz.visibility_lateral(
        "fld",
        start_parent_expr="fld.id",
        principal_type_param=3,
        principal_id_param=4,
        workspace_param=5,
    )
    + " WHERE fld.drive_id = $1 AND fld.parent_id = $2 AND fld.deleted_at IS NULL"
    + ' ORDER BY fld.name COLLATE "C" LIMIT $6'
)

_VISIBLE_ARTIFACTS = (
    "SELECT art.id, art.name, art.content_type, ver.size_bytes"
    " FROM artifacts AS art"
    + authz.visibility_lateral(
        "art",
        start_parent_expr="art.parent_id",
        include_direct_artifact=True,
        principal_type_param=3,
        principal_id_param=4,
        workspace_param=5,
    )
    + " LEFT JOIN artifact_versions AS ver ON ver.id = art.head_version_id"
    + " WHERE art.drive_id = $1 AND art.parent_id = $2 AND art.deleted_at IS NULL"
    + ' ORDER BY art.name COLLATE "C" LIMIT $6'
)


async def _visible_children(
    c: Any, drive_id: str, folder_id: str
) -> list[dict[str, Any]]:
    """The folder's immediate children, filtered to what anonymous may read.

    Two queries rather than N authorization round trips: `visibility_lateral`
    is the same grant resolution `has_role` performs, expressed as a per-row
    predicate — a drive grant, a direct artifact grant, or a grant anywhere on
    the row's folder ancestry. Folders first, then
    artifacts, each name-ordered, because a reader scans containers before
    contents.

    One more row than the cap is fetched from each side so the caller can tell
    "exactly full" from "truncated" without a second count query.
    """
    limit = MAX_LISTING_ENTRIES + 1
    subfolders = await c.fetch(
        _VISIBLE_SUBFOLDERS, drive_id, folder_id, *_ANON_ARGS, limit
    )
    artifacts = await c.fetch(
        _VISIBLE_ARTIFACTS, drive_id, folder_id, *_ANON_ARGS, limit
    )
    return [
        {
            "kind": "folder",
            "name": r["name"],
            "id": r["id"],
            "size_bytes": 0,
            "is_folder": True,
        }
        for r in subfolders
    ] + [
        {
            # `kind_for` and nothing else, exactly as the artifact page does:
            # a second table here would let the chip on the listing disagree
            # with the chip on the page it links to.
            "kind": kind_for(r["content_type"] or "", r["name"]),
            # The listing shows the same label the artifact's own page does —
            # a PDF reading `bundle` here and `pdf` one click later is the
            # kind of small inconsistency that reads as a bug.
            "chip": chip_label(r["content_type"] or "", r["name"]),
            "name": r["name"],
            "id": r["id"],
            "size_bytes": r["size_bytes"] or 0,
            "is_folder": False,
        }
        for r in artifacts
    ]


async def _published_descriptor(c: Any, row: Any) -> dict[str, Any] | None:
    """Row → descriptor, or None if it does not exist or is not published.

    The two failures share one return so no caller can branch on which it was.
    """
    if row is None or not await _is_published(
        c, row["drive_id"], "artifact", row["artifact_id"]
    ):
        return None
    coordinates = read_coordinates(row)
    if coordinates is None:
        return None
    return {
        "kind": "artifact",
        "storage_object": row["storage_object"],
        "storage_bucket": coordinates[0],
        "storage_generation": coordinates[1],
        "size_bytes": row["size_bytes"],
        "content_type": row["content_type"],
        "name": row["name"],
        "etag": row["version_id"],
        "artifact_id": row["artifact_id"],
        "drive_id": row["drive_id"],
        "workspace_id": row["workspace_id"],
        "updated_at": row["updated_at"],
        # §4.1: there is no `path` column, so the display path is derived from
        # the parent chain on read.
        "path": await paths.artifact_path(c, row["artifact_id"]) or row["name"],
    }


async def _is_published(
    c: Any, drive_id: str, resource_type: str, resource_id: str
) -> bool:
    """Does a live `public` grant reach this artifact or folder?

    `viewer` is the only role a public grant can hold (schema-enforced), so
    this is "any live public grant at all" expressed in the vocabulary the
    rest of the codebase uses.
    """
    try:
        return await authz.has_role(
            c,
            actor=ANONYMOUS,
            drive_id=drive_id,
            resource_type=resource_type,
            resource_id=resource_id,
            minimum="viewer",
        )
    except DriveNotFoundError:
        # A soft-deleted (or vanished) drive stops publishing, exactly as it
        # stops a share link. Not an error out here — just "not published".
        return False
