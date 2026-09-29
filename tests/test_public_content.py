"""`/s/{share_key}/content` — the raw-bytes sub-route behind a share page.

The rendered page emits `<img src="content">` for images and
`<a href="content" download>` on the download card, so without this route every
image share renders a broken link and every unrenderable file has a dead
button.

Two properties are what this module actually pins, and both are security
properties rather than features:

  * **The sub-route inherits the parent's authorization exactly.** It resolves
    the key through the same `resolve_secret`, so unknown, revoked, expired and
    deleted-target all produce the response the parent produces, byte for byte.
    A sub-route that resolved more permissively than the page it hangs off
    would be a silent bypass of revocation — the page 404s, the bytes still
    flow.

  * **These bytes are agent-authored and they leave our own origin.** An
    artifact whose declared type is `text/html` or `image/svg+xml` is a script
    the moment a browser treats the response as a document. Three levers keep
    that from happening and each is pinned below: `nosniff` (no type
    confusion), `Content-Disposition: attachment` for active types (a top-level
    navigation downloads instead of rendering), and a CSP whose `sandbox`
    drops any document that does get rendered into an opaque origin.

Fixtures follow `tests/test_v0_shares.py` / `tests/test_public_share.py`
(local `http` / `override_actor` / `_clean_tables`), not the deleted root
`client` fixture.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"

# What a browser sends on a top-level navigation.
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
JSON = {"Accept": "application/json"}
# What a browser sends when fetching `<img src="content">`.
IMG = {"Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"}

# Pinned literally. On a *content* response the CSP has to do more work than on
# the page: `sandbox` is what makes an HTML or SVG artifact that a browser does
# render land in an opaque origin instead of on share.tokencanopy.com.
# Note this is STRICTER than the page CSP: the page needs `script-src 'self'`
# for its theme toggle, raw bytes never do, so content grants neither script
# nor object rather than granting them and relying on `sandbox` to undo it.
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
) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart(drive["root_folder_id"], name, content_type, body),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": key,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_share(http, drive_id: str, key: str, **body):
    resp = await http.post(
        f"/v0/drives/{drive_id}/shares", json=body, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp


async def _revoke_share(http, drive_id: str, share, key: str) -> None:
    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive_id}/shares/{share.json()['id']}",
        headers={"Idempotency-Key": key, "If-Match": share.headers["etag"]},
    )
    assert resp.status_code == 200, resp.text


def _fingerprint(response) -> tuple:
    """Everything a prober can observe, minus the per-request correlation id."""
    headers = {k: v for k, v in response.headers.items() if k != "x-request-id"}
    return response.status_code, headers, response.content


# ── it serves the bytes the page points at ───────────────────────────────────


async def test_image_share_serves_its_bytes_at_the_content_sub_route(
    http, override_actor
):
    """The page's `<img src="content">` must resolve to the actual object."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cimg", "kcimg")
    art = await _create_artifact(
        http, drive, name="chart.png", body=PNG, key="kcimg-1",
        content_type="image/png",
    )
    share = await _create_share(
        http, drive["id"], "kcimg-2", resource_type="artifact", resource_id=art["id"]
    )
    secret = share.json()["secret"]

    page = await http.get(f"/s/{secret}/", headers=BROWSER)
    assert '<img class="artifact-image"' in page.text
    assert 'src="content"' in page.text

    r = await http.get(f"/s/{secret}/content", headers=IMG)

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG
    assert r.headers["etag"] == f'"{art["head_version_id"]}"'


async def test_content_route_carries_the_public_response_headers(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "chdr", "kchdr")
    art = await _create_artifact(
        http, drive, name="chart.png", body=PNG, key="kchdr-1",
        content_type="image/png",
    )
    share = await _create_share(
        http, drive["id"], "kchdr-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/content", headers=IMG)

    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == CONTENT_CSP
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_the_share_key_never_appears_in_the_content_response(
    http, override_actor
):
    override_actor(make_actor())
    drive = await _create_drive(http, "ckey", "kckey")
    art = await _create_artifact(
        http, drive, name="chart.png", body=PNG, key="kckey-1",
        content_type="image/png",
    )
    share = await _create_share(
        http, drive["id"], "kckey-2", resource_type="artifact", resource_id=art["id"]
    )
    secret = share.json()["secret"]

    r = await http.get(f"/s/{secret}/content", headers=IMG)

    assert r.status_code == 200
    for value in r.headers.values():
        assert secret not in value
    assert secret.encode() not in r.content


