"""Custom Starlette URL convertors for the v1 permalink cutover.

FastAPI's `Path(..., pattern=...)` validates parameter values AFTER
routing — a pattern mismatch surfaces as a 422 instead of falling
through to the next route. Convertors, in contrast, embed their
regex into the route's compiled pattern at registration time; a
non-matching value causes ROUTE mismatch and Starlette tries the
next route. That's what we need for `/v0/artifacts/{art_id}` to
coexist with `/v0/artifacts/{path:path}/meta` under the same prefix.

Both convertors use the wire-protocol-pinned `<prefix>_[a-f0-9]{16}`
shape (see CLAUDE.md — these prefixes are immutable). If the ID
format ever evolves, the regex here MUST update in lockstep with
`ids.new_artifact_id` / `ids.new_folder_id`.

The convertors are registered as a module-import side effect so
loading this module from `app.py` once at startup is enough — no
per-route registration call is needed.
"""

from __future__ import annotations

from starlette.convertors import Convertor, register_url_convertor


class ArtIdConvertor(Convertor[str]):
    """Matches `art_<16 hex>` IDs. Drop-in identity convert — the
    route handler receives the raw string."""

    regex = "art_[a-f0-9]{16}"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


class FldIdConvertor(Convertor[str]):
    """Matches `fld_<16 hex>` IDs (folders+permalinks design §13.2)."""

    regex = "fld_[a-f0-9]{16}"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


class ShsIdConvertor(Convertor[str]):
    """Matches `shs_<16 hex>` sheet edit-session IDs (sheet edit-session
    design §6). Pinned to `core.ids` PREFIXES and the schema CHECK."""

    regex = "shs_[a-f0-9]{16}"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("art_id", ArtIdConvertor())
register_url_convertor("fld_id", FldIdConvertor())
register_url_convertor("shs_id", ShsIdConvertor())
