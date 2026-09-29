"""Drift guard for the canonical error-code registry
(`agentdrive.api.error_codes.ERROR_CODES`).

Same mechanical-guard pattern as ``test_soft_delete_filter_discipline``:
a deterministic, DB-free static scan of ``src/agentdrive/**/*.py`` in
both directions:

  (a) EMITTED ⊆ REGISTRY — every error code the source can put on the
      wire must be registered. A new unregistered literal fails with
      its file:line, forcing the author to register it deliberately.
  (b) REGISTRY ⊆ EMITTED — every registered code must still be emitted
      somewhere. Dead registry entries rot the doc; a code that is only
      constructed dynamically must be on ``_DYNAMIC_ONLY`` with a
      comment.

What counts as "emitted" (the public error surfaces):

  * ``{"error": {"code": "X"}}`` envelope literals — REST routes, the
    central handlers in app.py, and the web routes that share the
    envelope. Plus the two frozen legacy lowercase codes.
  * ``SomeError("CODE: message")`` colon-prefixed messages — MCP tool
    errors (``ValueError``) and the core exceptions whose prefix the
    central handlers split into the envelope ``code``
    (``InvalidFilterValue``, the BAD_PRECONDITION ``ValueError``\\ s).
  * ``QueryError("CODE", ...)`` / ``FolderConflict("CODE", ...)`` —
    exceptions that take the code as a bare first argument.
  * ``code = "X"`` class attributes / kwargs — the latex
    ``CompileError`` family and ``ReservedPathError``, re-emitted via
    ``e.code`` at the route boundary.
  * ``_send_error(send, status, "X", ...)`` — the MCP transport's raw
    error body.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentdrive.api.error_codes import ERROR_CODES

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "agentdrive"

_UPPER = r"[A-Z][A-Z0-9_]{2,}"

# Each pattern's group(1) is the candidate code. Patterns run over the
# whole file text (``\s`` spans newlines) so multi-line raises match.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # {"error": {"code": "X", ...}} envelope literals. Uppercase-only:
    # lowercase '"code": "..."' hits are data maps, not error codes
    # (render/shell.py file-type map, embed/client.py task-type map) —
    # except the two frozen legacy codes matched explicitly below.
    ("envelope", re.compile(r'"code":\s*"(' + _UPPER + r')"')),
    ("envelope-legacy",
     re.compile(r'"code":\s*"(drive_required|drive_limit_exceeded)"')),
    # Exception("CODE: message") / f"CODE: {e}". Requires an uppercase
    # token of >=3 chars immediately after an opening paren and before
    # a colon, so prose like "TODO:" outside a call doesn't match.
    ("colon-prefix", re.compile(r'\(\s*f?"(' + _UPPER + r'):')),
    # Exceptions carrying the code as a bare first argument.
    ("first-arg",
     re.compile(r'(?:QueryError|FolderConflict)\(\s*"(' + _UPPER + r')"')),
    # Class attributes / kwargs: code = "X" (latex repo.py, reserved.py).
    ("code-attr", re.compile(r'\bcode\s*=\s*"(' + _UPPER + r')"')),
    # MCP transport raw error body.
    ("send-error",
     re.compile(r'_send_error\(\s*send,\s*\d+,\s*"(' + _UPPER + r')"')),
    # The /v0 surface: V0ApiError(status, "CODE", message) — the code is
    # the second positional argument; the raise is often multi-line.
    ("v0-api-error",
     re.compile(r'V0ApiError\(\s*[^,()]+,\s*"(' + _UPPER + r')"')),
    # Core-layer typed errors that carry the code as a bare first argument
    # and are re-emitted by the routes' _mapping_error / handlers.
    ("v0-first-arg",
     re.compile(r'(?:PreconditionError|ChangeFeedError)\(\s*\d*,?\s*"(' + _UPPER + r')"')),
    # The B3 uploads surface (api/v0_uploads.py) renders/stores envelopes
    # through its own helpers: `_error_response(status, "CODE", ...)`,
    # `_store_error_result(owner_id, status, "CODE", ...)`, and the local
    # `_reject("CODE")` closure — the code is a positional string literal.
    ("v0-upload-error",
     re.compile(r'_error_response\(\s*\d+,\s*"(' + _UPPER + r')"')),
    ("v0-upload-stored-error",
     re.compile(r'_store_error_result\(\s*\w+,\s*\d+,\s*"(' + _UPPER + r')"')),
    ("v0-upload-reject",
     re.compile(r'_reject\(\s*"(' + _UPPER + r')"')),
    # Round-3 refactor: deterministic completion outcomes are typed core
    # values the route maps to the envelope — `CompletionOutcome(
    # kind="reject", failure_code="CODE", ...)` — and the route builds
    # terminal envelopes via `_error_body("CODE", ...)`.
    ("v0-upload-outcome",
     re.compile(r'kind="reject",\s*failure_code="(' + _UPPER + r')"')),
    ("v0-upload-error-body",
     re.compile(r'_error_body\(\s*"(' + _UPPER + r')"')),
]

# Genuine prose that the colon-prefix pattern would otherwise flag.
# Keep SMALL — every entry is a token, with the reason it's prose.
_PROSE_EXCLUSIONS: frozenset[str] = frozenset({
    # Comments/docstrings write the tool-error grammar as a literal
    # placeholder: `ValueError ("CODE: message")` — mcp_server/tools.py,
    # query/service.py. Not an emitted code.
    "CODE",
})

# Registered codes that ARE emitted, but only via a dynamically built
# string the static scan cannot see. Each entry needs a comment saying
# where. Guarded by its own test below so entries can't go stale.
_DYNAMIC_ONLY: frozenset[str] = frozenset({
    # app.py's Starlette 404/405 handler picks the code in a conditional
    # expression ('"NOT_FOUND" if exc.status_code == 404 else
    # "METHOD_NOT_ALLOWED"') — the envelope pattern only sees the first
    # literal.
    "METHOD_NOT_ALLOWED",
})


def _scan_emitted() -> dict[str, list[str]]:
    """One walk of src/agentdrive: code → ['path:line', ...]."""
    emitted: dict[str, list[str]] = {}
    for path in sorted(SRC_DIR.rglob("*.py")):
        text = path.read_text()
        rel = str(path.relative_to(REPO_ROOT))
        for _name, rx in _PATTERNS:
            for m in rx.finditer(text):
                code = m.group(1)
                if code in _PROSE_EXCLUSIONS:
                    continue
                line = text.count("\n", 0, m.start()) + 1
                emitted.setdefault(code, []).append(f"{rel}:{line}")
    return emitted


def test_every_emitted_code_is_registered():
    """(a) EMITTED ⊆ REGISTRY."""
    emitted = _scan_emitted()
    unregistered = {c: sites for c, sites in emitted.items()
                    if c not in ERROR_CODES}
    if unregistered:
        lines = [
            f"  {code}  (emitted at {', '.join(sites[:3])}"
            + (f" +{len(sites) - 3} more)" if len(sites) > 3 else ")")
            for code, sites in sorted(unregistered.items())
        ]
        raise AssertionError(
            "Error code(s) emitted in source but missing from the "
            "canonical registry (src/agentdrive/api/error_codes.py):\n"
            + "\n".join(lines)
            + "\n\nAdding a public error code is deliberate: add it to "
            "ERROR_CODES under the right family. If the match is prose "
            "(a comment/docstring), extend _PROSE_EXCLUSIONS with a "
            "reason instead."
        )


def test_every_registered_code_is_emitted():
    """(b) REGISTRY ⊆ EMITTED — dead registry entries rot the doc.

    A code the source can no longer put on the wire must be dropped from
    the registry (a breaking change — grep consumers first) or, if it is
    emitted through a dynamically built string, listed on
    ``_DYNAMIC_ONLY`` with a comment saying where."""
    emitted = set(_scan_emitted())
    dead = ERROR_CODES - emitted - _DYNAMIC_ONLY
    assert not dead, (
        "Registered error code(s) no longer emitted anywhere in "
        "src/agentdrive:\n  " + "\n  ".join(sorted(dead))
        + "\n\nEither the emitter moved/was removed (drop the registry "
        "entry — breaking change, grep consumers first) or the code is "
        "built dynamically (add it to _DYNAMIC_ONLY with a comment)."
    )


def test_dynamic_allowlist_is_sound():
    """_DYNAMIC_ONLY entries must be registered, must NOT be statically
    scannable (else the entry is stale — drop it), and their literal
    must still exist somewhere in the tree (else the feature is gone)."""
    assert _DYNAMIC_ONLY <= ERROR_CODES, (
        "_DYNAMIC_ONLY contains unregistered codes: "
        f"{sorted(_DYNAMIC_ONLY - ERROR_CODES)}"
    )
    emitted = set(_scan_emitted())
    stale = _DYNAMIC_ONLY & emitted
    assert not stale, (
        f"_DYNAMIC_ONLY entries now found by the static scan — remove "
        f"them from the allowlist: {sorted(stale)}"
    )
    for code in _DYNAMIC_ONLY:
        # Exclude the registry module itself: its own entry would make
        # this check self-satisfying forever.
        hits = [p for p in sorted(SRC_DIR.rglob("*.py"))
                if p.name != "error_codes.py" and f'"{code}"' in p.read_text()]
        assert hits, (
            f"_DYNAMIC_ONLY code {code!r} no longer appears as a string "
            "literal anywhere in src/agentdrive (outside the registry) — "
            "the dynamic emitter was removed; drop the registry entry "
            "(breaking change: grep consumers first)."
        )
