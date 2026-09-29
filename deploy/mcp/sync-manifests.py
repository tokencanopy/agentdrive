#!/usr/bin/env python3
"""Copy runtime dependencies from the source packages into the image's build
manifests.

The Dockerfile builds the sidecar from `mcp/server/src` and `mcp/auth/src`
(relative to the AgentDrive root, which is the build context) but installs from the
manifests beside this script.
That duplication is deliberate — the packages' own manifests carry eslint,
prettier, vitest and `prebuild` hooks that resolve the root npm workspace,
none of which belong in an image builder — but it means the dependency list
is typed twice, and the two copies have disagreed before: the package moved
to `@tokencanopy/agentdrive-sdk` 0.0.4 while this manifest stayed on 0.0.3,
whose facade sends the pre-rename `lifecycle` query parameter. Every gate
stayed green, because the source was right and the image was not.

So the list is copied, not retyped. ONLY `dependencies` — never
devDependencies (the toolchain is exactly what the duplication exists to keep
out) and never the packaging fields, which are shaped for the image build and
differ from the source on purpose.

    python3 deploy/mcp/sync-manifests.py            # write (from the AgentDrive root)
    python3 deploy/mcp/sync-manifests.py --check    # verify only

After writing, regenerate the lockfile — `npm ci` installs that, so a manifest
change alone reaches nothing:

    cd deploy/mcp && npm install --package-lock-only --ignore-scripts

The contract tests assert both halves, so a drift fails the PR that
introduces it rather than the release that ships it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The AgentDrive root. The source packages moved inside this app with the build
# context (open-source design §4.4), so neither this script nor the image
# reaches outside it any more. The parameter below is still called `root`
# because callers pass a scratch tree in tests.
APP = HERE.parents[1]

def _here(path: Path) -> str:
    """`path` as the person running this script would type it."""
    return os.path.relpath(path)


# (manifest that ships, package whose source is built into it)
PAIRS = [
    (HERE / "server/package.json", APP / "mcp/server/package.json"),
    (HERE / "auth/package.json", APP / "mcp/auth/package.json"),
]


def sync(
    check_only: bool,
    pairs: list[tuple[Path, Path]] | None = None,
    root: Path = APP,
) -> int:
    drifted: list[str] = []
    for vendored_path, source_path in pairs if pairs is not None else PAIRS:
        vendored = json.loads(vendored_path.read_text())
        source = json.loads(source_path.read_text())
        # Both sides default to {} — the SAME default
        # `tests/test_mcp_deployment_contract.py` uses. A package with no
        # dependencies may omit the key entirely, and a bare `.get()` would
        # read that as None, call it drift, and disagree with the gate.
        wanted = source.get("dependencies", {})
        if vendored.get("dependencies", {}) == wanted:
            continue

        # Relative to where the command runs, like the remediation commands
        # below: two frames of reference in three lines is one too many.
        rel = _here(vendored_path)
        drifted.append(str(rel))
        if check_only:
            print(f"drift: {rel}", file=sys.stderr)
            print(f"  image installs: {vendored.get('dependencies')}", file=sys.stderr)
            print(f"  package is tested against: {wanted}", file=sys.stderr)
            continue

        # Rewrite in place, preserving key order and every other field.
        vendored["dependencies"] = wanted
        vendored_path.write_text(json.dumps(vendored, indent=2) + "\n")
        print(f"updated {rel}")

    if check_only and drifted:
        print(
            f"\nrun: python3 {_here(HERE / 'sync-manifests.py')}"
            f"\nthen: cd {_here(HERE)} && "
            "npm install --package-lock-only --ignore-scripts",
            file=sys.stderr,
        )
        return 1
    if not drifted:
        print("manifests already match their source packages")
    else:
        print(
            "\nnow regenerate the lockfile — `npm ci` installs that, not the "
            f"manifest:\n  cd {_here(HERE)} && "
            "npm install --package-lock-only --ignore-scripts"
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero; write nothing",
    )
    raise SystemExit(sync(parser.parse_args().check))
