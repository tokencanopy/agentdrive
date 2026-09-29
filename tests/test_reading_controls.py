"""The same safe document markup feeds public and console viewers."""
from agentdrive.rendering.render import MAX_INLINE_VIDEO_BYTES, render_body


def test_markdown_has_rendered_and_escaped_source_views():
    result = render_body(b'# Heading\n<script>alert(1)</script>', 'text/markdown', 'note.md')
    assert result.mode == 'markdown'
    assert 'data-view="source"' in result.html
    assert '<h1>Heading</h1>' in result.html
    assert '<script>' not in result.html


def test_csv_has_row_numbers_and_raw_view_without_interpreting_cells():
    result = render_body(b'name,value\n"<img src=x>",=1+2\nlast,3', 'text/csv', 'rows.csv')
    assert result.mode == 'table'
    assert 'scope="row">1</th>' in result.html
    assert 'scope="row">2</th>' in result.html
    assert 'data-view="source"' in result.html
    assert '<img src=x>' not in result.html
    assert '=1+2' in result.html


def test_audio_is_bounded_and_never_autoplays():
    result = render_body(b'', 'audio/wav', 'clip.wav', size_bytes=100)
    assert result.mode == 'audio'
    assert '<audio ' in result.html and ' controls ' in result.html
    assert 'autoplay' not in result.html
    too_large = render_body(b'', 'audio/wav', 'clip.wav', size_bytes=MAX_INLINE_VIDEO_BYTES + 1)
    assert too_large.mode == 'download'
