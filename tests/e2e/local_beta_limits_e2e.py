"""Hermetic beta-limit acceptance over local PostgreSQL, GCS, and HTTP.

The harness first boots the production ASGI entrypoint behind a real uvicorn
socket in its dedicated public-renderer role and proves the service boundary.
It then runs the named transactional acceptance journeys against the same local
PostgreSQL and GCS emulator. It never reads cloud credentials or non-local
origins.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

DRIVE_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://agentdrive:dev@localhost:5432/agentdrive"
)
GCS_EMULATOR_HOST = os.environ.get("GCS_EMULATOR_HOST", "http://localhost:4443")

ACCEPTANCE_TESTS = (
    "tests/test_v0_drives.py::test_usage_reports_byte_counters",
    "tests/test_v0_logical_accounting.py::test_concurrent_reservations_cannot_overshoot_hard_ceiling",
    "tests/test_v0_uploads.py::test_beta_file_ceiling_refuses_before_target_issuance",
    "tests/test_v0_uploads.py::test_upload_hour_exhaustion_refuses_before_target_issuance",
    "tests/test_v0_download_capabilities.py::test_response_shape_headers_and_expiry_derivation",
    "tests/test_v0_download_capabilities.py::test_monthly_download_refuses_before_signing",
    "tests/test_public_usage_limits.py::test_public_range_commits_only_yielded_bytes",
    "tests/test_public_usage_limits.py::test_share_request_limit_is_distributed_and_post_resolution",
    "tests/test_public_usage_limits.py::test_hot_share_concurrency_admits_only_the_distributed_ceiling",
    "tests/test_public_usage_limits.py::test_share_bandwidth_refuses_before_storage",
    "tests/test_public_usage_limits.py::test_unknown_probe_writes_no_usage_row",
    "tests/test_v0_shares.py::test_redeem_share_404_after_target_soft_deleted",
    "tests/test_v0_shares.py::test_redeem_share_404_after_drive_soft_deleted",
    "tests/test_v0_shares.py::test_redeem_share_404_after_revoke",
    "tests/test_public_usage_limits.py::test_omitted_expiry_defaults_to_seven_days",
    "tests/test_public_usage_limits.py::test_share_expiry_cannot_exceed_thirty_days",
    "tests/test_public_upload_absence.py::test_anonymous_and_share_credentials_cannot_begin_uploads",
    "tests/test_usage_snapshot.py::test_expired_public_reservation_commits_full_amount",
    "tests/test_usage_snapshot.py::test_expired_public_reservation_updates_visible_retrieval",
    "tests/test_usage_meter.py::test_two_connections_cannot_cross_shared_limit",
    "tests/test_usage_meter.py::test_concurrent_share_refusal_never_partially_charges_workspace",
    "tests/test_usage_meter.py::test_shadow_admission_records_usage_and_reports_would_refuse",
    "tests/test_public_usage_limits.py::test_public_stream_holds_no_database_connection",
    "tests/test_public_usage_limits.py::test_public_finalize_failure_leaves_conservative_reservation",
)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def require_local_dependency(host: str, port: int, name: str) -> None:
    try:
        with socket.create_connection((host, port), timeout=1):
            return
    except OSError as exc:
        raise RuntimeError(f"{name} is not reachable at {host}:{port}") from exc


def local_env() -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": DATABASE_URL,
        "GCS_BUCKET": f"agentdrive-beta-e2e-{os.getpid()}",
        "GCS_EMULATOR_HOST": GCS_EMULATOR_HOST,
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "USAGE_DIMENSION_HMAC_SECRET": secrets.token_urlsafe(48),
        "PUBLIC_BASE_URL": "http://127.0.0.1",
        "WIKI_ENABLED": "false",
        "EMBED_ENABLED": "false",
    }


def wait_for_health(base_url: str, server: subprocess.Popen[bytes]) -> None:
    with httpx.Client(base_url=base_url, timeout=1) as client:
        for _ in range(80):
            if server.poll() is not None:
                _stdout, stderr = server.communicate()
                detail = stderr.decode(errors="replace").strip()
                raise RuntimeError(
                    f"public renderer exited during startup: {detail}"
                )
            try:
                if client.get("/health").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.25)
    raise RuntimeError("public renderer never became healthy")


def prove_public_service_boundary(env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "agentdrive.scripts.apply_schema"],
        cwd=DRIVE_ROOT,
        env=env,
        check=True,
    )
    port = free_port()
    server_env = {
        **env,
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "PUBLIC_CONTENT_BASE_URL": f"http://public.localhost:{port}",
        "SHARE_BASE_URL": f"http://share.localhost:{port}",
        "SERVICE_SURFACE_ROLE": "public-renderer",
    }
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "agentdrive.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=DRIVE_ROOT,
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        wait_for_health(base_url, server)
        with httpx.Client(base_url=base_url, timeout=3) as client:
            for path in ("/v0/drives", "/view/demo", "/mcp", "/"):
                response = client.get(path)
                if response.status_code != 404:
                    raise RuntimeError(
                        f"public renderer exposed {path}: {response.status_code}"
                    )
        print("beta-limits-e2e: public service boundary PASS")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        bucket = env["GCS_BUCKET"]
        with httpx.Client(base_url=GCS_EMULATOR_HOST, timeout=3) as client:
            response = client.delete(f"/storage/v1/b/{bucket}")
            if response.status_code not in (200, 204, 404):
                raise RuntimeError(
                    f"could not remove E2E bucket {bucket}: {response.status_code}"
                )


def main() -> int:
    database = urlparse(DATABASE_URL)
    emulator = urlparse(GCS_EMULATOR_HOST)
    require_local_dependency(
        database.hostname or "127.0.0.1",
        database.port or 5432,
        "PostgreSQL",
    )
    require_local_dependency(
        emulator.hostname or "127.0.0.1",
        emulator.port or 80,
        "GCS emulator",
    )
    env = local_env()
    prove_public_service_boundary(env)

    pytest_env = {**env}
    pytest_env.pop("SERVICE_SURFACE_ROLE", None)
    pytest_env.pop("GCS_BUCKET", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *ACCEPTANCE_TESTS, "-q"],
        cwd=DRIVE_ROOT,
        env=pytest_env,
        check=False,
    )
    if result.returncode:
        print("beta-limits-e2e: acceptance journeys FAIL", file=sys.stderr)
        return result.returncode
    print("beta-limits-e2e: acceptance journeys PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
