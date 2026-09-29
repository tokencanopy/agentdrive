"""Search vertical (slice 9): the 1 search operation over real Postgres.

Drive-scoped lexical search over ``artifacts.search_tsv``. Mirrors the
drives/folders test shape: ``content:read`` gates the token scope; hits are
visibility-filtered by local grants (drive manager sees all live artifacts,
an outsider with scope sees none); pagination uses D14 sealed cursors; the
snippet comes from ``content_preview``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
OTHER_AGENT = "tcagt_0000000000000002"
WS_A = "tcws_0000000000000001"
WS_B = "tcws_0000000000000002"


def make_actor(
    *,
    subject: str = AGENT,
    subject_type: str = "agent",
    workspace: str = WS_A,
    scopes: set[str] | None = None,
    sponsor: str | None = SPONSOR,
) -> V0ActorContext:
    scopes = scopes if scopes is not None else {
        "drives:read", "drives:write", "usage:read",
        "content:read", "content:write", "sharing:read", "sharing:write",
    }
    is_agent = subject_type == "agent"
    return V0ActorContext(
        subject=subject,
        subject_type=subject_type,
        workspace_id=workspace,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(scopes),
        credential_id="tccred_0000000000000001" if is_agent else None,
        runtime_id="tcrun_0000000000000001" if is_agent else None,
        sponsor_id=sponsor if is_agent else None,
        workspace_role=None if is_agent else "admin",
    )


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def override_actor(app_with_lifespan):
    def _set(actor: V0ActorContext) -> None:
        app.dependency_overrides[v0_actor] = lambda: actor

    yield _set
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    yield
    async with conn() as c:
        await c.execute("TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE")


async def _create_drive(http, name: str, key: str) -> dict:
    prev = app.dependency_overrides.get(v0_actor)
    app.dependency_overrides[v0_actor] = lambda: make_actor(
        scopes={"drives:read", "drives:write", "usage:read"}
    )
    try:
        resp = await http.post(
            "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
        )
    finally:
        app.dependency_overrides.clear()
        if prev is not None:
            app.dependency_overrides[v0_actor] = prev
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_artifact(
    http, drive_id: str, parent_id: str, name: str, key: str, content: bytes
):
    resp = await http.post(
        f"/v0/drives/{drive_id}/artifacts",
        content=(
            b"--b\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n"
            + parent_id.encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"name\"\r\n\r\n"
            + name.encode() + b"\r\n--b\r\n"
            b"Content-Disposition: form-data; name=\"content\"; filename=\"f\"\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            + content + b"\r\n--b--\r\n"
        ),
        headers={"Content-Type": "multipart/form-data; boundary=b", "Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_search_requires_content_scope(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "sscope", "ksscope")
    override_actor(make_actor(scopes={"drives:read"}))
    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_search_returns_matching_artifact(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "search1", "ksearch1")
    await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "report.txt", "ksearch1-1",
        b"the quarterly revenue needle report",
    )
    await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "notes.txt", "ksearch1-2",
        b"unrelated shopping list",
    )

    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle revenue"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) >= 1
    hit = body["items"][0]
    assert hit["name"] == "report.txt"
    assert hit["drive_id"] == drive["id"]
    assert "snippet" in hit
    assert hit["rank"] > 0


async def test_search_finds_a_unicode_artifact_name(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "unicode-search", "k-unicode-search-drive")
    await _create_artifact(
        http,
        drive["id"],
        drive["root_folder_id"],
        "研究数据.xlsx",
        "k-unicode-search-artifact",
        b"spreadsheet",
    )
    result = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "研究数据"}
    )
    assert result.status_code == 200, result.text
    assert [item["name"] for item in result.json()["items"]] == ["研究数据.xlsx"]


async def test_search_finds_an_artifact_by_its_own_hyphenated_name(
    http, override_actor
):
    """The index normalizes separators; the query must too.

    `search_tsv` stores the NAME through `regexp_replace(name, '[._-]+', ' ')`,
    so `report-q3-final.txt` is indexed as report/q3/final/txt. The query side
    had no matching step and `websearch_to_tsquery` turns a hyphenated term
    into a PHRASE demanding a compound lexeme the index deliberately split --
    so searching an artifact by its own filename returned NOTHING while every
    individual word in that filename matched. The likeliest query a caller
    makes, silently empty.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "hyphen-search", "k-hyphen-search-drive")
    await _create_artifact(
        http,
        drive["id"],
        drive["root_folder_id"],
        "report-q3-final.txt",
        "k-hyphen-search-artifact",
        b"quarterly numbers",
    )
    for query in (
        "report-q3-final.txt",
        "report-q3-final",
        "report-q3",
        "report_q3",
        "report",
        "q3",
    ):
        result = await http.get(
            f"/v0/drives/{drive['id']}/search", params={"q": query}
        )
        assert result.status_code == 200, result.text
        assert [item["name"] for item in result.json()["items"]] == [
            "report-q3-final.txt"
        ], f"query {query!r} found nothing"


