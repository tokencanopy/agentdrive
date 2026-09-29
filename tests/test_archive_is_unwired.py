"""`archive/` must be unreachable from the live package.

Layer 0 of the v0 contract reset moves the legacy subsystems out of
`src/agentdrive/` into `archive/`. Their tables are dropped in Layer 1, so this
code cannot run against a v0 database. An archived module that is still imported
is therefore not dormant -- it is a dependency that will fail at runtime.

The failure mode this guards is subtle. After `git mv src/agentdrive/wiki
archive/wiki`, a stale reference does *not* become `from archive.wiki import x`
-- it stays `from ..wiki import x` inside `core/artifacts.py` and simply raises
`ImportError` when that line executes. A check that looks for the string
"archive" in import names catches exactly none of them. So this guard resolves
every relative import to an absolute dotted name and matches it against an
explicit manifest of what left.

The manifest is deliberately hand-written rather than derived from the contents
of `archive/`: it is the layer's decision record, and a typo in a move should
fail here rather than silently shrink the check.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "agentdrive"
ARCHIVE = REPO / "archive"

PKG = "agentdrive"

# Dotted names removed from the `agentdrive` package by the v0 contract reset.
# A reference matches if it is an exact hit or a dotted-prefix hit, so
# `agentdrive.wiki` covers `agentdrive.wiki.gemini` without enumerating it.
ARCHIVED_MODULES = frozenset(
    {
        # Whole subsystems.
        f"{PKG}.wiki",
        f"{PKG}.embed",
        f"{PKG}.latex",
        f"{PKG}.query",
        f"{PKG}.render",
        f"{PKG}.mcp_server",
        f"{PKG}.web",
        f"{PKG}.mock",
        f"{PKG}.workers",
        # Core services with no v0 operation behind them.
        f"{PKG}.core.billing",
        f"{PKG}.core.query_compile",
        f"{PKG}.core.retrieval",
        f"{PKG}.core.feedback",
        f"{PKG}.core.wiki_paths",
        # The legacy router surface.
        f"{PKG}.api.routes",
        f"{PKG}.api.routes_agenttag",
        f"{PKG}.api.routes_agenttag_tasks",
        f"{PKG}.api.routes_billing",
        f"{PKG}.api.routes_compile",
        f"{PKG}.api.routes_drives",
        f"{PKG}.api.routes_members",
        f"{PKG}.api.routes_query",
        f"{PKG}.api.routes_tokens",
        f"{PKG}.api.routes_uploads",
        f"{PKG}.api.routes_workspaces",
        f"{PKG}.api.feedback_internal",
        # AgentDrive is no longer an authorization server.
        f"{PKG}.identity.mcp_oauth",
        # Billing jobs.
        f"{PKG}.jobs.billing_meter_flush",
        f"{PKG}.jobs.billing_reconcile",
        # --- Layers 1+2: everything bound to the legacy 52-table schema. ---
        # Hub owns the identity plane and product entitlement (§3.1, §9.1),
        # so the tables these read -- users, organizations, org_memberships,
        # tiers, drive_api_keys, user_api_keys -- are not in the day-0
        # schema. Token validation is `identity/product_token.py` now.
        f"{PKG}.auth",
        f"{PKG}.models",
        f"{PKG}.ids_legacy",
        f"{PKG}.identity.users",
        f"{PKG}.identity.organizations",
        f"{PKG}.identity.memberships",
        f"{PKG}.identity.invitations",
        f"{PKG}.identity.user_tokens",
        f"{PKG}.identity.onboarding",
        f"{PKG}.identity.workos_client",
        f"{PKG}.identity.hub_oidc",
        f"{PKG}.identity.agenttag_assertion",
        f"{PKG}.identity.agent_auth",
        # Product entitlement, metering and view counting: Hub's side of the
        # split, and v0 usage is two counter columns on `drives`.
        f"{PKG}.core.quota",
        f"{PKG}.core.tier",
        f"{PKG}.core.entitlements",
        f"{PKG}.core.drive_keys",
        f"{PKG}.core.views",
        f"{PKG}.core.view_counting",
        f"{PKG}.core.view_accumulator",
        f"{PKG}.core.share_accumulator",
        f"{PKG}.core.share_session",
        f"{PKG}.core.uploads",
        f"{PKG}.core.events",
        # The core services, rewritten against the day-0 tables in Layers
        # 3-9 rather than ported. Every one of them reads a dropped table or
        # the removed `path` column.
        f"{PKG}.core.artifacts",
        f"{PKG}.core.drives",
        f"{PKG}.core.folders",
        f"{PKG}.core.grants",
        f"{PKG}.core.shares",
        f"{PKG}.core.sharing",
        f"{PKG}.core.versions",
        f"{PKG}.core.search",
        f"{PKG}.core.permissions",
        # NOTE: `core.gc` and the `jobs.gc` entrypoint were REBUILT LIVE for
        # the day-0 schema at B3 packet 1 (2026-08-14 direct-transfer design
        # §9) — the scheduled `python -m agentdrive.jobs.gc` command is a
        # launch precondition, so those names left this manifest. The legacy
        # implementations remain under archive/ as history only.
        f"{PKG}.core.filters",
        f"{PKG}.core.reserved",
        # The pipeline job FRAMEWORK served the archived workers and stays
        # archived; the live jobs package is self-contained.
        f"{PKG}.jobs.audit",
        f"{PKG}.jobs.cloud_run",
        f"{PKG}.jobs.continuous",
        f"{PKG}.jobs.failure",
        f"{PKG}.jobs.lock_ids",
        f"{PKG}.jobs.observability",
        f"{PKG}.jobs.queue",
        f"{PKG}.jobs.quota",
        f"{PKG}.jobs.registry",
        # Scripts bound to the legacy identity plane.
        f"{PKG}.scripts.provision_org",
        f"{PKG}.scripts.reconcile_invoice",
        f"{PKG}.scripts.reconcile_usage",
        f"{PKG}.scripts.seed",
    }
)

# Filesystem twin of the manifest: (path under src/agentdrive, path under archive).
# `archive/X` is `src/agentdrive/X` as of the reset -- see archive/README.md.
ARCHIVED_PATHS = tuple(
    (name[len(PKG) + 1 :].replace(".", "/"), name[len(PKG) + 1 :].replace(".", "/"))
    for name in sorted(ARCHIVED_MODULES)
)


def _is_archived(dotted: str) -> bool:
    """True when `dotted` names an archived module or something inside one."""
    return any(
        dotted == name or dotted.startswith(name + ".") for name in ARCHIVED_MODULES
    )


def _module_name(path: pathlib.Path) -> str:
    """`src/agentdrive/core/artifacts.py` -> `agentdrive.core.artifacts`."""
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _absolute(module: str, node: ast.ImportFrom, name: str) -> str:
    """Resolve one imported name to an absolute dotted path.

    `node.level` counts leading dots. Level 1 is relative to the importing
    module's own package, level 2 to its parent, and so on -- so the base is the
    module's package with `level - 1` trailing components dropped.
    """
    package = module.rsplit(".", 1)[0] if "." in module else module
    parts = package.split(".")
    if node.level > 1:
        parts = parts[: -(node.level - 1)] or parts[:1]
    base = ".".join(parts)
    return ".".join(p for p in (base, node.module, name) if p)


def _referenced_modules(path: pathlib.Path) -> set[str]:
    """Every absolute dotted name this module imports.

    Both halves of an `ImportFrom` are emitted: the module it reads from and
    each name it pulls out. The second half is load-bearing -- `from . import
    feedback` has `module=None`, so the archived name appears only in the alias
    list.
    """
    module = _module_name(path)
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    found.add(node.module)
                    found.update(f"{node.module}.{a.name}" for a in node.names)
            else:
                found.add(_absolute(module, node, ""))
                found.update(_absolute(module, node, a.name) for a in node.names)
    return {name for name in found if name}


def test_no_live_module_imports_an_archived_one():
    """The whole point of the layer: `src/` reaches nothing under `archive/`."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for name in sorted(_referenced_modules(path)):
            if _is_archived(name):
                offenders.append(f"{path.relative_to(REPO)} -> {name}")
    assert not offenders, "live code references archived modules:\n" + "\n".join(
        offenders
    )


