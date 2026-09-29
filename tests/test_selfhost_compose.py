"""The self-host quickstart's shape (open-source design §4.5).

Static assertions over `compose.selfhost.yml`, the Dockerfile, the README and
the smoke script — the parts of the quickstart a test can hold still without
Docker. The smoke script is the live proof and is run by hand today; it gets
its CI job in phase 2 (§5, on the published image). This pins the invariants
a well-meaning edit would break silently: a Hub setting creeping into local
mode, Postgres published on the host, the API published beyond loopback by
default, the migrate step losing its ordering, the port and the base URL
coming apart, the README and the script drifting apart.
"""

from __future__ import annotations

import pathlib
import re

import yaml

from agentdrive.process_supervisor import MCP_SIDECAR_PORT

APP = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = APP / "compose.selfhost.yml"
README = APP / "README.md"
SMOKE = APP / "scripts" / "selfhost-smoke.sh"
DOCKERFILE = APP / "Dockerfile"

# Exactly the variables the api service sets — an allowlist, so a Hub-only
# setting (which local mode refuses by name: AUTH_ISSUER, HUB_ISSUER,
# AUTH_JWKS_URL, HUB_PRODUCT_AUDIENCE, HUB_MCP_AUDIENCE,
# AUTH_LOCAL_SIGNING_KEY_FILE) or a GCS setting (meaningless under the
# filesystem store) cannot arrive without this test noticing.
API_ENVIRONMENT = {
    "DATABASE_URL",
    "AUTH_MODE",
    "STORAGE_BACKEND",
    "STORAGE_FS_ROOT",
    "PUBLIC_BASE_URL",
    "CONSOLE_BASE_URL",
    "SESSION_SECRET",
    "MCP_PROXY_URL",
    "PORT",
    "SCHEDULER_ENABLED",
    "SCHEDULER_STATE_FILE",
}


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_three_services_and_a_fixed_project_name():
    doc = _compose()
    assert set(doc["services"]) == {"postgres", "migrate", "api"}
    # Not `drive`, which is the developer compose's project on the same
    # machine; both define `postgres`, and one name would recreate the other.
    assert doc["name"] == "agentdrive"
    assert set(doc["volumes"]) == {"pgdata", "data"}


def test_postgres_is_plain_and_unpublished():
    pg = _compose()["services"]["postgres"]
    assert pg["image"] == "postgres:16"
    assert "ports" not in pg, "the database must not be published on the host"
    assert pg["healthcheck"]["test"][-1].startswith("pg_isready")