async def test_search_still_honours_websearch_negation(http, override_actor):
    """The raw query is OR-ed with the normalized one rather than replaced, so
    `websearch`'s `-term` exclusion keeps working. Replacing would have traded
    one silent surprise for another."""
    override_actor(make_actor())
    drive = await _create_drive(http, "negation-search", "k-neg-search-drive")
    await _create_artifact(
        http,
        drive["id"],
        drive["root_folder_id"],
        "alpha.txt",
        "k-neg-search-alpha",
        b"shared token apple",
    )
    await _create_artifact(
        http,
        drive["id"],
        drive["root_folder_id"],
        "beta.txt",
        "k-neg-search-beta",
        b"shared token banana",
    )
    result = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "shared -banana"}
    )
    assert result.status_code == 200, result.text
    assert [item["name"] for item in result.json()["items"]] == ["alpha.txt"]


async def test_search_drive_scoped_no_cross_workspace_leak(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "search2", "ksearch2")
    await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "secret.txt", "ksearch2-1",
        b"classified needle material",
    )
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_B))
    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DRIVE_NOT_FOUND"


async def test_search_outsider_with_scope_gets_empty_page(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "search3", "ksearch3")
    await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "visible.txt", "ksearch3-1",
        b"needle in the haystack",
    )
    # An outsider in the SAME workspace with content:read but no grants gets
    # an empty page, not a leak.
    override_actor(make_actor(subject=OTHER_AGENT, workspace=WS_A))
    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_search_rejects_bad_mode_and_query(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "search4", "ksearch4")

    bad_mode = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "x", "mode": "semantic"}
    )
    assert bad_mode.status_code == 400
    assert bad_mode.json()["error"]["code"] == "SEARCH_MODE_UNAVAILABLE"

    unknown_mode = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "x", "mode": "bogus"}
    )
    assert unknown_mode.status_code == 422
    assert unknown_mode.json()["error"]["code"] == "VALIDATION_ERROR"

    missing_q = await http.get(f"/v0/drives/{drive['id']}/search")
    assert missing_q.status_code == 422
    assert missing_q.json()["error"]["code"] == "VALIDATION_ERROR"

    empty_q = await http.get(f"/v0/drives/{drive['id']}/search", params={"q": ""})
    assert empty_q.status_code == 422
    assert empty_q.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_search_paginates(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "search5", "ksearch5")
    for i in range(3):
        await _create_artifact(
            http, drive["id"], drive["root_folder_id"], f"doc-{i}.txt", f"ksearch5-{i}",
            b"common searchable needle body",
        )

    page = await http.get(
        f"/v0/drives/{drive['id']}/search",
        params={"q": "needle", "limit": 2},
    )
    assert page.status_code == 200
    body = page.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]

    page2 = await http.get(
        f"/v0/drives/{drive['id']}/search",
        params={"q": "needle", "limit": 2, "cursor": body["next_cursor"]},
    )
    assert page2.status_code == 200
    assert len(page2.json()["items"]) == 1


