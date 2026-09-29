"""Drive-scoped full-text search (slice 9): one operation.

Lexical retrieval over ``artifacts.search_tsv``, ``ts_rank_cd`` for scoring, and
``ts_headline`` over ``content_preview`` producing a bounded snippet. Live
content only — deleted artifacts never match, and folders have no content so
they are never hits.

**Separator normalization (why the query is OR-ed with itself).** The
``search_tsv`` generated column indexes the NAME through
``regexp_replace(name, '[._-]+', ' ', 'g')``, so ``report-q3-final.txt`` is
stored as the separate lexemes ``report``/``q3``/``final``/``txt``. The query
side had no matching step, and ``websearch_to_tsquery`` turns a hyphenated
term into a PHRASE (``'report-q3' <-> 'report' <-> 'q3'``) demanding a
compound lexeme the index deliberately split apart. The effect was that
searching an artifact by its own filename returned nothing while each word in
that filename matched on its own -- the single most likely query a caller
makes, silently empty.

The predicate therefore ORs the raw query with a separator-normalized one
(``tsquery || tsquery`` is OR), and both branches feed ``ts_rank_cd`` and
``ts_headline`` so a hit found through normalization still ranks and
highlights.

Normalization is INTERIOR-ONLY -- ``_normalize_separators`` rewrites a
separator only when alphanumerics sit on both sides. A naive
``regexp_replace(q, '[._-]+', ' ', 'g')`` also eats the leading ``-`` that
``websearch_to_tsquery`` reads as NOT, so the normalized branch would match
the very documents the caller asked to exclude and the OR would hand them
back. Anchoring on both sides leaves ``-term`` untouched. It runs TWICE
because ``regexp_replace`` does not rescan what it consumed, so a run of
single-character segments (``a-b-c-d``) needs a second pass to finish.

**Snippet safety (wire contract).** ``snippet`` is HTML-SAFE. The only markup
it may carry is the server's own ``<mark>``/``</mark>`` highlight pair;
everything else arrives entity-escaped. ``ts_headline`` interpolates those
markers into text taken from ``content_preview`` — i.e. attacker-controlled
artifact bytes — so the raw headline is NOT safe to render. Every hit
therefore passes through ``core.snippets.safe_snippet`` (see
``_hit_document``), which escapes the content and restores only the two
server-emitted tags. Clients may render ``snippet`` as HTML on that promise;
do not remove the wrapper.

**Visibility (the §12 gate).** A hit is returned iff the caller holds a live
grant on it — drive grant, direct artifact grant, or a folder grant anywhere
on its ancestry (additive-only) — resolved with the SAME predicate as
``v0_authz`` (public capped at viewer, revoked/expired excluded). Folded into
the list SQL as a LATERAL so a page is ONE statement, not N+1 role lookups.
A caller with ``content:read`` but no applicable grants simply gets an empty
page — no leak.

**Pagination.** Rank-ordered results have no natural keyset column, so the
cursor is a D14 sealed token (``core.cursors.seal``, kind ``search``) binding
the full filter fingerprint (query included) and anchoring on the last
returned artifact id. A cursor whose anchor has fallen out of the visible set
is rejected rather than silently re-anchoring.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import cursors
from .snippets import safe_snippet
from .timestamps import to_rfc3339
from .v0_authz import workspace_admin_overlay

SEARCH_MODE = "lexical"

# $1 drive_id, $2 actor subject_type, $3 actor subject, $4 actor workspace,
# $5 original query, $6 parent_id, $7 content_type, $8 label,
# $9 updated_after, $10 updated_before.
# Interior separators only: an alphanumeric must sit on BOTH sides, so a
# leading `-` (websearch's NOT operator) survives untouched. Applied twice
# because `regexp_replace` does not rescan what it consumed, so a run of
# single-character segments (`a-b-c-d`) needs the second pass. See the module
# docstring for why the query must mirror the index here.
_SEPARATOR_RE = "([[:alnum:]])[._-]+([[:alnum:]])"
_NORMALIZED_Q = (
    f"regexp_replace(regexp_replace($5, '{_SEPARATOR_RE}', E'\\\\1 \\\\2', 'g'), "
    f"'{_SEPARATOR_RE}', E'\\\\1 \\\\2', 'g')"
)

_SCORED_CTE = f"""
    WITH RECURSIVE subtree AS (
      SELECT folder.id
        FROM folders AS folder
       WHERE folder.drive_id = $1
         AND folder.id = $6
      UNION ALL
      SELECT child.id
        FROM folders AS child
        JOIN subtree ON child.parent_id = subtree.id
       WHERE child.drive_id = $1
    ),
    scored AS (
      SELECT target.id,
             target.parent_id,
             target.name,
             target.head_version_id,
             target.content_type,
             target.content_preview,
             target.labels,
             target.updated_at,
             ts_rank_cd(
               target.search_tsv,
               (websearch_to_tsquery('english', $5)
                || websearch_to_tsquery('english', {_NORMALIZED_Q}))
             ) AS rank,
             ts_headline(
               'english',
               coalesce(target.content_preview, ''),
               (websearch_to_tsquery('english', $5)
                || websearch_to_tsquery('english', {_NORMALIZED_Q})),
               'StartSel=<mark>, StopSel=</mark>, MaxFragments=2, '
               'MaxWords=18, MinWords=5, ShortWord=2'
             ) AS snippet
        FROM artifacts AS target
        JOIN drives AS drive
          ON drive.id = target.drive_id
         AND drive.deleted_at IS NULL
         AND target.deleted_at IS NULL
        JOIN LATERAL (
          WITH RECURSIVE ancestry AS (
            SELECT parent.id,
                   parent.parent_id
              FROM folders AS parent
             WHERE parent.drive_id = target.drive_id
               AND parent.id = target.parent_id
            UNION ALL
            SELECT parent.id,
                   parent.parent_id
              FROM folders AS parent
              JOIN ancestry ON parent.id = ancestry.parent_id
             WHERE parent.drive_id = target.drive_id
          )
          SELECT max(
                   CASE g.role WHEN 'manager' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END
                 ) AS role_level
            FROM grants g
            LEFT JOIN ancestry a
              ON g.resource_type = 'folder' AND g.resource_id = a.id
           WHERE g.drive_id = target.drive_id
             AND g.revoked_at IS NULL
             AND (g.expires_at IS NULL OR g.expires_at > clock_timestamp())
             AND (_principal_matches($2, $3, $4, g.principal_type, g.principal_id))
             AND (
               g.resource_type = 'drive' AND g.resource_id = target.drive_id
               OR g.resource_type = 'artifact' AND g.resource_id = target.id
               OR a.id IS NOT NULL
             )
        ) AS effective
          -- $11: the workspace-admin overlay flag (§8) — a workspace
          -- owner/admin sees every live artifact in the drive, mirroring the
          -- manager `effective_role` confers. `search_authorized` has
          -- already pinned the drive to the actor's own workspace.
          ON (effective.role_level >= 1 OR $11::boolean)
       WHERE target.drive_id = $1
         AND target.parent_id IS NOT NULL
         AND target.name IS NOT NULL
         AND target.search_tsv @@ (websearch_to_tsquery('english', $5)
                || websearch_to_tsquery('english', {_NORMALIZED_Q}))
         AND ($6::text IS NULL OR target.parent_id IN (SELECT id FROM subtree))
         AND ($7::text IS NULL OR target.content_type = ($7::text COLLATE "C"))
         AND ($8::text IS NULL OR target.labels @> ARRAY[$8]::text[])
         AND ($9::timestamptz IS NULL OR target.updated_at >= $9)
         AND ($10::timestamptz IS NULL OR target.updated_at <= $10)
    )