def test_migrate_runs_apply_schema_once_after_postgres_is_healthy():
    mg = _compose()["services"]["migrate"]
    assert mg["command"] == ["python", "-m", "agentdrive.scripts.apply_schema"]
    assert mg["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert mg["restart"] == "no"
    assert set(mg["environment"]) == {"DATABASE_URL"}


def test_api_is_local_mode_on_the_filesystem_store_with_the_sidecar():
    api = _compose()["services"]["api"]
    env = api["environment"]
    assert set(env) == API_ENVIRONMENT
    assert env["AUTH_MODE"] == "local"
    assert env["STORAGE_BACKEND"] == "fs"
    root = env["STORAGE_FS_ROOT"]
    assert f"data:{root}" in api["volumes"]
    # The proxy target must be the port the supervisor starts the sidecar on;
    # change one without the other and /mcp answers 503 while a literal here
    # stays green.
    assert env["MCP_PROXY_URL"] == f"http://127.0.0.1:{MCP_SIDECAR_PORT}"
    # No console in a self-hosted install: empty removes the button.
    assert env["CONSOLE_BASE_URL"] == ""
    # The session secret is required and must come from the operator.
    assert env["SESSION_SECRET"].startswith("${AGENTDRIVE_SESSION_SECRET:?")
    assert api["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert api["build"] == "." and api["image"] == "agentdrive:selfhost"
    assert api["stop_grace_period"] == "20s"


def test_the_api_container_runs_the_maintenance_jobs_on_its_own_store():
    """The jobs delete what the API wrote, so they run INSIDE its container
    (the supervisor starts the scheduler): no override can point them at a
    different database or store. Their state lives on the data volume, as a
    dot-file the filesystem store never lists as an object."""
    api = _compose()["services"]["api"]
    env = api["environment"]
    assert env["SCHEDULER_ENABLED"] == "true"
    root = env["STORAGE_FS_ROOT"]
    state = env["SCHEDULER_STATE_FILE"]
    assert state.startswith(root + "/.")
    assert f"data:{root}" in api["volumes"]
    assert api["init"] is True


def test_the_published_port_and_the_public_base_url_cannot_come_apart():
    """An operator who sets only AGENTDRIVE_PORT must get share links and an
    MCP discovery document on that port, not on 8080 — and must get the API
    on loopback unless they opt into another interface."""
    api = _compose()["services"]["api"]
    assert api["ports"] == ["${AGENTDRIVE_BIND:-127.0.0.1}:${AGENTDRIVE_PORT:-8080}:8080"]
    assert api["environment"]["PUBLIC_BASE_URL"] == (
        "${AGENTDRIVE_PUBLIC_BASE_URL:-http://localhost:${AGENTDRIVE_PORT:-8080}}"
    )


def test_the_image_owns_the_data_volume_mount_point():
    """A named volume takes its ownership from the image path it covers;
    without this line /data is root-owned and the fs store refuses to boot."""
    root = _compose()["services"]["api"]["environment"]["STORAGE_FS_ROOT"]
    text = DOCKERFILE.read_text()
    line = f"RUN mkdir -p {root} && chown app:app {root}"
    assert line in text, line
    assert text.index(line) < text.index("\nUSER app\n"), (
        "the chown must run as root, before USER app"
    )


def _readme_quickstart() -> list[str]:
    text = README.read_text()
    start = text.index("## Self-host in ten minutes")
    block = re.search(r"```bash\n(.*?)```", text[start:], re.S)
    assert block, "the README quickstart must be a bash block"
    # Join continuation lines, drop comments and the clone step (the script
    # runs from a checkout).
    joined = block.group(1).replace("\\\n", " ")
    return [
        " ".join(line.split())
        for line in joined.splitlines()
        if line.strip()
        and not line.strip().startswith("#")
        and not line.strip().startswith("git clone")
    ]


def _smoke_as_readme_would_spell_it() -> str:
    """The script spells the compose invocation through one variable and
    runs `exec` without a TTY; the README's interactive form has neither.
    Same commands."""
    return (
        SMOKE.read_text()
        .replace("$COMPOSE", "docker compose -f compose.selfhost.yml")
        .replace("exec -T api", "exec api")
    )


def test_every_readme_quickstart_command_is_run_by_the_smoke_script_in_order():
    """§4.5: the README quickstart is the smoke test verbatim. Expectations
    come from the README, not from constants restated here."""
    commands = _readme_quickstart()
    assert len(commands) >= 4, commands
    smoke = _smoke_as_readme_would_spell_it()
    position = 0
    for command in commands:
        if "AGENTDRIVE_SESSION_SECRET=" in command:
            # The README's one-liner is the script's guarded block; both must
            # carry the same guard and the same append.
            assert "grep -q '^AGENTDRIVE_SESSION_SECRET=' .env" in command
            assert "grep -q '^AGENTDRIVE_SESSION_SECRET=' .env" in smoke
            needle = 'echo "AGENTDRIVE_SESSION_SECRET=$(openssl rand -hex 32)" >> .env'
            assert needle in command
        else:
            needle = command
        found = smoke.find(needle, position)
        assert found >= 0, f"the smoke script does not run (or runs out of order): {command}"
        position = found


def test_the_smoke_script_runs_nothing_the_readme_does_not_show():
    """The other direction of parity: every `exec` the script runs against
    the stack is a command the README shows the reader."""
    readme = " ".join(_readme_quickstart())
    whole_readme = " ".join(README.read_text().split())
    for line in _smoke_as_readme_would_spell_it().splitlines():
        line = line.strip()
        if "exec api python -m agentdrive.jobs.scheduler" in line:
            # The maintenance commands sit outside the quickstart block; the
            # script runs each job by name where the README shows one.
            command = line.split("exec api", 1)[1].split("|")[0].split(">")[0].split(")")[0]
            command = " ".join(command.replace('"$name"', "gc-daily").split())
            assert f"exec api {command}" in whole_readme, f"the README never shows: {command}"
            continue
        if "docker compose -f compose.selfhost.yml exec api" not in line:
            continue
        command = line.split("exec api", 1)[1].strip().split("|")[0].strip().rstrip(")")
        if command.startswith("python -m agentdrive.keys revoke"):
            continue  # revocation is what the script proves, after the quickstart
        assert " ".join(command.split()) in readme, f"the README never shows: {command}"


def test_the_smoke_script_is_isolated_portable_and_executable():
    text = SMOKE.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert SMOKE.stat().st_mode & 0o111, "scripts/selfhost-smoke.sh must be executable"
    # Its own project and port, so `--down -v` can never reach a real
    # install's volumes and `up` never fights it for the port.
    assert "docker compose -p $PROJECT -f compose.selfhost.yml" in text
    assert "PROJECT=agentdrive-smoke" in text
    assert 'export AGENTDRIVE_PORT="${AGENTDRIVE_SMOKE_PORT:-' in text
    # GNU-only forms a stock macOS lacks; the sandbox e2e broke on the first.
    for gnu_only in (
        "head -n -", "timeout ", "readarray", "mapfile", "| jq", "sed -i ",
        "date -d", "grep -P", "base64 -w", "realpath", "stat -c",
    ):
        assert gnu_only not in text, f"not portable: {gnu_only!r}"
