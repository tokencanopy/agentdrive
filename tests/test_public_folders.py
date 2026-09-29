"""`/f/{fld_id}/` — the folder permalink, and the one surface that enumerates.

`/a/` and `/v/` answer a question about ONE resource the caller already named.
`/f/` hands out a list of resources the caller did not name, which makes it the
only public route that can leak by *inclusion* rather than by response code.
So the property this file pins is narrower and stronger than "the folder is
published":

    **every child the listing names is a child the surface would serve.**

Concretely, a listed entry's link must return 200 from `/a/{id}/` or
`/f/{id}/`. If the listing could ever name a row those routes refuse, the page
would be an oracle over names the grant does not cover — the exact failure the
404 on those routes exists to prevent. The listing filter is therefore
`v0_authz.visibility_lateral` with the same anonymous principal
`public_reads` resolves with, not a second predicate: one interpretation of
grant matching, so the list and the serve cannot disagree.

The three cases that make the invariant non-trivial, each pinned below:

  * a **soft-deleted** child — gone from the listing, and `/a/` 404s it (its
    *name* is withheld too, not shown-but-unlinked);
  * a **nested** child — inheritance is additive-only, so the parent's grant
    reaches the whole subtree; it is served, so it is listed;
  * a child whose **own** grant was revoked — still covered by the folder
    grant, still served by `/a/`, so still listed. Under-listing would be a
    bug in the other direction.

Fixtures follow `tests/test_public_permalinks.py` (local `http` /
`override_actor` / `_clean_tables`), not the deleted root `client` fixture.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.core import public_reads
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext
from agentdrive.public.page import ASSET_V, render_folder_page

# No module-level `pytest.mark.asyncio`: `asyncio_mode = "auto"` already runs
# the async tests, and the mark would warn on the two synchronous
# template-level tests at the bottom of this file.

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"

# What a browser sends on a top-level navigation.
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# A well-formed id that names nothing. Well-formed matters: a malformed id
# could be refused before the handler runs, which tests nothing.
NO_SUCH_FOLDER = "fld_ffffffffffffffff"

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


async def _mkdir(
    http,
    drive: dict,
    name: str,
    key: str,
    *,
    parent_id: str | None = None,
) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/folders",
        json={
            "parent_id": parent_id or drive["root_folder_id"],
            "name": name,
        },
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


async def _delete_artifact(http, drive_id: str, artifact: dict, key: str) -> None:
    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive_id}/artifacts/{artifact['id']}",
        headers={"Idempotency-Key": key, "If-Match": f'"{artifact["revision"]}"'},
    )
    assert resp.status_code == 200, resp.text


async def _delete_folder(http, drive_id: str, folder: dict, key: str) -> None:
    resp = await http.request(
        "DELETE",
        f"/v0/drives/{drive_id}/folders/{folder['id']}?recursive=true",
        headers={"Idempotency-Key": key, "If-Match": f'"{folder["revision"]}"'},
    )
    assert resp.status_code == 200, resp.text


def _fingerprint(response) -> tuple:
    """Everything a prober can observe, minus the per-request correlation id."""
    headers = {k: v for k, v in response.headers.items() if k != "x-request-id"}
    return response.status_code, headers, response.content


def _origin() -> str:
    return settings.public_base_url.rstrip("/")


def _listed_links(html: str) -> list[str]:
    """Every child permalink the listing offers, in page order."""
    return re.findall(r'href="((?:/a/|/f/)[^"]+)"', html)


async def _published_folder(http, override_actor, tag: str) -> tuple:
    """A drive, a folder, and a live `public` grant on that folder."""
    override_actor(make_actor())
    drive = await _create_drive(http, tag, f"k{tag}")
    folder = await _mkdir(http, drive, "reports", f"k{tag}-1")
    grant = await _publish(
        http, drive["id"], f"k{tag}-2",
        resource_type="folder", resource_id=folder["id"],
    )
    return drive, folder, grant


# ── the listing ──────────────────────────────────────────────────────────────


async def test_public_folder_lists_its_children(http, override_actor):
    drive, folder, _ = await _published_folder(http, override_actor, "flist")
    await _create_artifact(
        http, drive, name="q1.md", body=b"# Q1\n", key="kflist-3",
        parent_id=folder["id"],
    )
    await _mkdir(http, drive, "archive", "kflist-4", parent_id=folder["id"])

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "q1.md" in r.text
    assert "archive" in r.text
    assert '<meta property="og:title" content="reports" />' in r.text
    assert "2 items" in r.text


async def test_the_folder_permalink_fills_in_its_own_canonical_url(
    http, override_actor
):
    """Like `/a/`, and unlike `/s/`, this URL carries no secret — so `og:url`
    is filled in, and it ends in the slash relative links resolve against."""
    _, folder, _ = await _published_folder(http, override_actor, "fog")

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    canonical = f"{_origin()}/f/{folder['id']}/"
    assert f'<meta property="og:url" content="{canonical}" />' in r.text
    assert canonical.endswith("/")


async def test_children_link_to_their_own_canonical_permalinks(http, override_actor):
    """A child link is a permalink, not a nested path: it resolves through the
    child's OWN grant check. The trailing slash keeps the link off the 308."""
    drive, folder, _ = await _published_folder(http, override_actor, "flink")
    art = await _create_artifact(
        http, drive, name="q1.md", body=b"# Q1\n", key="kflink-3",
        parent_id=folder["id"],
    )
    sub = await _mkdir(http, drive, "archive", "kflink-4", parent_id=folder["id"])

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    links = _listed_links(r.text)
    assert f"/f/{sub['id']}/" in links
    assert f"/a/{art['id']}/" in links