async def test_search_rejects_cursor_replayed_across_query(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "search6", "ksearch6")
    for i in range(3):
        await _create_artifact(
            http, drive["id"], drive["root_folder_id"], f"doc-{i}.txt", f"ksearch6-{i}",
            b"common searchable needle body",
        )

    page = await http.get(
        f"/v0/drives/{drive['id']}/search",
        params={"q": "needle", "limit": 2},
    )
    cursor = page.json()["next_cursor"]
    assert cursor

    replayed = await http.get(
        f"/v0/drives/{drive['id']}/search",
        params={"q": "different", "limit": 2, "cursor": cursor},
    )
    assert replayed.status_code == 400
    assert replayed.json()["error"]["code"] == "INVALID_CURSOR"


async def _create_grant(http, drive_id: str, key: str, **body):
    resp = await http.post(
        f"/v0/drives/{drive_id}/grants",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _mkdir(http, drive_id: str, parent_id: str, name: str, key: str):
    resp = await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp


async def test_search_folder_grant_sees_only_its_subtree(http, override_actor):
    """A folder-granted agent search-sees artifacts under that folder only —
    artifacts elsewhere in the drive stay hidden (the LATERAL visibility)."""
    override_actor(make_actor())
    drive = await _create_drive(http, "sv1", "ksv1")
    grantee = make_actor(subject=OTHER_AGENT, workspace=WS_A)

    # Two folders, one artifact in each, same searchable term.
    folder_a = await _mkdir(http, drive["id"], drive["root_folder_id"], "fa", "ksv1-1")
    folder_b = await _mkdir(http, drive["id"], drive["root_folder_id"], "fb", "ksv1-2")
    await _create_artifact(
        http, drive["id"], folder_a.json()["id"], "in-a.txt", "ksv1-3",
        b"shared needle content",
    )
    await _create_artifact(
        http, drive["id"], folder_b.json()["id"], "in-b.txt", "ksv1-4",
        b"shared needle content",
    )

    # Grant the outsider EDITOR on folder A only.
    await _create_grant(
        http, drive["id"], "ksv1-5",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder_a.json()["id"], role="editor",
    )

    override_actor(grantee)
    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert resp.status_code == 200
    names = {item["name"] for item in resp.json()["items"]}
    assert names == {"in-a.txt"}, f"granted agent must see only folder A, got {names}"


async def test_search_folder_grant_reaches_nested_descendants(http, override_actor):
    """The search gate walks the WHOLE ancestry: a grant on a folder reveals
    artifacts nested arbitrarily deep beneath it, not just its direct
    children. (This subtree used to be hideable with
    `grant_inheritance='sealed'`; inheritance is additive-only now.)"""
    override_actor(make_actor())
    drive = await _create_drive(http, "sv2", "ksv2")
    grantee = make_actor(subject=OTHER_AGENT, workspace=WS_A)

    folder_a = await _mkdir(http, drive["id"], drive["root_folder_id"], "fa", "ksv2-1")
    nested = await _mkdir(http, drive["id"], folder_a.json()["id"], "nested", "ksv2-2")
    await _create_artifact(
        http, drive["id"], nested.json()["id"], "secret.txt", "ksv2-3",
        b"needle two levels down",
    )

    # Ungranted, the artifact is invisible to the outsider.
    override_actor(grantee)
    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []

    # A grant on the GRANDparent folder reveals it.
    override_actor(make_actor())
    await _create_grant(
        http, drive["id"], "ksv2-4",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder_a.json()["id"], role="editor",
    )

    override_actor(grantee)
    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    names = {item["name"] for item in resp.json()["items"]}
    assert names == {"secret.txt"}


async def test_search_direct_artifact_grant(http, override_actor):
    """A direct artifact grant reveals exactly that artifact in search."""
    override_actor(make_actor())
    drive = await _create_drive(http, "sv3", "ksv3")
    grantee = make_actor(subject=OTHER_AGENT, workspace=WS_A)

    art_a = await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "granted.txt", "ksv3-1",
        b"needle content",
    )
    await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "other.txt", "ksv3-2",
        b"needle content",
    )

    await _create_grant(
        http, drive["id"], "ksv3-3",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="artifact", resource_id=art_a["id"], role="viewer",
    )

    override_actor(grantee)
    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    names = {item["name"] for item in resp.json()["items"]}
    assert names == {"granted.txt"}, (
        f"direct artifact grant must reveal only that artifact, got {names}"
    )


