"""Over-the-wire e2e for read de-throttle + public view metrics
(read-dethrottle-and-view-metrics-design.md).

Boots the real app under uvicorn against the host's docker Postgres + the
fake-GCS emulator (its own database), then drives the changed surfaces as a
real HTTP client:

  Read de-throttle
    1. reads never 429 past the (overridden) read budget
    2. a read response carries NO X-RateLimit-* headers
    3. a write response DOES carry them
    4. /usage has no `reads_this_hour`; monthly `ops_this_month.reads` stays

  View metrics
    5. anonymous public renders are counted (total + daily-unique) after flush
    6. a bot UA is not counted
    7. a raw=1 fetch is not counted
    8. the owner sees the count in the owner banner; a visitor does not

Run:  uv run python tests/e2e/local_read_views_e2e.py
(Needs docker-compose Postgres on :5432 + fake-gcs on :4443, like the
sibling local_metering_e2e.py.)
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
DB_NAME = "agentdrive_e2e_readviews"
HOST_CREDS = "postgresql://agentdrive:dev@localhost:5432"
DB_URL = f"{HOST_CREDS}/{DB_NAME}"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  {detail}")
    print(f"  {'✓' if cond else '✗'} {name}  {'' if cond else detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def ensure_db() -> None:
    import asyncpg

    admin = await asyncpg.connect(f"{HOST_CREDS}/postgres")
    try:
        if not await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", DB_NAME
        ):
            await admin.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        await admin.close()
    c = await asyncpg.connect(DB_URL)
    try:
        await c.execute((REPO / "schema.sql").read_text())
        await c.execute(
            "TRUNCATE artifacts, drives, folders, org_memberships, users, "
            "organizations, events, artifact_versions, artifact_views_daily, "
            "artifact_view_dedup RESTART IDENTITY CASCADE"
        )
    finally:
        await c.close()


async def main() -> int:
    await ensure_db()
    port = free_port()
    env = {
        **os.environ,
        "DATABASE_URL": DB_URL,
        "GCS_BUCKET": "agentdrive-test",
        "GCS_EMULATOR_HOST": "http://localhost:4443",
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "E2A_API_KEY": "",
        "WIKI_ENABLED": "false",
        "EMBED_ENABLED": "false",
        "VIEW_SALT_SECRET": secrets.token_urlsafe(48),
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentdrive.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=20) as client:
            for _ in range(60):
                try:
                    if (await client.get("/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.5)
            else:
                print(server.stdout.read()[-3000:] if server.stdout else "")
                return 1
            check("0. /health 200", True)

            os.environ.update({k: env[k] for k in (
                "DATABASE_URL", "GCS_BUCKET", "GCS_EMULATOR_HOST",
                "SESSION_SECRET", "E2A_API_KEY", "VIEW_SALT_SECRET",
            )})
            sys.path.insert(0, str(REPO / "src"))
            from agentdrive.db import close_pool, conn, init_pool
            await init_pool()
            from agentdrive.core.drives import create_drive

            drive_id, api_key = await create_drive("e2e-readviews@example.com")
            auth = {"Authorization": f"Bearer {api_key}"}

            # Tighten read budget to 1 — proves it's ignored now.
            async with conn() as c:
                await c.execute(
                    "UPDATE organizations SET quota_overrides = $1::jsonb "
                    "WHERE id = (SELECT organization_id FROM drives WHERE id = $2)",
                    json.dumps({"read_per_hour": 1}), drive_id,
                )

            # ── Read de-throttle ────────────────────────────────────
            statuses = []
            for _ in range(10):
                r = await client.get("/v0/artifacts", headers=auth)
                statuses.append(r.status_code)
            check("1. reads never 429 past read_per_hour=1",
                  all(s == 200 for s in statuses), f"got {statuses}")

            r_read = await client.get("/v0/artifacts", headers=auth)
            check("2. read response has no X-RateLimit-* headers",
                  "x-ratelimit-limit" not in r_read.headers,
                  f"headers={dict(r_read.headers)}")

            r_write = await client.put(
                "/v0/artifacts/probe.md", content=b"# probe",
                headers={**auth, "Content-Type": "text/markdown"},
            )
            check("3. write response carries X-RateLimit-* headers",
                  r_write.headers.get("x-ratelimit-resource") == "write"
                  and "x-ratelimit-limit" in r_write.headers,
                  f"headers={dict(r_write.headers)}")

            usage = (await client.get("/v0/drives/me/usage", headers=auth)).json()
            check("4. /usage has no reads_this_hour",
                  "reads_this_hour" not in usage, f"keys={list(usage)}")
            check("4. /usage still reports monthly read ops",
                  "reads" in usage.get("ops_this_month", {}),
                  f"ops={usage.get('ops_this_month')}")

            # ── View metrics ────────────────────────────────────────
            r = await client.put(
                "/v0/artifacts/page.md", content=b"# hello",
                headers={**auth, "Content-Type": "text/markdown",
                         "X-AgentDrive-Visibility": "public"},
            )
            art_id = r.json()["id"]

            # 3 anonymous browser renders (same client IP+UA → 1 unique).
            for _ in range(3):
                rv = await client.get(f"/{drive_id}/page.md",
                                      headers={"user-agent": _BROWSER_UA})
                assert rv.status_code == 200, rv.text
            # Bot + raw must not count.
            await client.get(f"/{drive_id}/page.md",
                             headers={"user-agent": "Googlebot/2.1"})
            await client.get(f"/{drive_id}/page.md?raw=1",
                             headers={"user-agent": _BROWSER_UA})

            # Wait out one flush window (server owns its accumulator).
            print("  … waiting 16s for the view-count flush window")
            await asyncio.sleep(16)

            async with conn() as c:
                row = await c.fetchrow(
                    "SELECT total_views, unique_views FROM artifact_views_daily "
                    "WHERE art_id = $1", art_id,
                )
            check("5. anonymous renders counted (total=3, unique=1)",
                  row is not None and row["total_views"] == 3
                  and row["unique_views"] == 1,
                  f"row={dict(row) if row else None}")
            # bot + raw added nothing beyond the 3 → covered by total==3.
            check("6/7. bot UA and raw=1 not counted (total still 3)",
                  row is not None and row["total_views"] == 3, "")

            # Owner sees the count; visitor does not.
            owner_html = (await client.get(
                f"/{drive_id}/page.md",
                headers={**auth, "user-agent": _BROWSER_UA})).text
            check("8. owner banner shows the count",
                  "owner-banner" in owner_html and "3" in owner_html
                  and "views" in owner_html, "")
            visitor_html = (await client.get(
                f"/{drive_id}/page.md",
                headers={"user-agent": _BROWSER_UA})).text
            check("8. visitor sees neither banner nor count",
                  "owner-banner" not in visitor_html
                  and "3 views" not in visitor_html, "")

            await close_pool()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
