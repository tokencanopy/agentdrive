"""Derive display paths by walking the parent chain.

There is no `path` column: `parent_id` + `name` are authoritative (§4.1), so
a rename is one row and a stored path can never go stale. The cost lands
here, on read, as one recursive CTE — not a round trip per level.

The drive's root folder has a NULL name and contributes nothing, so an
artifact directly in the root renders as "report.md".
"""

from __future__ import annotations

from typing import Any

_FOLDER_CHAIN = """
WITH RECURSIVE chain AS (
  SELECT id, parent_id, name, 0 AS depth
    FROM folders WHERE id = $1
  UNION ALL
  SELECT f.id, f.parent_id, f.name, chain.depth + 1
    FROM folders f JOIN chain ON chain.parent_id = f.id
)
SELECT name FROM chain WHERE name IS NOT NULL ORDER BY depth DESC
"""


async def folder_path(c: Any, folder_id: str) -> str | None:
    """`reports/q3`, or None if the folder does not exist.

    The drive's root folder resolves to the empty string: it is a real folder
    with no segment to contribute.
    """
    exists = await c.fetchval("SELECT 1 FROM folders WHERE id = $1", folder_id)
    if not exists:
        return None
    rows = await c.fetch(_FOLDER_CHAIN, folder_id)
    return "/".join(r["name"] for r in rows)


async def artifact_path(c: Any, artifact_id: str) -> str | None:
    """`reports/q3/analysis.md`, or None if the artifact does not exist."""
    row = await c.fetchrow(
        "SELECT parent_id, name FROM artifacts WHERE id = $1", artifact_id
    )
    if row is None:
        return None
    parent = await folder_path(c, row["parent_id"])
    return f"{parent}/{row['name']}" if parent else row["name"]