async def test_search_revoked_grant_excludes_hits(http, override_actor):
    """After a grant is revoked, the agent can no longer search-see the
    artifact the grant used to cover."""
    override_actor(make_actor())
    drive = await _create_drive(http, "sv4", "ksv4")
    grantee = make_actor(subject=OTHER_AGENT, workspace=WS_A)

    folder = await _mkdir(http, drive["id"], drive["root_folder_id"], "f", "ksv4-1")
    await _create_artifact(
        http, drive["id"], folder.json()["id"], "revoked.txt", "ksv4-2",
        b"needle content",
    )

    grant = await _create_grant(
        http, drive["id"], "ksv4-3",
        principal_type="agent", principal_id=OTHER_AGENT,
        resource_type="folder", resource_id=folder.json()["id"], role="viewer",
    )

    override_actor(grantee)
    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert {item["name"] for item in resp.json()["items"]} == {"revoked.txt"}

    # Revoke the grant as the manager.
    override_actor(make_actor())
    revoked = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{grant['id']}",
        headers={"Idempotency-Key": "ksv4-4", "If-Match": f'"{grant["revision"]}"'},
    )
    assert revoked.status_code == 200

    override_actor(grantee)
    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert resp.json()["items"] == []


# ── snippet is HTML-safe by contract ─────────────────────────────────────────


async def test_snippet_escapes_artifact_markup_but_keeps_mark(http, override_actor):
    """A search snippet must never hand a client raw artifact markup.

    `snippet` is `ts_headline` output over `content_preview`, i.e. bytes an
    attacker uploaded. ts_headline is tag-AWARE, which is exactly what makes
    the naive reading ("Postgres strips tags for us") wrong: it drops
    `<script>` but passes `<img src=x onerror=...>` through untouched, so the
    payload that actually fires is the one that survives. The wire contract
    is that the ONLY markup in `snippet` is the server's own <mark> pair;
    everything else arrives entity-escaped.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "xss", "kxss")
    await _create_artifact(
        http, drive["id"], drive["root_folder_id"], "payload.txt", "kxss-1",
        b'quarterly needle report <img src=x onerror=alert(1)> end of file',
    )

    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert resp.status_code == 200, resp.text
    snippet = resp.json()["items"][0]["snippet"]

    # The live payload is escaped, not passed through.
    assert "<img" not in snippet
    assert "onerror=alert(1)>" not in snippet
    assert "&lt;img src=x onerror=alert(1)&gt;" in snippet
    # The server's own highlight survives — escaping must not flatten it.
    assert "<mark>needle</mark>" in snippet
    # Nothing but the highlight pair is left as real markup.
    assert snippet.replace("<mark>", "").replace("</mark>", "").find("<") == -1


async def test_snippet_escapes_every_tag_shape_that_survives_headline(
    http, override_actor
):
    """The same guard across the payload shapes ts_headline does NOT strip.

    One assertion per shape rather than one blended fixture, so a regression
    names the vector it reopened."""
    override_actor(make_actor())
    drive = await _create_drive(http, "xss2", "kxss2")
    payloads = {
        "svg.txt": b"needle <svg onload=alert(1)> trailing filler words here",
        "anchor.txt": b"needle <a href=javascript:alert(1)>x</a> filler words",
        "unclosed.txt": b"needle <img src=x onerror=alert(1) filler words here",
    }
    for i, (name, content) in enumerate(payloads.items()):
        await _create_artifact(
            http, drive["id"], drive["root_folder_id"], name, f"kxss2-{i}", content
        )

    resp = await http.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == len(payloads)
    for hit in items:
        bare = hit["snippet"].replace("<mark>", "").replace("</mark>", "")
        assert "<" not in bare, (hit["name"], hit["snippet"])
        assert "onload" not in hit["snippet"] or "&lt;svg" in hit["snippet"]
