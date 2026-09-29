"""The AgentDrive prober: one battery of `/v0` calls, as an external caller.

Design: ``docs/superpowers/specs/2026-09-12-production-probers-design.md``
§3. This is the port of ``deploy/beta-journey-rest.sh`` into the image, so
it can run as a scheduled Cloud Run job and as the release lanes' post-flip
bake gate. The shell script stays the operator's by-hand tool.

Two batteries:

* **light** — against a fixed probe drive that ``seed`` created once and
  nothing deletes. Every artifact it creates it deletes. Needs no
  ``drives:write``.
* **full** — the light battery inside a drive created for the run, plus
  the viewer-session mint and the viewer-origin isolation check, and the
  drive is deleted at the end even when a row failed.

Everything a row learns is reduced to a status code and a short detail
string before it is logged. A token, a share URL, a signed object-store
URL or a body is never logged and never appears in the summary — the
detail strings are built from row names and integers only.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

log = logging.getLogger("agentdrive.prober")

Probe = Literal["light", "full"]

LIGHT_SCOPES = (
    "drives:read content:read content:write changes:read sharing:read sharing:write usage:read"
)
FULL_SCOPES = LIGHT_SCOPES + " drives:write"

V1_BODY = b"# Prober v1\n\nfirst body\n"
V2_BODY = b"# Prober v2\n\nsecond body\n"


@dataclass(frozen=True)
class ProberConfig:
    origin: str
    token_endpoint: str
    client_id: str
    client_secret: str
    audience: str
    probe: Probe
    scopes: str
    drive_id: str | None = None
    viewer_host: str | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.origin.endswith("/"):
            raise ValueError("origin must not end with '/'")
        if self.probe == "light" and not self.drive_id:
            raise ValueError("the light battery needs drive_id (run `seed` first)")
        if self.viewer_host is not None:
            _bare_host(self.viewer_host)


@dataclass
class Row:
    name: str
    ok: bool
    status: int | None
    detail: str
    ms: int


@dataclass
class Result:
    probe: Probe
    rows: list[Row] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def failed(self) -> bool:
        return any(not r.ok for r in self.rows)

    @property
    def outcome(self) -> str:
        return "failed" if self.failed else "succeeded"

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "outcome": self.outcome,
            "rows_passed": sum(1 for r in self.rows if r.ok),
            "rows_failed": sum(1 for r in self.rows if not r.ok),
            "duration_ms": self.duration_ms,
            "rows": [
                {"name": r.name, "ok": r.ok, "status": r.status, "detail": r.detail, "ms": r.ms}
                for r in self.rows
            ],
        }


class RowFailed(Exception):
    """A row's precondition is missing (an earlier row failed), so the row
    cannot run. Recorded as a failure with that detail, never a traceback."""


@dataclass
class _Outcome:
    ok: bool
    status: int | None = None
    detail: str = ""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bare_host(host: str) -> str:
    """Return a normalized hostname, rejecting URL-shaped viewer config."""
    value = host.strip()
    if value != host or not value or any(char in value for char in "/?#@"):
        raise ValueError("viewer_host must be a bare hostname without a scheme, port, or path")
    try:
        parsed = httpx.URL(f"https://{value}")
    except ValueError as exc:
        raise ValueError(
            "viewer_host must be a bare hostname without a scheme, port, or path"
        ) from exc
    if parsed.host is None or parsed.port is not None or parsed.path not in ("", "/"):
        raise ValueError("viewer_host must be a bare hostname without a scheme, port, or path")
    return parsed.host


def _key(row: str) -> str:
    return f"prober-{row}-{uuid.uuid4().hex}"


def _items(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict):
        for k in ("items", "artifacts", "folders", "entries", "shares", "grants", "versions"):
            if isinstance(body.get(k), list):
                return body[k]
        return []
    return body if isinstance(body, list) else []


class Battery:
    """One prober run. Construct with the two HTTP clients so tests can hand
    in an in-process ASGI transport; production hands in real ones."""

    def __init__(
        self,
        config: ProberConfig,
        *,
        api: httpx.AsyncClient,
        anon: httpx.AsyncClient,
    ) -> None:
        self.cfg = config
        self.api = api
        self.anon = anon
        self.result = Result(probe=config.probe)
        # State handed from row to row. None until the producing row passed.
        self.drive_id: str | None = config.drive_id
        self.root_id: str | None = None
        self.folder_id: str | None = None
        self.artifact_id: str | None = None
        self.artifact_rev: str | None = None
        self.share_id: str | None = None
        self.share_url: str | None = None
        self.download_url: str | None = None
        self.deleted_rev: str | None = None
        self._created_drive = False

    # ── driver ─────────────────────────────────────────────────────────

    async def run(self) -> Result:
        started = time.monotonic()
        try:
            await self._row("token minted", self.row_token)
            if self.cfg.probe == "full":
                await self._row("drive created", self.row_create_drive)
            await self._row("drive read", self.row_drive_read)
            await self._row("drives listed with state=active", self.row_drives_active)
            await self._row("usage read", self.row_usage)
            await self._row("folder created", self.row_folder_created)
            await self._row("folders listed with state=active", self.row_folders_active)
            await self._row("artifact uploaded and finalized", self.row_upload)
            await self._row("content round-trips byte-identical", self.row_content_v1)
            await self._row("second version appended", self.row_version)
            await self._row("version history has both versions", self.row_versions)
            await self._row("head serves the new version", self.row_content_v2)
            await self._row("entries listed with state=active", self.row_entries_active)
            await self._row("artifacts listed with state=active", self.row_artifacts_active)
            await self._row("changes listed", self.row_changes)
            await self._row("search answers", self.row_search)
            await self._row("grants listed with state=active", self.row_grants)
            await self._row("download capability minted", self.row_download_mint)
            await self._row("signed download returns the exact bytes", self.row_download_fetch)
            await self._row("share created", self.row_share_created)
            await self._row("a signed-out recipient can open the share", self.row_share_open)
            await self._row("share revoked", self.row_share_revoked)
            await self._row("the revoked link is a uniform 404", self.row_share_gone)
            await self._row("shares listed with state=all", self.row_shares_all)
            if self.cfg.probe == "full":
                await self._row("viewer session minted", self.row_viewer_session)
                await self._row("viewer origin is live and distinct", self.row_viewer_origin)
            await self._row("artifact deleted", self.row_artifact_deleted)
            await self._row("entries listed with state=deleted", self.row_entries_deleted)
            await self._row("artifacts listed with state=deleted", self.row_artifacts_deleted)
            await self._row("artifact restored", self.row_artifact_restored)
            await self._row("restored artifact serves its exact bytes", self.row_content_restored)
        finally:
            await self._cleanup()
            self.result.duration_ms = int((time.monotonic() - started) * 1000)
        return self.result

    async def _row(self, name: str, fn: Callable[[], Awaitable[_Outcome]]) -> None:
        started = time.monotonic()
        try:
            out = await fn()
        except RowFailed as exc:
            out = _Outcome(False, None, f"skipped: {exc}")
        except httpx.HTTPError as exc:
            # The exception's text can carry the URL; keep the class only.
            out = _Outcome(False, None, f"transport error: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 — a row must never take the battery down
            out = _Outcome(False, None, f"unexpected: {type(exc).__name__}")
        ms = int((time.monotonic() - started) * 1000)
        row = Row(name=name, ok=out.ok, status=out.status, detail=out.detail, ms=ms)
        self.result.rows.append(row)
        if row.ok:
            log.info(
                "at=prober_row_ok probe=%s row=%r status=%s ms=%d",
                self.cfg.probe,
                name,
                row.status,
                ms,
            )
        else:
            log.warning(
                "at=prober_row_failed probe=%s row=%r status=%s ms=%d detail=%r",
                self.cfg.probe,
                name,
                row.status,
                ms,
                row.detail,
            )

    async def _cleanup(self) -> None:
        """Delete what this run created, even after a failed row. The light
        battery deletes its folder (recursively — the artifact goes with it);
        the full battery deletes its drive."""
        if self.cfg.probe == "full":
            if self._created_drive and self.drive_id:
                await self._row("synthetic drive deleted", self.row_delete_drive)
                await self._row("drives listed with state=deleted", self.row_drives_deleted)
            return
        if self.folder_id and self.drive_id:
            await self._row("probe folder deleted", self.row_delete_folder)
            await self._row("folders listed with state=deleted", self.row_folders_deleted)

    # ── helpers ────────────────────────────────────────────────────────

    def _need(self, value: str | None, what: str) -> str:
        if not value:
            raise RowFailed(f"no {what} from an earlier row")
        return value

    async def _get_revision(self, path: str) -> str:
        r = await self.api.get(path)
        if r.status_code != 200:
            raise RowFailed(f"could not read revision (HTTP {r.status_code})")
        rev = r.json().get("revision")
        if not rev:
            raise RowFailed("resource carries no revision")
        return str(rev)

    @staticmethod
    def _expect(r: httpx.Response, status: int, detail: str = "") -> _Outcome:
        if r.status_code == status:
            return _Outcome(True, r.status_code, detail)
        return _Outcome(False, r.status_code, f"wanted HTTP {status}")

    # ── rows ───────────────────────────────────────────────────────────

    async def row_token(self) -> _Outcome:
        r = await self.anon.post(
            self.cfg.token_endpoint,
            auth=(self.cfg.client_id, self.cfg.client_secret),
            data={
                "grant_type": "client_credentials",
                "resource": self.cfg.audience,
                "scope": self.cfg.scopes,
            },
        )
        if r.status_code != 200:
            return _Outcome(False, r.status_code, "token endpoint refused the credential")
        token = r.json().get("access_token")
        if not token:
            return _Outcome(False, r.status_code, "no access_token in the response")
        self.api.headers["Authorization"] = f"Bearer {token}"
        return _Outcome(True, r.status_code, f"expires_in={r.json().get('expires_in')}")

    async def row_create_drive(self) -> _Outcome:
        r = await self.api.post(
            "/v0/drives",
            json={"name": f"prober-{uuid.uuid4().hex[:12]}"},
            headers={"Idempotency-Key": _key("drive")},
        )
        out = self._expect(r, 201)
        if out.ok:
            self.drive_id = r.json()["id"]
            self.root_id = r.json().get("root_folder_id")
            self._created_drive = True
        return out

    async def row_drive_read(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        r = await self.api.get(f"/v0/drives/{d}")
        out = self._expect(r, 200)
        if out.ok:
            self.root_id = r.json().get("root_folder_id") or self.root_id
            if not self.root_id:
                return _Outcome(False, r.status_code, "drive carries no root_folder_id")
        return out

    async def _row_drives_listed(self, state: str) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        return await self._listing_contains(
            "/v0/drives", {"state": state, "limit": 100}, d, expected_state=state
        )

    async def row_drives_active(self) -> _Outcome:
        return await self._row_drives_listed("active")

    async def row_drives_deleted(self) -> _Outcome:
        return await self._row_drives_listed("deleted")

    async def row_usage(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        return self._expect(await self.api.get(f"/v0/drives/{d}/usage"), 200)

    async def row_folder_created(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        root = self._need(self.root_id, "root folder")
        r = await self.api.post(
            f"/v0/drives/{d}/folders",
            json={"name": f"prober-{uuid.uuid4().hex[:8]}", "parent_id": root},
            headers={"Idempotency-Key": _key("folder")},
        )
        out = self._expect(r, 201)
        if out.ok:
            self.folder_id = r.json()["id"]
        return out

    async def row_upload(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        f = self._need(self.folder_id, "folder")
        r = await self.api.post(
            f"/v0/drives/{d}/artifacts",
            data={"name": "prober.md", "parent_id": f},
            files={"content": ("prober.md", V1_BODY, "text/markdown")},
            headers={"Idempotency-Key": _key("artifact")},
        )
        out = self._expect(r, 201)
        if out.ok:
            self.artifact_id = r.json()["id"]
            self.artifact_rev = str(r.json().get("revision", ""))
        return out

    async def _content_matches(self, body: bytes, path: str) -> _Outcome:
        r = await self.api.get(path)
        if r.status_code != 200:
            return _Outcome(False, r.status_code, "wanted HTTP 200")
        if _sha(r.content) != _sha(body):
            return _Outcome(False, r.status_code, "sha256 mismatch")
        return _Outcome(True, r.status_code, f"bytes={len(r.content)}")

    async def row_content_v1(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        return await self._content_matches(V1_BODY, f"/v0/drives/{d}/artifacts/{a}/content")

    async def row_version(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        rev = self._need(self.artifact_rev, "artifact revision")
        r = await self.api.post(
            f"/v0/drives/{d}/artifacts/{a}/versions",
            files={"content": ("prober.md", V2_BODY, "text/markdown")},
            headers={"Idempotency-Key": _key("version"), "If-Match": f'"{rev}"'},
        )
        out = self._expect(r, 201)
        if out.ok and r.headers.get("content-type", "").startswith("application/json"):
            self.artifact_rev = str(r.json().get("artifact_revision") or self.artifact_rev)
        return out

    async def row_versions(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        r = await self.api.get(f"/v0/drives/{d}/artifacts/{a}/versions")
        out = self._expect(r, 200)
        if not out.ok:
            return out
        n = len(_items(r.json()))
        if n != 2:
            return _Outcome(False, r.status_code, f"history has {n} versions, wanted 2")
        return _Outcome(True, r.status_code, "versions=2")

    async def row_content_v2(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        return await self._content_matches(V2_BODY, f"/v0/drives/{d}/artifacts/{a}/content")

    async def _listing_contains(
        self,
        path: str,
        params: dict[str, Any],
        wanted: str,
        *,
        expected_state: str | None = None,
    ) -> _Outcome:
        r = await self.api.get(path, params=params)
        out = self._expect(r, 200)
        if not out.ok:
            return out
        items = _items(r.json())
        ids = {item.get("id") for item in items}
        if wanted not in ids:
            return _Outcome(False, r.status_code, "expected item absent from the listing")
        if expected_state is not None and not any(
            item.get("id") == wanted and item.get("state") == expected_state for item in items
        ):
            return _Outcome(False, r.status_code, "expected item has the wrong state")
        return _Outcome(True, r.status_code, f"items={len(ids)}")

    async def row_entries_active(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        f = self._need(self.folder_id, "folder")
        a = self._need(self.artifact_id, "artifact")
        return await self._listing_contains(
            f"/v0/drives/{d}/entries", {"parent_id": f, "state": "active"}, a,
            expected_state="active",
        )

    async def row_entries_deleted(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        f = self._need(self.folder_id, "folder")
        a = self._need(self.artifact_id, "artifact")
        return await self._listing_contains(
            f"/v0/drives/{d}/entries", {"parent_id": f, "state": "deleted"}, a,
            expected_state="deleted",
        )

    async def row_folders_active(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        root = self._need(self.root_id, "root folder")
        f = self._need(self.folder_id, "folder")
        return await self._listing_contains(
            f"/v0/drives/{d}/folders", {"parent_id": root, "state": "active"}, f,
            expected_state="active",
        )

    async def row_artifacts_active(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        f = self._need(self.folder_id, "folder")
        a = self._need(self.artifact_id, "artifact")
        return await self._listing_contains(
            f"/v0/drives/{d}/artifacts", {"parent_id": f, "state": "active"}, a,
            expected_state="active",
        )

    async def row_changes(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        r = await self.api.get(
            f"/v0/drives/{d}/changes", params={"start": "beginning", "limit": 10}
        )
        return self._expect(r, 200)

    async def row_search(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        r = await self.api.get(f"/v0/drives/{d}/search", params={"q": "prober"})
        return self._expect(r, 200)

    async def row_grants(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        return self._expect(
            await self.api.get(f"/v0/drives/{d}/grants", params={"state": "active"}), 200
        )

    async def row_download_mint(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        # No Idempotency-Key: the manifest marks this operation idempotency-
        # forbidden — every request mints a fresh target.
        r = await self.api.post(
            f"/v0/drives/{d}/download-capabilities",
            json={"target": {"kind": "artifact", "artifact_id": a}},
        )
        out = self._expect(r, 200)
        if out.ok:
            url = ((r.json().get("download") or {}).get("target") or {}).get("url")
            if not url:
                return _Outcome(False, r.status_code, "mint returned no url field")
            self.download_url = url
        return out

    async def row_download_fetch(self) -> _Outcome:
        url = self._need(self.download_url, "download url")
        r = await self.anon.get(url)
        if r.status_code != 200:
            return _Outcome(False, r.status_code, "wanted HTTP 200")
        if _sha(r.content) != _sha(V2_BODY):
            return _Outcome(False, r.status_code, "sha256 mismatch")
        return _Outcome(True, r.status_code, "no product credential")

    async def row_share_created(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        r = await self.api.post(
            f"/v0/drives/{d}/shares",
            json={"resource_type": "artifact", "resource_id": a},
            headers={"Idempotency-Key": _key("share")},
        )
        out = self._expect(r, 201)
        if out.ok:
            body = r.json()
            self.share_id = body["id"]
            self.share_url = body.get("url") or body.get("share_url")
            if not self.share_url:
                return _Outcome(False, r.status_code, "share response carried no url")
        return out

    async def row_share_open(self) -> _Outcome:
        url = self._need(self.share_url, "share url")
        return self._expect(await self.anon.get(url), 200)

    async def row_share_revoked(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        s = self._need(self.share_id, "share")
        rev = await self._get_revision(f"/v0/drives/{d}/shares/{s}")
        r = await self.api.delete(
            f"/v0/drives/{d}/shares/{s}",
            headers={"Idempotency-Key": _key("revoke"), "If-Match": f'"{rev}"'},
        )
        return self._expect(r, 200)

    async def row_share_gone(self) -> _Outcome:
        url = self._need(self.share_url, "share url")
        r = await self.anon.get(url)
        # 404, not 403: a revoked link must not confirm it ever existed.
        return self._expect(r, 404)

    async def row_shares_all(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        s = self._need(self.share_id, "share")
        return await self._listing_contains(
            f"/v0/drives/{d}/shares",
            {"state": "all", "resource_type": "artifact", "resource_id": a},
            s,
        )

    async def row_viewer_session(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        r = await self.api.post(
            f"/v0/drives/{d}/artifacts/{a}/viewer-sessions",
            json={},
            headers={"Idempotency-Key": _key("viewer")},
        )
        out = self._expect(r, 200)
        if not out.ok:
            return out
        body = r.json()
        if body.get("artifact_id") != a or not body.get("credential") or not body.get("expires_at"):
            return _Outcome(False, r.status_code, "session not bound with an expiring credential")
        return _Outcome(True, r.status_code, "bound")

    async def row_viewer_origin(self) -> _Outcome:
        host = self.cfg.viewer_host
        if not host:
            return _Outcome(False, None, "viewer host is required for the full battery")
        viewer_host = _bare_host(host)
        api_host = httpx.URL(self.cfg.origin).host
        if viewer_host == api_host:
            return _Outcome(False, None, "the viewer origin is the API origin")
        r = await self.anon.get(f"https://{viewer_host}/view/")
        if r.status_code == 200:
            return _Outcome(True, r.status_code, "viewer shell answered on the distinct origin")
        return _Outcome(
            False, r.status_code, "viewer shell did not answer on the configured origin"
        )

    async def row_artifact_deleted(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        rev = await self._get_revision(f"/v0/drives/{d}/artifacts/{a}")
        r = await self.api.delete(
            f"/v0/drives/{d}/artifacts/{a}",
            headers={"Idempotency-Key": _key("delete"), "If-Match": f'"{rev}"'},
        )
        out = self._expect(r, 200)
        if out.ok:
            # From the DELETE response, not a re-read: a deleted artifact is
            # filtered out of every read path.
            self.deleted_rev = str(r.json().get("revision", ""))
        return out

    async def row_artifacts_deleted(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        f = self._need(self.folder_id, "folder")
        a = self._need(self.artifact_id, "artifact")
        return await self._listing_contains(
            f"/v0/drives/{d}/artifacts", {"parent_id": f, "state": "deleted"}, a,
            expected_state="deleted",
        )

    async def row_folders_deleted(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        root = self._need(self.root_id, "root folder")
        f = self._need(self.folder_id, "folder")
        return await self._listing_contains(
            f"/v0/drives/{d}/folders", {"parent_id": root, "state": "deleted"}, f,
            expected_state="deleted",
        )

    async def row_artifact_restored(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        rev = self._need(self.deleted_rev, "post-delete revision")
        # No body and no Content-Type: this endpoint refuses a body.
        r = await self.api.post(
            f"/v0/drives/{d}/artifacts/{a}/restore",
            headers={"Idempotency-Key": _key("restore"), "If-Match": f'"{rev}"'},
        )
        return self._expect(r, 200)

    async def row_content_restored(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        a = self._need(self.artifact_id, "artifact")
        return await self._content_matches(V2_BODY, f"/v0/drives/{d}/artifacts/{a}/content")

    async def row_delete_folder(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        f = self._need(self.folder_id, "folder")
        rev = await self._get_revision(f"/v0/drives/{d}/folders/{f}")
        r = await self.api.delete(
            f"/v0/drives/{d}/folders/{f}",
            params={"recursive": "true"},
            headers={"Idempotency-Key": _key("rm-folder"), "If-Match": f'"{rev}"'},
        )
        return self._expect(r, 200)

    async def row_delete_drive(self) -> _Outcome:
        d = self._need(self.drive_id, "drive")
        rev = await self._get_revision(f"/v0/drives/{d}")
        r = await self.api.delete(
            f"/v0/drives/{d}",
            headers={"Idempotency-Key": _key("rm-drive"), "If-Match": f'"{rev}"'},
        )
        return self._expect(r, 200)


async def seed_probe_drive(api: httpx.AsyncClient, *, name: str) -> tuple[str, bool]:
    """Find or create the light battery's fixed drive. Returns (id, created).
    Idempotent: a drive with this exact name in the first page is reused."""
    r = await api.get("/v0/drives", params={"state": "active", "limit": 100})
    r.raise_for_status()
    for item in _items(r.json()):
        if item.get("name") == name:
            return str(item["id"]), False
    r = await api.post("/v0/drives", json={"name": name}, headers={"Idempotency-Key": _key("seed")})
    r.raise_for_status()
    return str(r.json()["id"]), True


async def mint_token(anon: httpx.AsyncClient, cfg: ProberConfig) -> str:
    r = await anon.post(
        cfg.token_endpoint,
        auth=(cfg.client_id, cfg.client_secret),
        data={"grant_type": "client_credentials", "resource": cfg.audience, "scope": cfg.scopes},
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError("token endpoint returned no access_token")
    return str(token)