"""

# Continuation predicate for (rank DESC, updated_at DESC, id ASC).
_PAGE_QUERY = (
    _SCORED_CTE
    + """
    ,
    anchor AS (
      SELECT scored.rank, scored.updated_at, scored.id
        FROM scored
       WHERE scored.id = $12
    )
    SELECT scored.*
      FROM scored
     WHERE (
       $12::text IS NULL
       OR scored.rank < (SELECT rank FROM anchor)
       OR (
         scored.rank = (SELECT rank FROM anchor)
         AND scored.updated_at < (SELECT updated_at FROM anchor)
       )
       OR (
         scored.rank = (SELECT rank FROM anchor)
         AND scored.updated_at = (SELECT updated_at FROM anchor)
         AND scored.id > (SELECT id FROM anchor)
       )
     )
     ORDER BY scored.rank DESC, scored.updated_at DESC, scored.id ASC
     LIMIT $13
    """
)

_ANCHOR_QUERY = _SCORED_CTE + "\nSELECT 1 FROM scored WHERE scored.id = $12"


class SearchDriveNotFoundError(LookupError):
    """The drive is absent, deleted, or in another workspace. 404."""


def _hit_document(row: Any, *, drive_id: str) -> dict[str, Any]:
    """One search hit, with the snippet made HTML-safe.

    ``row["snippet"]`` is raw ``ts_headline`` output: the server's
    ``<mark>``/``</mark>`` markers interpolated into ARTIFACT CONTENT, which
    is attacker-controlled. Handing that to a client verbatim makes every
    consumer that renders search results an XSS sink, and "the client should
    escape it" is not a contract a shared API can rely on. ``safe_snippet``
    escapes the content and restores only the two markers, so the wire
    promise is: the sole markup in ``snippet`` is the highlight pair.
    """
    snippet = safe_snippet(row["snippet"])
    return {
        "id": row["id"],
        "drive_id": drive_id,
        "parent_id": row["parent_id"],
        "name": row["name"],
        "version_id": row["head_version_id"],
        "rank": float(row["rank"] or 0.0),
        "snippet": snippet,
        "content_type": row["content_type"],
        "updated_at": to_rfc3339(row["updated_at"]),
    }


async def search_authorized(
    c: Any,
    actor: Any,
    drive_id: str,
    *,
    q: str,
    limit: int,
    cursor: str | None,
    parent_id: str | None,
    content_type: str | None,
    label: str | None,
    updated_after: datetime | None,
    updated_before: datetime | None,
) -> dict[str, Any]:
    """One page of rank-ordered search hits. Returns ``{"items",
    "next_cursor"}``."""
    if not q:
        raise ValueError("q must be non-empty")

    bound = {
        "q": q,
        "parent_id": parent_id,
        "content_type": content_type,
        "label": label,
        "updated_after": to_rfc3339(updated_after) if updated_after else None,
        "updated_before": to_rfc3339(updated_before) if updated_before else None,
    }
    position = None
    if cursor is not None:
        position = cursors.unseal("search", drive_id, cursor, bound=bound)
    anchor_id = position.get("id") if position else None

    drive_exists = await c.fetchval(
        "SELECT 1 FROM drives WHERE id=$1 AND deleted_at IS NULL",
        drive_id,
    )
    if not drive_exists:
        raise SearchDriveNotFoundError(drive_id)
    drive_workspace = await c.fetchval(
        "SELECT workspace_id FROM drives WHERE id=$1",
        drive_id,
    )
    if drive_workspace != actor.workspace_id:
        raise SearchDriveNotFoundError(drive_id)

    params = (
        drive_id,
        actor.subject_type,
        actor.subject,
        actor.workspace_id,
        q,
        parent_id,
        content_type,
        label,
        updated_after,
        updated_before,
        # Safe here and only here because the drive was just confirmed to be
        # in the actor's own workspace (the overlay never crosses one).
        workspace_admin_overlay(actor),
        anchor_id,
        limit + 1,
    )
    if anchor_id is not None:
        anchor_visible = await c.fetchval(_ANCHOR_QUERY, *params[:12])
        if anchor_visible is None:
            raise cursors.BadCursor("cursor anchor is no longer in the result set")

    rows = await c.fetch(_PAGE_QUERY, *params)
    has_more = len(rows) > limit
    selected = rows[:limit]
    items = [_hit_document(dict(r), drive_id=drive_id) for r in selected]
    next_cursor = None
    if has_more and selected:
        last = selected[-1]
        next_cursor = cursors.seal(
            "search", drive_id,
            {"id": last["id"]},
            bound=bound,
        )
    return {"items": items, "next_cursor": next_cursor}