def test_archived_paths_left_src():
    missing = [
        src
        for src, _ in ARCHIVED_PATHS
        if (SRC / src).exists() or (SRC / f"{src}.py").exists()
    ]
    assert not missing, "still under src/agentdrive/:\n" + "\n".join(sorted(missing))


def test_archive_was_not_ported_into_the_monorepo():
    """The inverse of the guard this replaces, and deliberately so.

    In the agentdrive repository this asserted every archived path was PRESENT
    under `archive/` — it guarded a v0-reset move that deleted instead of
    relocating, at a moment when losing that code would have been silent.

    That move is history now and it is preserved in the archived
    `tokencanopy/agentdrive` repository, which is where it should be read. The
    port into this monorepo deliberately left `archive/` behind: 461 files, 43%
    of the tree, none of it reachable from `src/` and all of it one `git log`
    away in the other repo (owner decision, 2026-08-29).

    So the assertion is flipped rather than dropped. Dropping it would leave
    nothing to stop someone re-adding half a megabyte of dead code "for
    reference"; asserting absence makes that a deliberate, reviewed act.

    The guards that actually matter are untouched:
    `test_no_live_module_imports_an_archived_one` still refuses any `src/`
    reference to an archived module by NAME, which needs no files on disk.
    """
    assert not ARCHIVE.exists(), (
        f"{ARCHIVE.relative_to(REPO)}/ is back in the tree. The archived v0 "
        "subsystems were deliberately not ported into the monorepo — they live "
        "in the archived tokencanopy/agentdrive repository. Reviving one is a "
        "rebuild from its accepted contract with a new migration, never a "
        "restore of these files."
    )


@pytest.mark.skipif(
    not ARCHIVE.exists(),
    reason="archive/ was not ported into the monorepo — nothing to collect from",
)
def test_pytest_cannot_reach_archive():
    """A conftest under `archive/` would be collected despite norecursedirs.

    `norecursedirs` stops directory *recursion*, but a `conftest.py` on the path
    to a collected test is imported regardless. Archived conftests must live
    beside the archived tests, never at the root of `archive/`.

    Skipped rather than deleted: it would pass vacuously against a missing
    directory, which reads as coverage that is not there. If `archive/` ever
    returns this becomes real again on its own.
    """
    stray = [
        p.relative_to(REPO)
        for p in ARCHIVE.rglob("conftest.py")
        if p.parent == ARCHIVE or p.parent.name == "tests"
    ]
    assert not stray, "conftest.py at a collectable position under archive/:\n" + "\n".join(
        str(s) for s in stray
    )


def test_the_app_still_boots():
    """`import agentdrive.app` is the layer's contract with every later layer."""
    app_module = importlib.import_module(f"{PKG}.app")
    # `add_stability_metadata` raises on a manifest entry that no longer exists
    # in the spec, so building the schema is a real assertion, not a smoke test.
    spec = app_module.app.openapi()
    assert spec["paths"], "the app serves no paths at all"
