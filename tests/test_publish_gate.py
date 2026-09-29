"""The publish gate (scripts/check_public_snapshot.py, open-source design §4.7).

Each rule is proven by planting one instance of the thing it refuses and one
instance of the nearest thing it must allow, so a rule that silently stops
matching — or starts matching too much — fails here, not in a published tree.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "check_public_snapshot.py"
_spec = importlib.util.spec_from_file_location("check_public_snapshot", GATE)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# Assembled from pieces so this test file cannot trip the gate itself.
PROJECT = "mnexa" + "-agentdrive"
OPS = "private" + "-ops"
CONTENT_HOST = "public." + "tokencanopyusercontent.com"
MNEXA = "@" + "mnexa.ai"
TC = "@" + "tokencanopy.com"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
PROJECT_NUMBER = "1234567" + "89012"
AGENT = "@" + "agents.e2a.dev"


def _findings(tmp_path: Path, files: dict[str, str]) -> list[str]:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return gate.scan(tmp_path)


@pytest.mark.parametrize(
    "text, label",
    [
        (f"write to ops{MNEXA}", "company address (@mnexa.ai)"),
        (f"write to someone{TC}", "company address (@tokencanopy.com)"),
        (f"gcloud --project {PROJECT}-staging", "production GCP project"),
        (f"see ~/{OPS}/notes", "private operations system"),
        (f"projects/{PROJECT_NUMBER}/secrets/x", "GCP project number"),
        (f"{PROJECT_NUMBER}-compute@developer.gserviceaccount.com", "service-agent address"),
        (PEM, "key material"),
        (f"https://{CONTENT_HOST}/x", "production content host"),
        (f"reply to bot{AGENT}", "agent address (@agents.e2a.dev)"),
        # Case, escapes and encodings do not hide a match.
        (f"write to OPS{MNEXA.upper()}", "company address (@mnexa.ai)"),
        (f"--project {PROJECT.upper()}", "production GCP project"),
        (f"see ~/{OPS.replace('-', '_')}/notes", "private operations system"),
        (f"mailto:ops{MNEXA.replace('@', '%40')}", "company address (@mnexa.ai)"),
        ('"ops' + MNEXA.replace("@", "\\u0040") + '"', "company address (@mnexa.ai)"),
    ],
)
def test_each_rule_refuses_its_pattern(tmp_path, text, label):
    findings = _findings(tmp_path, {"src/leak.py": f"# {text}\n"})
    assert len(findings) == 1, findings
    assert f": {label}: " in findings[0]
    assert findings[0].startswith("src/leak.py:1:")


def test_the_disclosure_address_is_the_only_allowed_company_address(tmp_path):
    assert _findings(tmp_path, {"SECURITY.md": f"Email security{TC}.\n"}) == []
    assert len(_findings(tmp_path, {"SECURITY.md": f"Email support{TC}.\n"})) == 1


def test_content_hosts_are_tolerated_in_docs_and_tests_only(tmp_path):
    files = {
        "docs/contract.md": f"renders on {CONTENT_HOST}\n",
        "tests/test_x.py": f"HOST = '{CONTENT_HOST}'\n",
    }
    assert _findings(tmp_path, files) == []
    assert len(_findings(tmp_path, {"README.md": f"{CONTENT_HOST}\n"})) == 1


def test_env_files_terraform_state_and_stray_files_are_refused(tmp_path):
    refused = [".env", ".env.local", "prod.env", ".envrc", "terraform.tfstate",
               "terraform.tfstate.backup", "prod.tfvars", ".DS_Store"]
    files = {name: "X=1\n" for name in refused}
    files[".env.example"] = "X=\n"
    files[".terraform/providers/x"] = "x\n"
    findings = _findings(tmp_path, files)
    for name in refused:
        assert any(f.startswith(f"{name}:") for f in findings), name
    assert not any(f.startswith(".env.example") for f in findings)
    assert any(f.startswith(".terraform:") for f in findings)


def test_binary_and_undecodable_files_are_still_read(tmp_path):
    """A string in an image's metadata or a non-UTF-8 file is still text."""
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00ops" + MNEXA.encode() + b"\x00\xff")
    findings = gate.scan(tmp_path)
    assert len(findings) == 1 and findings[0].startswith("blob.bin:1:"), findings


def _png(tmp_path: Path, rel: str, payload: bytes = b"pixels") -> str:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + payload)
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_image_must_be_listed_with_its_reviewed_hash(tmp_path):
    digest = _png(tmp_path, "assets/shot.png")
    findings = gate.scan(tmp_path)
    assert len(findings) == 1 and "image not in" in findings[0], findings

    manifest = tmp_path / "scripts" / "reviewed-images.txt"
    manifest.parent.mkdir()
    manifest.write_text(f"# reviewed\n{digest}  assets/shot.png\n")
    assert gate.scan(tmp_path) == []

    # Changed pixels under the same name are a new, unreviewed image.
    _png(tmp_path, "assets/shot.png", b"other pixels")
    assert len(gate.scan(tmp_path)) == 1


def _git(tmp_path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)


def test_in_a_checkout_only_tracked_files_count_and_committed_caches_are_refused(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("clean\n")
    # Untracked local environments are not the published tree.
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "leak.py").write_text(f"x{MNEXA}\n")
    (tmp_path / "scratch.txt").write_text(f"x{MNEXA}\n")
    _git(tmp_path, "add", "README.md")
    assert gate.scan(tmp_path) == []

    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("clean\n")
    _git(tmp_path, "add", "-f", "node_modules")
    assert gate.scan(tmp_path) == ["node_modules: forbidden directory in a published tree"]


def test_the_shipped_image_manifest_matches_the_shipped_images():
    """Every image in this tree is listed with its current bytes."""
    app = GATE.parent.parent
    reviewed = gate._reviewed_images(app)
    import hashlib

    for rel, digest in reviewed.items():
        assert (app / rel).exists(), f"{rel} is listed but missing"
        assert hashlib.sha256((app / rel).read_bytes()).hexdigest() == digest, rel


def test_the_gate_and_this_test_do_not_match_their_own_rules(tmp_path):
    """Both ship in the tree the gate scans, so both must be clean by
    construction."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / GATE.name).write_bytes(GATE.read_bytes())
    (tmp_path / "scripts" / "test_publish_gate.py").write_bytes(Path(__file__).read_bytes())
    assert gate.scan(tmp_path) == []


def test_the_command_line_exit_status(tmp_path):
    clean = subprocess.run([sys.executable, str(GATE), str(tmp_path)], capture_output=True)
    assert clean.returncode == 0
    (tmp_path / "leak.txt").write_text(f"x{MNEXA}\n")
    dirty = subprocess.run([sys.executable, str(GATE), str(tmp_path)], capture_output=True)
    assert dirty.returncode == 1
