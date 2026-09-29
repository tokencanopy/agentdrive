"""Sheet reads over an artifact's head version (design §5.3, §5.4).

Composes three things that already exist: the artifact read (which carries
authorization and the revision the ETag needs), the head-content coordinates,
and the pure workbook adapter. Nothing here parses bytes itself and nothing
here talks HTTP — the API layer maps these errors onto status codes.

Layer rule: never import ``agentdrive.api``.
"""

from __future__ import annotations

from typing import Any

from .. import storage
from ..config import settings
from ..sheets import cache
from ..sheets.a1 import Rect, cell_count
from ..sheets.workbook import (
    Unparseable,
    Value,
    WorkbookIndex,
    detect_format,
    read_grid,
    read_index,
)
from . import v0_artifacts

# Above this a single read is refused rather than served. Range IS the
# pagination on this surface: the caller chooses the window, and the cap is
# what stops it choosing the whole workbook.



class NotASpreadsheet(ValueError):
    """The artifact exists and is readable, but is not a workbook.

    Deliberately distinct from "missing": answering `404` would claim the
    artifact is gone when the caller can plainly see it in a listing.
    """


class SheetNotFound(ValueError):
    """A named sheet is absent from the workbook."""


class RangeTooLarge(ValueError):
    """The requested rectangle exceeds `MAX_READ_CELLS`."""


class AmbiguousSheet(ValueError):
    """`sheet` was omitted on a workbook with more than one sheet."""


async def _source(c: Any, actor: Any, drive_id: str, artifact_id: str) -> dict[str, Any]:
    """Revision + head-version coordinates for a readable artifact.

    Two calls rather than one bespoke query: `get_artifact` is where
    authorization and the not-found story already live, and duplicating that
    predicate here is how the two drift apart.
    """
    artifact = await v0_artifacts.get_artifact(c, actor, drive_id, artifact_id)
    row = await v0_artifacts.head_content(c, actor, drive_id, artifact_id)
    if row is None:
        raise NotASpreadsheet("artifact has no readable content")
    return {**row, "revision": artifact["revision"], "artifact": artifact}


def _require_spreadsheet(content_type: str | None, name: str) -> str:
    try:
        return detect_format(content_type or "", name)
    except Unparseable as exc:
        raise NotASpreadsheet(str(exc)) from exc


async def _bytes(row: dict[str, Any]) -> bytes:
    return await storage.get(
        row["storage_object"],
        bucket=row["storage_bucket"],
        generation=row["storage_generation"],
    )


async def _version_source(
    c: Any, actor: Any, drive_id: str, artifact_id: str, version_id: str
) -> dict[str, Any]:
    """One version's content row, shaped like `_source`.

    The version-scoped twin of the head reader. It exists because an agent
    whose session lost a race needs to see the bytes it BASED on in order
    to work out what changed — and once the head has moved, the head reader
    can no longer reach them.

    The ETag is the VERSION id, not the artifact revision: a version is
    immutable, so its parsed contents can never change and the strongest
    validator available is the identity of the thing itself.
    """
    row = await v0_artifacts.version_content(c, actor, drive_id, artifact_id, version_id)
    if row is None:
        raise NotASpreadsheet("artifact has no readable content")
    return {**row, "revision": row["version_id"]}


async def read_workbook_index(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    version_id: str | None = None,
) -> tuple[WorkbookIndex, str]:
    """The sheet index plus the validator for the ETag."""
    row = (
        await _version_source(c, actor, drive_id, artifact_id, version_id)
        if version_id is not None
        else await _source(c, actor, drive_id, artifact_id)
    )
    _require_spreadsheet(row["content_type"], row["artifact_name"])
    index = read_index(
        await _bytes(row),
        content_type=row["content_type"] or "",
        name=row["artifact_name"],
    )
    return index, row["revision"]


async def _grid(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    version_id: str | None = None,
) -> tuple[dict, str]:
    row = (
        await _version_source(c, actor, drive_id, artifact_id, version_id)
        if version_id is not None
        else await _source(c, actor, drive_id, artifact_id)
    )
    _require_spreadsheet(row["content_type"], row["artifact_name"])

    # The key is the immutable version id, so a hit can never be stale. The
    # bytes are fetched only on a miss, which is the entire point.
    grid = cache.get(row["version_id"])
    if grid is None:
        grid = cache.store(
            row["version_id"],
            read_grid(
                await _bytes(row),
                content_type=row["content_type"] or "",
                name=row["artifact_name"],
            ),
        )
    return grid, row["revision"]


def resolve_sheet(grid: dict[str, Any], sheet: str | None) -> str:
    """Which sheet a request means.

    Omitting `sheet` is legal only when there is exactly one — always true
    for csv/tsv. On a multi-sheet workbook it is ambiguous, and guessing
    (say, the first sheet) would silently read the wrong data.
    """
    if sheet is not None:
        if sheet not in grid:
            raise SheetNotFound(f"no such sheet: {sheet!r}")
        return sheet
    if len(grid) == 1:
        return next(iter(grid))
    raise AmbiguousSheet("sheet is required when the workbook has several")


def slice_grid(rows: list[list[Value]], rect: Rect) -> list[list[Value]]:
    """The requested rectangle, padded with `None` outside the used range.

    Always exactly the shape asked for. Google's Sheets API omits empty
    trailing rows and columns, which makes every client handle ragged
    arrays; returning the rectangle verbatim is the simpler contract.
    """
    if cell_count(rect) > settings.sheet_max_read_cells:
        raise RangeTooLarge(
            f"range covers {cell_count(rect)} cells; the limit is {settings.sheet_max_read_cells}"
        )
    out: list[list[Value]] = []
    for r in range(rect.row0, rect.row1 + 1):
        source = rows[r] if r < len(rows) else []
        out.append(
            [source[c] if c < len(source) else None for c in range(rect.col0, rect.col1 + 1)]
        )
    return out


async def read_cells(
    c: Any,
    actor: Any,
    drive_id: str,
    artifact_id: str,
    *,
    sheet: str | None,
    rect: Rect,
    version_id: str | None = None,
) -> dict[str, Any]:
    """One rectangle of committed values, from the head or a named version."""
    grid, revision = await _grid(
        c, actor, drive_id, artifact_id, version_id=version_id
    )
    name = resolve_sheet(grid, sheet)
    return {
        "sheet": name,
        "values": slice_grid(grid[name], rect),
        "revision": revision,
    }
