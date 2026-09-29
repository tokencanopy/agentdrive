"""Dump exact production and staging OpenAPI 3.1 contract snapshots.

Used by `test_openapi_snapshot.py` as the contract baseline. Devs
re-run this script after intentional API changes:

    uv run python -m agentdrive.scripts.dump_openapi

``openapi.golden.json`` is the fail-closed production contract.
``openapi.staging.golden.json`` is the feature-on staging contract. The
script launches one clean interpreter per profile because route mounting and
the active manifest are intentionally decided once, at process startup.

Why a script (not just a test fixture):
  * Devs can regenerate without running pytest.
  * The script normalizes for stability: the spec is dumped with
    sorted keys + 2-space indent so a key reorder inside FastAPI's
    introspection doesn't churn the diff.
  * The `version` field is replaced with the literal "<PINNED>"
    before write, so bumping the project version in pyproject.toml
    doesn't force a snapshot regen for unrelated PRs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
GOLDEN_PATH = REPO_ROOT / "tests" / "openapi.golden.json"
STAGING_GOLDEN_PATH = REPO_ROOT / "tests" / "openapi.staging.golden.json"

# Sentinel substituted for `info.version` before writing. Mirrors the
# value the test substitutes before comparison; the two must match.
VERSION_SENTINEL = "<PINNED>"

# Sentinel substituted for the whole `servers` block before writing —
# see `normalize()`. Kept as a list so the snapshot's JSON type for the
# key is unchanged.
SERVERS_SENTINEL = ["<DEPLOYMENT-DERIVED>"]


def normalize(spec: dict) -> dict:
    """Replace volatile fields with sentinels so the snapshot stays
    stable across builds. Today: `info.version` and `servers`. Add
    more as drift surfaces (e.g., `info.x-build-sha` if we ever start
    emitting one).

    `servers` is deployment-derived (`app._openapi_servers()` reads
    API_BASE_URL falling back to PUBLIC_BASE_URL, the same derivation
    as JWT `iss`/`aud`) so that a staging spec never hands an SDK
    generator production's host. That makes the block a function of
    the dumping process's env, not of the API contract — so it is
    normalized out here rather than committed. The derivation itself
    is covered by `tests/test_openapi_snapshot.py`.
    """
    spec = dict(spec)
    info = dict(spec.get("info") or {})
    if "version" in info:
        info["version"] = VERSION_SENTINEL
    spec["info"] = info
    if "servers" in spec:
        spec["servers"] = SERVERS_SENTINEL
    return spec


def _write_current_process(output: Path) -> None:
    # Import inside main so an import-time crash in the app module
    # (e.g., bad env at script-run time) surfaces with a clear stack
    # rather than at module load.
    from agentdrive.app import app

    spec = normalize(app.openapi())
    output.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths = len(spec.get("paths", {}))
    operations = sum(len(v) for v in spec.get("paths", {}).values())
    try:
        display_path = output.relative_to(REPO_ROOT)
    except ValueError:
        display_path = output
    print(
        f"wrote {display_path} "
        f"({paths} paths, {operations} operations, version={VERSION_SENTINEL})"
    )


def _write_profile(*, enabled: bool, output: Path) -> None:
    env = os.environ.copy()
    env["SHEET_SESSIONS_ENABLED"] = "true" if enabled else "false"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "agentdrive.scripts.dump_openapi",
            "--current-process-output",
            str(output),
        ],
        check=True,
        env=env,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--current-process-output",
        type=Path,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.current_process_output is not None:
        _write_current_process(args.current_process_output)
        return

    _write_profile(enabled=False, output=GOLDEN_PATH)
    _write_profile(enabled=True, output=STAGING_GOLDEN_PATH)


if __name__ == "__main__":
    sys.exit(main() or 0)
