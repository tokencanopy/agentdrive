"""HTML-safe snippet wrapping for `<mark>`-highlighted results.

ts_headline (Postgres) outputs the source text with `<mark>...</mark>`
wrappers injected around matched terms. Source text comes from user
uploads — naive HTML render would be an XSS vector. We stash the marker
tags as NUL sentinels, html-escape everything else, then restore the
markers.

Lives in `core/` because both the artifact-level search (`core.search`)
and the chunk-level retrieval (`core.retrieval`) need exactly this
guard. Keeping two copies in sync was a footgun; the shared function
removes the drift risk.
"""

from __future__ import annotations

import html


def safe_snippet(raw: str | None) -> str:
    """Return an HTML-safe snippet that preserves server-injected
    `<mark>...</mark>` highlights and escapes everything else.

    Returns the empty string for None / empty input — callers can
    pass through ts_headline output without a None-check."""
    if not raw:
        return ""
    s = raw.replace("<mark>", "\x00MO\x00").replace("</mark>", "\x00MC\x00")
    s = html.escape(s)
    return s.replace("\x00MO\x00", "<mark>").replace("\x00MC\x00", "</mark>")
