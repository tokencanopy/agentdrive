"""Unit tests for the search-snippet HTML escape (XSS safety)."""

from agentdrive.core.snippets import safe_snippet


def test_preserves_mark_tags():
    assert safe_snippet("<mark>foo</mark>") == "<mark>foo</mark>"


def test_escapes_other_html():
    out = safe_snippet("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;/script&gt;" in out


def test_preserves_mark_around_html():
    out = safe_snippet('foo <mark>bar</mark> <img onerror="x">')
    assert "<mark>bar</mark>" in out
    assert "&lt;img" in out
    assert "<img" not in out


def test_escapes_ampersands_and_quotes():
    out = safe_snippet('foo & "bar"')
    assert "&amp;" in out
    assert "&quot;" in out


def test_empty_input():
    assert safe_snippet("") == ""
    assert safe_snippet(None) == ""


def test_mark_tag_attempts_in_user_content_are_neutralised():
    """A malicious upload trying to inject its own <mark> wrapper to bypass
    escape gets the inner content escaped along with everything else."""
    out = safe_snippet("<mark>real</mark><mark><script>bad</script></mark>")
    # The first mark pair survives (we treat both as our own — there's no way
    # to distinguish injected ones from ts_headline's). The script inside the
    # second mark is escaped because html.escape runs on the string with mark
    # tags already replaced by sentinels.
    # The key safety property: no raw <script> survives.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
