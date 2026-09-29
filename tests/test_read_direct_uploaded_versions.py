"""Reading versions that live OUTSIDE the artifact CAS bucket.

A B3 direct upload publishes a version whose bytes live in the DEDICATED
transfer bucket at a pinned generation (design §8/§9): the row records
``storage_bucket`` and ``storage_generation``, and copy/folder-copy/restore
propagate them. The download-capability mint (packet 4) honors those
coordinates; every OTHER read surface fetched from the artifact bucket by
assumption and therefore answered 404/500 for any direct-uploaded version —
which the local end-to-end run surfaced the first time a browser upload was
actually rendered.

These tests seed a version row shaped exactly like a B3 completion (bytes
present in a second emulator bucket, generation pinned) and pin the reads:

  * the machine API's head + version content routes;
  * the private viewer's ``/view/doc`` and ``/view/content``;
  * the share resolver behind the public surface;
  * and the closed-set rule: a bucket outside {artifact bucket, configured
    transfer bucket} is never fetched — uniform 404, mirroring the mint's
    foreign-bucket refusal.

Synthetic fixtures only. The emulator bucket is created per test run.
"""

from __future__ import annotations

import secrets

import httpx
import pytest
import pytest_asyncio

from agentdrive.api.v0_deps import v0_actor
from agentdrive.app import app
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.actor import V0ActorContext

pytestmark = [
    pytest.mark.asyncio,
    # These write transfer-bucket objects straight into the GCS emulator and
    # read them back through the recorded coordinates; the surface is GCS-only.
    pytest.mark.skipif(settings.storage_backend != "gcs", reason="direct transfer is GCS-only"),
]

AGENT = "tcagt_0000000000000001"
WS_A = "tcws_0000000000000001"

TRANSFER_BUCKET = "direct-read-demo-transfer"
DIRECT_BODY = b"# direct upload\n\nbytes from the transfer bucket\n"


def _unique_version() -> str:
    """A fresh synthetic version id per test.

    The emulator bucket is SHARED across xdist workers (only the database
    is per-PID), and fake-GCS has versioning off — so a fixed object key
    lets one worker's insert delete the generation another worker pinned.
    Unique per test keeps the pins independent."""
    return f"ver_{secrets.token_hex(8)}"


def _unique_object(prefix: str = "immutable/") -> str:
    return f"{prefix}upld_{secrets.token_hex(8)}-{secrets.token_hex(8)}"


