"""Shared sealed-cursor helpers for the paginated /v0 lists (§6.3, D14).

Every resource-list endpoint carries its position in a sealed HMAC cursor
(``core.cursors``) bound to the collection kind, the drive (or workspace, for
the drives list), and a normalized filter fingerprint. This module is the ONE
place that maps a ``core.cursors.BadCursor`` to the wire error, so no route
re-implements the mapping and the error code cannot drift between lists.
"""

from __future__ import annotations

from typing import Any

from ..core import cursors as sealed
from .v0_errors import V0ApiError


def unseal(
    kind: str,
    binding: str,
    token: str | None,
    *,
    bound: dict[str, Any],
) -> dict[str, Any] | None:
    """Recover a list cursor's position, or 400 INVALID_CURSOR.

    ``token`` None (no cursor) → None. A malformed / tampered / cross-kind /
    cross-drive / filter-changed token raises ``BadCursor`` in
    ``core.cursors``; this maps it to the uniform wire code so a caller can
    never tell WHICH part mismatched (the §7.1 no-leak rule).
    """
    if token is None:
        return None
    try:
        return sealed.unseal(kind, binding, token, bound=bound)
    except sealed.BadCursor:
        raise V0ApiError(
            400, "INVALID_CURSOR", "the cursor is not valid for this request"
        ) from None


def seal(
    kind: str,
    binding: str,
    position: dict[str, Any] | None,
    *,
    bound: dict[str, Any],
) -> str | None:
    """Mint a sealed cursor for ``position`` (None at the last page)."""
    if position is None:
        return None
    return sealed.seal(kind, binding, position, bound=bound)
