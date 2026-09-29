#!/usr/bin/env python3
"""The publish gate: refuse a tree that would leak private data if published.

Open-source design §4.7 (docs/superpowers/specs/2026-09-19-agentdrive-open-source-design.md
in the Token Canopy monorepo). This is the machine-wide public-data boundary
rule made executable: every GitHub surface is public, so nothing that names a
company address, an agent address, a production project, a private operations
system, key material or a production content host may enter the public
repository.

Runs in two places with the same rules:

  * the monorepo's extraction step, over the snapshot it is about to push;
  * the public repository's CI, over every commit and pull request.

    python scripts/check_public_snapshot.py [ROOT]

ROOT defaults to the repository root (the parent of this script's directory).
When ROOT is the top of a git checkout the gate scans exactly the files git
tracks — so a local `.venv` or `node_modules` is ignored, but a COMMITTED one
is refused. Otherwise it scans every file under ROOT.

Every file is scanned, binaries included: text is decoded leniently, so a
string embedded in a PNG's metadata or a PDF is still read. What no pattern can
read is pixels, so every image must be listed, with its SHA-256, in
scripts/reviewed-images.txt — adding or changing an image is a deliberate,
reviewed edit to that file, never a silent one.

Exit 0 when clean, 1 with one line per finding otherwise. Stdlib only.
Additions to the rule list go through review, like any other contract change.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

_I = re.IGNORECASE

# Each rule: a name, a pattern, and whether matches under docs/ and tests/ are
# tolerated. Only the production content hostnames are tolerated there — the
# contract documents and the rendering tests name those hosts on purpose.
RULES: list[tuple[str, re.Pattern[str], bool]] = [
    ("company address (@mnexa.ai)", re.compile(r"[A-Za-z0-9._%+-]+@mnexa\.ai\b", _I), False),
    (
        "company address (@tokencanopy.com)",
        re.compile(r"[A-Za-z0-9._%+-]+@tokencanopy\.com\b", _I),
        False,
    ),
    # Real agent addresses; synthetic ones use agents.localhost.
    (
        "agent address (@agents.e2a.dev)",
        re.compile(r"[A-Za-z0-9._%+-]+@agents\.e2a\.dev\b", _I),
        False,
    ),
    # These two are assembled from pieces so this file does not match itself.
    ("production GCP project", re.compile("mnexa" + r"[-_]agentdrive", _I), False),
    ("private operations system", re.compile("private" + r"[-_]ops", _I), False),
    ("GCP project number", re.compile(r"projects/[0-9]{9,}", _I), False),
    ("service-agent address", re.compile(r"[0-9]{9,}-[a-z0-9]+@", _I), False),
    ("key material", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", _I), False),
    (
        "production content host",
        re.compile(r"\b[a-z0-9-]+\.(?:staging\.)?tokencanopyusercontent\.com\b", _I),
        True,
    ),
]

# Exact-match allowlist (compared lower-cased). SECURITY.md names the
# disclosure address, as e2a's does; nothing else is admitted.
ALLOWED_MATCHES = {"security@tokencanopy.com"}

# Directory names that must not be published. In a git checkout only TRACKED
# files are seen, so these are refused only when committed; in a plain tree
# (the extraction snapshot) nothing generated should exist at all.
FORBIDDEN_DIRS = {
    ".terraform",
    "node_modules",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".hypothesis",
    ".ruff_cache",
}
TOLERANT_ROOTS = ("docs", "tests")
IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff",
    ".avif", ".heic", ".pdf", ".mp4", ".mov", ".webm",
}
IMAGE_MANIFEST = "scripts/reviewed-images.txt"


def _forbidden_file(name: str) -> str | None:
    lower = name.lower()
    if lower == ".env.example":
        return None
    if lower in {".env", ".envrc"} or lower.startswith(".env.") or lower.endswith(".env"):
        return "environment file (only .env.example may be published)"
    if ".tfstate" in lower or lower.endswith(".tfvars"):
        return "Terraform state or variables"
    if lower == ".ds_store":
        return "stray file"
    return None


def _git_files(root: Path) -> list[Path] | None:
    """Tracked files when ROOT is the top of a git checkout, else None."""
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=True,
        ).stdout.decode().strip()
        if Path(top).resolve() != root.resolve():
            return None
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
        ).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        return None
    return [Path(p) for p in out.split("\0") if p]


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        rel_dir = Path(dirpath).relative_to(root)
        files.extend(rel_dir / name for name in sorted(filenames))
    return files


def _reviewed_images(root: Path) -> dict[str, str]:
    manifest = root / IMAGE_MANIFEST
    if not manifest.exists():
        return {}
    reviewed: dict[str, str] = {}
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, path = line.partition("  ")
        reviewed[path.strip()] = digest.strip().lower()
    return reviewed


def _variants(line: str) -> list[str]:
    """The line as written, plus URL- and escape-decoded readings of it."""
    decoded = unquote(line).replace("\\u0040", "@").replace("\\x40", "@").replace("&#64;", "@")
    return [line] if decoded == line else [line, decoded]


def scan(root: Path) -> list[str]:
    root = root.resolve()
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)
    reviewed = _reviewed_images(root)
    findings: list[str] = []
    refused_dirs: set[Path] = set()
    for rel in files:
        for i, part in enumerate(rel.parts[:-1]):
            if part in FORBIDDEN_DIRS:
                bad = Path(*rel.parts[: i + 1])
                if bad not in refused_dirs:
                    refused_dirs.add(bad)
                    findings.append(f"{bad}: forbidden directory in a published tree")
                break
        else:
            reason = _forbidden_file(rel.name)
            if reason:
                findings.append(f"{rel}: {reason}")
                continue
            try:
                data = (root / rel).read_bytes()
            except OSError:
                continue
            if rel.suffix.lower() in IMAGE_SUFFIXES:
                digest = hashlib.sha256(data).hexdigest()
                if reviewed.get(rel.as_posix()) != digest:
                    findings.append(
                        f"{rel}: image not in {IMAGE_MANIFEST} with this content "
                        f"(sha256 {digest}); review it by eye, then list it"
                    )
            findings.extend(_scan_text(rel, data))
    return findings


def _scan_text(rel: Path, data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="replace")
    tolerant = bool(rel.parts) and rel.parts[0] in TOLERANT_ROOTS
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        seen: set[tuple[str, str]] = set()
        for variant in _variants(line):
            for label, pattern, docs_ok in RULES:
                if docs_ok and tolerant:
                    continue
                for match in pattern.finditer(variant):
                    hit = match.group(0)
                    if hit.lower() in ALLOWED_MATCHES or (label, hit) in seen:
                        continue
                    seen.add((label, hit))
                    findings.append(f"{rel}:{lineno}: {label}: {hit}")
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    findings = scan(root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"\npublish gate: {len(findings)} finding(s) in {root}", file=sys.stderr)
        return 1
    print(f"publish gate: clean ({root})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
