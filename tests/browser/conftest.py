"""Hermetic two-origin harness for the private viewer browser suite."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn
from playwright.async_api import FrameLocator, Page, async_playwright
from preflight import assert_service_ready

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

PARENT_ORIGIN = "http://app.localhost:8765"
VIEWER_ORIGIN = "http://viewer.localhost:8766"
_ACTOR = "tcagt_0000000000000001"
_WORKSPACE = "tcws_0000000000000001"
_BROWSER_CALLS = 0


def pytest_runtest_call(item: pytest.Item) -> None:
    global _BROWSER_CALLS
    _BROWSER_CALLS += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if session.testscollected and _BROWSER_CALLS == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    # A skipped browser test is a silently untested security property, not
    # a soft pass — the CI gate must be red, never "green with skips"
    # (B4 plan, Task 2 Step 5: the gate is provably non-vacuous).
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None and reporter.stats.get("skipped"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@dataclass(frozen=True)
class ViewerFixture:
    drive_id: str
    root_folder_id: str
    artifact_id: str
    version_id: str
    name: str


@dataclass(frozen=True)
class Minted:
    id: str
    credential: str
    expected: dict[str, str]


def _actor() -> V0ActorContext:
    return V0ActorContext(
        subject=_ACTOR,
        subject_type="agent",
        workspace_id=_WORKSPACE,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({"drives:read", "drives:write", "content:read", "content:write"}),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id="tcusr_0000000000000009",
        workspace_role=None,
    )


def _multipart(parent_id: str, name: str, content_type: str, body: bytes) -> bytes:
    return (
        b'--b\r\nContent-Disposition: form-data; name="parent_id"\r\n\r\n'
        + parent_id.encode()
        + b'\r\n--b\r\nContent-Disposition: form-data; name="name"\r\n\r\n'
        + name.encode()
        + b'\r\n--b\r\nContent-Disposition: form-data; name="content"; filename="'
        + name.encode()
        + b'"\r\nContent-Type: '
        + content_type.encode()
        + b"\r\n\r\n"
        + body
        + b"\r\n--b--\r\n"
    )


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        pass


def _is_evil_parent_probe(scope: dict[str, object]) -> bool:
    """Allow the CSP bypass only for the explicit wrong-origin browser probe."""
    return scope["path"] == "/view/" and scope.get("query_string") == b"browser-evil=1"


async def _browser_viewer_app(scope, receive, send):
    """Permit an evil test parent through CSP while keeping shell config strict.

    The real shell still receives only ``VIEWER_EMBED_ORIGINS`` in its config,
    so this wrapper creates an actual wrong-origin postMessage without changing
    the protocol allowlist being proved.
    """

    async def browser_send(message):
        if message["type"] == "http.response.start" and _is_evil_parent_probe(scope):
            headers = [
                header
                for header in message["headers"]
                if header[0].lower() != b"content-security-policy"
            ]
            headers.append(
                (
                    b"content-security-policy",
                    b"default-src 'none'; script-src 'self'; connect-src 'self' blob: "
                    b"https://storage.googleapis.com; worker-src 'self' blob:; "
                    b"img-src 'self' blob: data:; media-src 'self' blob:; "
                    b"style-src 'self'; font-src 'self'; frame-src 'self'; "
                    b"base-uri 'none'; form-action 'none'; "
                    b"frame-ancestors http://app.localhost:8765 http://evil.localhost:8767",
                )
            )
            message = {**message, "headers": headers}
        await send(message)

    await app(scope, receive, browser_send)


async def _wait_for_port(port: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError(f"server did not listen on 127.0.0.1:{port}")


@dataclass
class BrowserViewerHarness:
    """Real app + real parent host; only database setup stays in-process."""

    http: httpx.AsyncClient

    async def create_artifact(
        self, *, name: str, content_type: str, body: bytes
    ) -> ViewerFixture:
        drive = await self.http.post(
            "/v0/drives",
            json={"name": "browser-viewer"},
            headers={"Idempotency-Key": "browser-drive"},
        )
        assert drive.status_code == 201, drive.text
        payload = drive.json()
        artifact = await self.http.post(
            f"/v0/drives/{payload['id']}/artifacts",
            content=_multipart(payload["root_folder_id"], name, content_type, body),
            headers={
                "Content-Type": "multipart/form-data; boundary=b",
                "Idempotency-Key": "browser-artifact",
            },
        )
        assert artifact.status_code == 201, artifact.text
        artifact_payload = artifact.json()
        return ViewerFixture(
            drive_id=payload["id"],
            root_folder_id=payload["root_folder_id"],
            artifact_id=artifact_payload["id"],
            version_id=artifact_payload["head_version_id"],
            name=name,
        )

    async def mint(self, fixture: ViewerFixture, *, ttl_seconds: int = 180) -> Minted:
        # The v0 mint request has no caller-selected TTL: the server enforces
        # its own bounded setting. Keep the required harness argument so tests
        # state the intended default lifetime without widening that contract.
        assert ttl_seconds == 180
        response = await self.http.post(
            f"/v0/drives/{fixture.drive_id}/artifacts/{fixture.artifact_id}/viewer-sessions",
            json={},
            headers={"Idempotency-Key": "browser-mint"},
        )
        assert response.status_code == 200, response.text
        minted = response.json()
        return Minted(
            id=minted["id"],
            credential=minted["credential"],
            expected={
                "drive_id": minted["drive_id"],
                "artifact_id": minted["artifact_id"],
                "version_id": minted["version_id"],
            },
        )

    async def revoke(self, fixture: ViewerFixture) -> None:
        async with conn() as connection:
            await connection.execute(
                "UPDATE grants SET revoked_at=clock_timestamp() "
                "WHERE drive_id=$1 AND principal_type='agent' AND principal_id=$2",
                fixture.drive_id,
                _ACTOR,
            )

    async def expire(self, minted: Minted) -> None:
        async with conn() as connection:
            await connection.execute(
                "UPDATE viewer_sessions SET expires_at=clock_timestamp() - interval '1 second' "
                "WHERE id=$1",
                minted.id,
            )

    async def open(
        self, page: Page, minted: Minted, *, chrome: str | None = None
    ) -> FrameLocator:
        await page.goto(PARENT_ORIGIN + "/parent.html")
        payload: dict[str, object] = {
            "credential": minted.credential,
            "expected": minted.expected,
        }
        # Absent unless a test asks: the shell defaults to drawing its header,
        # and the console suppresses it with `chrome: "none"` because it draws
        # the name, path and size directly above the frame itself.
        if chrome is not None:
            payload["chrome"] = chrome
        await page.evaluate("payload => window.__setViewerCredential(payload)", payload)
        return page.frame_locator("#private-viewer")


@pytest_asyncio.fixture
async def browser_viewer(browser_services, app_with_lifespan):
    """Run the real FastAPI app and a minimal parent on distinct loopback origins."""
    app.dependency_overrides[v0_actor] = _actor
    transport = httpx.ASGITransport(app=app_with_lifespan)
    handler = partial(_QuietHandler, directory=str(Path(__file__).parent))
    parent_server = ThreadingHTTPServer(("127.0.0.1", 8765), handler)
    wrong_origin_server = ThreadingHTTPServer(("127.0.0.1", 8767), handler)
    parent_thread = threading.Thread(target=parent_server.serve_forever, daemon=True)
    wrong_origin_thread = threading.Thread(target=wrong_origin_server.serve_forever, daemon=True)
    parent_thread.start()
    wrong_origin_thread.start()
    viewer_server = uvicorn.Server(
        uvicorn.Config(
            _browser_viewer_app, host="127.0.0.1", port=8766, lifespan="off", log_level="warning"
        )
    )
    viewer_task = asyncio.create_task(viewer_server.serve())
    await _wait_for_port(8766)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield BrowserViewerHarness(http)
    finally:
        viewer_server.should_exit = True
        await viewer_task
        parent_server.shutdown()
        wrong_origin_server.shutdown()
        parent_server.server_close()
        wrong_origin_server.server_close()
        parent_thread.join(timeout=2)
        wrong_origin_thread.join(timeout=2)
        app.dependency_overrides.clear()


@pytest.fixture(scope="session", autouse=True)
def browser_services() -> None:
    """Fail rather than skip when explicit browser-suite prerequisites are absent."""
    assert_service_ready(
        "DATABASE_URL",
        os.environ.get("DATABASE_URL", "postgresql://agentdrive:dev@localhost:5432/agentdrive"),
    )
    assert_service_ready(
        "GCS_EMULATOR_HOST", os.environ.get("GCS_EMULATOR_HOST", "http://localhost:4443")
    )


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    yield
    async with conn() as connection:
        await connection.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


@pytest_asyncio.fixture
async def page() -> Page:
    """Async API fixture compatible with the repository's shared pytest loop."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        try:
            yield await context.new_page()
        finally:
            await context.close()
            await browser.close()