# ── active content types never execute on our origin ─────────────────────────


async def test_html_typed_artifact_is_served_as_an_attachment(http, override_actor):
    """`text/html` from an agent, served inline from our own origin, IS stored
    XSS on share.tokencanopy.com. `attachment` turns the only path that makes
    it a document — a top-level navigation — into a download instead."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chtml", "kchtml")
    art = await _create_artifact(
        http,
        drive,
        name="Résumé 研究.html",
        body=b"<script>alert(document.domain)</script>",
        key="kchtml-1",
        content_type="text/html",
    )
    share = await _create_share(
        http, drive["id"], "kchtml-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/content", headers=BROWSER)

    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith(
        'attachment; filename="Rsum .html"; '
    )
    assert (
        "filename*=UTF-8''R%C3%A9sum%C3%A9%20%E7%A0%94%E7%A9%B6.html"
        in r.headers["content-disposition"]
    )
    assert r.headers["x-content-type-options"] == "nosniff"
    # `sandbox` is the backstop: if a browser renders it anyway, it renders in
    # an opaque origin, not ours.
    assert "sandbox" in r.headers["content-security-policy"]


async def test_svg_is_an_attachment_yet_still_usable_as_an_image(http, override_actor):
    """SVG is the awkward one: it is a real image kind (the page inlines it via
    `<img src="content">`, a context where its script never runs) AND a
    scriptable document if navigated to. So keep the honest `image/svg+xml`
    Content-Type — `<img>` ignores Content-Disposition — and refuse the
    navigation with `attachment`."""
    override_actor(make_actor())
    drive = await _create_drive(http, "csvg", "kcsvg")
    art = await _create_artifact(
        http,
        drive,
        name="logo.svg",
        body=b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        key="kcsvg-1",
        content_type="image/svg+xml",
    )
    share = await _create_share(
        http, drive["id"], "kcsvg-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/content", headers=IMG)

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.headers["content-disposition"] == (
        'attachment; filename="logo.svg"; filename*=UTF-8\'\'logo.svg'
    )


async def test_inert_types_are_served_inline(http, override_actor):
    """A PNG cannot execute, so it keeps `inline` — an attachment header on
    every response would make the viewer download files nobody asked for."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cinl", "kcinl")
    art = await _create_artifact(
        http, drive, name="Résumé 研究.png", body=PNG, key="kcinl-1",
        content_type="image/png",
    )
    share = await _create_share(
        http, drive["id"], "kcinl-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/content", headers=IMG)

    assert r.headers["content-disposition"].startswith(
        'inline; filename="Rsum .png"; '
    )
    assert (
        "filename*=UTF-8''R%C3%A9sum%C3%A9%20%E7%A0%94%E7%A9%B6.png"
        in r.headers["content-disposition"]
    )


async def test_html_hidden_behind_a_text_plain_type_is_still_not_sniffed(
    http, override_actor
):
    """The declared type is attacker-chosen too. `text/plain` carrying markup
    is the classic sniffing bypass; `nosniff` is what closes it, and the
    sandboxed CSP still applies if the type were ever honoured as a document."""
    override_actor(make_actor())
    drive = await _create_drive(http, "csniff", "kcsniff")
    art = await _create_artifact(
        http,
        drive,
        name="notes.txt",
        body=b"<html><script>alert(1)</script></html>",
        key="kcsniff-1",
        content_type="text/plain",
    )
    share = await _create_share(
        http, drive["id"], "kcsniff-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/content", headers=BROWSER)

    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


# ── authorization is the parent's, exactly ───────────────────────────────────


