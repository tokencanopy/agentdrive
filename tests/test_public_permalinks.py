"""`/a/{art_id}/` and `/v/{art_id}/{ver_id}/` — the anonymous permalinks.

A share link is a credential you hold; a permalink is a fact about the world.
These two routes carry no secret, so anyone who guesses the URL reaches them —
which makes the *grant* the only thing standing between an artifact and the
internet. Three properties are what this module pins:

  * **A live `public` grant is the whole authorization.** No grant, a revoked
    grant, an expired grant, a soft-deleted artifact — every one of them is
    the same 404 an id that never existed gets, byte for byte. Anything else
    turns the permalink into an existence oracle over `art_*` ids, and unlike
    a share key an artifact id travels in API responses, logs and UIs.

  * **Cache policy is per-route and authorization-sensitive.** `/v/` names
    immutable bytes but remains subject to a live, revocable public grant, so
    it must revalidate before reuse. `/a/` names a moving head and keeps its
    60-second revalidation policy. Getting these wrong can serve a revoked or
    stale document.

  * **`og:url` IS filled here.** The share surface deliberately leaves it
    empty (the only URL identifying that page carries the key). These URLs
    contain no secret, so the canonical URL goes in the tag — that is what
    makes an unfurl point back at us instead of nowhere.

Fixtures follow `tests/test_v0_shares.py` / `tests/test_public_share.py`
(local `http` / `override_actor` / `_clean_tables`), not the deleted root
`client` fixture.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext
from agentdrive.rendering import render as render_module

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"

# What a browser sends on a top-level navigation.
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
# What a browser sends when fetching `<img src="content">`.
IMG = {"Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"}

# Well-formed ids that name nothing. Well-formed matters: a malformed id could
# be refused by the router before the handler runs, which would not test the
# anti-enumeration path at all.
NO_SUCH_ARTIFACT = "art_ffffffffffffffff"
NO_SUCH_VERSION = "ver_ffffffffffffffff"

CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "worker-src 'self' blob:; img-src 'self' data: https:; "
    "media-src 'self'; "
    "style-src 'self'; font-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Deliberately NOT `CSP + "; sandbox"`: the byte policy is stricter than the
# page's, granting neither script nor object rather than granting them and
# relying on `sandbox` to take them back.
CONTENT_CSP = (
    "default-src 'none'; script-src 'none'; object-src 'none'; "
    "img-src 'self' data: https:; style-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox"
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def make_actor() -> V0ActorContext:
    return V0ActorContext(
        subject=AGENT,
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({
            "drives:read", "drives:write", "usage:read",
            "content:read", "content:write", "sharing:read", "sharing:write",
        }),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id=SPONSOR,
        workspace_role=None,
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


# ── local helpers ────────────────────────────────────────────────────────────


async def _create_drive(http, name: str, key: str) -> dict:
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


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


async def _create_artifact(
    http,
    drive: dict,
    *,
    name: str,
    body: bytes,
    key: str,
    content_type: str = "text/markdown",
    parent_id: str | None = None,
) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart(
            parent_id or drive["root_folder_id"], name, content_type, body
        ),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": key,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _mkdir(http, drive: dict, name: str, key: str) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        json={"parent_id": drive["root_folder_id"], "name": name},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _publish(http, drive_id: str, key: str, **body):
    """Mint the `public` grant that makes a permalink resolve at all."""
    resp = await http.post(
        f"/v0/drives/{drive_id}/grants",
        json={"principal_type": "public", "role": "viewer", **body},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp


async def _revoke_grant(http, drive_id: str, grant, key: str) -> None:
    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive_id}/grants/{grant.json()['id']}",
        headers={"Idempotency-Key": key, "If-Match": grant.headers["etag"]},
    )
    assert resp.status_code == 200, resp.text


def _fingerprint(response) -> tuple:
    """Everything a prober can observe, minus the per-request correlation id.

    `X-Request-Id` is minted fresh per request and carries no information
    about the target, so it is the only field excluded.
    """
    headers = {k: v for k, v in response.headers.items() if k != "x-request-id"}
    return response.status_code, headers, response.content


def _origin() -> str:
    return settings.public_base_url.rstrip("/")


async def _published_artifact(http, override_actor, tag: str, **kwargs) -> tuple:
    """A drive, an artifact, and a live `public` grant on that artifact."""
    override_actor(make_actor())
    drive = await _create_drive(http, tag, f"k{tag}")
    art = await _create_artifact(http, drive, key=f"k{tag}-1", **kwargs)
    grant = await _publish(
        http, drive["id"], f"k{tag}-2",
        resource_type="artifact", resource_id=art["id"],
    )
    return drive, art, grant


# ── the rendered permalink ───────────────────────────────────────────────────


async def test_artifact_permalink_renders_once_public(http, override_actor):
    _, art, _ = await _published_artifact(
        http, override_actor, "apub", name="report.md", body=b"# Public Report\n"
    )

    r = await http.get(f"/a/{art['id']}/", headers=BROWSER)

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "<h1>Public Report</h1>" in r.text


async def test_a_public_pdf_permalink_offers_print(http, override_actor):
    """The control legacy shipped, restored on the surface that can honour it.

    This page is top-level, not a sandboxed frame, and `pdf-visitor.js`
    wires the button — `view.print()` opens `content`, which is served
    `inline`, so the browser prints the PDF rather than the page chrome.
    """
    _, art, _ = await _published_artifact(
        http, override_actor, "apdf",
        name="report.pdf", body=b"%PDF-1.4 fake", content_type="application/pdf",
    )

    r = await http.get(f"/a/{art['id']}/", headers=BROWSER)

    assert r.status_code == 200, r.text
    assert 'id="pv-print"' in r.text
    assert 'aria-label="Print"' in r.text


async def test_the_permalink_fills_in_its_own_canonical_url(http, override_actor):
    """Unlike `/s/`, this URL carries no secret — so `og:url` is filled in.

    An unfurl with an empty `og:url` points nowhere; this is the tag that
    makes a pasted link resolve back to us in a chat preview.
    """
    _, art, _ = await _published_artifact(
        http, override_actor, "aog", name="report.md", body=b"# Quarterly\n"
    )

    r = await http.get(f"/a/{art['id']}/", headers=BROWSER)

    canonical = f"{_origin()}/a/{art['id']}/"
    assert f'<meta property="og:url" content="{canonical}" />' in r.text
    assert '<meta property="og:title" content="Quarterly" />' in r.text
    # The canonical URL ends in a slash for the same reason the page does:
    # relative sub-resources resolve against it.
    assert canonical.endswith("/")


async def test_version_permalink_renders_that_version(http, override_actor):
    _, art, _ = await _published_artifact(
        http, override_actor, "vpub", name="report.md", body=b"# Pinned\n"
    )

    r = await http.get(f"/v/{art['id']}/{art['head_version_id']}/", headers=BROWSER)

    assert r.status_code == 200, r.text
    assert "<h1>Pinned</h1>" in r.text
    canonical = f"{_origin()}/v/{art['id']}/{art['head_version_id']}/"
    assert f'<meta property="og:url" content="{canonical}" />' in r.text


async def test_a_folder_grant_publishes_the_artifacts_beneath_it(
    http, override_actor
):
    """The grant need not sit on the artifact: folder ancestry resolves the
    same way it does for an authenticated reader. One resolution, not two."""
    override_actor(make_actor())
    drive = await _create_drive(http, "afld", "kafld")
    folder = await _mkdir(http, drive, "public", "kafld-1")
    art = await _create_artifact(
        http, drive, name="r.md", body=b"# Inherited\n", key="kafld-2",
        parent_id=folder["id"],
    )
    await _publish(
        http, drive["id"], "kafld-3",
        resource_type="folder", resource_id=folder["id"],
    )

    r = await http.get(f"/a/{art['id']}/", headers=BROWSER)

    assert r.status_code == 200, r.text
    assert "<h1>Inherited</h1>" in r.text
    # The derived display path is in the page, not just the bare name.
    assert "public/r.md" in r.text


# ── anti-enumeration ─────────────────────────────────────────────────────────


async def test_unpublished_revoked_deleted_and_unknown_are_byte_identical(
    http, override_actor
):
    """The headline property. An `art_*` id is not a secret — it rides in API
    responses, logs and UIs — so if "published once, revoked since" answered
    even one header differently from "never existed", the permalink would be a
    read oracle over every id anyone has ever seen.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "aanti", "kanti")

    # 1. never published
    private = await _create_artifact(
        http, drive, name="a.md", body=b"# A\n", key="kanti-1"
    )

    # 2. published, then revoked
    revoked_art = await _create_artifact(
        http, drive, name="b.md", body=b"# B\n", key="kanti-2"
    )
    grant = await _publish(
        http, drive["id"], "kanti-3",
        resource_type="artifact", resource_id=revoked_art["id"],
    )
    await _revoke_grant(http, drive["id"], grant, "kanti-4")

    # 3. published, then expired
    expired_art = await _create_artifact(
        http, drive, name="c.md", body=b"# C\n", key="kanti-5"
    )
    await _publish(
        http, drive["id"], "kanti-6",
        resource_type="artifact", resource_id=expired_art["id"],
        expires_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    )

    # 4. published, then soft-deleted
    gone_art = await _create_artifact(
        http, drive, name="d.md", body=b"# D\n", key="kanti-7"
    )
    await _publish(
        http, drive["id"], "kanti-8",
        resource_type="artifact", resource_id=gone_art["id"],
    )
    deleted = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/artifacts/{gone_art['id']}",
        headers={
            "Idempotency-Key": "kanti-9",
            "If-Match": f'"{gone_art["revision"]}"',
        },
    )
    assert deleted.status_code == 200, deleted.text

    responses = {
        "unknown": await http.get(f"/a/{NO_SUCH_ARTIFACT}/", headers=BROWSER),
        "unpublished": await http.get(f"/a/{private['id']}/", headers=BROWSER),
        "revoked": await http.get(f"/a/{revoked_art['id']}/", headers=BROWSER),
        "expired": await http.get(f"/a/{expired_art['id']}/", headers=BROWSER),
        "deleted": await http.get(f"/a/{gone_art['id']}/", headers=BROWSER),
    }

    baseline = _fingerprint(responses["unknown"])
    assert baseline[0] == 404
    for label, r in responses.items():
        assert _fingerprint(r) == baseline, f"{label} is distinguishable from unknown"

    # And nothing in the page names a reason.
    body = responses["revoked"].text.lower()
    for leak in ("revoked", "expired", "deleted", "grant", "permission", "forbidden"):
        assert leak not in body