def make_actor(subject: str = AGENT) -> V0ActorContext:
    return V0ActorContext(
        subject=subject,
        subject_type="agent",
        workspace_id=WS_A,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({
            "drives:read", "drives:write",
            "content:read", "content:write",
            "sharing:read", "sharing:write",
        }),
        credential_id="tccred_0000000000000001",
        runtime_id="tcrun_0000000000000001",
        sponsor_id="tcusr_0000000000000009",
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
        await c.execute(
            "TRUNCATE idempotency_records, drives RESTART IDENTITY CASCADE"
        )


@pytest.fixture(autouse=True)
def _bound_viewer(monkeypatch):
    """The viewer-session mint below fails closed with 503 VIEWER_DISABLED
    while `viewer_base_url` is empty; bind a synthetic host for it."""
    monkeypatch.setattr(settings, "viewer_base_url", "https://viewer.example.test")


async def _create_drive(http, name: str, key: str) -> dict:
    resp = await http.post(
        "/v0/drives", json={"name": name}, headers={"Idempotency-Key": key}
    )
    assert resp.status_code == 201, resp.text
    drive = resp.json()
    detail = await http.get(f"/v0/drives/{drive['id']}")
    assert detail.status_code == 200
    return detail.json()


def _multipart(parent_id: str, name: str, content_type: str, body: bytes) -> bytes:
    return (
        b"--b\r\n"
        b'Content-Disposition: form-data; name="parent_id"\r\n\r\n'
        + parent_id.encode()
        + b"\r\n--b\r\n"
        b'Content-Disposition: form-data; name="name"\r\n\r\n'
        + name.encode()
        + b"\r\n--b\r\n"
        b'Content-Disposition: form-data; name="content_type"\r\n\r\n'
        + content_type.encode()
        + b"\r\n--b\r\n"
        b'Content-Disposition: form-data; name="content"; filename="f"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        + body
        + b"\r\n--b--\r\n"
    )


async def _create_artifact(http, drive: dict, *, name: str, key: str) -> dict:
    resp = await http.post(
        f"/v0/drives/{drive['id']}/artifacts",
        content=_multipart(drive["root_folder_id"], name, "text/markdown", b"seed"),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": key,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _put_emulator_object(bucket: str, object_name: str, body: bytes) -> int:
    """Create `bucket` (409 tolerated) and insert the object; return its
    generation — the same coordinates a B3 completion records."""
    assert settings.gcs_emulator_host, "these tests require the GCS emulator"
    async with httpx.AsyncClient(base_url=settings.gcs_emulator_host) as client:
        created = await client.post(
            "/storage/v1/b?project=test", json={"name": bucket}
        )
        assert created.status_code in (200, 409), created.text
        inserted = await client.post(
            f"/upload/storage/v1/b/{bucket}/o",
            params={"uploadType": "media", "name": object_name},
            content=body,
            headers={"Content-Type": "text/markdown"},
        )
        assert inserted.status_code == 200, inserted.text
        return int(inserted.json()["generation"])


@pytest_asyncio.fixture
async def direct_version(http, override_actor, monkeypatch):
    """A drive whose artifact HEAD is a direct-uploaded-shaped version:
    bytes in the configured transfer bucket, generation pinned."""
    monkeypatch.setattr(settings, "direct_transfer_bucket", TRANSFER_BUCKET)
    monkeypatch.setattr(settings, "direct_transfer_immutable_prefix", "immutable/")
    override_actor(make_actor())
    drive = await _create_drive(http, "direct", "k-drive")
    artifact = await _create_artifact(http, drive, name="direct.md", key="k-art")

    direct_version_id = _unique_version()
    object_name = _unique_object()
    generation = await _put_emulator_object(TRANSFER_BUCKET, object_name, DIRECT_BODY)

    async with conn() as c:
        await c.execute(
            "INSERT INTO artifact_versions "
            "(id, artifact_id, checksum, content_type, size_bytes, "
            " storage_object, storage_bucket, storage_generation, "
            " actor_type, actor_id, ordinal) "
            "VALUES ($1, $2, 'sha256:d1', 'text/markdown', $3, "
            "        $4, $5, $6, 'agent', $7, 2)",
            direct_version_id, artifact["id"], len(DIRECT_BODY),
            object_name, TRANSFER_BUCKET, generation, AGENT,
        )
        await c.execute(
            "UPDATE artifacts SET head_version_id = $1 WHERE id = $2",
            direct_version_id, artifact["id"],
        )
    return {
        "drive": drive,
        "artifact": artifact,
        "version_id": direct_version_id,
        "object_name": object_name,
        "generation": generation,
    }


# ── the machine API ──────────────────────────────────────────────────────────


async def test_v0_head_content_reads_the_recorded_bucket(direct_version, http):
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/content"
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == DIRECT_BODY


async def test_v0_version_content_reads_the_recorded_bucket(direct_version, http):
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}"
        f"/versions/{direct_version['version_id']}/content"
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == DIRECT_BODY


# ── the private viewer ───────────────────────────────────────────────────────


async def test_viewer_doc_and_content_read_the_recorded_bucket(
    direct_version, http
):
    minted = await http.post(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/viewer-sessions",
        json={},
        headers={"Idempotency-Key": "k-mint-direct"},
    )
    assert minted.status_code == 200, minted.text
    auth = {"Authorization": f"Bearer {minted.json()['credential']}"}

    doc = await http.get("/view/doc", headers=auth)
    assert doc.status_code == 200, doc.text
    assert "bytes from the transfer bucket" in doc.json()["html"]

    content = await http.get("/view/content", headers=auth)
    assert content.status_code == 200
    assert content.content == DIRECT_BODY


# ── the share resolver behind the public surface ─────────────────────────────


async def test_share_renders_the_recorded_bucket(direct_version, http):
    share = await http.post(
        f"/v0/drives/{direct_version['drive']['id']}/shares",
        json={
            "resource_type": "artifact",
            "resource_id": direct_version["artifact"]["id"],
        },
        headers={"Idempotency-Key": "k-share-direct"},
    )
    assert share.status_code == 201, share.text
    secret = share.json()["secret"]

    page = await http.get(f"/s/{secret}/content")
    assert page.status_code == 200, page.text
    assert page.content == DIRECT_BODY


# ── copying a historical direct-uploaded version ─────────────────────────────


async def test_copy_of_a_historical_direct_version_reads_the_recorded_bucket(
    direct_version, http
):
    """The copy path derives a preview from a NON-head version's bytes. That
    read is a byte read like any other: for a direct-uploaded source it must
    use the recorded bucket, not 500 inside the mutation."""
    # Append a new head so the direct-uploaded version is historical.
    detail = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}"
    )
    assert detail.status_code == 200, detail.text
    resp = await http.post(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/versions",
        content=_multipart(
            direct_version["drive"]["root_folder_id"],
            "direct.md",
            "text/markdown",
            b"new head",
        ),
        headers={
            "Content-Type": "multipart/form-data; boundary=b",
            "Idempotency-Key": "k-head",
            "If-Match": detail.headers["etag"],
        },
    )
    assert resp.status_code in (201, 200), resp.text

    copied = await http.post(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/copy",
        json={
            "destination_parent_id": direct_version["drive"]["root_folder_id"],
            "destination_name": "copy-of-direct.md",
            "version_id": direct_version["version_id"],
        },
        headers={"Idempotency-Key": "k-copy"},
    )
    assert copied.status_code == 201, copied.text
    # The preview came from the transfer-bucket bytes, not from a 500.
    assert "bytes from the transfer bucket" in (
        copied.json().get("content_preview") or ""
    )


