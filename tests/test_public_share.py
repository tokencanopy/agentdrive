"""`/s/{share_key}` as a recipient actually receives it.

Until now the redemption route 404'd every browser: it required a JSON
`Accept` and rejected anything else, so the product's founding promise — paste
a link, see a rendered document — did not hold. This module pins
the flipped behaviour: a browser gets HTML with OpenGraph tags in the *first*
response (unfurl bots run no JavaScript), a JSON client keeps getting bytes.

Two properties matter more than the rendering:

  * **Anti-enumeration.** Unknown, revoked, expired and deleted-target keys
    must be indistinguishable — same status, same headers, same bytes. A
    prober with a list of candidate keys must learn nothing from the
    difference between "never existed" and "existed and was revoked".
  * **The key is a credential.** It rides in the URL, so it must not appear in
    the page, in an OpenGraph tag, in a canonical URL, or in any header.

Fixtures follow `tests/test_v0_shares.py` (local `http` / `override_actor` /
`_clean_tables`), not the deleted root `client` fixture.
"""

from __future__ import annotations

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

# Pinned literally: this is the one thing standing between an agent-authored
# artifact and script execution on our own origin if the escaping ever slips.
CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "worker-src 'self' blob:; img-src 'self' data: https:; "
    "media-src 'self'; "
    "style-src 'self'; font-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


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
    """Everything a prober can observe, minus the per-request correlation id.

    `X-Request-Id` is minted fresh per request by RequestContextMiddleware and
    carries no information about the target, so it is the only field excluded.
    """
    headers = {k: v for k, v in response.headers.items() if k != "x-request-id"}
    return response.status_code, headers, response.content


# ── the rendered page ────────────────────────────────────────────────────────


async def test_share_link_renders_html_for_a_browser(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "pub", "kpub")
    art = await _create_artifact(
        http, drive, name="report.md", body=b"# Quarterly\n\nBody.\n", key="kpub-1"
    )
    share = await _create_share(
        http, drive["id"], "kpub-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/", headers=BROWSER)

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "<h1>Quarterly</h1>" in r.text


async def test_share_link_carries_opengraph_tags_in_the_first_response(
    http, override_actor
):
    override_actor(make_actor())
    drive = await _create_drive(http, "pubog", "kog")
    art = await _create_artifact(
        http, drive, name="report.md", body=b"# Quarterly\n", key="kog-1"
    )
    share = await _create_share(
        http, drive["id"], "kog-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/", headers=BROWSER)

    assert '<meta property="og:title" content="Quarterly"' in r.text
    # The description is METADATA, never content: a chat preview that quotes
    # the document body is a disclosure decision nobody made.
    assert '<meta property="og:description"' in r.text
    assert "Body." not in r.text.split("<main")[0]


async def test_share_page_carries_the_three_public_response_headers(
    http, override_actor
):
    """A cached page for a revoked link is a leak; a referer carries the key
    to every link the reader clicks; a CSP is what stops artifact-authored
    markup from executing on our origin."""
    override_actor(make_actor())
    drive = await _create_drive(http, "pubhdr", "khdr")
    art = await _create_artifact(http, drive, name="r.md", body=b"# R\n", key="khdr-1")
    share = await _create_share(
        http, drive["id"], "khdr-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/", headers=BROWSER)

    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == CSP


async def test_the_share_key_never_appears_in_the_response(http, override_actor):
    """Possession of the key IS the credential, so it must not be echoed into
    an OG tag, a canonical URL, a Location, or the page text — anywhere an
    unfurl bot or a copy-paste would carry it further than the recipient."""
    override_actor(make_actor())
    drive = await _create_drive(http, "pubkey", "kkey")
    art = await _create_artifact(http, drive, name="r.md", body=b"# R\n", key="kkey-1")
    share = await _create_share(
        http, drive["id"], "kkey-2", resource_type="artifact", resource_id=art["id"]
    )
    secret = share.json()["secret"]

    r = await http.get(f"/s/{secret}/", headers=BROWSER)

    assert secret not in r.text
    for value in r.headers.values():
        assert secret not in value


async def test_json_clients_still_get_bytes(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "pubjson", "kjson")
    art = await _create_artifact(http, drive, name="r.md", body=b"# R\n", key="kjson-1")
    share = await _create_share(
        http, drive["id"], "kjson-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/", headers=JSON)

    assert r.status_code == 200
    assert r.content == b"# R\n"
    assert r.headers["etag"] == f'"{art["head_version_id"]}"'


async def test_folder_share_is_the_uniform_404_for_a_browser(http, override_actor):
    """There is no folder viewer yet. A browser gets the same page an unknown
    key gets — not a hint that the key was good but the target unsupported."""
    override_actor(make_actor())
    drive = await _create_drive(http, "pubfld", "kfld")
    share = await _create_share(
        http,
        drive["id"],
        "kfld-1",
        resource_type="folder",
        resource_id=drive["root_folder_id"],
    )

    r = await http.get(f"/s/{share.json()['secret']}/", headers=BROWSER)
    unknown = await http.get("/s/no-such-key-at-all/", headers=BROWSER)

    assert r.status_code == 404
    assert _fingerprint(r) == _fingerprint(unknown)

    # ...while the JSON contract for a live folder share is untouched.
    j = await http.get(f"/s/{share.json()['secret']}/", headers=JSON)
    assert j.status_code == 200
    assert j.json()["resource_type"] == "folder"