async def test_the_kind_chip_comes_from_the_one_classifier(http, override_actor):
    """`core.kinds.kind_for` decides, here as on the artifact page — a second
    content-type table would let the chip and the rendered body disagree about
    the same artifact."""
    drive, folder, _ = await _published_folder(http, override_actor, "fkind")
    await _create_artifact(
        http, drive, name="chart.png", body=b"\x89PNG\r\n\x1a\n" + b"0" * 32,
        key="kfkind-3", content_type="image/png", parent_id=folder["id"],
    )
    await _create_artifact(
        http, drive, name="rows.csv", body=b"a,b\n1,2\n", key="kfkind-4",
        content_type="text/csv", parent_id=folder["id"],
    )

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert 'data-k="image"' in r.text
    assert 'data-k="dataset"' in r.text
    assert 'data-k="folder"' in r.text  # the page's own header chip


# ── what is listed, and what is withheld ─────────────────────────────────────


async def test_every_listed_child_is_one_the_surface_would_serve(
    http, override_actor
):
    """The headline invariant, asserted end to end over a folder holding one of
    each interesting child: follow every link the listing offers and require a
    200. A name in the listing that 404s on click is a name the grant never
    covered being disclosed anyway."""
    override_actor(make_actor())
    drive = await _create_drive(http, "finv", "kfinv")
    folder = await _mkdir(http, drive, "reports", "kfinv-1")
    await _publish(
        http, drive["id"], "kfinv-2",
        resource_type="folder", resource_id=folder["id"],
    )
    # a plain artifact, a plain sub-folder, a nested sub-folder, a
    # soft-deleted artifact, and an artifact whose own grant was revoked
    await _create_artifact(
        http, drive, name="q1.md", body=b"# Q1\n", key="kfinv-3", parent_id=folder["id"]
    )
    archive = await _mkdir(http, drive, "archive", "kfinv-4", parent_id=folder["id"])
    await _mkdir(http, drive, "nested", "kfinv-5", parent_id=archive["id"])
    gone = await _create_artifact(
        http, drive, name="gone.md", body=b"# Gone\n", key="kfinv-6",
        parent_id=folder["id"],
    )
    await _delete_artifact(http, drive["id"], gone, "kfinv-7")
    unshared = await _create_artifact(
        http, drive, name="own.md", body=b"# Own\n", key="kfinv-8",
        parent_id=folder["id"],
    )
    own_grant = await _publish(
        http, drive["id"], "kfinv-9",
        resource_type="artifact", resource_id=unshared["id"],
    )
    await _revoke_grant(http, drive["id"], own_grant, "kfinv-10")

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)
    assert r.status_code == 200, r.text

    links = _listed_links(r.text)
    assert links, "the listing offered nothing to check"
    for link in links:
        child = await http.get(link, headers=BROWSER)
        assert child.status_code == 200, f"{link} is listed but not servable"