async def test_content_route_authorization_matches_the_parent_exactly(
    http, override_actor
):
    """The headline property. Four ways for a key to be dead; for each one the
    sub-route must answer what the page answers, byte for byte. Anything looser
    is revocation that does not revoke."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cauth", "kcauth")

    revoked_art = await _create_artifact(
        http, drive, name="a.png", body=PNG, key="kcauth-1", content_type="image/png"
    )
    revoked = await _create_share(
        http, drive["id"], "kcauth-2",
        resource_type="artifact", resource_id=revoked_art["id"],
    )
    await _revoke_share(http, drive["id"], revoked, "kcauth-3")

    expired_art = await _create_artifact(
        http, drive, name="b.png", body=PNG, key="kcauth-4", content_type="image/png"
    )
    expired = await _create_share(
        http, drive["id"], "kcauth-5",
        resource_type="artifact", resource_id=expired_art["id"],
    )
    async with conn() as c:
        await c.execute(
            "UPDATE shares SET expires_at=$2 WHERE id=$1",
            expired.json()["id"],
            datetime.now(UTC) - timedelta(minutes=5),
        )

    gone_art = await _create_artifact(
        http, drive, name="c.png", body=PNG, key="kcauth-6", content_type="image/png"
    )
    gone = await _create_share(
        http, drive["id"], "kcauth-7",
        resource_type="artifact", resource_id=gone_art["id"],
    )
    deleted = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/artifacts/{gone_art['id']}",
        headers={
            "Idempotency-Key": "kcauth-8",
            "If-Match": f'"{gone_art["revision"]}"',
        },
    )
    assert deleted.status_code == 200, deleted.text

    keys = {
        "unknown": "never-existed-key",
        "revoked": revoked.json()["secret"],
        "expired": expired.json()["secret"],
        "deleted": gone.json()["secret"],
    }

    for label, key in keys.items():
        parent = await http.get(f"/s/{key}/", headers=BROWSER)
        content = await http.get(f"/s/{key}/content", headers=BROWSER)
        assert parent.status_code == 404, label
        assert _fingerprint(content) == _fingerprint(parent), (
            f"{label}: /content diverges from its parent page"
        )

    # ...and on the byte path, where the refusal is the v0 error envelope.
    for label, key in keys.items():
        parent = await http.get(f"/s/{key}/", headers=JSON)
        content = await http.get(f"/s/{key}/content", headers=JSON)
        assert content.status_code == 404, label
        assert content.json()["error"]["code"] == "SHARE_NOT_FOUND"
        assert _fingerprint(content) == _fingerprint(parent), label


async def test_a_folder_share_has_no_content(http, override_actor):
    """A folder key is live, so `resolve_secret` returns a target — but there
    are no bytes. That must read as the uniform 404, not as a hint that the key
    was good."""
    override_actor(make_actor())
    drive = await _create_drive(http, "cfld", "kcfld")
    share = await _create_share(
        http, drive["id"], "kcfld-1",
        resource_type="folder", resource_id=drive["root_folder_id"],
    )

    for accept in (BROWSER, JSON):
        r = await http.get(f"/s/{share.json()['secret']}/content", headers=accept)
        unknown = await http.get("/s/never-existed-key/content", headers=accept)
        assert r.status_code == 404
        assert _fingerprint(r) == _fingerprint(unknown)


async def test_a_version_share_serves_that_versions_bytes(http, override_actor):
    """`resolve_secret` also resolves `artifact_version` shares; the sub-route
    inherits that branch for free and must serve its bytes, not 404.

    (v0 has no second-version write path — creation is the only upload — so
    this pins the branch, not head-vs-pinned divergence.)"""
    override_actor(make_actor())
    drive = await _create_drive(http, "cver", "kcver")
    art = await _create_artifact(
        http, drive, name="v.md", body=b"# One\n", key="kcver-1"
    )

    share = await _create_share(
        http, drive["id"], "kcver-2",
        resource_type="artifact_version", resource_id=art["head_version_id"],
    )

    r = await http.get(f"/s/{share.json()['secret']}/content", headers=BROWSER)

    assert r.status_code == 200
    assert r.content == b"# One\n"


async def test_a_browser_resolving_the_img_src_actually_reaches_the_bytes(
    http, override_actor
):
    """The end-to-end property every other test here only approximates.

    Asserting `src="content"` is in the page proves nothing on its own — the
    question is where a browser *resolves* it to. Relative resolution replaces
    the last path segment, so the page URL's trailing slash decides whether
    that lands on the bytes or on `/s/content`. This resolves the src the way
    a browser would and fetches it, from both the canonical URL and a legacy
    un-slashed link.
    """
    from urllib.parse import urljoin

    override_actor(make_actor())
    drive = await _create_drive(http, "resolv", "kres")
    art = await _create_artifact(
        http, drive, name="chart.png", body=PNG, key="kres-1",
        content_type="image/png",
    )
    share = await _create_share(
        http, drive["id"], "kres-2", resource_type="artifact", resource_id=art["id"]
    )
    secret = share.json()["secret"]

    for label, url in (("canonical", f"/s/{secret}/"), ("legacy", f"/s/{secret}")):
        page = await http.get(url, headers=BROWSER, follow_redirects=True)
        assert page.status_code == 200, label

        src = re.search(r'<img[^>]*src="([^"]+)"', page.text).group(1)
        resolved = urljoin(str(page.request.url), src)
        assert resolved.endswith(f"/s/{secret}/content"), (label, resolved)

        got = await http.get(resolved, headers=IMG)
        assert got.status_code == 200, (label, got.text)
        assert got.content == PNG, label


async def test_a_hostile_stored_content_type_cannot_break_the_byte_route(
    http, override_actor
):
    """Nothing validates `content_type` on upload, so it can hold a CRLF.

    Emitted raw that is a header-injection attempt. The server refuses the
    malformed header — but by raising, which drops the connection and returns
    zero bytes. One bad upload would then break that artifact's bytes for
    every reader, permanently, with no way for them to fix it. Sanitizing
    before the value can become a header is what keeps this a served response.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "hostct", "khct")
    art = await _create_artifact(
        http, drive, name="ok.md", body=b"# fine\n", key="khct-1",
        content_type="text/markdown",
    )
    # Reach past the API to store what a validated upload would not accept —
    # the point is that the read path must survive it regardless. Versions are
    # append-only, so this adds a head rather than rewriting one.
    hostile = "text/markdown\r\nX-Evil: injected"
    async with conn() as c:
        old = await c.fetchrow(
            "SELECT storage_object, size_bytes, checksum FROM artifact_versions"
            " WHERE artifact_id = $1", art["id"],
        )
        new_version = "ver_" + "c0ffee00" * 2
        await c.execute(
            "INSERT INTO artifact_versions (id, artifact_id, ordinal, storage_object,"
            " size_bytes, content_type, checksum, actor_type, actor_id)"
            " VALUES ($1,$2,2,$3,$4,$5,$6,'system',NULL)",
            new_version, art["id"], old["storage_object"], old["size_bytes"],
            hostile, old["checksum"],
        )
        await c.execute(
            "UPDATE artifacts SET content_type = $2, head_version_id = $3 WHERE id = $1",
            art["id"], hostile, new_version,
        )
    share = await _create_share(
        http, drive["id"], "khct-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/content", headers=BROWSER)

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert "x-evil" not in {k.lower() for k in r.headers}
    for header in r.headers.values():
        assert "\r" not in header and "\n" not in header


async def test_the_byte_csp_is_stricter_than_the_page_csp(http, override_actor):
    """Raw bytes never need script or object; the page does.

    The page carries `script-src 'self'` for its theme toggle. Letting the
    byte responses inherit that would grant a capability they have no use
    for, leaving `sandbox` as the only thing taking it back. Not granting it
    is the stronger arrangement.
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "cspx", "kcspx")
    art = await _create_artifact(
        http, drive, name="chart.png", body=PNG, key="kcspx-1",
        content_type="image/png",
    )
    share = await _create_share(
        http, drive["id"], "kcspx-2", resource_type="artifact", resource_id=art["id"]
    )
    secret = share.json()["secret"]

    page = await http.get(f"/s/{secret}/", headers=BROWSER)
    content = await http.get(f"/s/{secret}/content", headers=IMG)

    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "script-src 'none'" in content.headers["content-security-policy"]
    assert "object-src 'none'" in content.headers["content-security-policy"]
    assert "sandbox" in content.headers["content-security-policy"]