async def test_version_permalink_anti_enumeration(http, override_actor):
    """Same property on `/v/`, plus its own failure mode: a real version id
    under an unpublished artifact must not read differently from a version id
    that names nothing."""
    override_actor(make_actor())
    drive = await _create_drive(http, "vanti", "kvanti")
    private = await _create_artifact(
        http, drive, name="a.md", body=b"# A\n", key="kvanti-1"
    )
    published = await _create_artifact(
        http, drive, name="b.md", body=b"# B\n", key="kvanti-2"
    )
    await _publish(
        http, drive["id"], "kvanti-3",
        resource_type="artifact", resource_id=published["id"],
    )

    responses = {
        "unknown artifact": await http.get(
            f"/v/{NO_SUCH_ARTIFACT}/{NO_SUCH_VERSION}/", headers=BROWSER
        ),
        "unpublished artifact": await http.get(
            f"/v/{private['id']}/{private['head_version_id']}/", headers=BROWSER
        ),
        "unknown version of a published artifact": await http.get(
            f"/v/{published['id']}/{NO_SUCH_VERSION}/", headers=BROWSER
        ),
        # A live version id, but paired with the wrong artifact.
        "mismatched pair": await http.get(
            f"/v/{published['id']}/{private['head_version_id']}/", headers=BROWSER
        ),
    }

    baseline = _fingerprint(responses["unknown artifact"])
    assert baseline[0] == 404
    for label, r in responses.items():
        assert _fingerprint(r) == baseline, f"{label} is distinguishable"