async def test_a_soft_deleted_child_is_not_listed(http, override_actor):
    """A soft-deleted artifact 404s at `/a/`; naming it here would leak the
    filename of something the publisher has already taken down."""
    drive, folder, _ = await _published_folder(http, override_actor, "fdel")
    gone = await _create_artifact(
        http, drive, name="retracted.md", body=b"# Oops\n", key="kfdel-3",
        parent_id=folder["id"],
    )
    kept = await _create_artifact(
        http, drive, name="kept.md", body=b"# Kept\n", key="kfdel-4",
        parent_id=folder["id"],
    )
    await _delete_artifact(http, drive["id"], gone, "kfdel-5")

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert "kept.md" in r.text
    assert "retracted.md" not in r.text
    assert gone["id"] not in r.text
    assert (await http.get(f"/a/{gone['id']}/", headers=BROWSER)).status_code == 404
    assert (await http.get(f"/a/{kept['id']}/", headers=BROWSER)).status_code == 200


async def test_a_soft_deleted_child_folder_is_not_listed(http, override_actor):
    drive, folder, _ = await _published_folder(http, override_actor, "fdelf")
    sub = await _mkdir(http, drive, "retired", "kfdelf-3", parent_id=folder["id"])
    await _delete_folder(http, drive["id"], sub, "kfdelf-4")

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert "retired" not in r.text
    assert sub["id"] not in r.text
    assert (await http.get(f"/f/{sub['id']}/", headers=BROWSER)).status_code == 404


async def test_the_published_grant_reaches_the_whole_subtree(http, override_actor):
    """Additive-only inheritance at the public surface: one `public` grant on
    the parent publishes every descendant, at any depth. Nothing under a
    published folder can opt out of being reachable — that used to be exactly
    what `grant_inheritance='sealed'` did, and it is gone.

    Read together with the headline invariant: the listing names the child
    BECAUSE `/f/` would serve it, not the other way round."""
    drive, folder, _ = await _published_folder(http, override_actor, "fsub")
    child = await _mkdir(http, drive, "quarterly", "kfsub-3", parent_id=folder["id"])
    grandchild = await _mkdir(http, drive, "q3", "kfsub-4", parent_id=child["id"])
    deep = await _create_artifact(
        http, drive, name="inside.md", body=b"# Inside\n", key="kfsub-5",
        parent_id=grandchild["id"],
    )

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    # The immediate child is listed and servable...
    assert "quarterly" in r.text
    assert f"/f/{child['id']}/" in r.text
    assert (await http.get(f"/f/{child['id']}/", headers=BROWSER)).status_code == 200
    # ...and the grant keeps reaching past it, all the way down.
    assert (await http.get(f"/f/{grandchild['id']}/", headers=BROWSER)).status_code == 200
    assert (await http.get(f"/a/{deep['id']}/", headers=BROWSER)).status_code == 200


async def test_a_drive_grant_lists_every_child(http, override_actor):
    """A drive grant is authoritative throughout the drive (§8). The listing
    inherits that from `v0_authz` rather than re-deciding it — under-listing
    here would be its own kind of wrong."""
    override_actor(make_actor())
    drive = await _create_drive(http, "fdrv", "kfdrv")
    folder = await _mkdir(http, drive, "reports", "kfdrv-1")
    child = await _mkdir(http, drive, "embargoed", "kfdrv-2", parent_id=folder["id"])
    await _publish(
        http, drive["id"], "kfdrv-3",
        resource_type="drive", resource_id=drive["id"],
    )

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert r.status_code == 200, r.text
    assert f"/f/{child['id']}/" in r.text
    assert (await http.get(f"/f/{child['id']}/", headers=BROWSER)).status_code == 200