# ── anti-enumeration ─────────────────────────────────────────────────────────


async def test_unknown_revoked_expired_and_deleted_are_byte_identical(
    http, override_actor
):
    """The headline property: four different reasons, one indistinguishable
    answer. If a revoked key were even one header different from an unknown
    one, a prober could sort a candidate list into "was real" and "never was".
    """
    override_actor(make_actor())
    drive = await _create_drive(http, "pubanti", "kanti")

    # 1. revoked
    revoked_art = await _create_artifact(
        http, drive, name="a.md", body=b"# A\n", key="kanti-1"
    )
    revoked = await _create_share(
        http,
        drive["id"],
        "kanti-2",
        resource_type="artifact",
        resource_id=revoked_art["id"],
    )
    await _revoke_share(http, drive["id"], revoked, "kanti-3")

    # 2. expired
    expired_art = await _create_artifact(
        http, drive, name="b.md", body=b"# B\n", key="kanti-4"
    )
    expired = await _create_share(
        http,
        drive["id"],
        "kanti-5",
        resource_type="artifact",
        resource_id=expired_art["id"],
    )
    async with conn() as c:
        await c.execute(
            "UPDATE shares SET expires_at=$2 WHERE id=$1",
            expired.json()["id"],
            datetime.now(UTC) - timedelta(minutes=5),
        )

    # 3. live share over a soft-deleted artifact
    gone_art = await _create_artifact(
        http, drive, name="c.md", body=b"# C\n", key="kanti-6"
    )
    gone = await _create_share(
        http,
        drive["id"],
        "kanti-7",
        resource_type="artifact",
        resource_id=gone_art["id"],
    )
    deleted = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/artifacts/{gone_art['id']}",
        headers={
            "Idempotency-Key": "kanti-8",
            "If-Match": f'"{gone_art["revision"]}"',
        },
    )
    assert deleted.status_code == 200, deleted.text

    responses = {
        "unknown": await http.get("/s/never-existed-key/", headers=BROWSER),
        "revoked": await http.get(f"/s/{revoked.json()['secret']}/", headers=BROWSER),
        "expired": await http.get(f"/s/{expired.json()['secret']}/", headers=BROWSER),
        "deleted": await http.get(f"/s/{gone.json()['secret']}/", headers=BROWSER),
    }

    baseline = _fingerprint(responses["unknown"])
    assert baseline[0] == 404
    for label, r in responses.items():
        assert _fingerprint(r) == baseline, f"{label} is distinguishable from unknown"

    # And nothing in the page names a reason.
    body = responses["revoked"].text.lower()
    for leak in ("revoked", "expired", "deleted", "grant", "permission"):
        assert leak not in body


async def test_unknown_and_revoked_are_identical_for_json_clients(
    http, override_actor
):
    """The same property on the byte path, which keeps its v0 error envelope."""
    override_actor(make_actor())
    drive = await _create_drive(http, "pubantij", "kantij")
    art = await _create_artifact(http, drive, name="a.md", body=b"# A\n", key="kantij-1")
    share = await _create_share(
        http, drive["id"], "kantij-2", resource_type="artifact", resource_id=art["id"]
    )
    await _revoke_share(http, drive["id"], share, "kantij-3")

    unknown = await http.get("/s/never-existed-key/", headers=JSON)
    revoked = await http.get(f"/s/{share.json()['secret']}/", headers=JSON)

    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "SHARE_NOT_FOUND"
    assert _fingerprint(unknown) == _fingerprint(revoked)


# ── rendering modes reachable through the route ──────────────────────────────


async def test_code_artifact_renders_highlighted_not_downloaded(http, override_actor):
    override_actor(make_actor())
    drive = await _create_drive(http, "pubcode", "kcode")
    art = await _create_artifact(
        http,
        drive,
        name="main.py",
        body=b"def hello():\n    return 1\n",
        key="kcode-1",
        content_type="text/x-python",
    )
    share = await _create_share(
        http, drive["id"], "kcode-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/", headers=BROWSER)

    assert r.status_code == 200
    assert 'class="highlight"' in r.text
    assert "hello" in r.text


async def test_artifact_html_is_escaped_not_executed(http, override_actor):
    """Artifact bytes are attacker-authored and render on our own origin."""
    override_actor(make_actor())
    drive = await _create_drive(http, "pubxss", "kxss")
    art = await _create_artifact(
        http,
        drive,
        name="evil.md",
        body=b"# Title\n\n<script>alert(1)</script>\n",
        key="kxss-1",
    )
    share = await _create_share(
        http, drive["id"], "kxss-2", resource_type="artifact", resource_id=art["id"]
    )

    r = await http.get(f"/s/{share.json()['secret']}/", headers=BROWSER)

    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


