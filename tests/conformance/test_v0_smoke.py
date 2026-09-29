"""Task 10 Step 2 — in-process E2E smoke over the canonical v0 flow (§11.4).

A composed in-process app (the same `app_with_lifespan` + actor-override
pattern as the unit suites) exercising the full happy path a real client runs:
auth → create drive → folder → artifact (multipart) → version append → share →
redemption → changes cursor → search. This is the release-proof smoke: if every
vertical composes in one unbroken flow against the mounted routers, the cutover
composition works.

The legacy `tests/e2e/` harness is a black-box PROD test (hits agentdrive.run,
needs e2a keys). This one is deliberately in-process: no network, no keys, runs
with the normal suite — the "release proof" before anything deploys.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentdrive.api.v0_deps import v0_actor
from agentdrive.identity.actor import V0ActorContext

pytestmark = pytest.mark.asyncio

AGENT = "tcagt_0000000000000001"
SPONSOR = "tcusr_0000000000000009"
WS_A = "tcws_0000000000000001"


def _actor() -> V0ActorContext:
    return V0ActorContext(
        subject=AGENT,
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({
            "drives:read", "drives:write", "usage:read",
            "content:read", "content:write", "sharing:read", "sharing:write",
            "changes:read",
        }),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id=SPONSOR,
    )


@pytest_asyncio.fixture
async def smoke(app_with_lifespan):
    """An authenticated in-process client over the mounted app."""
    app_with_lifespan.dependency_overrides[v0_actor] = _actor
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app_with_lifespan.dependency_overrides.clear()


async def test_v0_canonical_flow_smoke(smoke):
    """Auth → drive → folder → multipart artifact → version → share →
    redemption → changes → search, end to end against the mounted routers."""
    key = 0

    def nk() -> str:
        nonlocal key
        key += 1
        return f"smoke-{key}"

    # 1. Create a drive (returns its root folder).
    r = await smoke.post("/v0/drives", json={"name": "smoke"}, headers={"Idempotency-Key": nk()})
    assert r.status_code == 201, r.text
    drive = r.json()
    assert drive["id"].startswith("drv_")
    root = drive["root_folder_id"]
    assert root.startswith("fld_")

    # 2. Create a folder under the root.
    r = await smoke.post(
        f"/v0/drives/{drive['id']}/folders",
        json={"parent_id": root, "name": "docs"},
        headers={"Idempotency-Key": nk()},
    )
    assert r.status_code == 201, r.text
    folder = r.json()
    assert folder["name"] == "docs"

    # 3. Upload an artifact (multipart) into that folder.
    r = await smoke.post(
        f"/v0/drives/{drive['id']}/artifacts",
        files={
            "parent_id": (None, folder["id"]),
            "name": (None, "report.md"),
            "content": ("report.md", b"# Smoke\n\nneedle content", "text/markdown"),
        },
        headers={"Idempotency-Key": nk()},
    )
    assert r.status_code == 201, r.text
    art = r.json()
    assert art["id"].startswith("art_")
    assert art["name"] == "report.md"
    head = art["head_version_id"]
    assert head.startswith("ver_")

    # 4. Append a version (immutable trail).
    r = await smoke.post(
        f"/v0/drives/{drive['id']}/artifacts/{art['id']}/versions",
        files={
            "content": ("report.md", b"# Smoke v2\n\nupdated needle", "text/markdown"),
        },
        headers={"Idempotency-Key": nk(), "If-Match": f'"{art["revision"]}"'},
    )
    assert r.status_code == 201, r.text
    version = r.json()
    assert version["version_number"] >= 2
    assert version["id"].startswith("ver_")

    # 5. Read the artifact content back (streams the head bytes).
    r = await smoke.get(f"/v0/drives/{drive['id']}/artifacts/{art['id']}/content")
    assert r.status_code == 200, r.text
    assert b"updated needle" in r.content

    # 6. Mint a share link; the secret is returned once.
    r = await smoke.post(
        f"/v0/drives/{drive['id']}/shares",
        json={"resource_type": "artifact", "resource_id": art["id"]},
        headers={"Idempotency-Key": nk()},
    )
    assert r.status_code == 201, r.text
    share = r.json()
    assert share["id"].startswith("shr_")
    secret = share["secret"]
    assert secret

    # 7. Redeem the share link with NO auth — possession alone.
    r = await smoke.get(f"/s/{secret}", headers={"Accept": "application/json"})
    assert r.status_code == 200, r.text
    assert r.content == b"# Smoke v2\n\nupdated needle"

    # 8. Change feed: capture now, then pull from the beginning.
    r = await smoke.get(f"/v0/drives/{drive['id']}/changes", params={"start": "now"})
    assert r.status_code == 200, r.text
    now_cursor = r.json()["next_cursor"]
    assert now_cursor

    r = await smoke.get(f"/v0/drives/{drive['id']}/changes", params={"start": "beginning"})
    assert r.status_code == 200, r.text
    feed = r.json()
    assert feed["items"], "the feed should contain the drive's mutations"
    types = {c["type"] for c in feed["items"]}
    assert "drive.updated" in types  # drive create
    assert "folder.created" in types  # folder create
    assert "artifact.created" in types  # artifact create

    # 9. Search the drive (lexical, grant-filtered).
    r = await smoke.get(
        f"/v0/drives/{drive['id']}/search", params={"q": "needle"}
    )
    assert r.status_code == 200, r.text
    hits = r.json()["items"]
    assert any(h["name"] == "report.md" for h in hits)

    # 10. Sanity: the list endpoints compose too.
    r = await smoke.get(f"/v0/drives/{drive['id']}/artifacts")
    assert r.status_code == 200, r.text
    assert {i["name"] for i in r.json()["items"]} == {"report.md"}