async def test_a_child_whose_own_grant_was_revoked_is_still_listed(
    http, override_actor
):
    """Revoking a redundant direct grant must not un-publish what the folder
    grant already covers: `/a/` still serves it, so the listing still names
    it. This is the invariant read in the other direction."""
    drive, folder, _ = await _published_folder(http, override_actor, "frev")
    art = await _create_artifact(
        http, drive, name="still.md", body=b"# Still\n", key="kfrev-3",
        parent_id=folder["id"],
    )
    own = await _publish(
        http, drive["id"], "kfrev-4",
        resource_type="artifact", resource_id=art["id"],
    )
    await _revoke_grant(http, drive["id"], own, "kfrev-5")

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert f"/a/{art['id']}/" in r.text
    assert (await http.get(f"/a/{art['id']}/", headers=BROWSER)).status_code == 200


async def test_only_immediate_children_are_listed(http, override_actor):
    """A grandchild is reached by clicking through to its own `/f/` page. The
    listing is one level, so a published subtree cannot be walked in one
    unbounded response."""
    drive, folder, _ = await _published_folder(http, override_actor, "fdeep")
    sub = await _mkdir(http, drive, "archive", "kfdeep-3", parent_id=folder["id"])
    grandchild = await _create_artifact(
        http, drive, name="deep.md", body=b"# Deep\n", key="kfdeep-4",
        parent_id=sub["id"],
    )

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert "deep.md" not in r.text
    assert grandchild["id"] not in r.text
    # ...but it is reachable one click down, through the sub-folder's page.
    nested = await http.get(f"/f/{sub['id']}/", headers=BROWSER)
    assert nested.status_code == 200
    assert f"/a/{grandchild['id']}/" in nested.text


# ── anti-enumeration ─────────────────────────────────────────────────────────


async def test_unpublished_revoked_expired_deleted_and_unknown_are_identical(
    http, override_actor
):
    """A `fld_*` id is not a secret. If "published once, revoked since"
    answered even one header differently from "never existed", `/f/` would be
    an existence oracle over every folder id anyone has ever seen."""
    override_actor(make_actor())
    drive = await _create_drive(http, "fanti", "kfanti")

    private = await _mkdir(http, drive, "private", "kfanti-1")

    revoked_folder = await _mkdir(http, drive, "revoked", "kfanti-2")
    grant = await _publish(
        http, drive["id"], "kfanti-3",
        resource_type="folder", resource_id=revoked_folder["id"],
    )
    await _revoke_grant(http, drive["id"], grant, "kfanti-4")

    expired_folder = await _mkdir(http, drive, "expired", "kfanti-5")
    await _publish(
        http, drive["id"], "kfanti-6",
        resource_type="folder", resource_id=expired_folder["id"],
        expires_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    )

    gone_folder = await _mkdir(http, drive, "gone", "kfanti-7")
    await _publish(
        http, drive["id"], "kfanti-8",
        resource_type="folder", resource_id=gone_folder["id"],
    )
    await _delete_folder(http, drive["id"], gone_folder, "kfanti-9")

    responses = {
        "unknown": await http.get(f"/f/{NO_SUCH_FOLDER}/", headers=BROWSER),
        "unpublished": await http.get(f"/f/{private['id']}/", headers=BROWSER),
        "revoked": await http.get(f"/f/{revoked_folder['id']}/", headers=BROWSER),
        "expired": await http.get(f"/f/{expired_folder['id']}/", headers=BROWSER),
        "deleted": await http.get(f"/f/{gone_folder['id']}/", headers=BROWSER),
    }

    baseline = _fingerprint(responses["unknown"])
    assert baseline[0] == 404
    for label, r in responses.items():
        assert _fingerprint(r) == baseline, f"{label} is distinguishable from unknown"

    body = responses["revoked"].text.lower()
    for leak in ("revoked", "expired", "deleted", "grant", "permission", "forbidden"):
        assert leak not in body


