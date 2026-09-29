"""Cloud Run Job entrypoint for the AgentDrive prober.

``python -m agentdrive.jobs.prober <mode>`` — the modes mirror
``e2a-prober`` (design spec §2a):

  seed        Find or create the light battery's fixed probe drive and print
              its id. Idempotent. Needs ``drives:write`` in PROBER_SCOPES.
  validate    Mint a token and read the configured drive. No writes.
  run-once    Run one battery and exit non-zero on any failed row.
              ``--light`` (the scheduled hourly pass) runs inside the fixed
              probe drive; without it the full journey creates and deletes
              its own drive.

Configuration is environment only (the job's Terraform sets it):

  PROBER_ORIGIN          https://drive.tokencanopy.com   (no trailing slash)
  PROBER_TOKEN_ENDPOINT  defaults to ``HUB_ISSUER`` + "/token"
  PROBER_CLIENT_ID       the probe agent's tccred_* id      (Secret Manager)
  PROBER_CLIENT_SECRET   its secret                         (Secret Manager)
  PROBER_AUDIENCE        defaults to PROBER_ORIGIN
  PROBER_SCOPES          defaults per mode (light: no drives:write)
  PROBER_DRIVE_ID        the fixed probe drive (from ``seed``)
  PROBER_VIEWER_HOST     required for the full battery; bare isolated viewer host

Output: one ``at=prober_row_ok`` / ``at=prober_row_failed`` line per row,
one ``at=prober_result`` line, and the JSON summary on stdout. Nothing here
prints a token, a URL, or a body. Exit 0 = every row passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

import httpx

from agentdrive.core.prober import (
    FULL_SCOPES,
    LIGHT_SCOPES,
    Battery,
    ProberConfig,
    mint_token,
    seed_probe_drive,
)

log = logging.getLogger("agentdrive.prober")


def _setup_logging() -> None:
    """Deliberately NOT `agentdrive.observability.setup_logging`: importing
    that module constructs `agentdrive.config.settings`, which requires
    DATABASE_URL, GCS_BUCKET and SESSION_SECRET — the service's secrets. The
    prober must see the service as a customer does and holds none of them,
    so it logs with the standard library and the same `at=` idiom."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
        force=True,
    )
    # httpx logs the complete request URL at INFO. The prober follows signed
    # download and share URLs, so letting those records inherit the root INFO
    # level would write possession credentials to Cloud Logging.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

SEED_DRIVE_NAME = "Token Canopy probe drive"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentdrive.jobs.prober",
        description="AgentDrive /v0 prober — one battery as an external caller.",
    )
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("seed", help="find or create the fixed probe drive and print its id")
    sub.add_parser("validate", help="mint a token and read the probe drive; no writes")
    run = sub.add_parser("run-once", help="run one battery; exit non-zero on a failed row")
    run.add_argument(
        "--light",
        action="store_true",
        help="the hourly pass: inside the fixed probe drive, no drive create/delete",
    )
    return p


def config_from_env(
    env: dict[str, str], *, probe: str, require_viewer: bool = False
) -> ProberConfig:
    origin = env.get("PROBER_ORIGIN", "").rstrip("/")
    if not origin:
        raise SystemExit("PROBER_ORIGIN is required")
    issuer = env.get("HUB_ISSUER", "").rstrip("/")
    token_endpoint = env.get("PROBER_TOKEN_ENDPOINT") or (f"{issuer}/token" if issuer else "")
    if not token_endpoint:
        raise SystemExit("PROBER_TOKEN_ENDPOINT (or HUB_ISSUER) is required")
    client_id = env.get("PROBER_CLIENT_ID", "")
    client_secret = env.get("PROBER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise SystemExit("PROBER_CLIENT_ID and PROBER_CLIENT_SECRET are required")
    default_scopes = LIGHT_SCOPES if probe == "light" else FULL_SCOPES
    viewer_host = env.get("PROBER_VIEWER_HOST") or None
    if require_viewer and not viewer_host:
        raise SystemExit("PROBER_VIEWER_HOST is required for the full battery")
    return ProberConfig(
        origin=origin,
        token_endpoint=token_endpoint,
        client_id=client_id,
        client_secret=client_secret,
        audience=env.get("PROBER_AUDIENCE") or origin,
        probe=probe,  # type: ignore[arg-type]
        scopes=env.get("PROBER_SCOPES") or default_scopes,
        drive_id=env.get("PROBER_DRIVE_ID") or None,
        viewer_host=viewer_host,
        timeout_seconds=float(env.get("PROBER_TIMEOUT_SECONDS", "30")),
    )


def _clients(cfg: ProberConfig) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    timeout = httpx.Timeout(cfg.timeout_seconds)
    api = httpx.AsyncClient(base_url=cfg.origin, timeout=timeout)
    anon = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    return api, anon


async def _seed(cfg: ProberConfig) -> int:
    api, anon = _clients(cfg)
    async with api, anon:
        api.headers["Authorization"] = f"Bearer {await mint_token(anon, cfg)}"
        drive_id, created = await seed_probe_drive(api, name=SEED_DRIVE_NAME)
    log.info("at=prober_seed drive_id=%s created=%s", drive_id, created)
    print(json.dumps({"drive_id": drive_id, "created": created, "name": SEED_DRIVE_NAME}))
    return 0


async def _validate(cfg: ProberConfig) -> int:
    api, anon = _clients(cfg)
    async with api, anon:
        api.headers["Authorization"] = f"Bearer {await mint_token(anon, cfg)}"
        r = await api.get(f"/v0/drives/{cfg.drive_id}")
    ok = r.status_code == 200
    log.info("at=prober_validate status=%d ok=%s", r.status_code, ok)
    print(json.dumps({"drive_id": cfg.drive_id, "status": r.status_code, "ok": ok}))
    return 0 if ok else 1


async def _run_once(cfg: ProberConfig) -> int:
    api, anon = _clients(cfg)
    async with api, anon:
        result = await Battery(cfg, api=api, anon=anon).run()
    summary = result.as_dict()
    log.log(
        logging.WARNING if result.failed else logging.INFO,
        "at=prober_result probe=%s outcome=%s rows_passed=%d rows_failed=%d duration_ms=%d",
        result.probe,
        result.outcome,
        summary["rows_passed"],
        summary["rows_failed"],
        result.duration_ms,
    )
    print(json.dumps(summary, sort_keys=True))
    # A failed row FAILS the job so the execution is `Failed` and the alert
    # policy pages; exiting 0 would be fail-open.
    return 1 if result.failed else 0


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)
    env = dict(os.environ if env is None else env)
    if args.mode == "seed":
        return asyncio.run(_seed(config_from_env(env, probe="full")))
    if args.mode == "validate":
        cfg = config_from_env(env, probe="light")
        return asyncio.run(_validate(cfg))
    probe = "light" if args.light else "full"
    return asyncio.run(
        _run_once(config_from_env(env, probe=probe, require_viewer=probe == "full"))
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
