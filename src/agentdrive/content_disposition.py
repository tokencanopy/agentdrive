"""Safe Content-Disposition values for untrusted Unicode item names."""

from __future__ import annotations

import unicodedata
from typing import Literal
from urllib.parse import quote

_BIDI_CONTROLS = frozenset(
    chr(codepoint)
    for start, end in ((0x202A, 0x202E), (0x2066, 0x2069))
    for codepoint in range(start, end + 1)
)


def build_content_disposition(
    disposition: Literal["inline", "attachment"], filename: str
) -> str:
    """Build an ASCII fallback plus an RFC 5987 UTF-8 filename.

    The filename is defense-in-depth input: discard path context and
    header-breaking or display-direction controls even though canonical item
    names reject them at the write boundary. Valid Unicode format characters,
    including ZWJ and ZWNJ, remain part of the UTF-8 filename.
    """
    basename = unicodedata.normalize(
        "NFC", filename.replace("\\", "/").rsplit("/", 1)[-1]
    )
    utf8_name = "".join(
        ch
        for ch in basename
        if unicodedata.category(ch) not in {"Cc", "Cs", "Zl", "Zp"}
        and ch != "\ufeff"
        and ch not in _BIDI_CONTROLS
    )
    if utf8_name.strip(".") == "":
        utf8_name = "download"

    fallback = utf8_name.replace('"', "").encode("ascii", "ignore").decode("ascii")
    if fallback.strip(".") == "":
        fallback = "download"

    return (
        f'{disposition}; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(utf8_name, safe='')}"
    )