async def test_the_content_sub_routes_refuse_exactly_as_their_pages_do(
    http, override_actor
):
    """A sub-route that resolved more permissively than the page it hangs off
    would be revocation that does not revoke — the page 404s, the bytes flow.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "acnt", "kacnt")
    private = await _create_artifact(
        http, drive, name="a.png", body=PNG, key="kacnt-1", content_type="image/png"
    )
    revoked_art = await _create_artifact(
        http, drive, name="b.png", body=PNG, key="kacnt-2", content_type="image/png"
    )
    grant = await _publish(
        http, drive["id"], "kacnt-3",
        resource_type="artifact", resource_id=revoked_art["id"],
    )
    await _revoke_grant(http, drive["id"], grant, "kacnt-4")

    unknown = await http.get(f"/a/{NO_SUCH_ARTIFACT}/content", headers=IMG)
    assert unknown.status_code == 404

    for label, art_id in (
        ("unpublished", private["id"]),
        ("revoked", revoked_art["id"]),
    ):
        r = await http.get(f"/a/{art_id}/content", headers=IMG)
        assert _fingerprint(r) == _fingerprint(unknown), label

    v = await http.get(
        f"/v/{private['id']}/{private['head_version_id']}/content", headers=IMG
    )
    assert _fingerprint(v) == _fingerprint(unknown)


# ── canonical URL shape ──────────────────────────────────────────────────────


async def test_bare_permalinks_canonicalize_onto_the_trailing_slash(http):
    """`/a/ID` → `/a/ID/`, `/v/A/V` → `/v/A/V/`.

    The page links its bytes as `content`, and relative resolution replaces
    the last path segment: from `/a/ID` that reaches `/a/content`, so every
    image on every permalink page would 404.
    """
    r = await http.get(f"/a/{NO_SUCH_ARTIFACT}", headers=BROWSER, follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == f"/a/{NO_SUCH_ARTIFACT}/"

    v = await http.get(
        f"/v/{NO_SUCH_ARTIFACT}/{NO_SUCH_VERSION}",
        headers=BROWSER,
        follow_redirects=False,
    )
    assert v.status_code == 308
    assert v.headers["location"] == f"/v/{NO_SUCH_ARTIFACT}/{NO_SUCH_VERSION}/"


async def test_the_canonicalizing_redirect_is_not_an_existence_oracle(
    http, override_actor
):
    """It must fire before any lookup. If a published artifact redirected and
    an unpublished one 404'd, the status code alone would answer "does this id
    exist, and is it public?" — the question anti-enumeration refuses."""
    override_actor(make_actor())
    drive = await _create_drive(http, "aorac", "korac")
    private = await _create_artifact(
        http, drive, name="a.md", body=b"# A\n", key="korac-1"
    )
    published = await _create_artifact(
        http, drive, name="b.md", body=b"# B\n", key="korac-2"
    )
    await _publish(
        http, drive["id"], "korac-3",
        resource_type="artifact", resource_id=published["id"],
    )

    statuses = set()
    for art_id in (published["id"], private["id"], NO_SUCH_ARTIFACT, "not-an-id"):
        r = await http.get(f"/a/{art_id}", headers=BROWSER, follow_redirects=False)
        statuses.add(r.status_code)
        v = await http.get(
            f"/v/{art_id}/{NO_SUCH_VERSION}", headers=BROWSER, follow_redirects=False
        )
        statuses.add(v.status_code)

    assert statuses == {308}