# ── canonical URL shape ──────────────────────────────────────────────────────


async def _live_share(http, override_actor, tag: str):
    override_actor(make_actor())
    drive = await _create_drive(http, tag, f"k{tag}")
    art = await _create_artifact(
        http, drive, name="report.md", body=b"# Quarterly\n\nBody.\n", key=f"k{tag}-1"
    )
    return await _create_share(
        http, drive["id"], f"k{tag}-2", resource_type="artifact", resource_id=art["id"]
    )


async def test_bare_share_url_canonicalizes_browsers_onto_the_trailing_slash(http):
    """`/s/KEY` → `/s/KEY/`, because relative sub-resources need the slash.

    The page links its bytes as `content`, and relative resolution replaces
    the last path segment: from `/s/KEY` that reaches `/s/content`, so every
    image on every share page would 404. Links already in the wild carry no
    slash, so this redirect is what keeps them working.
    """
    r = await http.get("/s/some-key", headers=BROWSER, follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/s/some-key/"


async def test_the_canonicalizing_redirect_is_not_an_existence_oracle(
    http, override_actor
):
    """It must fire before any lookup.

    If a live key redirected and a dead one 404'd, the status code alone would
    answer "is this key real?" — the very question anti-enumeration refuses.
    """
    live = await _live_share(http, override_actor, "orac")
    responses = [
        await http.get(
            f"/s/{live.json()['secret']}", headers=BROWSER, follow_redirects=False
        ),
        await http.get("/s/never-existed-key", headers=BROWSER, follow_redirects=False),
        await http.get("/s/x", headers=BROWSER, follow_redirects=False),
    ]
    assert {r.status_code for r in responses} == {308}


async def test_the_bare_url_still_serves_json_clients_in_place(http, override_actor):
    """API clients are never redirected, so the shipped v0 contract for this
    URL is byte-for-byte what it was."""
    share = await _live_share(http, override_actor, "json")
    r = await http.get(
        f"/s/{share.json()['secret']}", headers=JSON, follow_redirects=False
    )
    assert r.status_code == 200
    assert r.content == b"# Quarterly\n\nBody.\n"


@pytest.mark.parametrize(
    "hostile",
    [
        "k%0d%0aX-Injected:%20yes",   # response splitting
        "k%0aSet-Cookie:%20a=b",      # bare LF
        "k%0dLocation:%20evil",       # bare CR (no slashes: they would add path segments)
        "k%00null",                   # NUL
    ],
)
async def test_the_canonical_redirect_cannot_inject_a_response_header(http, hostile):
    """Ids come off the wire, and `%0d%0a` in a request line arrives decoded.

    Interpolating that straight into `Location` is response splitting. uvicorn
    happens to refuse the malformed header, but it refuses by raising — the
    connection drops and the caller gets zero bytes, which is a response shape
    none of this surface's uniform-404 guarantees cover, reachable by anyone
    with a crafted link. Encoding the segment fixes it here rather than
    relying on the server to catch it.

    Sent percent-encoded because that is how it arrives: a raw CR in a
    request line is not transmissible, but `%0d` is, and the router decodes
    it into the path param before this code sees it.
    """
    r = await http.get(f"/s/{hostile}", headers=BROWSER, follow_redirects=False)

    assert r.status_code == 308
    location = r.headers["location"]
    for forbidden in ("\r", "\n", "\x00"):
        assert forbidden not in location
    assert "x-injected" not in {k.lower() for k in r.headers}
    assert "set-cookie" not in {k.lower() for k in r.headers}
    assert location.startswith("/s/") and location.endswith("/")


async def test_the_public_surface_is_rate_limited(http, monkeypatch):
    """The only fully anonymous surface must not be the only unthrottled one.

    It does the most expensive work per request — markdown and Pygments over
    up to 2 MiB of attacker-authored content, plus recursive grant resolution
    — behind a URL anyone can publish once and then hammer. The `/v0` routers
    have carried this dependency all along; this one was left out.
    """
    from limits import parse_many

    from agentdrive.api import v0_rate_limit

    monkeypatch.setattr(v0_rate_limit, "_ITEMS", list(parse_many("3/minute")))

    codes = [
        (await http.get("/s/some-key", headers=BROWSER, follow_redirects=False)).status_code
        for _ in range(6)
    ]

    assert 429 in codes, codes
    assert codes[0] == 308, "the first requests must still be served"


async def test_the_csp_allows_self_scripts_but_never_unsafe_inline(http):
    """The one directive that must never appear on this surface.

    `script-src 'self'` still stops an injected script: an attacker cannot
    write a file into our origin to point one at. `'unsafe-inline'` would
    permit exactly the injected-inline case the directive exists to catch,
    which is why the viewer's script is an external file.
    """
    r = await http.get("/s/nope", headers=BROWSER, follow_redirects=False)
    csp = r.headers["content-security-policy"]

    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "default-src 'none'" in csp


async def test_the_viewer_script_serves(http):
    r = await http.get("/public-static/viewer.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert b"agentdrive-theme" in r.content  # same key as the rest of the product
