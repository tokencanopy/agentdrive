"""The prober against an in-process fake of the `/v0` surface it drives.

No database and no object store: the fake is a small stateful FastAPI app
that speaks just enough of the contract for every row, including the parts
the prober is there to catch — an unknown query parameter answers 400
``INVALID_QUERY`` exactly as ``known_params`` does, a revoked share is a
uniform 404, a deleted artifact is filtered out of every read path.

The fake has a `contract` switch. ``"state"`` is today's spelling;
``"lifecycle"`` is the pre-#681 one. Running the prober against the old
spelling is a client/server rename skew seen from the caller's side, and
the test asserts it goes red rather than green.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from agentdrive.core.prober import Battery, ProberConfig, seed_probe_drive
from agentdrive.jobs import prober as prober_job

CLIENT_ID = "tccred_probe_0123456789abcdef"
CLIENT_SECRET = "s3cr3t-never-logged-9f8e7d6c"
TOKEN = "eyJ-not-a-real-token-but-must-never-be-logged"
FILTER_PARAM_BY_CONTRACT = {"state": "state", "lifecycle": "lifecycle"}


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class FakeDrive:
    """Stateful stand-in for AgentDrive `/v0` — the rows the prober drives."""

    def __init__(self, *, contract: str = "state") -> None:
        self.contract = contract
        self.filter = FILTER_PARAM_BY_CONTRACT[contract]
        self.drives: dict[str, dict[str, Any]] = {}
        self.folders: dict[str, dict[str, Any]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.shares: dict[str, dict[str, Any]] = {}
        self.share_keys: dict[str, str] = {}
        self.downloads: dict[str, str] = {}
        self.change_starts: list[str | None] = []
        self.app = self._build()

    # ── helpers ──────────────────────────────────────────────────────

    def add_drive(self, name: str) -> dict[str, Any]:
        did = f"drv_{uuid.uuid4().hex[:16]}"
        root = f"fld_{uuid.uuid4().hex[:16]}"
        self.folders[root] = {"id": root, "drive_id": did, "name": "", "parent_id": None,
                              "revision": 1, "state": "active"}
        self.drives[did] = {"id": did, "name": name, "root_folder_id": root,
                            "revision": 1, "state": "active"}
        return self.drives[did]

    def _known(self, request: Request, allowed: set[str]) -> JSONResponse | None:
        extra = set(request.query_params) - allowed
        if extra:
            return _err(400, "INVALID_QUERY", f"unknown query parameter(s): {sorted(extra)}")
        return None

    @staticmethod
    def _if_match(header: str | None, revision: int) -> JSONResponse | None:
        if header is None:
            return _err(428, "PRECONDITION_REQUIRED", "If-Match required")
        if header.strip('"') != str(revision):
            return _err(412, "PRECONDITION_FAILED", "stale")
        return None

    def _build(self) -> FastAPI:  # noqa: C901 — one fake, many routes
        app = FastAPI()
        fake = self
        F = self.filter

        @app.middleware("http")
        async def _auth(request: Request, call_next):  # type: ignore[no-untyped-def]
            if (
                request.url.path.startswith("/v0/")
                and request.headers.get("authorization") != f"Bearer {TOKEN}"
            ):
                return _err(401, "UNAUTHENTICATED", "no")
            return await call_next(request)

        @app.post("/oidc/token")
        async def token(request: Request) -> Any:
            form = await request.form()
            auth = httpx.BasicAuth(CLIENT_ID, CLIENT_SECRET)
            expected = auth._auth_header  # noqa: SLF001
            if request.headers.get("authorization") != expected:
                return _err(401, "invalid_client", "bad credential")
            if form.get("grant_type") != "client_credentials" or not form.get("resource"):
                return _err(400, "invalid_request", "bad grant")
            return {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 120,
                    "scope": form.get("scope", "")}

        @app.get("/v0/drives")
        async def list_drives(request: Request) -> Any:
            if bad := fake._known(request, {F, "limit", "cursor"}):
                return bad
            want = request.query_params.get(F, "active")
            return {"items": [d for d in fake.drives.values()
                               if want == "all" or d["state"] == want]}

        @app.post("/v0/drives", status_code=201)
        async def create_drive(request: Request) -> Any:
            body = await request.json()
            return fake.add_drive(body["name"])

        @app.get("/v0/drives/{did}")
        async def get_drive(did: str) -> Any:
            d = fake.drives.get(did)
            return d if d and d["state"] == "active" else _err(404, "DRIVE_NOT_FOUND", "no")

        @app.delete("/v0/drives/{did}")
        async def delete_drive(did: str, if_match: str | None = Header(default=None)) -> Any:
            d = fake.drives.get(did)
            if not d or d["state"] != "active":
                return _err(404, "DRIVE_NOT_FOUND", "no")
            if bad := fake._if_match(if_match, d["revision"]):
                return bad
            d["state"] = "deleted"
            d["revision"] += 1
            return d

        @app.get("/v0/drives/{did}/usage")
        async def usage(did: str) -> Any:
            return {"drive_id": did, "bytes": 0}

        @app.post("/v0/drives/{did}/folders", status_code=201)
        async def create_folder(did: str, request: Request) -> Any:
            body = await request.json()
            fid = f"fld_{uuid.uuid4().hex[:16]}"
            fake.folders[fid] = {"id": fid, "drive_id": did, "name": body["name"],
                                 "parent_id": body["parent_id"], "revision": 1, "state": "active"}
            return fake.folders[fid]

        @app.get("/v0/drives/{did}/folders/{fid}")
        async def get_folder(did: str, fid: str) -> Any:
            f = fake.folders.get(fid)
            return f if f and f["state"] == "active" else _err(404, "FOLDER_NOT_FOUND", "no")

        @app.get("/v0/drives/{did}/folders")
        async def list_folders(did: str, request: Request) -> Any:
            if bad := fake._known(request, {F, "limit", "cursor", "parent_id", "name"}):
                return bad
            want = request.query_params.get(F, "active")
            parent = request.query_params.get("parent_id")
            items = [f for f in fake.folders.values()
                     if f["drive_id"] == did and (want == "all" or f["state"] == want)
                     and (parent is None or f["parent_id"] == parent)]
            return {"items": items}

        @app.delete("/v0/drives/{did}/folders/{fid}")
        async def delete_folder(did: str, fid: str, request: Request,
                                if_match: str | None = Header(default=None)) -> Any:
            if bad := fake._known(request, {"recursive"}):
                return bad
            f = fake.folders.get(fid)
            if not f or f["state"] != "active":
                return _err(404, "FOLDER_NOT_FOUND", "no")
            if bad := fake._if_match(if_match, f["revision"]):
                return bad
            f["state"] = "deleted"
            f["revision"] += 1
            for a in fake.artifacts.values():
                if a["parent_id"] == fid:
                    a["state"] = "deleted"
            return f

        @app.post("/v0/drives/{did}/artifacts", status_code=201)
        async def create_artifact(did: str, request: Request) -> Any:
            form = await request.form()
            content = form["content"]
            assert isinstance(content, UploadFile)
            data = await content.read()
            aid = f"art_{uuid.uuid4().hex[:16]}"
            fake.artifacts[aid] = {"id": aid, "drive_id": did, "name": form["name"],
                                   "parent_id": form["parent_id"], "revision": 1, "state": "active"}
            vid = f"ver_{uuid.uuid4().hex[:16]}"
            fake.versions[aid] = [{"id": vid, "version_number": 1, "bytes": data}]
            return fake.artifacts[aid]

        def _live(aid: str) -> dict[str, Any] | None:
            a = fake.artifacts.get(aid)
            return a if a and a["state"] == "active" else None

        @app.get("/v0/drives/{did}/artifacts")
        async def list_artifacts(did: str, request: Request) -> Any:
            if bad := fake._known(request, {F, "limit", "cursor", "parent_id", "name",
                                            "content_type", "label", "updated_after",
                                            "updated_before"}):
                return bad
            want = request.query_params.get(F, "active")
            parent = request.query_params.get("parent_id")
            items = [a for a in fake.artifacts.values()
                     if a["drive_id"] == did and (want == "all" or a["state"] == want)
                     and (parent is None or a["parent_id"] == parent)]
            return {"items": [{k: v for k, v in a.items()} for a in items]}

        @app.get("/v0/drives/{did}/entries")
        async def list_entries(did: str, request: Request) -> Any:
            if bad := fake._known(request, {"parent_id", "type", "name", "label", "content_type",
                                            "updated_after", "updated_before", "state", "limit",
                                            "cursor"}):
                return bad
            parent = request.query_params.get("parent_id")
            want = request.query_params.get("state", "active")
            items = [a for a in fake.artifacts.values()
                     if a["drive_id"] == did and (want == "all" or a["state"] == want)
                     and (parent is None or a["parent_id"] == parent)]
            return {"entries": items, "next_cursor": None}

        @app.get("/v0/drives/{did}/changes")
        async def changes(did: str, request: Request) -> Any:
            if bad := fake._known(request, {"limit", "start", "cursor", "type", "order"}):
                return bad
            start = request.query_params.get("start")
            cursor = request.query_params.get("cursor")
            fake.change_starts.append(start)
            if (start is None) == (cursor is None):
                return _err(400, "INVALID_REQUEST", "pass exactly one of start or cursor")
            if start not in (None, "now", "beginning"):
                return _err(400, "INVALID_ARGUMENT", "bad start")
            return {"items": [], "next_cursor": None}

        @app.get("/view/")
        async def viewer_shell() -> Any:
            return Response("<html>viewer</html>", media_type="text/html")

        @app.get("/v0/drives/{did}/search")
        async def search(did: str, request: Request) -> Any:
            if bad := fake._known(request, {"q", "mode", "limit", "cursor", "parent_id",
                                            "content_type", "label", "updated_after",
                                            "updated_before"}):
                return bad
            return {"items": []}

        @app.get("/v0/drives/{did}/grants")
        async def grants(did: str, request: Request) -> Any:
            if bad := fake._known(request, {F, "limit", "cursor", "resource_type",
                                            "resource_id", "principal_type"}):
                return bad
            return {"items": []}

        @app.get("/v0/drives/{did}/artifacts/{aid}")
        async def get_artifact(did: str, aid: str) -> Any:
            return _live(aid) or _err(404, "ARTIFACT_NOT_FOUND", "no")

        @app.get("/v0/drives/{did}/artifacts/{aid}/content")
        async def content(did: str, aid: str) -> Any:
            if not _live(aid):
                return _err(404, "ARTIFACT_NOT_FOUND", "no")
            return Response(fake.versions[aid][-1]["bytes"], media_type="text/markdown")

        @app.post("/v0/drives/{did}/artifacts/{aid}/versions", status_code=201)
        async def add_version(did: str, aid: str, request: Request,
                              if_match: str | None = Header(default=None)) -> Any:
            a = _live(aid)
            if not a:
                return _err(404, "ARTIFACT_NOT_FOUND", "no")
            if bad := fake._if_match(if_match, a["revision"]):
                return bad
            form = await request.form()
            content = form["content"]
            assert isinstance(content, UploadFile)
            data = await content.read()
            a["revision"] += 1
            vid = f"ver_{uuid.uuid4().hex[:16]}"
            fake.versions[aid].append({"id": vid, "version_number": len(fake.versions[aid]) + 1,
                                       "bytes": data})
            return {"id": vid, "artifact_id": aid, "artifact_revision": f"rev_{a['revision']:016x}"}

        @app.get("/v0/drives/{did}/artifacts/{aid}/versions")
        async def list_versions(did: str, aid: str, request: Request) -> Any:
            if bad := fake._known(request, {"limit", "cursor"}):
                return bad
            if not _live(aid):
                return _err(404, "ARTIFACT_NOT_FOUND", "no")
            return {"items": [{"id": v["id"], "version_number": v["version_number"]}
                              for v in fake.versions[aid]]}

        @app.delete("/v0/drives/{did}/artifacts/{aid}")
        async def delete_artifact(did: str, aid: str,
                                  if_match: str | None = Header(default=None)) -> Any:
            a = _live(aid)
            if not a:
                return _err(404, "ARTIFACT_NOT_FOUND", "no")
            if bad := fake._if_match(if_match, a["revision"]):
                return bad
            a["state"] = "deleted"
            a["revision"] += 1
            return a

        @app.post("/v0/drives/{did}/artifacts/{aid}/restore")
        async def restore_artifact(did: str, aid: str, request: Request,
                                   if_match: str | None = Header(default=None)) -> Any:
            if await request.body():
                return _err(400, "INVALID_ARGUMENT", "no body")
            a = fake.artifacts.get(aid)
            if not a or a["state"] != "deleted":
                return _err(404, "ARTIFACT_NOT_FOUND", "no")
            if bad := fake._if_match(if_match, a["revision"]):
                return bad
            a["state"] = "active"
            a["revision"] += 1
            return a

        @app.post("/v0/drives/{did}/download-capabilities")
        async def download_capability(did: str, request: Request) -> Any:
            if request.headers.get("idempotency-key"):
                return _err(400, "INVALID_ARGUMENT", "idempotency forbidden here")
            body = await request.json()
            aid = body["target"]["artifact_id"]
            if not _live(aid):
                return _err(404, "ARTIFACT_NOT_FOUND", "no")
            tok = uuid.uuid4().hex
            fake.downloads[tok] = aid
            return {"download": {"target": {"url": f"http://testserver/dl/{tok}"}}}

        @app.get("/dl/{tok}")
        async def download(tok: str) -> Any:
            aid = fake.downloads.get(tok)
            if not aid:
                return _err(404, "NOT_FOUND", "no")
            return Response(fake.versions[aid][-1]["bytes"], media_type="text/markdown")

        @app.post("/v0/drives/{did}/shares", status_code=201)
        async def create_share(did: str, request: Request) -> Any:
            body = await request.json()
            sid = f"shr_{uuid.uuid4().hex[:16]}"
            key = f"sk_{uuid.uuid4().hex}"
            fake.shares[sid] = {"id": sid, "resource_type": body["resource_type"],
                                "resource_id": body["resource_id"], "revision": 1,
                                "state": "active", "url": f"http://testserver/s/{key}"}
            fake.share_keys[key] = sid
            return fake.shares[sid]

        @app.get("/v0/drives/{did}/shares")
        async def list_shares(did: str, request: Request) -> Any:
            if bad := fake._known(request, {F, "limit", "cursor", "resource_type",
                                            "resource_id"}):
                return bad
            want = request.query_params.get(F, "active")
            return {"items": [s for s in fake.shares.values()
                              if want == "all" or s["state"] == want]}

        @app.get("/v0/drives/{did}/shares/{sid}")
        async def get_share(did: str, sid: str) -> Any:
            return fake.shares.get(sid) or _err(404, "SHARE_NOT_FOUND", "no")

        @app.delete("/v0/drives/{did}/shares/{sid}")
        async def revoke_share(did: str, sid: str,
                               if_match: str | None = Header(default=None)) -> Any:
            s = fake.shares.get(sid)
            if not s or s["state"] != "active":
                return _err(404, "SHARE_NOT_FOUND", "no")
            if bad := fake._if_match(if_match, s["revision"]):
                return bad
            s["state"] = "revoked"
            s["revision"] += 1
            return s

        @app.get("/s/{key}")
        async def open_share(key: str) -> Any:
            sid = fake.share_keys.get(key)
            if sid and fake.shares[sid]["state"] == "active":
                return Response("<html>shared</html>", media_type="text/html")
            return _err(404, "NOT_FOUND", "no")

        @app.post("/v0/drives/{did}/artifacts/{aid}/viewer-sessions")
        async def viewer_session(did: str, aid: str) -> Any:
            if not _live(aid):
                return _err(404, "ARTIFACT_NOT_FOUND", "no")
            return {"artifact_id": aid, "credential": "vs_" + uuid.uuid4().hex,
                    "expires_at": "2026-09-12T00:00:00Z"}

        return app


def _config(fake: FakeDrive, probe: str, drive_id: str | None) -> ProberConfig:
    return ProberConfig(
        origin="http://testserver",
        token_endpoint="http://testserver/oidc/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        audience="http://testserver",
        probe=probe,  # type: ignore[arg-type]
        scopes="drives:read content:read content:write sharing:read sharing:write changes:read",
        drive_id=drive_id,
        viewer_host="viewer.example.test" if probe == "full" else None,
    )


def _clients(fake: FakeDrive) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=fake.app)
    return (
        httpx.AsyncClient(transport=transport, base_url="http://testserver"),
        httpx.AsyncClient(transport=transport),
    )


async def _run(fake: FakeDrive, probe: str, drive_id: str | None):
    api, anon = _clients(fake)
    async with api, anon:
        return await Battery(_config(fake, probe, drive_id), api=api, anon=anon).run()


# ── tests ────────────────────────────────────────────────────────────


async def test_light_battery_is_green_and_cleans_up() -> None:
    fake = FakeDrive()
    probe_drive = fake.add_drive("Token Canopy probe drive")
    result = await _run(fake, "light", probe_drive["id"])
    failed = [(r.name, r.status, r.detail) for r in result.rows if not r.ok]
    assert failed == []
    assert result.outcome == "succeeded"
    names = [r.name for r in result.rows]
    assert names[0] == "token minted"
    assert "artifacts listed with state=deleted" in names
    assert "folders listed with state=deleted" in names
    assert "entries listed with state=deleted" in names
    assert names[-1] == "folders listed with state=deleted"
    assert fake.change_starts == ["beginning"]
    # Everything the run created is gone; the probe drive itself remains.
    assert fake.drives[probe_drive["id"]]["state"] == "active"
    assert all(f["state"] == "deleted" for f in fake.folders.values()
               if f["parent_id"] == probe_drive["root_folder_id"])
    assert all(a["state"] == "deleted" for a in fake.artifacts.values())


async def test_full_battery_creates_and_deletes_its_own_drive() -> None:
    fake = FakeDrive()
    result = await _run(fake, "full", None)
    failed = [(r.name, r.status, r.detail) for r in result.rows if not r.ok]
    assert failed == []
    names = [r.name for r in result.rows]
    assert names[1] == "drive created"
    assert "viewer session minted" in names
    assert names[-2] == "synthetic drive deleted"
    assert names[-1] == "drives listed with state=deleted"
    assert "drives listed with state=deleted" in names
    assert len(fake.drives) == 1
    assert next(iter(fake.drives.values()))["state"] == "deleted"


async def test_the_old_filter_spelling_goes_red_not_green() -> None:
    """A rename skew from the caller's side: the service only
    knows `lifecycle`; the prober sends `state`; every filtered listing is
    a 400 INVALID_QUERY and the battery fails — while still cleaning up."""
    fake = FakeDrive(contract="lifecycle")
    probe_drive = fake.add_drive("Token Canopy probe drive")
    result = await _run(fake, "light", probe_drive["id"])
    assert result.outcome == "failed"
    red = {r.name: (r.status, r.detail) for r in result.rows if not r.ok}
    assert red["drives listed with state=active"] == (400, "wanted HTTP 200")
    assert red["artifacts listed with state=active"] == (400, "wanted HTTP 200")
    assert red["shares listed with state=all"] == (400, "wanted HTTP 200")
    assert "probe folder deleted" in {r.name for r in result.rows if r.ok}


async def test_a_failed_precondition_skips_dependents_without_a_traceback() -> None:
    fake = FakeDrive()
    probe_drive = fake.add_drive("Token Canopy probe drive")
    # Break uploads: the artifact route answers 503.
    @fake.app.middleware("http")
    async def _break(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "POST" and request.url.path.endswith("/artifacts"):
            return _err(503, "UNAVAILABLE", "object store down")
        return await call_next(request)

    result = await _run(fake, "light", probe_drive["id"])
    by_name = {r.name: r for r in result.rows}
    assert by_name["artifact uploaded and finalized"].status == 503
    assert by_name["second version appended"].detail == "skipped: no artifact from an earlier row"
    assert by_name["probe folder deleted"].ok  # cleanup still ran


async def test_nothing_secret_reaches_logs_or_summary(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeDrive()
    probe_drive = fake.add_drive("Token Canopy probe drive")
    with caplog.at_level(logging.INFO, logger="agentdrive.prober"):
        result = await _run(fake, "light", probe_drive["id"])
    # This unit boundary owns only the prober's structured records. The CLI
    # test below covers whole-process stderr after logging is configured,
    # including suppressing possession URLs emitted by dependencies.
    text = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "agentdrive.prober"
    ) + json.dumps(result.as_dict())
    assert TOKEN not in text
    assert CLIENT_SECRET not in text
    assert "/s/sk_" not in text
    assert "/dl/" not in text
    assert "at=prober_row_ok" in text


async def test_seed_is_idempotent() -> None:
    fake = FakeDrive()
    api, _anon = _clients(fake)
    api.headers["Authorization"] = f"Bearer {TOKEN}"
    async with api:
        first, created = await seed_probe_drive(api, name="Token Canopy probe drive")
        second, created_again = await seed_probe_drive(api, name="Token Canopy probe drive")
    assert created is True and created_again is False
    assert first == second


def test_config_from_env_defaults_and_requirements() -> None:
    base = {
        "PROBER_ORIGIN": "https://drive.example.test/",
        "HUB_ISSUER": "https://auth.example.test/oidc",
        "PROBER_CLIENT_ID": CLIENT_ID,
        "PROBER_CLIENT_SECRET": CLIENT_SECRET,
        "PROBER_DRIVE_ID": "drv_0123456789abcdef",
    }
    cfg = prober_job.config_from_env(base, probe="light")
    assert cfg.origin == "https://drive.example.test"
    assert cfg.token_endpoint == "https://auth.example.test/oidc/token"
    assert cfg.audience == cfg.origin
    assert "drives:write" not in cfg.scopes
    assert "drives:write" in prober_job.config_from_env(base, probe="full").scopes
    with pytest.raises(SystemExit, match="PROBER_VIEWER_HOST is required"):
        prober_job.config_from_env(base, probe="full", require_viewer=True)
    full = prober_job.config_from_env(
        {**base, "PROBER_VIEWER_HOST": "viewer.example.test"},
        probe="full",
        require_viewer=True,
    )
    assert full.viewer_host == "viewer.example.test"
    with pytest.raises(ValueError, match="bare hostname"):
        prober_job.config_from_env(
            {**base, "PROBER_VIEWER_HOST": "https://viewer.example.test/view/"},
            probe="full",
        )
    with pytest.raises(SystemExit):
        prober_job.config_from_env({**base, "PROBER_CLIENT_SECRET": ""}, probe="light")
    with pytest.raises(ValueError):
        prober_job.config_from_env({**base, "PROBER_DRIVE_ID": ""}, probe="light")


def test_main_exit_code_follows_the_battery(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    fake = FakeDrive(contract="lifecycle")
    probe_drive = fake.add_drive("Token Canopy probe drive")

    def clients(cfg: ProberConfig):  # in-process transport instead of sockets
        return _clients(fake)

    monkeypatch.setattr(prober_job, "_clients", clients)
    env = {
        "PROBER_ORIGIN": "http://testserver",
        "PROBER_TOKEN_ENDPOINT": "http://testserver/oidc/token",
        "PROBER_CLIENT_ID": CLIENT_ID,
        "PROBER_CLIENT_SECRET": CLIENT_SECRET,
        "PROBER_DRIVE_ID": probe_drive["id"],
    }
    assert prober_job.main(["run-once", "--light"], env=env) == 1
    captured = capsys.readouterr()
    summary = json.loads(captured.out.strip().splitlines()[-1])
    assert summary["outcome"] == "failed"
    assert summary["probe"] == "light"
    assert summary["rows_failed"] >= 3
    assert "/s/sk_" not in captured.err
    assert "/dl/" not in captured.err

    fake.contract = "state"
    fake.filter = "state"
    fake.app = fake._build()
    assert prober_job.main(["run-once", "--light"], env=env) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip().splitlines()[-1])["outcome"] == "succeeded"
    assert "/s/sk_" not in captured.err
    assert "/dl/" not in captured.err


def test_the_job_imports_without_the_service_secrets() -> None:
    """The prober runs with no DATABASE_URL / GCS_BUCKET / SESSION_SECRET.
    `agentdrive.observability` would pull `config.settings` in at import and
    refuse to start; the job must not touch it."""
    env = {k: v for k, v in os.environ.items()
           if k not in {"DATABASE_URL", "GCS_BUCKET", "SESSION_SECRET"}}
    env["PYTHONPATH"] = "src"
    proc = subprocess.run(
        [sys.executable, "-m", "agentdrive.jobs.prober", "--help"],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "run-once" in proc.stdout
