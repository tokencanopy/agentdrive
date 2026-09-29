"""Permission events in the drive change feed (grants + shares).

The sharing mutators (``core.v0_grants`` / ``core.v0_shares``) append six new
change types — ``grant.created|updated|revoked`` and
``share.created|revoked|rotated`` — on the SAME transaction as the mutation
they record. These rows re-expose the access graph #430 gated to managers, so
``changes_list`` filters them out server-side for non-managers via a WHERE
predicate on the page query (keeping the sealed keyset cursor stable).

Covered here:
  * each of the 6 mutators emits its event; a FAILED mutation emits none
    (in-transaction atomicity);
  * no share secret or hash ever reaches a change payload;
  * a viewer's feed excludes permission events and a manager's includes them —
    same drive, same cursor semantics, both stable and terminating across
    pagination (the crux);
  * the ``type`` filter narrows correctly and rejects unknown params.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.core.v0_changes import PERMISSION_CHANGE_TYPES
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

MANAGER = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
VIEWER = "tcagt_0000000000000002"
GRANTEE = "tcagt_0000000000000003"
WS_A = "tcws_0000000000000001"

_ALL_SCOPES = {
    "drives:read", "drives:write", "usage:read",
    "content:read", "content:write", "changes:read",
    "sharing:read", "sharing:write",
}


def make_actor(
    *,
    subject: str = MANAGER,
    subject_type: str = "agent",
    workspace: str = WS_A,
    scopes: set[str] | None = None,
    sponsor: str | None = SPONSOR,
) -> V0ActorContext:
    scopes = scopes if scopes is not None else set(_ALL_SCOPES)
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


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _create_drive(http, override_actor, name: str, key: str) -> dict:
    override_actor(make_actor())
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_folder(http, drive_id: str, parent_id: str, name: str, key: str) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive_id}/folders",
        json={"parent_id": parent_id, "name": name},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _insert_artifact(c, drive_id, folder_id, art_id, name) -> str:
    version_id = "ver_" + art_id[len("art_"):]
    await c.execute(
        "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
        "VALUES ($1, $2, $3, $4, $5)",
        art_id, drive_id, folder_id, name, "rev_00000000000000a1",
    )
    await c.execute(
        "INSERT INTO artifact_versions "
        "(id, artifact_id, checksum, content_type, size_bytes, storage_object, "
        "actor_type, actor_id, ordinal) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        version_id, art_id, "sha256:1", "application/octet-stream", 10,
        "cas/source.bin", "agent", MANAGER, 1,
    )
    await c.execute(
        "UPDATE artifacts SET head_version_id = $2 WHERE id = $1", art_id, version_id
    )
    return version_id


async def _create_grant(http, drive_id, key, **body) -> tuple[dict, str]:
    resp = await http.post(
        f"/v0/drives/{drive_id}/grants", json=body, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), resp.headers["etag"]


async def _create_share(http, drive_id, key, **body) -> tuple[dict, str]:
    resp = await http.post(
        f"/v0/drives/{drive_id}/shares", json=body, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), resp.headers["etag"]


async def _feed(c, drive_id) -> list[dict]:
    rows = []
    for r in await c.fetch(
        "SELECT type, resource_type, resource_id, data FROM drive_changes "
        "WHERE drive_id=$1 ORDER BY sequence",
        drive_id,
    ):
        row = dict(r)
        d = row["data"]
        row["data"] = json.loads(d) if isinstance(d, str) else (d or {})
        rows.append(row)
    return rows


async def _walk(http, drive_id, *, limit, type=None, order=None, max_iter=500):
    """Page the whole feed. Returns (items, pages).

    `order=None` walks forward from the beginning (the default sync walk);
    `order="newest"` walks backward from the head. Either way `order` rides
    only on the OPENING request — the cursor carries the direction after that.

    Bounded iteration count catches a non-terminating (looping) walk."""
    params = {"start": "beginning", "limit": limit}
    if order is not None:
        params = {"start": "now", "order": order, "limit": limit}
    if type is not None:
        params["type"] = type
    resp = await http.get(f"/v0/drives/{drive_id}/changes", params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    pages = [body]
    items = list(body["items"])
    iters = 0
    while body["has_more"]:
        iters += 1
        assert iters < max_iter, "pagination did not terminate (cursor loop)"
        p = {"cursor": body["next_cursor"], "limit": limit}
        if type is not None:
            p["type"] = type
        resp = await http.get(f"/v0/drives/{drive_id}/changes", params=p)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        pages.append(body)
        items.extend(body["items"])
    return items, pages


# --------------------------------------------------------------------------- #
# each mutator emits its event (in-transaction: success writes, failure doesn't)
# --------------------------------------------------------------------------- #


async def test_grant_mutators_emit_events(http, override_actor):
    drive = await _create_drive(http, override_actor, "g", "kg")
    override_actor(make_actor())

    grant, etag = await _create_grant(
        http, drive["id"], "kg-1",
        principal_type="agent", principal_id=GRANTEE,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    patch = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{grant['id']}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "kg-2", "If-Match": etag},
    )
    assert patch.status_code == 200, patch.text
    new_etag = patch.headers["etag"]
    revoke = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/grants/{grant['id']}",
        headers={"Idempotency-Key": "kg-3", "If-Match": new_etag},
    )
    assert revoke.status_code == 200, revoke.text

    async with conn() as c:
        feed = await _feed(c, drive["id"])
    created = [
        r for r in feed
        if r["type"] == "grant.created" and r["data"]["grant_id"] == grant["id"]
    ]
    updated = [r for r in feed if r["type"] == "grant.updated"]
    revoked = [r for r in feed if r["type"] == "grant.revoked"]
    assert len(created) == 1 and len(updated) == 1 and len(revoked) == 1
    # The event is ON the grant's target resource, and data carries the
    # grantee principal + role (the access-graph fact), never on the drive row.
    assert created[0]["resource_type"] == "drive"
    assert created[0]["resource_id"] == drive["id"]
    assert created[0]["data"]["grant_id"] == grant["id"]
    assert created[0]["data"]["principal_id"] == GRANTEE
    assert created[0]["data"]["role"] == "viewer"
    # update carries the delta (previous role).
    assert updated[0]["data"]["role"] == "editor"
    assert updated[0]["data"]["previous"]["role"] == "viewer"


async def test_regrant_over_expired_emits_revoke_then_create(http, override_actor):
    """When create_grant steps over a stale-but-expired grant, the displaced
    row must leave the feed as its own grant.revoked (not silently), so the
    ledger has no phantom — then the fresh grant.created follows."""
    drive = await _create_drive(http, override_actor, "exp", "ke")
    override_actor(make_actor())
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    first, _ = await _create_grant(
        http, drive["id"], "ke-1",
        principal_type="agent", principal_id=GRANTEE,
        resource_type="drive", resource_id=drive["id"], role="viewer",
        expires_at=past,
    )
    # Same (resource, principal): the expired row is swept, a new one minted.
    second, _ = await _create_grant(
        http, drive["id"], "ke-2",
        principal_type="agent", principal_id=GRANTEE,
        resource_type="drive", resource_id=drive["id"], role="editor",
    )
    assert second["id"] != first["id"]
    async with conn() as c:
        feed = await _feed(c, drive["id"])
    swept = [r for r in feed
             if r["type"] == "grant.revoked" and r["data"]["grant_id"] == first["id"]]
    minted = [r for r in feed
              if r["type"] == "grant.created" and r["data"]["grant_id"] == second["id"]]
    assert len(swept) == 1, "the displaced expired grant leaves as grant.revoked"
    assert len(minted) == 1


async def test_share_mutators_emit_events(http, override_actor):
    drive = await _create_drive(http, override_actor, "s", "ks")
    async with conn() as c:
        await _insert_artifact(c, drive["id"], drive["root_folder_id"],
                               "art_0000000000000f01", "a.bin")
    override_actor(make_actor())

    share, etag = await _create_share(
        http, drive["id"], "ks-1",
        resource_type="artifact", resource_id="art_0000000000000f01",
    )
    rotate = await http.post(
        f"/v0/drives/{drive['id']}/shares/{share['id']}/rotate",
        headers={"Idempotency-Key": "ks-2", "If-Match": etag},
    )
    assert rotate.status_code == 200, rotate.text
    revoke = await http.request(
        "DELETE",
        f"/v0/drives/{drive['id']}/shares/{share['id']}",
        headers={"Idempotency-Key": "ks-3", "If-Match": rotate.headers["etag"]},
    )
    assert revoke.status_code == 200, revoke.text

    async with conn() as c:
        feed = await _feed(c, drive["id"])
    for t in ("share.created", "share.revoked", "share.rotated"):
        assert len([r for r in feed if r["type"] == t]) == 1, t
    created = next(r for r in feed if r["type"] == "share.created")
    assert created["resource_type"] == "artifact"
    assert created["resource_id"] == "art_0000000000000f01"
    assert created["data"]["share_id"] == share["id"]


async def test_artifact_version_share_maps_to_parent_artifact(http, override_actor):
    """A version share's change resource is the parent ARTIFACT (the watchable
    resource, and the only kind ``drive_changes.resource_type`` admits); the
    exact version target is preserved in ``data``."""
    drive = await _create_drive(http, override_actor, "sv", "ksv")
    async with conn() as c:
        version_id = await _insert_artifact(
            c, drive["id"], drive["root_folder_id"], "art_0000000000000f02", "v.bin"
        )
    override_actor(make_actor())
    share, _ = await _create_share(
        http, drive["id"], "ksv-1",
        resource_type="artifact_version", resource_id=version_id,
    )
    async with conn() as c:
        created = next(r for r in await _feed(c, drive["id"])
                      if r["type"] == "share.created")
    assert created["resource_type"] == "artifact"          # mapped
    assert created["resource_id"] == "art_0000000000000f02"  # parent artifact
    assert created["data"]["resource_type"] == "artifact_version"  # exact target
    assert created["data"]["resource_id"] == version_id


async def test_grant_event_and_mutation_are_atomic(http, override_actor, monkeypatch):
    """In-transaction atomicity, both directions: if the event append fails,
    the mutation ITSELF must roll back (no grant without its event, and no
    event without its grant). Breaking the shared transaction — e.g. appending
    on a separate connection, or swallowing the append error — makes the grant
    commit anyway, which this test catches."""
    drive = await _create_drive(http, override_actor, "atom", "ka")
    override_actor(make_actor())

    import agentdrive.core.v0_changes as changes_mod

    real_append = changes_mod.append

    async def boom(*args, **kwargs):
        if kwargs.get("type") == "grant.created":
            raise RuntimeError("injected append failure")
        return await real_append(*args, **kwargs)

    monkeypatch.setattr(changes_mod, "append", boom)

    # The injected append failure aborts the request (surfaces as a 5xx / raised
    # app exception); either way the mutation's transaction must roll back.
    with pytest.raises(RuntimeError, match="injected append failure"):
        await http.post(
            f"/v0/drives/{drive['id']}/grants",
            json={"principal_type": "agent", "principal_id": GRANTEE,
                  "resource_type": "drive", "resource_id": drive["id"], "role": "viewer"},
            headers={"Idempotency-Key": "ka-1"},
        )

    monkeypatch.undo()
    async with conn() as c:
        grant_row = await c.fetchval(
            "SELECT 1 FROM grants WHERE drive_id=$1 AND principal_id=$2",
            drive["id"], GRANTEE,
        )
        events = [
            r for r in await _feed(c, drive["id"])
            if r["type"] == "grant.created" and r["data"]["principal_id"] == GRANTEE
        ]
    assert grant_row is None, "the grant must roll back when its event append fails"
    assert events == [], "no orphan event either"


async def test_failed_mutation_emits_no_event(http, override_actor):
    """In-transaction atomicity, negative half: a mutation that fails writes
    NO change row (the append is on the rolled-back transaction)."""
    drive = await _create_drive(http, override_actor, "f", "kf")
    override_actor(make_actor())
    grant, etag = await _create_grant(
        http, drive["id"], "kf-1",
        principal_type="agent", principal_id=GRANTEE,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    async with conn() as c:
        before = len(await _feed(c, drive["id"]))

    # (a) a duplicate live grant → 409, no event.
    dup = await http.post(
        f"/v0/drives/{drive['id']}/grants",
        json={"principal_type": "agent", "principal_id": GRANTEE,
              "resource_type": "drive", "resource_id": drive["id"], "role": "viewer"},
        headers={"Idempotency-Key": "kf-2"},
    )
    assert dup.status_code == 409
    # (b) a stale If-Match update → 412, no event.
    stale = await http.patch(
        f"/v0/drives/{drive['id']}/grants/{grant['id']}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "kf-3", "If-Match": '"rev_deadbeefdeadbeef"'},
    )
    assert stale.status_code == 412

    async with conn() as c:
        after = len(await _feed(c, drive["id"]))
    assert after == before, "a failed mutation must append no change row"


# --------------------------------------------------------------------------- #
# no secret in any share change payload
# --------------------------------------------------------------------------- #


async def test_no_share_secret_in_change_payload(http, override_actor):
    drive = await _create_drive(http, override_actor, "sec", "ksec")
    async with conn() as c:
        await _insert_artifact(c, drive["id"], drive["root_folder_id"],
                               "art_0000000000000f03", "x.bin")
    override_actor(make_actor())
    share, etag = await _create_share(
        http, drive["id"], "ksec-1",
        resource_type="artifact", resource_id="art_0000000000000f03",
    )
    create_secret = share["secret"]
    rotate = await http.post(
        f"/v0/drives/{drive['id']}/shares/{share['id']}/rotate",
        headers={"Idempotency-Key": "ksec-2", "If-Match": etag},
    )
    rotate_secret = rotate.json()["secret"]

    async with conn() as c:
        # secret_hash straight from storage — must not appear anywhere either.
        secret_hash = await c.fetchval(
            "SELECT secret_hash FROM shares WHERE id=$1", share["id"]
        )
        rows = await c.fetch(
            "SELECT type, data::text AS data_text FROM drive_changes "
            "WHERE drive_id=$1 AND type IN ('share.created','share.rotated','share.revoked')",
            drive["id"],
        )
    assert rows, "share events must exist"
    for r in rows:
        blob = r["data_text"]
        data = json.loads(blob)
        assert "secret" not in data
        assert "secret_hash" not in data
        assert create_secret not in blob
        assert rotate_secret not in blob
        assert secret_hash not in blob


# --------------------------------------------------------------------------- #
# THE CRUX: viewer excludes permission events, manager includes them —
# same drive, same cursor semantics, both stable across pagination.
# --------------------------------------------------------------------------- #


async def _seed_interleaved(http, override_actor) -> dict:
    """A drive with content and permission events interleaved, and VIEWER
    holding a drive viewer grant so it can read the feed."""
    drive = await _create_drive(http, override_actor, "mix", "km")
    override_actor(make_actor())
    root = drive["root_folder_id"]
    # viewer grant first (a permission event VIEWER itself must not see)
    await _create_grant(
        http, drive["id"], "km-v",
        principal_type="agent", principal_id=VIEWER,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    await _create_folder(http, drive["id"], root, "a", "km-1")          # content
    g, etag = await _create_grant(                                       # perm
        http, drive["id"], "km-2",
        principal_type="agent", principal_id=GRANTEE,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    await _create_folder(http, drive["id"], root, "b", "km-3")          # content
    async with conn() as c:
        await _insert_artifact(c, drive["id"], root, "art_0000000000000f10", "s.bin")
    await _create_share(                                                # perm
        http, drive["id"], "km-4",
        resource_type="artifact", resource_id="art_0000000000000f10",
    )
    await _create_folder(http, drive["id"], root, "c", "km-5")          # content
    await http.patch(                                                   # perm
        f"/v0/drives/{drive['id']}/grants/{g['id']}",
        json={"role": "editor"},
        headers={"Idempotency-Key": "km-6", "If-Match": etag},
    )
    await _create_folder(http, drive["id"], root, "d", "km-7")          # content
    return drive


def _assert_pagination_healthy(pages, limit):
    """No page exceeds `limit`; every non-final page is EXACTLY `limit` (the
    limit+1/in-SQL property — a filter-after-fetch would produce a short page
    while has_more is still true, the short-page oracle)."""
    for p in pages:
        assert len(p["items"]) <= limit
        if p["has_more"]:
            assert len(p["items"]) == limit, "short page while has_more=true"
    assert pages[-1]["has_more"] is False


async def test_manager_sees_permission_events_paginated(http, override_actor):
    drive = await _seed_interleaved(http, override_actor)
    override_actor(make_actor())  # MANAGER
    items, pages = await _walk(http, drive["id"], limit=2)
    types = [i["type"] for i in items]
    assert any(t in PERMISSION_CHANGE_TYPES for t in types), "manager sees perms"
    assert "grant.created" in types and "share.created" in types
    _assert_pagination_healthy(pages, 2)


async def test_viewer_never_sees_permission_events_paginated(http, override_actor):
    drive = await _seed_interleaved(http, override_actor)

    # Manager's content-only subset (source of truth for what a viewer sees).
    override_actor(make_actor())
    mgr_items, _ = await _walk(http, drive["id"], limit=2)
    mgr_content_ids = [i["id"] for i in mgr_items
                       if i["type"] not in PERMISSION_CHANGE_TYPES]

    # Viewer walks the SAME drive at the SAME limit.
    override_actor(make_actor(subject=VIEWER, scopes={"changes:read"}))
    view_items, view_pages = await _walk(http, drive["id"], limit=2)
    view_types = [i["type"] for i in view_items]

    assert all(t not in PERMISSION_CHANGE_TYPES for t in view_types), (
        "a viewer must never see a permission event"
    )
    # The viewer sees EXACTLY the content subset a manager sees — nothing
    # dropped, nothing extra — proving the SQL filter, not a lossy post-filter.
    assert [i["id"] for i in view_items] == mgr_content_ids
    _assert_pagination_healthy(view_pages, 2)


async def test_viewer_cursor_is_stable_and_replays(http, override_actor):
    """At-least-once holds under the viewer filter: re-presenting a cursor
    re-delivers the same page."""
    drive = await _seed_interleaved(http, override_actor)
    override_actor(make_actor(subject=VIEWER, scopes={"changes:read"}))
    first = await http.get(
        f"/v0/drives/{drive['id']}/changes", params={"start": "beginning", "limit": 2}
    )
    cursor = first.json()["next_cursor"]
    p1 = await http.get(f"/v0/drives/{drive['id']}/changes", params={"cursor": cursor})
    p2 = await http.get(f"/v0/drives/{drive['id']}/changes", params={"cursor": cursor})
    assert [i["id"] for i in p1.json()["items"]] == [i["id"] for i in p2.json()["items"]]


async def test_viewer_tail_of_hidden_permission_events_terminates(http, override_actor):
    """A run of permission events at the END of the feed must not strand the
    viewer on has_more=true forever, nor leak via a trailing short page."""
    drive = await _create_drive(http, override_actor, "tail", "kt")
    override_actor(make_actor())
    root = drive["root_folder_id"]
    await _create_grant(
        http, drive["id"], "kt-v",
        principal_type="agent", principal_id=VIEWER,
        resource_type="drive", resource_id=drive["id"], role="viewer",
    )
    await _create_folder(http, drive["id"], root, "only-content", "kt-1")
    # Several permission events AFTER the last content event.
    for i in range(4):
        await _create_grant(
            http, drive["id"], f"kt-g{i}",
            principal_type="agent", principal_id=f"tcagt_00000000000000{20 + i:02d}",
            resource_type="drive", resource_id=drive["id"], role="viewer",
        )

    override_actor(make_actor(subject=VIEWER, scopes={"changes:read"}))
    items, pages = await _walk(http, drive["id"], limit=1)
    assert all(i["type"] not in PERMISSION_CHANGE_TYPES for i in items)
    _assert_pagination_healthy(pages, 1)


# --------------------------------------------------------------------------- #
# the type filter
# --------------------------------------------------------------------------- #


async def test_type_filter_narrows(http, override_actor):
    drive = await _seed_interleaved(http, override_actor)
    override_actor(make_actor())  # MANAGER

    only_folders = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "beginning", "type": "folder.created"},
    )
    assert only_folders.status_code == 200
    assert {i["type"] for i in only_folders.json()["items"]} == {"folder.created"}

    grants_only = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "beginning", "type": "grant.created,grant.updated"},
    )
    assert grants_only.status_code == 200
    assert {i["type"] for i in grants_only.json()["items"]} <= {
        "grant.created", "grant.updated"
    }
    assert grants_only.json()["items"], "manager sees grant events"


async def test_type_filter_permission_type_is_empty_for_viewer(http, override_actor):
    """A viewer requesting a permission type gets an empty page — the visibility
    filter still applies, so the type param is no oracle."""
    drive = await _seed_interleaved(http, override_actor)
    override_actor(make_actor(subject=VIEWER, scopes={"changes:read"}))
    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "beginning", "type": "grant.created,share.created"},
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["has_more"] is False


async def test_type_filter_rejects_unknown(http, override_actor):
    drive = await _create_drive(http, override_actor, "u", "ku")
    override_actor(make_actor())
    bad = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "beginning", "type": "grant.exploded"},
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_ARGUMENT"

    empty = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "beginning", "type": ""},
    )
    assert empty.status_code == 400


async def test_unknown_query_param_still_rejected(http, override_actor):
    """§6.3: the new `type` param goes through known_params, so OTHER unknown
    params are still rejected."""
    drive = await _create_drive(http, override_actor, "uq", "kuq")
    override_actor(make_actor())
    resp = await http.get(
        f"/v0/drives/{drive['id']}/changes",
        params={"start": "beginning", "bogus": "1"},
    )
    assert resp.status_code == 400


async def test_viewer_never_sees_permission_events_walking_backward(http, override_actor):
    """The manager-only filter is a WHERE predicate, so it must hold in both
    directions — and the `limit + 1` probe must still keep pages honest when
    the hidden rows are encountered from the other end.

    A filter that leaked would show up here in a way the forward test cannot
    see: descending starts at the head, where `_seed_interleaved` leaves its
    permission events, so the very first page is the one most likely to go
    short or strand `has_more`."""
    drive = await _seed_interleaved(http, override_actor)

    # The manager's content-only subset, newest-first, is the source of truth.
    override_actor(make_actor())
    mgr_items, _ = await _walk(http, drive["id"], limit=2, order="newest")
    mgr_content_ids = [i["id"] for i in mgr_items
                       if i["type"] not in PERMISSION_CHANGE_TYPES]

    override_actor(make_actor(subject=VIEWER, scopes={"changes:read"}))
    view_items, view_pages = await _walk(http, drive["id"], limit=2, order="newest")

    assert all(i["type"] not in PERMISSION_CHANGE_TYPES for i in view_items), (
        "a viewer must never see a permission event, in either direction"
    )
    assert [i["id"] for i in view_items] == mgr_content_ids
    _assert_pagination_healthy(view_pages, 2)


async def test_backward_walk_is_the_reverse_of_the_forward_one_for_a_viewer(http, override_actor):
    """Whatever the filter hides, it hides identically in both directions."""
    drive = await _seed_interleaved(http, override_actor)
    override_actor(make_actor(subject=VIEWER, scopes={"changes:read"}))

    forward, _ = await _walk(http, drive["id"], limit=2)
    backward, _ = await _walk(http, drive["id"], limit=2, order="newest")

    assert [i["id"] for i in backward] == [i["id"] for i in reversed(forward)]