# ── the closed-set refusal ───────────────────────────────────────────────────


async def test_scratch_objects_are_never_readable(direct_version, http):
    """Scratch is browser-written. A version row pointing at it — corruption
    or a hostile write — must never be fetched, even though the bucket
    itself is the configured, legitimate one."""
    scratch_object = _unique_object("scratch/")
    generation = await _put_emulator_object(
        TRANSFER_BUCKET, scratch_object, b"SCRATCH BYTES"
    )
    scratch_version = _unique_version()
    async with conn() as c:
        await c.execute(
            "INSERT INTO artifact_versions "
            "(id, artifact_id, checksum, content_type, size_bytes, "
            " storage_object, storage_bucket, storage_generation, "
            " actor_type, actor_id, ordinal) "
            "VALUES ($1, $2, 'sha256:d3', 'text/markdown', 13, "
            "        $3, $4, $5, 'agent', $6, 4)",
            scratch_version, direct_version["artifact"]["id"],
            scratch_object, TRANSFER_BUCKET, generation, AGENT,
        )
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}"
        f"/versions/{scratch_version}/content"
    )
    assert resp.status_code == 404, resp.text
    assert b"SCRATCH" not in resp.content


async def test_traversal_and_dot_segments_are_never_readable(direct_version, http):
    """`immutable/../scratch/x` and friends decode to a different object on
    some backends. The persisted name must be a real namespace member."""
    for ordinal, hostile in enumerate(
        (
            "immutable/../scratch/escaped",
            "immutable//empty-segment",
            "immutable/./dot",
            "immutable/",
        ),
        start=10,
    ):
        version_id = _unique_version()
        async with conn() as c:
            await c.execute(
                "INSERT INTO artifact_versions "
                "(id, artifact_id, checksum, content_type, size_bytes, "
                " storage_object, storage_bucket, storage_generation, "
                " actor_type, actor_id, ordinal) "
                "VALUES ($1, $2, 'sha256:d4', 'text/markdown', 2, "
                "        $3, $4, 7, 'agent', $5, $6)",
                version_id, direct_version["artifact"]["id"],
                hostile, TRANSFER_BUCKET, AGENT, ordinal,
            )
        resp = await http.get(
            f"/v0/drives/{direct_version['drive']['id']}"
            f"/artifacts/{direct_version['artifact']['id']}"
            f"/versions/{version_id}/content"
        )
        assert resp.status_code == 404, (hostile, resp.text)