async def test_a_browser_resolving_the_img_src_reaches_the_bytes(http, override_actor):
    """The end-to-end property: resolve `<img src="content">` the way a
    browser would, from both the canonical URL and a legacy un-slashed link,
    and fetch what it lands on."""
    _, art, _ = await _published_artifact(
        http, override_actor, "aimg", name="chart.png", body=PNG,
        content_type="image/png",
    )

    for label, url in (("canonical", f"/a/{art['id']}/"), ("legacy", f"/a/{art['id']}")):
        page = await http.get(url, headers=BROWSER, follow_redirects=True)
        assert page.status_code == 200, label

        src = re.search(r'<img[^>]*src="([^"]+)"', page.text).group(1)
        resolved = urljoin(str(page.request.url), src)
        assert resolved.endswith(f"/a/{art['id']}/content"), (label, resolved)

        got = await http.get(resolved, headers=IMG)
        assert got.status_code == 200, (label, got.text)
        assert got.content == PNG, label


# ── cache policy: opposite on purpose ────────────────────────────────────────


async def test_the_artifact_permalink_revalidates_rather_than_caching_forever(
    http, override_actor
):
    """`/a/` names a MOVING head. Caching it immutably would serve a document
    the publisher has since replaced, for a year, with no way to correct it."""
    _, art, _ = await _published_artifact(
        http, override_actor, "acache", name="r.md", body=b"# R\n"
    )

    for url in (f"/a/{art['id']}/", f"/a/{art['id']}/content"):
        r = await http.get(url, headers=BROWSER)
        assert r.status_code == 200, url
        assert r.headers["cache-control"] == "public, max-age=60, must-revalidate", url
        assert "immutable" not in r.headers["cache-control"], url
        assert r.headers["etag"] == f'"{art["head_version_id"]}"', url