async def test_the_folder_404_is_the_same_404_the_artifact_permalink_gives(
    http, override_actor
):
    """One refusal on this surface, not one per route — otherwise the status
    line tells a prober which KIND of id they guessed."""
    unknown_folder = await http.get(f"/f/{NO_SUCH_FOLDER}/", headers=BROWSER)
    unknown_artifact = await http.get("/a/art_ffffffffffffffff/", headers=BROWSER)

    assert _fingerprint(unknown_folder) == _fingerprint(unknown_artifact)


@pytest.mark.parametrize(
    ("principal_type", "principal_id"),
    [("agent", AGENT), ("user", SPONSOR), ("workspace", WS_A)],
)
async def test_only_a_public_grant_publishes_a_folder(
    http, override_actor, principal_type, principal_id
):
    """A folder shared with one teammate must not be on the open internet.
    Asserted directly per principal type: every other test here would still
    pass if `_principal_matches` started matching the anonymous principal."""
    override_actor(make_actor())
    drive = await _create_drive(http, f"fo{principal_type}", f"kfo{principal_type}")
    folder = await _mkdir(http, drive, "team", f"kfo{principal_type}-1")
    await _create_artifact(
        http, drive, name="secret.md", body=b"# Secret\n",
        key=f"kfo{principal_type}-2", parent_id=folder["id"],
    )
    resp = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={
            "principal_type": principal_type,
            "principal_id": principal_id,
            "role": "viewer",
            "resource_type": "folder",
            "resource_id": folder["id"],
        },
        headers={"Idempotency-Key": f"kfo{principal_type}-3"},
    )
    assert resp.status_code == 201, resp.text

    page = await http.get(f"/f/{folder['id']}/", headers=BROWSER)
    unknown = await http.get(f"/f/{NO_SUCH_FOLDER}/", headers=BROWSER)

    assert _fingerprint(page) == _fingerprint(unknown)
    assert b"secret.md" not in page.content


# ── canonical URL shape ──────────────────────────────────────────────────────


async def test_the_bare_folder_permalink_canonicalizes_onto_the_slash(http):
    r = await http.get(f"/f/{NO_SUCH_FOLDER}", headers=BROWSER, follow_redirects=False)

    assert r.status_code == 308
    assert r.headers["location"] == f"/f/{NO_SUCH_FOLDER}/"


async def test_the_folder_redirect_is_not_an_existence_oracle(http, override_actor):
    """It fires before any lookup. If a published folder redirected and an
    unpublished one 404'd, the status code alone would answer the question the
    404 refuses."""
    override_actor(make_actor())
    drive = await _create_drive(http, "foracle", "kforacle")
    private = await _mkdir(http, drive, "private", "kforacle-1")
    published = await _mkdir(http, drive, "public", "kforacle-2")
    await _publish(
        http, drive["id"], "kforacle-3",
        resource_type="folder", resource_id=published["id"],
    )

    statuses = set()
    for fld_id in (published["id"], private["id"], NO_SUCH_FOLDER, "not-an-id"):
        r = await http.get(f"/f/{fld_id}", headers=BROWSER, follow_redirects=False)
        statuses.add(r.status_code)

    assert statuses == {308}


# ── headers ──────────────────────────────────────────────────────────────────