async def test_the_recorded_generation_is_actually_sent(direct_version):
    """The generation is what makes a transfer-bucket read immutable: the
    object NAME is server-chosen but reusable, so the read must name the
    exact generation rather than "latest".

    Asserted at the storage boundary because the emulator has versioning
    off — an overwrite there DESTROYS the superseded generation, so the
    pin cannot be demonstrated by overwriting. What is observable, and
    what the passthrough actually promises, is that a read carrying the
    wrong generation does not quietly succeed against the same name."""
    from agentdrive import storage

    pinned = await storage.get(
        direct_version["object_name"],
        bucket=TRANSFER_BUCKET,
        generation=direct_version["generation"],
    )
    assert pinned == DIRECT_BODY

    # The contract's own exception, not the provider's: callers never learn
    # which backend answered.
    with pytest.raises(storage.NotFound):
        await storage.get(
            direct_version["object_name"],
            bucket=TRANSFER_BUCKET,
            generation=direct_version["generation"] + 1,
        )


async def test_an_unset_immutable_prefix_makes_no_transfer_read_legitimate(
    direct_version, http, monkeypatch
):
    """Rollback/partial config: with the prefix unset, nothing in the
    transfer bucket is readable — the rule fails closed rather than opening
    the whole bucket, including scratch."""
    monkeypatch.setattr(settings, "direct_transfer_immutable_prefix", "")
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/content"
    )
    assert resp.status_code == 404, resp.text



async def test_foreign_bucket_reads_fail_closed(direct_version, http):
    """A recorded bucket outside {artifact bucket, configured transfer
    bucket} is never fetched — the uniform 404, mirroring the download
    mint's foreign-bucket refusal, and never a 500."""
    foreign_version = _unique_version()
    async with conn() as c:
        await c.execute(
            "INSERT INTO artifact_versions "
            "(id, artifact_id, checksum, content_type, size_bytes, "
            " storage_object, storage_bucket, storage_generation, "
            " actor_type, actor_id, ordinal) "
            "VALUES ($1, $2, 'sha256:d2', 'text/markdown', 2, "
            "        'immutable/foreign', 'foreign-bucket-demo', 5, "
            "        'agent', $3, 3)",
            foreign_version, direct_version["artifact"]["id"], AGENT,
        )
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}"
        f"/versions/{foreign_version}/content"
    )
    assert resp.status_code == 404, resp.text


# ── the redirect reach: large versions must not stream through the box ───────
#
# A direct-uploaded version may be up to `DIRECT_TRANSFER_MAX_BYTES` (1 GiB
# in production) and used to stream at ANY size, because the CAS-only
# redirect shortcut refused to sign a transfer-bucket object. Streaming one
# holds an anyio worker thread for as long as the reader takes, and
# production runs `api_max_instances = 1` — so a few slow readers were
# enough to starve `/health` on the only instance. The machine surface now
# hands those readers the same validated, generation-pinned signed GET the
# download-capability mint issues.
#
# These pin the INTEGRATION (which signer is reached, with what, and what
# happens when it declines). The signer's own V4 validation is pinned by
# tests/test_v0_download_capabilities.py and is deliberately not restated.


SIGNED_TARGET = "https://storage.example.test/signed-target?X-Goog-Signature=ab"


@pytest.fixture
def signer_calls(monkeypatch):
    """Stand a stub capability signer in front of the byte routes and lower
    the redirect threshold under the fixture body. Returns the call ledger;
    append an exception to `raises` to make the next mint decline."""
    from datetime import UTC, datetime

    from agentdrive.api import v0_download_capabilities as dl_api
    from agentdrive.storage_transfers import SignedCapability

    monkeypatch.setattr(settings, "download_signed_min_bytes", 8)
    monkeypatch.setattr(settings, "direct_download_capability_ttl_seconds", 300)
    calls: list[dict] = []
    raises: list[BaseException] = []

    class _Stub:
        async def sign_capability(self, **kwargs):
            calls.append(kwargs)
            if raises:
                raise raises[0]
            return SignedCapability(
                url=SIGNED_TARGET,
                disposition='attachment; filename="f"',
                expires_at=datetime.now(UTC),
            )

    monkeypatch.setattr(dl_api, "capability_signer", lambda: _Stub())
    return {"calls": calls, "raises": raises}


