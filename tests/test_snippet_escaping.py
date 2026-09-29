"""`safe_snippet` must escape user HTML and preserve server `<mark>`.

Rehomed from tests/test_mcp.py, which Layer 0 split down to this one test
and a fixture bound to the archived MCP transport. The invariant has
nothing to do with MCP -- it guards core/snippets.py, which survives --
and a load-bearing XSS check should not be parked in a file named for a
subsystem that no longer exists.
"""

from __future__ import annotations


def test_safe_snippet_escapes_user_html_but_preserves_mark():
    """Load-bearing XSS-safety invariant: `safe_snippet` must escape
    user-uploaded HTML while leaving the server-emitted `<mark>` tags
    intact. A future "simplification" of that escape pipeline would
    silently turn snippets into an XSS sink.

    Direct unit test of the function (rather than an end-to-end search)
    because Postgres `ts_headline` happens to strip raw angle brackets
    at headline-construction time — that's incidental, not the
    contract. The contract is: if user HTML reaches `safe_snippet`,
    it gets escaped; if server `<mark>` reaches it, it doesn't."""
    from agentdrive.core.snippets import safe_snippet

    # ts_headline-shaped input: server-emitted <mark> wrapping a term,
    # plus literal user HTML elsewhere in the fragment.
    raw = '<mark>kangaroo</mark> hops past <script>alert(1)</script>'
    out = safe_snippet(raw)
    # User HTML is escaped — raw `<script>` must not survive.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;/script&gt;" in out
    # Server-emitted <mark> tags survive intact (the whole point of
    # the sentinel-swap dance).
    assert "<mark>kangaroo</mark>" in out
    # Empty/None input returns "" — guard against accidental refactor
    # that drops the None branch.
    assert safe_snippet(None) == ""
    assert safe_snippet("") == ""