async def test_the_version_permalink_revalidates_the_live_public_grant(http, override_actor):
    """`/v/` bytes are frozen, but its live public grant remains revocable."""
    _, art, _ = await _published_artifact(
        http, override_actor, "vcache", name="r.md", body=b"# R\n"
    )
    version = art["head_version_id"]

    for url in (f"/v/{art['id']}/{version}/", f"/v/{art['id']}/{version}/content"):
        r = await http.get(url, headers=BROWSER)
        assert r.status_code == 200, url
        assert r.headers["cache-control"] == "public, max-age=0, must-revalidate", url
        assert "immutable" not in r.headers["cache-control"], url
        assert r.headers["etag"] == f'"{version}"', url


# ── the safety headers, inherited not re-implemented ─────────────────────────


async def test_the_permalink_page_carries_the_public_response_headers(
    http, override_actor
):
    _, art, _ = await _published_artifact(
        http, override_actor, "ahdr", name="r.md", body=b"# R\n"
    )

    for url in (f"/a/{art['id']}/", f"/v/{art['id']}/{art['head_version_id']}/"):
        r = await http.get(url, headers=BROWSER)
        assert r.headers["referrer-policy"] == "no-referrer", url
        assert r.headers["content-security-policy"] == CSP, url


async def test_permalink_bytes_inherit_the_sandboxed_byte_headers(
    http, override_actor
):
    """The content routes go through the same `_serve_bytes` the share surface
    uses, so nosniff / Content-Disposition / the sandboxed CSP are inherited
    rather than re-derived — a second implementation is a second place to
    forget one."""
    _, art, _ = await _published_artifact(
        http, override_actor, "abyte", name="evil.html",
        body=b"<script>alert(document.domain)</script>", content_type="text/html",
    )

    r = await http.get(f"/a/{art['id']}/content", headers=BROWSER)

    assert r.status_code == 200
    assert r.headers["content-security-policy"] == CONTENT_CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == (
        'attachment; filename="evil.html"; filename*=UTF-8\'\'evil.html'
    )


async def test_artifact_html_is_escaped_not_executed(http, override_actor):
    """Artifact bytes are attacker-authored and render on our own origin —
    and on this surface anyone can reach them, no link required."""
    _, art, _ = await _published_artifact(
        http, override_actor, "axss", name="evil.md",
        body=b"# Title\n\n<script>alert(1)</script>\n",
    )

    r = await http.get(f"/a/{art['id']}/", headers=BROWSER)

    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


