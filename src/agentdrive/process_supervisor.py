"""Run the API, the private MCP ingress, the MCP sidecar and (for a
self-hosted install) the job scheduler as one process.

Cloud Run's existing service owns the canonical AgentDrive domain, so the
MCP cannot be a second service without changing the domain-routing topology.
This small supervisor keeps the children in one container, forwards signals,
and treats any child exiting as a failed revision.

THE PER-BOOT PROOF. Since the 2026-08-28 security remediation the sidecar
cannot call the public `/v0` API — its bearer is bound to the `/mcp`
audience, which `/v0` rejects — so it calls the private loopback ingress
(`agentdrive.internal_ingress`) instead. This supervisor generates 256 random
bits at boot and puts them in the environment of the SIDECAR and the INGRESS
only; the public API child never receives them. The proof is not
authorization (see `identity/internal_proof.py`): the ingress still verifies
the caller's MCP JWT and still enforces scope against live local grants. It
exists so a process that merely reaches the loopback port cannot tell the
ingress from a closed route.

Generated per boot and never persisted: it is not in Terraform state, not in
Secret Manager, and not in any log. A revision restart mints a new one, and
the only two processes that need it are started by this file.

THE SCHEDULER. With `SCHEDULER_ENABLED=true` (set by `compose.selfhost.yml`,
never by the hosted deployment, whose platform scheduler runs the same jobs)
this also starts `python -m agentdrive.jobs.scheduler`, which runs garbage
collection and usage maintenance on their schedule. It lives here rather than
in a container of its own so the jobs that DELETE content can never be
configured apart from the API that wrote it: one environment, one volume.
"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence

MCP_SIDECAR_PORT = "8081"
INTERNAL_INGRESS_HOST = "127.0.0.1"
INTERNAL_INGRESS_PORT = "8082"
#: 256 bits. `secrets.token_urlsafe(32)` base64url-encodes to 43 characters,
#: which is the floor both the ingress and the sidecar enforce.
INTERNAL_PROOF_BYTES = 32
FORWARDED_ALLOW_IPS_DEFAULT = (
    "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "169.254.0.0/16,::1,fc00::/7,fe80::/10"
)
MCP_ENTRYPOINT = "/opt/agentdrive-mcp/server/dist/index.js"

log = logging.getLogger(__name__)


def _api_command() -> list[str]:
    forwarded_allow_ips = os.environ.get(
        "FORWARDED_ALLOW_IPS", FORWARDED_ALLOW_IPS_DEFAULT
    )
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "agentdrive.app:app",
        "--host",
        "0.0.0.0",
        "--port",
        os.environ.get("PORT", "8080"),
        "--workers",
        "1",
        "--proxy-headers",
        "--forwarded-allow-ips",
        forwarded_allow_ips,
    ]


def _mcp_command() -> list[str]:
    return ["/usr/local/bin/node", MCP_ENTRYPOINT]


def _internal_ingress_command() -> list[str]:
    """Uvicorn for the private ingress, bound to loopback and nothing else.

    A SEPARATE process from the public API on purpose. Routing by path inside
    the public app would put the private surface one routing bug away from the
    internet; a separate socket on a loopback address cannot be reached from
    outside the container at all. It runs a single worker with a small pool —
    it serves one local client.
    """
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "agentdrive.internal_ingress:app",
        "--factory",
        "--host",
        INTERNAL_INGRESS_HOST,
        "--port",
        INTERNAL_INGRESS_PORT,
        "--workers",
        "1",
    ]


def _scheduler_command() -> list[str]:
    return [sys.executable, "-m", "agentdrive.jobs.scheduler"]


def scheduler_enabled(source: Mapping[str, str]) -> bool:
    return source.get("SCHEDULER_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def _terminate(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 10
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
    for process in processes:
        if process.poll() is None:
            process.kill()


def local_api_key_sidecar_env(source: Mapping[str, str]) -> dict[str, str]:
    """Under AUTH_MODE=local the sidecar authenticates opaque API keys.

    Two variables, and nothing derived from an origin (§4.2 as amended
    2026-09-21). `MCP_AUTH_MODE=api-key` swaps the sidecar's JWT verification
    for a round trip to the ingress's `/_internal/introspect`, which it
    already has the loopback URL and the per-boot proof for;
    `MCP_ENVIRONMENT=local` is what the sidecar requires beside any override,
    and without it the sidecar refuses to start and the supervisor treats
    that exit as fatal.

    The retired issuer's five-variable tuple (MCP_AUTH_ISSUER, AUDIENCE,
    JWKS_URL, METADATA_URL, plus ENVIRONMENT) is gone with the issuer: there
    is no JWKS to point at and no `/mcp` audience to derive, because one key
    works on every surface.

    An operator who set either variable keeps their value. Empty under Hub,
    where the sidecar's production presets and its Hub JWT path apply
    untouched.

    The two halves of this have to ship together: the sidecar's `api-key`
    branch is what makes `MCP_ENVIRONMENT=local` bootable without the tuple,
    and without it this environment is a boot failure rather than a degraded
    mode. `tests/test_mcp_deployment_contract.py` pins the seam.
    """
    if source.get("AUTH_MODE", "").strip().lower() != "local":
        return {}
    derived = {"MCP_AUTH_MODE": "api-key", "MCP_ENVIRONMENT": "local"}
    return {k: v for k, v in derived.items() if not source.get(k, "").strip()}


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    children: list[subprocess.Popen[bytes]] = []
    stopping = False
    requested_stop = False
    failure_code: int | None = None

    def stop(_signum: int, _frame) -> None:
        nonlocal requested_stop, stopping
        requested_stop = True
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        if os.environ.get("MCP_PROXY_URL", "").strip():
            # One value, two children, and never written anywhere else.
            internal_proof = secrets.token_urlsafe(INTERNAL_PROOF_BYTES)
            internal_url = f"http://{INTERNAL_INGRESS_HOST}:{INTERNAL_INGRESS_PORT}"

            ingress_environment = os.environ.copy()
            ingress_environment["AGENTDRIVE_INTERNAL_PROOF"] = internal_proof
            children.append(
                subprocess.Popen(  # noqa: S603
                    _internal_ingress_command(), env=ingress_environment
                )
            )
            log.info(
                "started AgentDrive internal ingress on %s:%s",
                INTERNAL_INGRESS_HOST,
                INTERNAL_INGRESS_PORT,
            )

            mcp_environment = os.environ.copy()
            mcp_environment["PORT"] = MCP_SIDECAR_PORT
            mcp_environment["MCP_BIND_HOST"] = INTERNAL_INGRESS_HOST
            mcp_environment["MCP_AGENTDRIVE_INTERNAL_URL"] = internal_url
            mcp_environment["MCP_INTERNAL_PROOF"] = internal_proof
            mcp_environment.update(local_api_key_sidecar_env(os.environ))
            children.append(
                subprocess.Popen(_mcp_command(), env=mcp_environment)  # noqa: S603
            )
            log.info("started AgentDrive MCP sidecar on 127.0.0.1:%s", MCP_SIDECAR_PORT)

        if scheduler_enabled(os.environ):
            # No proof: the jobs talk to Postgres and the store, never to
            # the ingress.
            children.append(
                subprocess.Popen(_scheduler_command(), env=os.environ.copy())  # noqa: S603
            )
            log.info("started AgentDrive job scheduler")

        # The PUBLIC API child, which deliberately does NOT receive the proof:
        # it neither calls the ingress nor needs to recognise it.
        children.append(subprocess.Popen(_api_command(), env=os.environ.copy()))  # noqa: S603
        log.info("started AgentDrive API on port %s", os.environ.get("PORT", "8080"))

        while not stopping:
            for child in children:
                return_code = child.poll()
                if return_code is not None:
                    log.error("child process exited with status %s", return_code)
                    failure_code = return_code or 1
                    stopping = True
                    break
            if not stopping:
                time.sleep(0.2)
    except OSError as exc:
        log.error("failed to start AgentDrive process: %s", type(exc).__name__)
        failure_code = 1
        stopping = True
    except RuntimeError as exc:
        log.error("%s", exc)
        failure_code = 1
        stopping = True
    finally:
        _terminate(children)

    if requested_stop:
        return 0
    return failure_code or 1


if __name__ == "__main__":
    raise SystemExit(main())