async def test_the_folder_page_revalidates_and_carries_the_public_headers(
    http, override_actor
):
    """`/f/` names a MUTABLE listing — a child added or taken down must show up
    — so it revalidates like `/a/` rather than caching immutably like `/v/`."""
    _, folder, _ = await _published_folder(http, override_actor, "fhdr")

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert r.headers["cache-control"] == "public, max-age=60, must-revalidate"
    assert "immutable" not in r.headers["cache-control"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == CSP


async def test_the_folder_page_has_no_inline_style_or_script(http, override_actor):
    """The CSP is `default-src 'none'; style-src 'self'; script-src 'self'`, so
    an inline block would silently not apply — the page must be styled only by
    viewer.css and scripted only by viewer.js.

    The listing loads that script for the same two reasons the document pages
    do: it resolves the theme before first paint, and framed under the trusted
    shell it takes the reader's chosen theme from the parent and reports its
    own height back. A listing that followed the OS instead would render light
    inside a shell the reader had set to dark.
    """
    drive, folder, _ = await _published_folder(http, override_actor, "fcsp")
    await _create_artifact(
        http, drive, name="q1.md", body=b"# Q1\n", key="kfcsp-3",
        parent_id=folder["id"],
    )

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert "<style" not in r.text
    assert 'style="' not in r.text
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", r.text, re.I), "inline handler"
    scripts = re.findall(r"<script\b[^>]*>", r.text, re.I)
    assert scripts and all(
        re.search(r'src="/public-static/viewer\.js\?v=[a-f0-9]+"', tag) for tag in scripts
    ), scripts
    assert re.search(
        r'<link rel="stylesheet" href="/public-static/viewer\.css\?v=[a-f0-9]+" />', r.text
    )


# ── escaping (unit — names this hostile cannot be created through the API) ───


def test_child_names_are_escaped_in_the_listing():
    """`validate_name` restricts names created through `/v0`, so a hostile name
    can only arrive from some future write path. Escaping is asserted at the
    template, where it is actually guaranteed, rather than assumed upstream."""
    evil = '"><script>alert(1)</script>'
    html = render_folder_page(
        name=evil,
        path=evil,
        entries=[
            {
                "kind": "md", "name": evil, "id": evil,
                "size_bytes": 1, "is_folder": False,
            }
        ],
        canonical_url="https://share.example.test/f/fld_1/",
        description=evil,
        truncated=False,
    )

    # The page's own script is external and versioned; the hostile name must
    # not have produced a second one, nor any markup at all.
    assert re.findall(r"<script\b[^>]*>", html) == [
        f'<script src="/public-static/viewer.js?v={ASSET_V}">'
    ]
    assert "<script>alert(1)</script>" not in html
    assert "&#34;" in html  # the quote itself, not just the angle brackets
    for attr in ("og:title", "og:url", "og:description"):
        value = re.search(rf'property="{attr}" content="([^"]*)"', html).group(1)
        assert "<" not in value and '"' not in value


async def test_a_huge_folder_is_capped_rather_than_served_whole(
    http, override_actor, monkeypatch
):
    """A listing is unbounded work on an unauthenticated route: one `mkdir`
    plus 100k uploads would otherwise mint a page every crawler asks for and
    nobody can serve. The cap is exercised with a small value rather than by
    creating 501 rows — the LIMIT path is the same either way."""
    monkeypatch.setattr(public_reads, "MAX_LISTING_ENTRIES", 2)
    drive, folder, _ = await _published_folder(http, override_actor, "fcap")
    for n in range(4):
        await _create_artifact(
            http, drive, name=f"r{n}.md", body=b"# R\n", key=f"kfcap-a{n}",
            parent_id=folder["id"],
        )

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert r.status_code == 200
    assert len(_listed_links(r.text)) == 2
    assert "Only the first 2 items are shown." in r.text
    # Still a truthful listing: what it does name, it serves.
    for link in _listed_links(r.text):
        assert (await http.get(link, headers=BROWSER)).status_code == 200


def test_an_empty_folder_still_renders():
    html = render_folder_page(
        name="empty",
        path="empty",
        entries=[],
        canonical_url="https://share.example.test/f/fld_1/",
        description="empty · 0 items",
        truncated=False,
    )

    assert html.startswith("<!doctype html>")
    assert "0 items" in html


async def test_the_listing_chip_matches_the_artifact_page_chip(http, override_actor):
    """A PDF reading `bundle` in the listing and `pdf` one click later reads
    as a bug, even though both were technically true."""
    override_actor(make_actor())
    drive = await _create_drive(http, "chip", "kchip")
    folder = await _mkdir(http, drive, "docs", "kchip-f")
    await _create_artifact(
        http, drive, name="report.pdf", body=b"%PDF-1.4", key="kchip-1",
        content_type="application/pdf", parent_id=folder["id"],
    )
    await _publish(
        http, drive["id"], "kchip-2", resource_type="folder", resource_id=folder["id"]
    )

    r = await http.get(f"/f/{folder['id']}/", headers=BROWSER)

    assert ">pdf<" in r.text, "the listing should say pdf, not bundle"