@pytest.mark.parametrize(
    ("principal_type", "principal_id"),
    [
        ("agent", AGENT),
        ("user", SPONSOR),
        ("workspace", WS_A),
    ],
)
async def test_only_a_public_grant_publishes(
    http, override_actor, principal_type, principal_id
):
    """The inverse of the property the rest of this file tests.

    Publication is decided by asking `v0_authz` with an anonymous principal —
    null subject, null workspace. `_principal_matches` is written so that only
    the `public` disjunct can be true for such a principal; every other
    comparison is against NULL and yields NULL, which `WHERE` drops. If that
    ever stopped holding, a grant shared with one teammate would silently
    publish the artifact to the open internet, and every *other* test here
    would still pass. So assert it directly, for each principal type there is.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, f"only{principal_type}", f"ko{principal_type}")
    art = await _create_artifact(
        http, drive, key=f"ko{principal_type}-1", name="private.md", body=b"# Secret\n"
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": principal_type,
            "principal_id": principal_id,
            "role": "viewer",
            "resource_type": "artifact",
            "resource_id": art["id"],
        },
        headers={"Idempotency-Key": f"ko{principal_type}-2"},
    )
    assert resp.status_code == 201, resp.text

    page = await http.get(f"/a/{art['id']}/", headers=BROWSER)
    content = await http.get(f"/a/{art['id']}/content", headers=BROWSER)

    unknown = await http.get("/a/art_ffffffffffffffff/", headers=BROWSER)
    assert _fingerprint(page) == _fingerprint(unknown)
    assert content.status_code == 404
    assert b"Secret" not in page.content and b"Secret" not in content.content


def test_public_urls_are_minted_on_the_share_origin(monkeypatch):
    """Once a share host is configured, these URLs must name it.

    `HostSurfaceMiddleware` serves `/s/`, `/a/`, `/f/` and `/v/` on the share
    host and 404s them everywhere else. Minting them from `public_base_url`
    would hand out links this same deployment refuses — a share link dead on
    arrival, and an `og:url` pointing every unfurl at a 404.
    """
    from agentdrive.config import settings
    from agentdrive.core import urls

    monkeypatch.setattr(settings, "public_base_url", "https://api.example.test")
    monkeypatch.setattr(settings, "share_base_url", "https://share.example.test")

    assert urls.share_url("k").startswith("https://share.example.test/s/")
    assert urls.artifact_permalink_url("art_1") == "https://share.example.test/a/art_1/"
    assert urls.folder_permalink_url("fld_1") == "https://share.example.test/f/fld_1/"
    assert (
        urls.version_permalink_url("art_1", "ver_1")
        == "https://share.example.test/v/art_1/ver_1/"
    )
    # The drive viewer is not part of the public surface and stays put.
    assert urls.public_url("drv_1", "a.md").startswith("https://api.example.test/")


def test_public_urls_fall_back_to_one_origin_when_no_share_host_is_set(monkeypatch):
    """Dev and tests serve everything from one origin; no extra env var."""
    from agentdrive.config import settings
    from agentdrive.core import urls

    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    monkeypatch.setattr(settings, "share_base_url", "")

    assert urls.artifact_permalink_url("art_1") == "http://localhost:8000/a/art_1/"


async def test_an_oversized_artifact_offers_a_download_rather_than_a_blank_page(
    http, override_actor, monkeypatch
):
    """The route skips the fetch for oversized objects — it must still card.

    `_render_artifact` passes `b""` instead of pulling megabytes into memory,
    which is right, but it means `render_body` cannot learn the real size from
    `len(data)`. Left to infer, it read the placeholder as a zero-length
    document and rendered an empty `<main>`: the reader got a header naming a
    file, no content, and no download link — the page advertised a document it
    would not show and offered no way to get it.

    A unit test on `render_body` cannot catch this; it must go through the
    route, which is the only place the substitution happens.
    """
    monkeypatch.setattr(render_module, "DOCUMENT_MAX_BYTES", 64)

    _, art, _ = await _published_artifact(
        http, override_actor, "big",
        name="big.docx", body=b"PK\x03\x04" + b"x" * 500,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    r = await http.get(f"/a/{art['id']}/", headers=BROWSER)

    assert r.status_code == 200
    assert "download-card" in r.text
    assert "big.docx" in r.text


async def test_an_oversized_text_file_previews_its_head(http, override_actor, monkeypatch):
    """Text over its ceiling is no longer a card: the route hands the renderer
    a ranged source, the first `HEAD_PREVIEW_BYTES` render as source, and the
    page says how much of the file that was."""
    from agentdrive import storage

    monkeypatch.setattr(render_module, "MAX_RENDER_BYTES", 64)
    monkeypatch.setattr(render_module, "HEAD_PREVIEW_BYTES", 128)

    async def fail_if_fetched(*_args, **_kwargs):
        raise AssertionError("oversized text was fetched whole")

    monkeypatch.setattr(storage, "get", fail_if_fetched)
    body = b"".join(f"line {i}: something happened\n".encode() for i in range(200))
    _, art, _ = await _published_artifact(
        http, override_actor, "biglog", name="worker.log", body=body, content_type="text/plain",
    )

    r = await http.get(f"/a/{art['id']}/", headers=BROWSER)

    assert r.status_code == 200
    assert "download-card" not in r.text
    assert "line 0: something happened" in r.text
    assert "Showing the first 128 B of a" in r.text


async def test_an_oversized_csv_previews_through_ranged_reads(http, override_actor, monkeypatch):
    """Above the render cap a csv is no longer a card: the route hands the
    renderer a ranged source instead of bytes, and the table comes from the
    file's head. `storage.get` must not be called — that is the whole-object
    fetch this path exists to avoid."""
    from agentdrive import storage

    monkeypatch.setattr(render_module, "MAX_RENDER_BYTES", 64)
    monkeypatch.setattr(render_module, "HEAD_PREVIEW_BYTES", 256)

    async def fail_if_fetched(*_args, **_kwargs):
        raise AssertionError("oversized csv was fetched whole")

    monkeypatch.setattr(storage, "get", fail_if_fetched)
    body = b"id,name\n" + b"".join(f"{i},row-{i}\n".encode() for i in range(200))
    _, art, _ = await _published_artifact(
        http, override_actor, "bigcsv", name="big.csv", body=body,
    )

    r = await http.get(f"/a/{art['id']}/", headers=BROWSER)

    assert r.status_code == 200
    assert "download-card" not in r.text
    assert '<table class="data-table">' in r.text
    assert "<td>0</td><td>row-0</td>" in r.text
    assert "rows from the first 256 B of a" in r.text
