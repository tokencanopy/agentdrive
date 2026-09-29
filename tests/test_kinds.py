"""One classifier, so the viewer and the console cannot disagree.

content_type is the standard and stays authoritative; kind is derived from it
per request rather than stored, for the same reason paths are derived — a
stored projection goes stale when the classifier improves.
"""
import pytest

from agentdrive.core.kinds import chip_label, kind_for


@pytest.mark.parametrize(
    ("content_type", "name", "expected"),
    [
        ("text/markdown", "a.md", "md"),
        ("text/markdown; charset=utf-8", "a.md", "md"),
        ("text/plain", "notes.md", "md"),          # extension wins for markdown
        ("text/x-python", "f.py", "code"),
        ("application/json", "d.json", "code"),
        ("text/plain", "log.txt", "code"),
        ("image/png", "d.png", "image"),
        ("video/mp4", "clip.mp4", "video"),
        ("text/csv", "rows.csv", "dataset"),
        ("application/vnd.apache.parquet", "rows.parquet", "dataset"),
        ("application/octet-stream", "blob.bin", "bundle"),
        # `pdf` is its own kind now. It used to land in `bundle`, which is the
        # catch-all for archives, so a board deck was labelled "bundle" in the
        # listing, the gallery and the identity panel.
        ("application/pdf", "doc.pdf", "pdf"),
        ("application/octet-stream", "deck.pdf", "pdf"),  # filename is enough
        ("image/png", "chart.pdf", "image"),  # content_type still wins
    ],
)
def test_kind_for(content_type, name, expected):
    assert kind_for(content_type, name) == expected


def test_unknown_type_falls_back_rather_than_raising():
    assert kind_for("", "") == "bundle"


@pytest.mark.parametrize(
    ("content_type", "name", "expected"),
    [
        # A concrete media type settles it — the filename does not get a vote.
        ("image/png", "chart.csv", "image"),
        ("image/png", "notes.md", "image"),
        ("video/mp4", "data.parquet", "video"),
        # ...but a generic declared type lets the filename decide, because
        # uploaders routinely send text/plain (or nothing) for everything.
        ("text/plain", "notes.md", "md"),
        ("text/plain", "rows.csv", "dataset"),
        ("application/x-yaml", "cfg.yaml", "code"),
    ],
)
def test_content_type_wins_over_filename(content_type, name, expected):
    assert kind_for(content_type, name) == expected


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/x-python", "python"),      # not "x-python"
        ("image/svg+xml", "svg"),         # not "svg+xml"
        ("text/plain", "text"),           # not "plain"
        ("application/pdf", "pdf"),       # not the dispatch bucket "bundle"
        ("text/csv", "csv"),
        ("application/x-ndjson", "jsonl"),
        ("application/zip", "bundle"),    # nothing better to say
    ],
)
def test_chip_label_names_the_format_a_reader_recognises(content_type, expected):
    """The chip is display, `kind_for` is dispatch — legacy split them too.

    `x-` prefixes and `+xml` suffixes are media-type bookkeeping. A reader
    looking at a Python file wants "python", not "x-python".
    """
    assert chip_label(content_type, "f") == expected


def test_jsonl_renders_as_code_not_a_download():
    """It is text a reader can read; a download card would be a dead end."""
    assert kind_for("application/x-ndjson", "events.jsonl") == "code"


def test_opendocument_spreadsheets_are_datasets_not_bundles():
    """A ZIP on disk, a spreadsheet to the reader.

    `.ods` reached `bundle` because nothing matched it, so the same table
    saved from LibreOffice instead of Excel was described to the reader as an
    archive of things while its `.xlsx` neighbour read `dataset`.
    """
    ods = "application/vnd.oasis.opendocument.spreadsheet"
    assert kind_for(ods, "book.ods") == "dataset"
    assert kind_for("application/octet-stream", "book.ods") == "dataset"
    assert chip_label(ods, "book.ods") == "ods"


def test_a_plain_archive_is_still_a_bundle():
    """The ODS rule must not turn every ZIP into a spreadsheet."""
    assert kind_for("application/zip", "bundle.zip") == "bundle"


def test_word_documents_get_a_format_chip_a_reader_recognises():
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert chip_label(docx, "report.docx") == "docx"
    assert chip_label("application/msword", "old.doc") == "doc"
def test_the_legacy_parquet_spelling_is_still_a_dataset():
    assert kind_for("application/x-parquet", "rows") == "dataset"
    assert chip_label("application/x-parquet", "rows") == "parquet"


def test_slide_decks_get_a_format_chip_a_reader_recognises():
    pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    assert chip_label(pptx, "deck.pptx") == "pptx"
    assert chip_label("application/vnd.ms-powerpoint", "old.ppt") == "ppt"


def test_yaml_and_xml_text_types_get_their_format_chip():
    assert chip_label("text/yaml", "a") == "yaml"
    assert chip_label("text/x-yaml", "a") == "yaml"
    assert chip_label("text/xml", "a") == "xml"
    assert chip_label("application/atom+xml", "a") == "atom"


def test_xml_dialects_are_code_not_bundles():
    assert kind_for("application/atom+xml", "feed") == "code"
    assert kind_for("application/rss+xml", "feed") == "code"
    assert kind_for("image/svg+xml", "a.svg") == "image"
    assert chip_label("application/rss+xml", "feed") == "rss"