async def test_v0_content_redirects_a_large_transfer_bucket_version(
    direct_version, http, signer_calls
):
    """The regression this change exists for: over the threshold, a
    transfer-bucket version is a 307 to a signed target, not a stream."""
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/content",
        follow_redirects=False,
    )
    assert resp.status_code == 307, resp.text
    assert resp.headers["Location"] == SIGNED_TARGET
    # An empty body, and a Content-Length that says so. The framing rule
    # that matters is that a 307 must not claim the OBJECT's length while
    # sending none of it — that is what hangs keep-alive clients.
    assert resp.content == b""
    assert resp.headers["Content-Length"] == "0"
    # The 307 still carries the head version's ETag, so a conditional
    # re-read is answered by this route rather than by the object store.
    assert resp.headers["ETag"] == f'"{direct_version["version_id"]}"'


async def test_v0_content_signs_the_recorded_coordinates_and_media_type(
    direct_version, http, signer_calls
):
    """The mint is asked for THIS bucket at THIS generation — and for the
    version's own media type, not the mint route's forced octet-stream:
    machine clients get the same content type on the 307 that they get on
    the stream, and `sign_capability` forces `attachment` regardless."""
    await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/content",
        follow_redirects=False,
    )
    assert signer_calls["calls"] == [{
        "bucket": TRANSFER_BUCKET,
        "object_name": direct_version["object_name"],
        "generation": direct_version["generation"],
        "media_type": "text/markdown",
        "filename": "direct.md",
        "ttl_seconds": 300,
    }]


async def test_v0_version_content_redirects_too(
    direct_version, http, signer_calls
):
    """The versions router shares `_bytes_response`, so it must share the
    reach — a historical direct-uploaded version is the likeliest large one."""
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}"
        f"/versions/{direct_version['version_id']}/content",
        follow_redirects=False,
    )
    assert resp.status_code == 307, resp.text
    assert resp.headers["Location"] == SIGNED_TARGET


async def test_v0_content_streams_when_the_signer_declines(
    direct_version, http, signer_calls
):
    """A signer that fails closed is not a failed read. The bytes are still
    there and the stream path still serves them — never a 503, which would
    turn a signing outage into a content outage."""
    from agentdrive.storage_transfers import DownloadSigningUnavailableError

    signer_calls["raises"].append(
        DownloadSigningUnavailableError("signer_unavailable")
    )
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/content",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == DIRECT_BODY


async def test_v0_content_streams_an_unresolved_legacy_version(
    direct_version, http, signer_calls
):
    """A row the reconcile job has not resolved yet has no triple to sign.

    `schema.sql` CHECKs `(storage_bucket IS NULL) = (storage_generation IS
    NULL)`, so "unresolved" is always BOTH null — a legacy CAS row, never a
    transfer-bucket row missing only its generation. It keeps streaming
    rather than 404ing, and the capability signer is never even asked;
    signing it is `reconcile-generations`' job, not this route's.
    """
    legacy_version = _unique_version()
    legacy_object = _unique_object(prefix="cas/")
    await _put_emulator_object(settings.gcs_bucket, legacy_object, DIRECT_BODY)
    async with conn() as c:
        await c.execute(
            "INSERT INTO artifact_versions "
            "(id, artifact_id, checksum, content_type, size_bytes, "
            " storage_object, storage_bucket, storage_generation, "
            " actor_type, actor_id, ordinal) "
            "VALUES ($1, $2, 'sha256:d3', 'text/markdown', $3, "
            "        $4, NULL, NULL, 'agent', $5, 4)",
            legacy_version, direct_version["artifact"]["id"],
            len(DIRECT_BODY), legacy_object, AGENT,
        )
        await c.execute(
            "UPDATE artifacts SET head_version_id = $1 WHERE id = $2",
            legacy_version, direct_version["artifact"]["id"],
        )
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/content",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == DIRECT_BODY
    assert signer_calls["calls"] == []


async def test_v0_content_under_the_threshold_still_streams(
    direct_version, http, signer_calls, monkeypatch
):
    """The threshold still decides. Small reads stay a single round trip —
    a redirect would cost a second one for no availability gain."""
    monkeypatch.setattr(
        settings, "download_signed_min_bytes", len(DIRECT_BODY) + 1
    )
    resp = await http.get(
        f"/v0/drives/{direct_version['drive']['id']}"
        f"/artifacts/{direct_version['artifact']['id']}/content",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == DIRECT_BODY
    assert signer_calls["calls"] == []
