"""B3 packet 2 — the GCS XML transfer storage adapter contract.

Governing spec: TokenCanopy 2026-08-14 direct-transfer design §5.6/§6/§7.
These tests drive the runtime adapter entirely through a fake HTTP transport
(httpx.MockTransport) — no emulator, no network, synthetic values only.

The load-bearing claims (2026-08-20 amendment: the adapter SIGNS the XML
initiation — the client performs the actual GCS POST from its own context,
which is what earns the session browser CORS):
  * V4-signed XML initiation targets only (never the JSON API for the
    browser transport), signed over the EXACT header set
    content-type;host;x-goog-meta-adoption-marker;x-goog-resumable, with
    a minutes-scale TTL; signing makes NO provider request.
  * No `X-Upload-Content-Length`: the declared size is an AgentDrive
    contract enforced by reservation + completion stat, not a provider
    header claim — and the disclosed header set is EXACTLY the three
    signed names, nothing more.
  * The caller never supplies an origin, bucket, or endpoint — those come
    only from trusted deployment configuration.
  * The signed initiation URL is validated FAIL-CLOSED before return,
    returned once to the caller, and appears in no log record or
    exception representation.
  * Guarded rewrite: exact source generation + destination
    `ifGenerationMatch=0`; delete is generation-pinned.
  * Signed downloads are generation-pinned, validated before return, and
    FAIL CLOSED (typed error, never a soft None fallback).
"""

import base64
import dataclasses
import logging

import httpx
import pytest

from agentdrive import storage
from agentdrive.content_disposition import build_content_disposition

_BUCKET = "transfer-bucket-demo"
_ENDPOINT = "https://storage.example.test"
_ORIGIN = "https://console.example.test"
_SCRATCH = "transfer-scratch/drv_demo/upld_demo"
_FINAL = "transfer-immutable/drv_demo/ver_demo"
# The V4-signed-header set every valid initiation URL must be signed over.
_INITIATION_HEADERS = "content-type;host;x-goog-meta-adoption-marker;x-goog-resumable"


def _make_adapter(handler, **overrides):
    from agentdrive import storage_transfers as st

    kwargs = {
        "bucket": _BUCKET,
        "upload_endpoint": _ENDPOINT,
        "download_endpoint": _ENDPOINT,
        "canonical_origin": _ORIGIN,
        "scratch_prefix": "transfer-scratch/",
        "immutable_prefix": "transfer-immutable/",
        "client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        # Pin the validator's "now" just after the fixture signing date so
        # the freshness window is deterministic.
        "clock": lambda: _FIXED_NOW,
    }
    kwargs.update(overrides)
    return st.XmlTransferStorage(**kwargs)


def _recording(status=201, headers=None, json_body=None):
    recorded = []

    def handler(request):
        recorded.append(request)
        if json_body is not None:
            return httpx.Response(status, headers=headers or {}, json=json_body)
        return httpx.Response(status, headers=headers or {})

    return recorded, handler


def _initiate_request():
    from agentdrive import storage_transfers as st

    return st.XmlResumableRequest(
        object_name=_SCRATCH,
        content_type="text/plain",
        adoption_marker="marker-demo-0001",
    )


# ---------------------------------------------------------------------------
# V4-signed XML initiation (2026-08-20 amendment)
# ---------------------------------------------------------------------------


def _initiation_query(ttl_value=600, **overrides):
    """A complete, valid V4 initiation query. No semantic parameters — the
    initiation's semantics (method, resumable intent, content type, marker)
    ride in the SIGNED HEADERS. Overrides with a value of None DELETE the
    key; a `_dupe_<key>` entry appends a duplicate."""
    from urllib.parse import urlencode

    params = {
        "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
        "X-Goog-Credential": "signer-demo@example.test/20260815/auto/storage/goog4_request",
        "X-Goog-Date": "20260815T120000Z",
        "X-Goog-Expires": str(ttl_value),
        "X-Goog-SignedHeaders": _INITIATION_HEADERS,
        "X-Goog-Signature": "ab" * 256,
    }
    extras = []
    for key, value in overrides.items():
        if key.startswith("_dupe_"):
            extras.append((key.removeprefix("_dupe_"), value))
        elif value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return urlencode(list(params.items()) + extras)


def _fake_initiation_signer(**query_overrides):
    """An initiation signer whose output is a COMPLETE valid V4 URL bound
    to the requested TTL — the adapter's validator must accept exactly
    this (and refuse every mutation the tests below apply)."""
    calls = []

    def signer(bucket, object_name, content_type, adoption_marker, ttl_seconds):
        from urllib.parse import quote

        calls.append(
            {
                "bucket": bucket,
                "object_name": object_name,
                "content_type": content_type,
                "adoption_marker": adoption_marker,
                "ttl_seconds": ttl_seconds,
            }
        )
        query = _initiation_query(ttl_seconds, **query_overrides)
        return f"{_ENDPOINT}/{bucket}/{quote(object_name, safe='/')}?{query}"

    return calls, signer


async def test_signing_disclosed_bundle_and_no_provider_request():
    """Signing mints the validated bundle from configuration + the request,
    touches NO provider, and derives expires_at from the URL's own signed
    instant — never a server-clock guess."""
    from agentdrive import storage_transfers as st

    recorded, handler = _recording(500)  # any outbound request would 500 loudly
    calls, signer = _fake_initiation_signer()
    adapter = _make_adapter(handler, initiation_signer=signer)
    signed = await adapter.sign_resumable_initiation(_initiate_request())

    assert recorded == []  # no provider request — signing is computation
    assert calls == [
        {
            "bucket": _BUCKET,
            "object_name": _SCRATCH,
            "content_type": "text/plain",
            "adoption_marker": "marker-demo-0001",
            "ttl_seconds": st.INITIATION_TTL_SECONDS,
        }
    ]
    from urllib.parse import urlsplit

    parts = urlsplit(signed.url)
    assert parts.scheme == "https"
    assert parts.netloc == "storage.example.test"
    assert parts.path == f"/{_BUCKET}/{_SCRATCH}"
    # The disclosed header set is EXACTLY the signed one: the three names,
    # nothing more (in particular never X-Upload-Content-Length — §7).
    assert signed.required_headers == {
        "x-goog-resumable": "start",
        "Content-Type": "text/plain",
        "x-goog-meta-adoption-marker": "marker-demo-0001",
    }
    assert signed.expires_at == _datetime(2026, 8, 15, 12, 10, 0, tzinfo=_UTC)


async def test_initiation_request_carries_no_origin_bucket_or_endpoint_field():
    """The caller cannot steer the origin, bucket, or endpoint: the request
    dataclass simply has no such field."""
    from agentdrive import storage_transfers as st

    field_names = {f.name for f in dataclasses.fields(st.XmlResumableRequest)}
    assert field_names == {"object_name", "content_type", "adoption_marker"}


async def test_adapter_requires_an_https_gcs_upload_endpoint():
    from agentdrive import storage_transfers as st

    _, handler = _recording()
    for bad in (
        "http://storage.example.test",  # plaintext, non-loopback
        "https://storage.example.test/upload",  # path
        "https://storage.example.test?x=1",  # query
        "https://user:pw@storage.example.test",  # credentials
        "",  # unset
    ):
        with pytest.raises(ValueError):
            _make_adapter(handler, upload_endpoint=bad)
    # loopback http stays possible for the local emulator
    st_adapter = _make_adapter(handler, upload_endpoint="http://localhost:4443")
    assert st_adapter is not None
    del st


@pytest.mark.parametrize(
    "bad_name",
    [
        "other-prefix/drv_demo/upld_demo",  # outside the scratch namespace
        "transfer-scratch/../cas/x",  # traversal
        "/transfer-scratch/drv_demo/x",  # absolute-looking
        "transfer-scratch/a?b",  # query injection
        "transfer-scratch/a#b",  # fragment injection
        "transfer-scratch/a\nb",  # control characters
        "",
    ],
)
async def test_signing_rejects_object_names_outside_the_scratch_namespace(bad_name):
    from agentdrive import storage_transfers as st

    calls, signer = _fake_initiation_signer()
    _, handler = _recording()
    adapter = _make_adapter(handler, initiation_signer=signer)
    with pytest.raises(ValueError):
        await adapter.sign_resumable_initiation(
            st.XmlResumableRequest(
                object_name=bad_name,
                content_type="text/plain",
                adoption_marker="marker-demo-0001",
            )
        )
    assert calls == []  # rejected before the signer ran


async def test_signing_requires_the_exact_signed_header_set():
    """MUTATION TARGET (design amendment §9): a URL signed over any other
    header set is a credential with different powers than the contract
    disclosed — replayable as a plain PUT (no x-goog-resumable), or with
    forgeable metadata (no marker) — and must be refused."""
    from agentdrive import storage_transfers as st

    for signed_headers in (
        "host",
        "content-type;host",
        "content-type;host;x-goog-resumable",  # marker not signed
        "content-type;host;x-goog-meta-adoption-marker",  # resumable not signed
        _INITIATION_HEADERS + ";x-extra",  # broader than disclosed
        "content-type;host;x-goog-meta-other;x-goog-resumable",
    ):
        _, signer = _fake_initiation_signer(**{"X-Goog-SignedHeaders": signed_headers})
        _, handler = _recording()
        adapter = _make_adapter(handler, initiation_signer=signer)
        with pytest.raises(st.InitiationSigningUnavailableError):
            await adapter.sign_resumable_initiation(_initiate_request())


@pytest.mark.parametrize(
    "overrides",
    [
        {"X-Goog-Algorithm": "GOOG4-HMAC-SHA256"},  # wrong algorithm
        {"X-Goog-Signature": None},  # missing field
        {"X-Goog-Credential": "garbage"},  # unparseable scope
        {"X-Goog-Credential": "s@example.test/20260816/auto/storage/goog4_request"},
        {"X-Goog-Signature": "zz" * 256},  # non-hex signature
        {"X-Goog-Signature": "abc"},  # stub-sized signature
        {"X-Goog-Expires": "59"},  # expiry != the requested TTL
        {"X-Goog-Expires": "999999999999999"},  # absurd digit string
        {"X-Goog-Date": "20200101T000000Z"},  # decades stale
        {"X-Goog-Date": "20270101T000000Z"},  # future-dated
        {"X-Goog-Date": "20260815T996099Z"},  # regex-valid non-instant
        {"_dupe_X-Goog-Signature": "ab" * 256},  # duplicated key
        {"generation": "7"},  # a semantic key initiation never carries
        {"upload_id": "x"},  # the SESSION URI's key, never the initiation's
    ],
)
async def test_signing_validates_the_closed_v4_grammar(overrides):
    from agentdrive import storage_transfers as st

    _, signer = _fake_initiation_signer(**overrides)
    _, handler = _recording()
    adapter = _make_adapter(handler, initiation_signer=signer)
    with pytest.raises(st.InitiationSigningUnavailableError):
        await adapter.sign_resumable_initiation(_initiate_request())


@pytest.mark.parametrize(
    "bad_url",
    [
        # wrong host / plaintext / userinfo / alternate port
        "https://evil.example.test/transfer-bucket-demo/"
        "transfer-scratch/drv_demo/upld_demo?{q}",
        "http://storage.example.test/transfer-bucket-demo/"
        "transfer-scratch/drv_demo/upld_demo?{q}",
        "https://user@storage.example.test/transfer-bucket-demo/"
        "transfer-scratch/drv_demo/upld_demo?{q}",
        "https://storage.example.test:8443/transfer-bucket-demo/"
        "transfer-scratch/drv_demo/upld_demo?{q}",
        # wrong bucket / wrong object / encoded-slash ambiguity / fragment
        "https://storage.example.test/other-bucket/"
        "transfer-scratch/drv_demo/upld_demo?{q}",
        "https://storage.example.test/transfer-bucket-demo/"
        "transfer-scratch/drv_demo/other?{q}",
        "https://storage.example.test/transfer-bucket-demo/"
        "transfer-scratch%2Fdrv_demo%2Fupld_demo?{q}",
        "https://storage.example.test/transfer-bucket-demo/"
        "transfer-scratch/drv_demo/upld_demo?{q}#frag",
    ],
)
async def test_signing_rejects_urls_off_the_configured_target(bad_url):
    from agentdrive import storage_transfers as st

    def signer(bucket, object_name, content_type, adoption_marker, ttl_seconds):
        return bad_url.format(q=_initiation_query(ttl_seconds))

    _, handler = _recording()
    adapter = _make_adapter(handler, initiation_signer=signer)
    with pytest.raises(st.InitiationSigningUnavailableError):
        await adapter.sign_resumable_initiation(_initiate_request())


async def test_signing_rejects_raw_controls_and_unbounded_urls():
    from agentdrive import storage_transfers as st

    for bad in (
        f"{_ENDPOINT}/{_BUCKET}/{_SCRATCH}\n?{_initiation_query()}",
        f"{_ENDPOINT}/{_BUCKET}/{_SCRATCH}?{_initiation_query()}" + "&x=" + "a" * 9000,
    ):
        def signer(bucket, object_name, content_type, adoption_marker, ttl_seconds, _bad=bad):
            return _bad

        _, handler = _recording()
        adapter = _make_adapter(handler, initiation_signer=signer)
        with pytest.raises(st.InitiationSigningUnavailableError):
            await adapter.sign_resumable_initiation(_initiate_request())


async def test_signing_failure_is_typed_severed_and_redacted():
    """A signer failure may interpolate a credential host; it must surface
    as the typed error with an EMPTY exception chain, and — because signing
    mints no provider state — it is always safe for the caller to retry."""
    from agentdrive import storage_transfers as st

    def broken_signer(*args, **kwargs):
        raise RuntimeError("no identity for https://iam.example.test/leak?tok=demo")

    recorded, handler = _recording()
    adapter = _make_adapter(handler, initiation_signer=broken_signer)
    with pytest.raises(st.InitiationSigningUnavailableError) as excinfo:
        await adapter.sign_resumable_initiation(_initiate_request())
    rendered = f"{excinfo.value!s} {excinfo.value!r}"
    assert "https://" not in rendered
    assert "tok=demo" not in rendered
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert recorded == []


async def test_signing_without_a_signer_fails_closed():
    from agentdrive import storage_transfers as st

    _, handler = _recording()
    adapter = _make_adapter(handler)  # no initiation_signer
    with pytest.raises(st.InitiationSigningUnavailableError):
        await adapter.sign_resumable_initiation(_initiate_request())


async def test_run_initiation_signer_times_out_a_hanging_signer():
    """A hung IAM/signBlob worker maps to the typed refusal, quickly; its
    late result is discarded (a short real sleep, so the worker thread
    finishes before interpreter shutdown)."""
    import time

    from agentdrive import storage_transfers as st

    def hanging(*args):
        time.sleep(1.0)
        return "late-and-ignored"

    start = time.monotonic()
    with pytest.raises(st.InitiationSigningUnavailableError) as excinfo:
        await st._run_initiation_signer(
            hanging,
            bucket=_BUCKET,
            object_name=_SCRATCH,
            content_type="text/plain",
            adoption_marker="marker-demo-0001",
            ttl_seconds=600,
            timeout_seconds=0.05,
        )
    assert excinfo.value.classification == "signing_timeout"
    assert time.monotonic() - start < 0.9  # refused at the deadline


async def test_emulator_initiation_signer_output_validates(monkeypatch):
    """The local-emulator signer's shape-only URL must satisfy the SAME
    fail-closed validator production output does — against the loopback
    endpoint the emulator configuration names."""
    from agentdrive import storage_transfers as st
    from agentdrive.config import settings

    endpoint = "http://localhost:4443"
    monkeypatch.setattr(settings, "direct_transfer_upload_endpoint", endpoint)
    url = st._emulator_initiation_signer(
        _BUCKET, _SCRATCH, "text/plain", "marker-demo-0001", 600
    )
    st.validate_signed_initiation_url(
        url,
        upload_endpoint=endpoint,
        bucket=_BUCKET,
        object_name=_SCRATCH,
        ttl_seconds=600,
        now=_datetime.now(_UTC),
    )


def test_production_initiation_signer_uses_the_library_resumable_form(monkeypatch):
    """The production-only seam must ask google-cloud-storage for its
    RESUMABLE form. That documented sentinel is what makes the library sign
    a POST with ``x-goog-resumable: start``; an ordinary POST/PUT would keep
    the query shape plausible while producing the wrong credential."""
    from datetime import timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock

    from agentdrive import storage_transfers as st
    from agentdrive.config import settings

    generate_signed_url = Mock(return_value="https://signed.example.test/target")
    blob = SimpleNamespace(generate_signed_url=generate_signed_url)
    bucket = Mock()
    bucket.blob.return_value = blob
    client = Mock()
    client.bucket.return_value = bucket
    credentials = SimpleNamespace(
        service_account_email="signer@synthetic-demo.iam.gserviceaccount.com",
        token="synthetic-access-token",
    )

    monkeypatch.setattr(settings, "gcs_emulator_host", None)
    monkeypatch.setattr(settings, "direct_transfer_upload_endpoint", _ENDPOINT)
    from agentdrive.storage import gcs as gcs_storage

    monkeypatch.setattr(gcs_storage, "_get_signing_creds", lambda: credentials)
    monkeypatch.setattr(gcs_storage, "_client_singleton", lambda: client)

    result = st._production_initiation_signer(
        _BUCKET,
        _SCRATCH,
        "text/plain",
        "marker-demo-0001",
        600,
    )

    assert result == "https://signed.example.test/target"
    client.bucket.assert_called_once_with(_BUCKET)
    bucket.blob.assert_called_once_with(_SCRATCH)
    generate_signed_url.assert_called_once_with(
        version="v4",
        expiration=timedelta(seconds=600),
        method="RESUMABLE",
        content_type="text/plain",
        headers={"x-goog-meta-adoption-marker": "marker-demo-0001"},
        api_access_endpoint=_ENDPOINT,
        service_account_email="signer@synthetic-demo.iam.gserviceaccount.com",
        access_token="synthetic-access-token",
    )


# ---------------------------------------------------------------------------
# Provider transport policy (rereview blockers: redirects, timeouts,
# credential failures, status classification)
# ---------------------------------------------------------------------------


def _method_calls(adapter):
    """One thunk per provider method routed through the shared seam."""
    from agentdrive import storage_transfers as st

    return {
        "stat": lambda: adapter.stat_generation(_FINAL, None),
        "rewrite": lambda: adapter.rewrite_generation_create_only(
            st.ObjectGeneration(object_name=_SCRATCH, generation=41),
            _FINAL,
            content_type="text/plain",
            adoption_marker="m",
            source_fingerprint="fp",
        ),
        "delete": lambda: adapter.delete_generation(_SCRATCH, 41),
    }


@pytest.mark.parametrize("method", ["stat", "rewrite", "delete"])
async def test_provider_requests_never_follow_redirects(method):
    """Independent/adversarial rereview blocker: redirect policy must be
    ADAPTER-owned. A caller-injected client with `follow_redirects=True`
    must still make exactly one request — a 307 toward another origin is a
    typed provider failure, never a followed hop carrying the trusted
    Origin, metadata, or credential."""
    from agentdrive import storage_transfers as st

    recorded = []

    def handler(request):
        recorded.append(request)
        if request.url.host == "storage.example.test":
            return httpx.Response(
                307, headers={"Location": "https://evil.example.test/capture"}
            )
        return httpx.Response(200)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    adapter = _make_adapter(handler, client=client)
    with pytest.raises(st.TransferProviderUnavailableError):
        await _method_calls(adapter)[method]()
    assert len(recorded) == 1
    assert all(req.url.host == "storage.example.test" for req in recorded)


@pytest.mark.parametrize("method", ["stat", "rewrite", "delete"])
async def test_provider_requests_carry_a_bounded_adapter_owned_timeout(method):
    """A client constructed with `timeout=None` must not let a provider
    call hang a future begin/completion lease indefinitely: the adapter
    passes its own finite timeout on every request."""
    recorded, base_handler = _recording(200)

    def handler(request):
        recorded.append(request)
        return httpx.Response(200, json={"done": False, "rewriteToken": "t"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    adapter = _make_adapter(handler, client=client)
    import contextlib as _ctx

    with _ctx.suppress(Exception):
        await _method_calls(adapter)[method]()
    assert recorded, "no request reached the transport"
    timeout = recorded[-1].extensions.get("timeout")
    assert timeout is not None
    for phase, value in timeout.items():
        assert value is not None and value > 0, f"unbounded {phase} timeout"
    del base_handler


async def test_token_provider_that_hangs_is_bounded_and_typed(monkeypatch):
    """Rereview finding A: the adapter-owned timeout must cover the
    CREDENTIAL leg too — a token provider that hangs (rather than raises)
    would otherwise hold a future begin/completion lease indefinitely,
    exactly the stuck-lease class the redirect/timeout blocker targeted.
    The real ceiling is 30s; shortened here so the test proves the bound
    without sleeping."""
    import asyncio

    from agentdrive import storage_transfers as st

    monkeypatch.setattr(st, "PROVIDER_TIMEOUT_SECONDS", 0.05)

    async def token_provider():
        await asyncio.sleep(3600)

    recorded, handler = _recording(200)
    adapter = _make_adapter(handler, token_provider=token_provider)
    with pytest.raises(st.TransferProviderUnavailableError):
        await asyncio.wait_for(
            adapter.stat_generation(_SCRATCH, None), timeout=5
        )
    assert recorded == []


async def test_token_provider_failures_are_typed_severed_and_redacted():
    """Rereview blocker: a credential-provider exception must not escape
    untyped — its message may interpolate a token endpoint or secret."""
    from agentdrive import storage_transfers as st

    async def token_provider():
        raise RuntimeError(
            "credential leak https://sts.example.test/token?secret=tok-demo-secret"
        )

    recorded, handler = _recording(200)
    adapter = _make_adapter(handler, token_provider=token_provider)
    with pytest.raises(st.TransferProviderUnavailableError) as excinfo:
        await adapter.stat_generation(_SCRATCH, None)
    rendered = f"{excinfo.value!s} {excinfo.value!r}"
    assert "https://" not in rendered
    assert "tok-demo-secret" not in rendered
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert recorded == []  # no outbound request without a credential decision


# ---------------------------------------------------------------------------
# Strict provider observations (rereview blocker: contradictory or
# malformed identity must never become a valid observation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"x-goog-generation": "0"},  # non-positive
        {"x-goog-generation": "-5"},
        {"x-goog-stored-content-length": "-9"},  # negative size
        {"x-goog-hash": "crc32c=not-canonical,md5=x"},  # malformed CRC
        {"x-goog-hash": "crc32c=yZRlqg,md5=x"},  # unpadded CRC
    ],
)
async def test_stat_rejects_malformed_identity_as_typed_errors(overrides):
    from agentdrive import storage_transfers as st

    _, handler = _recording(200, _stat_headers(**overrides))
    with pytest.raises(st.TransferProviderUnavailableError):
        await _make_adapter(handler).stat_generation(_FINAL, None)


async def test_pinned_stat_must_return_the_requested_generation():
    """A generation-pinned stat answering with a DIFFERENT generation is
    provider corruption — ambiguous, never a usable observation."""
    from agentdrive import storage_transfers as st

    _, handler = _recording(200, _stat_headers(**{"x-goog-generation": "43"}))
    with pytest.raises(st.TransferProviderUnavailableError):
        await _make_adapter(handler).stat_generation(_FINAL, 42)


@pytest.mark.parametrize("bad_name", ["cas/drv_demo/deadbeef", "obj/drv_demo/x", ""])
async def test_stat_and_delete_are_confined_to_the_transfer_namespaces(bad_name):
    """The adapter is scoped to the configured scratch/immutable
    namespaces; a foreign key must be refused before any provider call."""
    recorded, handler = _recording(200, _stat_headers())
    adapter = _make_adapter(handler)
    with pytest.raises(ValueError):
        await adapter.stat_generation(bad_name, None)
    with pytest.raises(ValueError):
        await adapter.delete_generation(bad_name, 1)
    assert recorded == []


@pytest.mark.parametrize(
    "body",
    [
        {"done": "yes", "resource": {"generation": "1"}},  # non-boolean done
        {"done": True, "resource": {"generation": "-1"}},  # non-positive
        {"done": True, "resource": {"generation": "0"}},
        {"done": True, "resource": {"generation": "²"}},  # unicode digit ²
        {"done": True, "resource": {"generation": "½"}},  # fraction ½
        {"done": True, "resource": {"generation": 1.5}},  # float
        {"done": False, "rewriteToken": None},  # null token
        {"done": False, "rewriteToken": ""},  # empty token
        {"done": False, "rewriteToken": 7},  # non-string token
        {"done": False, "rewriteToken": "x" * 5000},  # unbounded token
        ["not", "an", "object"],  # top-level list
    ],
)
async def test_rewrite_rejects_unprovable_response_shapes(body):
    from agentdrive import storage_transfers as st

    _, handler = _recording(200, json_body=body)
    adapter = _make_adapter(handler)
    with pytest.raises(st.TransferProviderUnavailableError):
        await adapter.rewrite_generation_create_only(
            st.ObjectGeneration(object_name=_SCRATCH, generation=41),
            _FINAL,
            content_type="text/plain",
            adoption_marker="m",
            source_fingerprint="fp",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("continuation", ""),
        ("continuation", "tok\r\nen"),
        ("continuation", "x" * 5000),
        ("adoption_marker", "m\r\nX: 1"),
        ("source_fingerprint", "fp\x00"),
        ("content_type", ""),
    ],
)
async def test_rewrite_validates_outgoing_identity_and_continuation(field, value):
    from agentdrive import storage_transfers as st

    recorded, handler = _recording(200, json_body={"done": True, "resource": {"generation": "1"}})
    adapter = _make_adapter(handler)
    kwargs = {
        "content_type": "text/plain",
        "adoption_marker": "m",
        "source_fingerprint": "fp",
        "continuation": None,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        await adapter.rewrite_generation_create_only(
            st.ObjectGeneration(object_name=_SCRATCH, generation=41), _FINAL, **kwargs
        )
    assert recorded == []


async def test_rewrite_requires_a_positive_source_generation():
    from agentdrive import storage_transfers as st

    recorded, handler = _recording(200, json_body={"done": True, "resource": {"generation": "1"}})
    adapter = _make_adapter(handler)
    with pytest.raises(ValueError):
        await adapter.rewrite_generation_create_only(
            st.ObjectGeneration(object_name=_SCRATCH, generation=0),
            _FINAL,
            content_type="text/plain",
            adoption_marker="m",
            source_fingerprint="fp",
        )
    assert recorded == []


# ---------------------------------------------------------------------------
# Constructor enforces the exact-origin contract itself (shared validator)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_origin",
    [
        "*",
        "https://*.example.test",
        "https://console.example.test/app",
        "https://console.example.test?x=1",
        "https://user:pw@console.example.test",
        "http://console.example.test",  # plaintext, non-loopback
        "",
    ],
)
async def test_adapter_constructor_rejects_non_exact_canonical_origins(bad_origin):
    _, handler = _recording()
    with pytest.raises(ValueError):
        _make_adapter(handler, canonical_origin=bad_origin)


async def test_adapter_constructor_allows_loopback_origin_for_dev():
    _, handler = _recording()
    assert _make_adapter(handler, canonical_origin="http://localhost:3000") is not None


@pytest.mark.parametrize(
    ("scratch", "immutable"),
    [
        ("transfer-scratch/", "transfer-scratch/adopted/"),  # nested
        ("transfer-scratch/x/", "transfer-scratch/"),
        ("transfer-scratch/", "transfer-scratch/"),  # identical
    ],
)
async def test_adapter_constructor_requires_disjoint_prefixes(scratch, immutable):
    _, handler = _recording()
    with pytest.raises(ValueError):
        _make_adapter(handler, scratch_prefix=scratch, immutable_prefix=immutable)


@pytest.mark.parametrize("bad_bucket", ["", "UPPER-Bucket", "has space", "-leading"])
async def test_adapter_constructor_validates_the_bucket_name(bad_bucket):
    _, handler = _recording()
    with pytest.raises(ValueError):
        _make_adapter(handler, bucket=bad_bucket)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


async def test_provider_errors_redact_urls_objects_and_causes():
    from agentdrive import storage_transfers as st

    def handler(request):
        raise httpx.ConnectError(
            "connection refused for https://storage.example.test/leak?upload_id=zz"
        )

    adapter = _make_adapter(handler)
    with pytest.raises(st.TransferProviderUnavailableError) as excinfo:
        await adapter.stat_generation(_SCRATCH, None)
    rendered = f"{excinfo.value!s} {excinfo.value!r}"
    assert "https://" not in rendered
    assert "storage.example.test" not in rendered
    assert _SCRATCH not in rendered
    assert "upload_id" not in rendered
    # the httpx cause (whose message interpolates the URL) is severed so a
    # traceback formatter cannot resurrect it into a log line
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


async def test_nothing_is_logged_during_signing(caplog):
    """No log record may carry the signed initiation URL, the endpoint
    host, the object key, or the signature — §8 redaction applies to the
    signing path exactly as it did to the old initiation path."""
    caplog.set_level(logging.DEBUG)
    _, signer = _fake_initiation_signer()
    _, handler = _recording()
    adapter = _make_adapter(handler, initiation_signer=signer)
    signed = await adapter.sign_resumable_initiation(_initiate_request())
    for record in caplog.records:
        message = record.getMessage()
        assert "X-Goog-Signature" not in message
        assert signed.url not in message
        assert "storage.example.test" not in message
        assert _SCRATCH not in message


async def test_redaction_survives_concurrent_provider_calls(caplog):
    """Adversarial review finding 1: a per-call level-toggle restores the
    httpx logger's verbose level while a SECOND call is still in flight,
    letting `HTTP Request: <full URL>` leak the bucket + object key. The
    redaction control must be concurrency-safe — overlapping transfer calls
    with the httpx logger at INFO may still never record a coordinate."""
    import asyncio

    caplog.set_level(logging.DEBUG)
    httpx_logger = logging.getLogger("httpx")
    prior_level = httpx_logger.level
    httpx_logger.setLevel(logging.INFO)
    try:

        async def handler(request):
            await asyncio.sleep(0.05)
            return httpx.Response(200, headers=_stat_headers())

        adapter = _make_adapter(handler)

        async def staggered(delay):
            await asyncio.sleep(delay)
            return await adapter.stat_generation(_SCRATCH, None)

        await asyncio.gather(staggered(0), staggered(0.02))
    finally:
        httpx_logger.setLevel(prior_level)
    for record in caplog.records:
        message = record.getMessage()
        assert "storage.example.test" not in message
        assert _SCRATCH not in message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("adoption_marker", "ok\r\nX-Injected: 1"),
        ("adoption_marker", "ok\nX-Injected: 1"),
        ("adoption_marker", "ok\x00"),
        ("content_type", "text/plain\r\nX-Injected: 1"),
    ],
)
async def test_signing_rejects_header_breaking_metadata_values(field, value):
    """Adversarial review finding 2, carried to the signing path: adoption
    marker and content type become signed header VALUES the client will
    send raw; a CRLF must be rejected by the ADAPTER before any signer
    runs."""
    from agentdrive import storage_transfers as st

    calls, signer = _fake_initiation_signer()
    _, handler = _recording()
    adapter = _make_adapter(handler, initiation_signer=signer)
    fields = {
        "object_name": _SCRATCH,
        "content_type": "text/plain",
        "adoption_marker": "marker-demo-0001",
    }
    fields[field] = value
    with pytest.raises(ValueError):
        await adapter.sign_resumable_initiation(st.XmlResumableRequest(**fields))
    assert calls == []  # rejected before the signer ran


async def test_stat_with_malformed_numeric_headers_is_typed_unavailable():
    """Adversarial review finding 4: a malformed x-goog-generation or
    length header must surface as a typed provider error, not an untyped
    ValueError escaping the metadata path."""
    from agentdrive import storage_transfers as st

    _, handler = _recording(200, _stat_headers(**{"x-goog-generation": "not-a-number"}))
    with pytest.raises(st.TransferProviderUnavailableError):
        await _make_adapter(handler).stat_generation(_FINAL, None)


async def test_provider_error_str_carries_only_safe_classification():
    from agentdrive import storage_transfers as st

    _, handler = _recording(429)
    adapter = _make_adapter(handler)
    with pytest.raises(st.TransferProviderUnavailableError) as excinfo:
        await adapter.stat_generation(_SCRATCH, None)
    # classification + status only — grep-able, never coordinate-bearing
    assert excinfo.value.status == 429
    assert excinfo.value.classification


# ---------------------------------------------------------------------------
# Stat
# ---------------------------------------------------------------------------


def _stat_headers(**overrides):
    headers = {
        "x-goog-generation": "42",
        "x-goog-stored-content-length": "11",
        "x-goog-hash": "crc32c=yZRlqg==,md5=ignored-demo==",
        "content-type": "text/plain",
        "x-goog-meta-adoption-marker": "marker-demo-0001",
        "x-goog-meta-source-fingerprint": (
            "src=transfer-scratch/drv_demo/upld_demo@41;upld=upld_demo"
        ),
    }
    headers.update(overrides)
    return {k: v for k, v in headers.items() if v is not None}


async def test_stat_returns_a_full_observation():
    recorded, handler = _recording(200, _stat_headers())
    adapter = _make_adapter(handler)
    obs = await adapter.stat_generation(_FINAL, 42)
    req = recorded[0]
    assert req.method == "HEAD"
    assert req.url.path == f"/{_BUCKET}/{_FINAL}"
    assert req.url.params.get("generation") == "42"
    assert obs.object_name == _FINAL
    assert obs.generation == 42
    assert obs.size == 11
    assert obs.crc32c == "yZRlqg=="
    assert obs.content_type == "text/plain"
    assert obs.adoption_marker == "marker-demo-0001"
    assert obs.source_fingerprint == (
        "src=transfer-scratch/drv_demo/upld_demo@41;upld=upld_demo"
    )


async def test_stat_absent_object_is_none_and_missing_fields_stay_none():
    _, handler404 = _recording(404)
    assert await _make_adapter(handler404).stat_generation(_FINAL, None) is None

    # a response missing hash/marker yields None fields, never fabricated
    # values — the caller treats unobservable identity as AMBIGUOUS
    _, handler = _recording(
        200,
        _stat_headers(
            **{
                "x-goog-hash": None,
                "x-goog-meta-adoption-marker": None,
                "x-goog-meta-source-fingerprint": None,
            }
        ),
    )
    obs = await _make_adapter(handler).stat_generation(_FINAL, None)
    assert obs.crc32c is None
    assert obs.adoption_marker is None
    assert obs.source_fingerprint is None


async def test_stat_object_satisfies_the_packet_1_transfer_storage_protocol():
    from agentdrive.core import v0_uploads

    _, handler = _recording(200, _stat_headers())
    adapter = _make_adapter(handler)
    obs = await adapter.stat_object(_FINAL)
    assert isinstance(obs, v0_uploads.ObjectObservation)
    assert callable(adapter.delete_generation)


# ---------------------------------------------------------------------------
# Guarded rewrite
# ---------------------------------------------------------------------------


async def test_rewrite_pins_source_generation_and_destination_create_only():
    from agentdrive import storage_transfers as st

    recorded, handler = _recording(
        200,
        json_body={
            "done": True,
            "resource": {"generation": "77"},
        },
    )
    adapter = _make_adapter(handler)
    result = await adapter.rewrite_generation_create_only(
        st.ObjectGeneration(object_name=_SCRATCH, generation=41),
        _FINAL,
        content_type="text/plain",
        adoption_marker="marker-demo-0001",
        source_fingerprint="src=transfer-scratch/drv_demo/upld_demo@41;upld=upld_demo",
    )
    assert result.done is True
    assert result.generation == 77
    assert result.continuation is None

    req = recorded[0]
    assert req.method == "POST"
    assert req.url.params.get("sourceGeneration") == "41"
    assert req.url.params.get("ifGenerationMatch") == "0"
    body = req.read().decode()
    assert "marker-demo-0001" in body
    assert "source-fingerprint" in body


async def test_rewrite_surfaces_a_continuation_instead_of_looping():
    """§6: the continuation token is durable server-only recovery state —
    the CALLER commits it between calls; the adapter must not hide a
    multi-call rewrite behind an internal loop."""
    from agentdrive import storage_transfers as st

    recorded, handler = _recording(
        200, json_body={"done": False, "rewriteToken": "continue-demo"}
    )
    adapter = _make_adapter(handler)
    result = await adapter.rewrite_generation_create_only(
        st.ObjectGeneration(object_name=_SCRATCH, generation=41),
        _FINAL,
        content_type="text/plain",
        adoption_marker="marker-demo-0001",
        source_fingerprint="fp-demo",
    )
    assert result.done is False
    assert result.generation is None
    assert result.continuation == "continue-demo"
    assert len(recorded) == 1

    # the resumed call carries the persisted token back
    recorded2, handler2 = _recording(
        200, json_body={"done": True, "resource": {"generation": "78"}}
    )
    adapter2 = _make_adapter(handler2)
    await adapter2.rewrite_generation_create_only(
        st.ObjectGeneration(object_name=_SCRATCH, generation=41),
        _FINAL,
        content_type="text/plain",
        adoption_marker="marker-demo-0001",
        source_fingerprint="fp-demo",
        continuation="continue-demo",
    )
    assert recorded2[0].url.params.get("rewriteToken") == "continue-demo"


async def test_rewrite_precondition_failure_is_typed_not_adopted():
    from agentdrive import storage_transfers as st

    _, handler = _recording(412)
    adapter = _make_adapter(handler)
    with pytest.raises(st.TransferPreconditionFailedError):
        await adapter.rewrite_generation_create_only(
            st.ObjectGeneration(object_name=_SCRATCH, generation=41),
            _FINAL,
            content_type="text/plain",
            adoption_marker="m",
            source_fingerprint="fp",
        )


async def test_rewrite_with_an_unprovable_body_is_ambiguous_not_done():
    """§6: a 200 whose body cannot prove the outcome (no done/generation)
    is AMBIGUOUS — surfaced as a typed unavailable, never adopted."""
    from agentdrive import storage_transfers as st

    for body in ({}, {"done": True}, {"done": True, "resource": {}},
                 {"done": True, "resource": {"generation": "not-a-number"}}):
        _, handler = _recording(200, json_body=body)
        adapter = _make_adapter(handler)
        with pytest.raises(st.TransferProviderUnavailableError):
            await adapter.rewrite_generation_create_only(
                st.ObjectGeneration(object_name=_SCRATCH, generation=41),
                _FINAL,
                content_type="text/plain",
                adoption_marker="m",
                source_fingerprint="fp",
            )


async def test_token_provider_bearer_is_attached_to_provider_calls():
    from agentdrive import storage_transfers as st

    async def token_provider():
        return "server-token-demo"

    recorded, handler = _recording(200, _stat_headers())
    adapter = _make_adapter(handler, token_provider=token_provider)
    await adapter.stat_generation(_SCRATCH, None)
    assert recorded[0].headers["authorization"] == "Bearer server-token-demo"
    del st


async def test_rewrite_destination_must_be_in_the_immutable_namespace():
    from agentdrive import storage_transfers as st

    _, handler = _recording(200, json_body={"done": True, "resource": {"generation": "1"}})
    adapter = _make_adapter(handler)
    with pytest.raises(ValueError):
        await adapter.rewrite_generation_create_only(
            st.ObjectGeneration(object_name=_SCRATCH, generation=41),
            "cas/drv_demo/deadbeef",  # not the transfer immutable namespace
            content_type="text/plain",
            adoption_marker="m",
            source_fingerprint="fp",
        )


# ---------------------------------------------------------------------------
# Generation-pinned delete
# ---------------------------------------------------------------------------


async def test_delete_is_generation_pinned_and_idempotent():
    from agentdrive import storage_transfers as st

    recorded, handler = _recording(204)
    adapter = _make_adapter(handler)
    await adapter.delete_generation(_SCRATCH, 41)
    req = recorded[0]
    assert req.method == "DELETE"
    assert req.headers["x-goog-if-generation-match"] == "41"
    assert req.url.params.get("generation") == "41"

    _, handler404 = _recording(404)
    await _make_adapter(handler404).delete_generation(_SCRATCH, 41)  # already gone

    _, handler412 = _recording(412)
    with pytest.raises(st.TransferPreconditionFailedError):
        await _make_adapter(handler412).delete_generation(_SCRATCH, 41)


# ---------------------------------------------------------------------------
# Strict CRC32C canonicalization
# ---------------------------------------------------------------------------


def test_canonical_crc32c_accepts_only_the_gcs_metadata_form():
    from agentdrive import storage_transfers as st

    assert st.canonical_crc32c("yZRlqg==") == "yZRlqg=="
    # round-trip identity for a generated value
    digest = base64.b64encode(b"\x01\x02\x03\x04").decode("ascii")
    assert st.canonical_crc32c(digest) == digest


@pytest.mark.parametrize(
    "bad",
    [
        "yZRlqg",  # unpadded
        "yZRlqg=",  # bad padding
        "yZRlqv==",  # non-canonical trailing bits
        "yZ_lqg==",  # URL-safe alphabet
        "AAAA",  # 3 decoded bytes
        "AAAAAAAA",  # 6 decoded bytes
        "yZRlqg==\n",  # trailing noise
        " yZRlqg==",  # leading noise
        "",
        "not base64!",
    ],
)
def test_canonical_crc32c_rejects_every_non_canonical_encoding(bad):
    from agentdrive import storage_transfers as st

    with pytest.raises(st.InvalidChecksumError):
        st.canonical_crc32c(bad)


# ---------------------------------------------------------------------------
# Generation-pinned V4 signed GET
# ---------------------------------------------------------------------------


# The pinned "current instant" the download-signing fixtures validate
# against: 30s after the fixture X-Goog-Date, inside the skew window.
from datetime import UTC as _UTC  # noqa: E402
from datetime import datetime as _datetime  # noqa: E402

_FIXED_NOW = _datetime(2026, 8, 15, 12, 0, 30, tzinfo=_UTC)


def _v4_query(gen_value, ttl_value, type_value, disposition_value, **overrides):
    """A complete, valid V4 query for hand-built fixtures. Overrides with a
    value of None DELETE the key; a `_dupe_<key>` entry appends a duplicate.
    (Positional names avoid clashing with same-named query-key overrides.)"""
    from urllib.parse import urlencode

    params = {
        "generation": str(gen_value),
        "response-content-type": type_value,
        "response-content-disposition": disposition_value,
        "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
        "X-Goog-Credential": "signer-demo@example.test/20260815/auto/storage/goog4_request",
        "X-Goog-Date": "20260815T120000Z",
        "X-Goog-Expires": str(ttl_value),
        "X-Goog-SignedHeaders": "host",
        # A structurally valid provider signature: 512 hex chars (the
        # 2048-bit RSA size signBlob produces).
        "X-Goog-Signature": "ab" * 256,
    }
    extras = []
    for key, value in overrides.items():
        if key.startswith("_dupe_"):
            extras.append((key.removeprefix("_dupe_"), value))
        elif value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return urlencode(list(params.items()) + extras)


def _fake_signer():
    """A signer whose output is a COMPLETE valid V4 URL bound to the
    requested TTL — the adapter's validator must accept exactly this."""
    calls = []

    def signer(bucket, object_name, generation, ttl_seconds, response_type, disposition):
        from urllib.parse import quote

        calls.append(
            {
                "bucket": bucket,
                "object_name": object_name,
                "generation": generation,
                "ttl_seconds": ttl_seconds,
                "response_type": response_type,
                "disposition": disposition,
            }
        )
        query = _v4_query(generation, ttl_seconds, response_type, disposition)
        return f"{_ENDPOINT}/{bucket}/{quote(object_name, safe='/')}?{query}"

    return calls, signer


async def test_signed_download_pins_generation_type_and_disposition():
    calls, signer = _fake_signer()
    _, handler = _recording()
    adapter = _make_adapter(handler, url_signer=signer)
    from agentdrive import storage_transfers as st

    url = await adapter.sign_generation_download(
        st.SignedDownloadRequest(
            object_name=_FINAL,
            generation=77,
            media_type="application/octet-stream",
            filename="notes.txt",
            ttl_seconds=300,
        )
    )
    assert calls[0]["generation"] == 77
    assert calls[0]["response_type"] == "application/octet-stream"
    assert calls[0]["disposition"].startswith("attachment")
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.hostname == "storage.example.test"
    query = parse_qs(parts.query)
    assert query["generation"] == ["77"]
    assert query["response-content-type"] == ["application/octet-stream"]
    assert query["response-content-disposition"][0].startswith("attachment")


async def test_signed_download_fails_closed_never_soft():
    """The legacy artifact signer returned None and let a proxy stream take
    over; the transfer signer has no fallback (503 at the wire, packet 4)."""
    from agentdrive import storage_transfers as st

    def broken_signer(*args, **kwargs):
        raise RuntimeError("no signing identity for https://storage.example.test/leak")

    _, handler = _recording()
    adapter = _make_adapter(handler, url_signer=broken_signer)
    with pytest.raises(st.DownloadSigningUnavailableError) as excinfo:
        await adapter.sign_generation_download(
            st.SignedDownloadRequest(
                object_name=_FINAL,
                generation=77,
                media_type="application/octet-stream",
                filename="notes.txt",
                ttl_seconds=300,
            )
        )
    rendered = f"{excinfo.value!s} {excinfo.value!r}"
    assert "https://" not in rendered
    assert excinfo.value.__cause__ is None


async def test_signed_download_validates_the_signer_output():
    """§8: AgentDrive validates its own storage-helper output before
    returning it — a wrong-host, plaintext, or unpinned URL is refused."""
    from agentdrive import storage_transfers as st

    bad_urls = [
        "http://storage.example.test/transfer-bucket-demo/x?generation=77",  # http
        "https://evil.example.test/transfer-bucket-demo/x?generation=77",  # host
        f"{_ENDPOINT}/{_BUCKET}/{_FINAL}",  # no generation/signature at all
        f"{_ENDPOINT}/other-bucket/{_FINAL}?generation=77&X-Goog-Signature=s",  # bucket
        # right host/bucket/generation but an unpinned response type and
        # disposition — the semantic query params ARE the safety control
        f"{_ENDPOINT}/{_BUCKET}/{_FINAL}?generation=77"
        "&response-content-type=text%2Fhtml"
        "&response-content-disposition=inline&X-Goog-Signature=s",
        # duplicate generation params must not satisfy the pin
        f"{_ENDPOINT}/{_BUCKET}/{_FINAL}?generation=77&generation=78"
        "&response-content-type=application%2Foctet-stream&X-Goog-Signature=s",
    ]
    for bad in bad_urls:
        def signer(*args, _bad=bad, **kwargs):
            return _bad

        _, handler = _recording()
        adapter = _make_adapter(handler, url_signer=signer)
        with pytest.raises(st.DownloadSigningUnavailableError):
            await adapter.sign_generation_download(
                st.SignedDownloadRequest(
                    object_name=_FINAL,
                    generation=77,
                    media_type="application/octet-stream",
                    filename="notes.txt",
                    ttl_seconds=300,
                )
            )


def _sign_request(ttl_seconds=300):
    from agentdrive import storage_transfers as st

    return st.SignedDownloadRequest(
        object_name=_FINAL,
        generation=77,
        media_type="application/octet-stream",
        filename="notes.txt",
        ttl_seconds=ttl_seconds,
    )


async def _expect_signing_rejected(url_or_query, *, is_query=True):
    from urllib.parse import quote

    from agentdrive import storage_transfers as st

    url = (
        f"{_ENDPOINT}/{_BUCKET}/{quote(_FINAL, safe='/')}?{url_or_query}"
        if is_query
        else url_or_query
    )

    def signer(*args, **kwargs):
        return url

    _, handler = _recording()
    adapter = _make_adapter(handler, url_signer=signer)
    with pytest.raises(st.DownloadSigningUnavailableError):
        await adapter.sign_generation_download(_sign_request())


async def test_signed_download_requires_the_complete_v4_field_set():
    """Rereview blocker: presence of some X-Goog-Signature variant is not a
    V4 signature. Every mandatory field must appear exactly once."""
    disposition = 'attachment; filename="notes.txt"; filename*=UTF-8\'\'notes.txt'
    for missing in (
        "X-Goog-Algorithm",
        "X-Goog-Credential",
        "X-Goog-Date",
        "X-Goog-Expires",
        "X-Goog-SignedHeaders",
        "X-Goog-Signature",
    ):
        await _expect_signing_rejected(
            _v4_query(77, 300, "application/octet-stream", disposition, **{missing: None})
        )


async def test_signed_download_expiry_must_equal_the_requested_ttl():
    """Rereview blocker: a signer answering a 300s request with a week-long
    X-Goog-Expires must be refused — the returned target, not just the
    request, is what the expiry bound governs."""
    disposition = 'attachment; filename="notes.txt"; filename*=UTF-8\'\'notes.txt'
    for expires in ("604800", "999999", "301", "0", "-1", "not-a-number"):
        await _expect_signing_rejected(
            _v4_query(
                77, 300, "application/octet-stream", disposition,
                **{"X-Goog-Expires": expires},
            )
        )


async def test_signed_download_rejects_wrong_algorithm_duplicates_and_extras():
    disposition = 'attachment; filename="notes.txt"; filename*=UTF-8\'\'notes.txt'
    cases = [
        {"X-Goog-Algorithm": "AWS4-HMAC-SHA256"},
        {"X-Goog-Algorithm": "GOOG4-HMAC-SHA256"},
        {"_dupe_X-Goog-Signature": "second-sig"},
        {"_dupe_generation": "78"},
        {"unknown-param": "1"},
        {"X-Goog-SignedHeaders": "content-type"},  # host not signed
        {"X-Goog-Date": "yesterday"},
        {"X-Goog-Credential": "no-scope-suffix"},
        {"X-Goog-Signature": ""},
    ]
    for overrides in cases:
        await _expect_signing_rejected(
            _v4_query(77, 300, "application/octet-stream", disposition, **overrides)
        )


async def test_signed_download_rejects_non_wire_integers_and_encoded_keys():
    """Packet-4 review hardening: every hostile-output rejection must be the
    TYPED failure — unicode digits that pass isdigit() but are not wire
    integers, a regex-valid but calendar-invalid X-Goog-Date, and a
    percent-encoded query key that merely DECODES to an expected key must
    all raise DownloadSigningUnavailableError, never a bare ValueError."""
    disposition = 'attachment; filename="notes.txt"; filename*=UTF-8\'\'notes.txt'
    await _expect_signing_rejected(
        _v4_query(77, 300, "application/octet-stream", disposition,
                  **{"X-Goog-Expires": "²²²"})
    )
    await _expect_signing_rejected(
        _v4_query(77, 300, "application/octet-stream", disposition,
                  **{"X-Goog-Date": "20261399T996099Z"})
    )
    await _expect_signing_rejected(
        _v4_query(77, 300, "application/octet-stream", disposition,
                  generation=None)
        + "&%67eneration=77"
    )


async def test_signed_download_requires_a_closed_v4_grammar():
    """Rereview blockers (task-4 reports): the validator must prove a
    fresh, host-only, well-formed V4 capability — not merely field
    presence. Every case here was ACCEPTED at the reviewed head."""
    disposition = 'attachment; filename="notes.txt"; filename*=UTF-8\'\'notes.txt'
    cases = [
        # extra signed header contradicts required_headers={}
        {"X-Goog-SignedHeaders": "host;x-goog-user-project"},
        {"X-Goog-SignedHeaders": "host;x-extra"},
        # malformed credential scopes
        {"X-Goog-Credential": "garbage/goog4_request"},
        {"X-Goog-Credential": "/goog4_request"},
        {"X-Goog-Credential": "id/20260815/auto/storage"},
        {"X-Goog-Credential": "id/20260815/auto/compute/goog4_request"},
        # credential-scope date must equal the X-Goog-Date date
        {"X-Goog-Credential": "signer-demo@example.test/20200101/auto/storage/goog4_request"},
        # non-hex / short / odd-length signatures are not RSA signatures
        {"X-Goog-Signature": "!"},
        {"X-Goog-Signature": "deadbeef00"},
        {"X-Goog-Signature": "zz" * 256},
        {"X-Goog-Signature": "abc" + "ab" * 256},
        # stale and far-future signing instants (validator clock is pinned
        # to 20260815T120030Z by the fixtures)
        {"X-Goog-Date": "20010101T000000Z",
         "X-Goog-Credential": "signer-demo@example.test/20010101/auto/storage/goog4_request"},
        {"X-Goog-Date": "20990101T000000Z",
         "X-Goog-Credential": "signer-demo@example.test/20990101/auto/storage/goog4_request"},
        # beyond the documented skew window (clock 12:00:30, skew 60s)
        {"X-Goog-Date": "20260815T120300Z"},
        {"X-Goog-Date": "20260815T115500Z"},
    ]
    for overrides in cases:
        await _expect_signing_rejected(
            _v4_query(77, 300, "application/octet-stream", disposition, **overrides)
        )


async def test_signed_download_rejects_oversized_expires_on_the_adapter_path():
    """Rereview should-fix: Python's int() caps string conversion at 4300
    digits and raises ValueError beyond it. A ~4400-digit X-Goog-Expires
    fits under the URL bound and passes isascii()/isdigit(), so the
    conversion must be length-guarded — the ADAPTER path
    (sign_generation_download) has no belt-net and would otherwise leak a
    bare ValueError instead of the typed refusal."""
    disposition = 'attachment; filename="notes.txt"; filename*=UTF-8\'\'notes.txt'
    await _expect_signing_rejected(
        _v4_query(77, 300, "application/octet-stream", disposition,
                  **{"X-Goog-Expires": "1" * 4400})
    )


async def test_run_url_signer_severs_the_exception_context():
    """Adversarial residual: `raise ... from None` inside an except handler
    suppresses display but leaves __context__ pointing at the original
    signer exception, whose message can interpolate a credential host.
    The typed error must carry NO reachable cause or context."""
    from agentdrive import storage_transfers as st

    def leaky_signer(*args, **kwargs):
        raise RuntimeError("https://iam.example.test/secret?token=tok-demo")

    with pytest.raises(st.DownloadSigningUnavailableError) as excinfo:
        await st._run_url_signer(
            leaky_signer,
            bucket=_BUCKET, object_name=_FINAL, generation=77,
            ttl_seconds=300, media_type="application/octet-stream",
            disposition="attachment", timeout_seconds=5,
        )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


async def test_signer_timeout_must_fit_inside_the_freshness_skew():
    """A signer deadline larger than the freshness skew would let a
    slow-but-successful production sign be refused as stale — the
    constructor pins the coupling."""
    from agentdrive import storage_transfers as st

    with pytest.raises(ValueError):
        st.GenerationDownloadSigner(
            download_endpoint=_ENDPOINT,
            namespaces={_BUCKET: "cas/"},
            url_signer=lambda *a, **k: "unused",
            signer_timeout_seconds=st.DOWNLOAD_SIGNING_SKEW_SECONDS + 1,
        )


async def test_signed_download_rejects_raw_controls_and_unbounded_urls():
    """Raw control characters must be refused BEFORE urlsplit normalizes
    them away, and the URL must have a bounded length."""
    from urllib.parse import quote

    disposition = 'attachment; filename="notes.txt"; filename*=UTF-8\'\'notes.txt'
    good_query = _v4_query(77, 300, "application/octet-stream", disposition)
    base = f"{_ENDPOINT}/{_BUCKET}/{quote(_FINAL, safe='/')}"
    await _expect_signing_rejected(f"\n{base}?{good_query}", is_query=False)
    await _expect_signing_rejected(
        f"{_ENDPOINT}/{_BUCKET}/\n{quote(_FINAL, safe='/')}?{good_query}",
        is_query=False,
    )
    await _expect_signing_rejected(
        good_query + "&" * 9000, is_query=True
    )
    oversized = _v4_query(
        77, 300, "application/octet-stream", disposition,
        **{"X-Goog-Signature": "ab" * 6000},
    )
    await _expect_signing_rejected(oversized)


async def test_signed_download_accepts_a_real_google_v4_url():
    """Positive guard against over-tightening: a genuine
    google-cloud-storage V4 URL (signed locally with a synthetic RSA key)
    must pass the closed grammar."""
    pytest.importorskip("cryptography")
    from datetime import UTC, datetime, timedelta

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.cloud.storage import Client
    from google.oauth2 import service_account

    from agentdrive import storage_transfers as st

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    creds = service_account.Credentials.from_service_account_info({
        "type": "service_account",
        "project_id": "synthetic-demo",
        "private_key_id": "demo-key-id",
        "private_key": pem,
        "client_email": "signer-demo@synthetic-demo.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.example.test/token",
    })
    client = Client(project="synthetic-demo", credentials=creds)
    disposition = build_content_disposition("attachment", "notes.txt")
    url = client.bucket(_BUCKET).blob(_FINAL).generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=300),
        method="GET",
        generation=77,
        response_type="application/octet-stream",
        response_disposition=disposition,
        api_access_endpoint=_ENDPOINT,
        credentials=creds,
    )
    st.validate_signed_download_url(
        url,
        download_endpoint=_ENDPOINT,
        bucket=_BUCKET,
        object_name=_FINAL,
        generation=77,
        media_type="application/octet-stream",
        disposition=disposition,
        ttl_seconds=300,
        now=datetime.now(UTC),
    )


async def test_download_signer_is_https_only_even_on_loopback():
    """The download bearer contract is HTTPS-only (task-4 blocker 2): the
    capability boundary must reject every http:// endpoint, loopback
    included, without weakening the loopback-capable origin validator the
    upload/emulator surface uses."""
    from agentdrive import storage_transfers as st

    for endpoint in (
        "http://localhost:4443",
        "http://127.0.0.1:4443",
        "http://[::1]:4443",
        "http://demo.localhost:4443",
    ):
        with pytest.raises(ValueError):
            st.GenerationDownloadSigner(
                download_endpoint=endpoint,
                namespaces={_BUCKET: "transfer-immutable/"},
                url_signer=lambda *a, **k: "unused",
            )
        _, handler = _recording()
        with pytest.raises(ValueError):
            _make_adapter(handler, download_endpoint=endpoint)


async def test_sign_capability_rejects_namespace_marker_objects():
    """The signer is the last fail-closed boundary over persisted
    coordinates: a bare namespace marker, empty segments, dot segments,
    or encoded/backslash ambiguity must never become a bearer target."""
    from agentdrive import storage_transfers as st

    def signer(bucket, object_name, generation, ttl, response_type, disposition):
        from urllib.parse import quote

        query = _v4_query(generation, ttl, response_type, disposition)
        return f"{_ENDPOINT}/{bucket}/{quote(object_name, safe='/')}?{query}"

    gds = st.GenerationDownloadSigner(
        download_endpoint=_ENDPOINT,
        namespaces={_BUCKET: "cas/"},
        url_signer=signer,
        clock=lambda: _FIXED_NOW,
    )
    for bad in ("cas/", "cas//x", "cas/x/", "cas/./x", "cas/x%2fy", "cas/x\\y"):
        with pytest.raises(st.DownloadSigningUnavailableError):
            await gds.sign_capability(
                bucket=_BUCKET, object_name=bad, generation=77,
                media_type="application/octet-stream", filename="notes.txt",
                ttl_seconds=300,
            )
    # Legitimate nested objects keep working.
    capability = await gds.sign_capability(
        bucket=_BUCKET, object_name="cas/drv_demo/aa11bb22", generation=77,
        media_type="application/octet-stream", filename="notes.txt",
        ttl_seconds=300,
    )
    assert "generation=77" in capability.url


async def test_sign_capability_times_out_a_hanging_signer():
    """The signing composition owns a bounded deadline: a hung IAM/signBlob
    worker maps to the typed refusal, quickly, and its late result is
    discarded (the thread cannot alter returned state)."""
    import time

    from agentdrive import storage_transfers as st

    def hanging_signer(*args, **kwargs):
        time.sleep(1.0)
        return "late-and-ignored"

    gds = st.GenerationDownloadSigner(
        download_endpoint=_ENDPOINT,
        namespaces={_BUCKET: "cas/"},
        url_signer=hanging_signer,
        signer_timeout_seconds=0.05,
        clock=lambda: _FIXED_NOW,
    )
    start = time.monotonic()
    with pytest.raises(st.DownloadSigningUnavailableError):
        await gds.sign_capability(
            bucket=_BUCKET, object_name="cas/drv_demo/aa11bb22", generation=77,
            media_type="application/octet-stream", filename="notes.txt",
            ttl_seconds=300,
        )
    assert time.monotonic() - start < 0.9  # refused at the deadline, not after


@pytest.mark.parametrize("disposition", ["inline", "attachment"])
def test_content_disposition_has_safe_fallback_and_utf8_name(
    disposition: str,
) -> None:
    value = build_content_disposition(disposition, "Résumé 研究 🚀.pdf")
    assert value.startswith(f'{disposition}; filename="Rsum  .pdf"; ')
    assert (
        "filename*=UTF-8''R%C3%A9sum%C3%A9%20%E7%A0%94%E7%A9%B6%20%F0%9F%9A%80.pdf"
        in value
    )


@pytest.mark.parametrize(
    ("name", "expected_name"),
    [
        ("../secret.txt", "secret.txt"),
        ("..\\secret.txt", "secret.txt"),
        ("a/b\\c/evil.txt", "evil.txt"),
    ],
)
def test_content_disposition_strips_path_context(
    name: str, expected_name: str
) -> None:
    value = build_content_disposition("attachment", name)
    assert value == (
        f'attachment; filename="{expected_name}"; '
        f"filename*=UTF-8''{expected_name}"
    )


@pytest.mark.parametrize(
    ("name", "forbidden"),
    [
        ("x\r\nX-Evil: 1.txt", ("\r", "\n", "%0D", "%0A")),
        ("x\x00\x7fy.txt", ("\x00", "\x7f", "%00", "%7F")),
        ("x\u2028\u2029y.txt", ("\u2028", "\u2029", "%E2%80%A8", "%E2%80%A9")),
        ("x\ufeffy.txt", ("\ufeff", "%EF%BB%BF")),
        (".\u202ename\u2069.txt", ("\u202e", "\u2069", "%E2%80%AE", "%E2%81%A9")),
    ],
)
def test_content_disposition_strips_header_and_bidi_controls(
    name: str, forbidden: tuple[str, ...]
) -> None:
    value = build_content_disposition("attachment", name)
    assert all(token not in value for token in forbidden)


@pytest.mark.parametrize(
    "control",
    [
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    ],
)
def test_content_disposition_strips_every_forbidden_bidi_control(
    control: str,
) -> None:
    value = build_content_disposition("attachment", f"safe{control}.txt")
    assert value == (
        'attachment; filename="safe.txt"; filename*=UTF-8\'\'safe.txt'
    )


@pytest.mark.parametrize(
    "surrogate",
    ["\ud800", "\udfff"],
    ids=["high-surrogate", "low-surrogate"],
)
def test_content_disposition_strips_lone_surrogates(surrogate: str) -> None:
    value = build_content_disposition("attachment", f"safe{surrogate}.txt")
    assert value == (
        'attachment; filename="safe.txt"; filename*=UTF-8\'\'safe.txt'
    )


def test_content_disposition_preserves_emoji_joiners_in_filename_star() -> None:
    value = build_content_disposition(
        "attachment", "Family 👩‍👩‍👧‍👦 ن‌م.png"
    )
    assert "%E2%80%8D" in value
    assert "%E2%80%8C" in value


def test_content_disposition_percent_encodes_a_valid_quote() -> None:
    value = build_content_disposition("attachment", 'quarterly "final".pdf')
    assert 'filename="quarterly final.pdf"' in value
    assert "filename*=UTF-8''quarterly%20%22final%22.pdf" in value


def test_content_disposition_normalizes_the_utf8_name_to_nfc() -> None:
    value = build_content_disposition("attachment", "Cafe\u0301 — 2026.txt")
    assert "filename*=UTF-8''Caf%C3%A9%20%E2%80%94%202026.txt" in value


@pytest.mark.parametrize("name", ["...", "\r\n\ufeff\u202e"])
def test_content_disposition_uses_download_for_an_empty_safe_name(name: str) -> None:
    assert build_content_disposition("attachment", name) == (
        'attachment; filename="download"; filename*=UTF-8\'\'download'
    )


async def test_legacy_signed_download_uses_safe_content_disposition_builder(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeBlob:
        def generate_signed_url(self, **kwargs) -> str:
            captured.update(kwargs)
            return "https://storage.example.test/signed"

    class FakeBucket:
        def blob(self, object_name: str, *, generation: int | None = None) -> FakeBlob:
            captured["object_name"] = object_name
            captured["generation"] = generation
            return FakeBlob()

    class FakeCredentials:
        service_account_email = "signer@example.test"
        token = "synthetic-token"

    # This exercises the GCS module's own signer, so the patches land on
    # that module rather than on the backend-neutral facade.
    from agentdrive.storage import gcs as gcs_storage

    monkeypatch.setattr(gcs_storage.settings, "gcs_emulator_host", "")
    monkeypatch.setattr(gcs_storage, "_get_signing_creds", lambda: FakeCredentials())
    monkeypatch.setattr(gcs_storage, "_bucket_for", lambda bucket: FakeBucket())

    result = await gcs_storage.signed_download_url(
        "cas/drv_demo/digest",
        content_type="image/png",
        filename='../Résumé "final"\r\n\u202e研究.png',
        ttl_s=300,
        bucket="artifact-bucket",
        generation=77,
    )

    assert result == "https://storage.example.test/signed"
    assert captured["object_name"] == "cas/drv_demo/digest"
    assert captured["generation"] == 77
    assert captured["response_disposition"] == (
        'attachment; filename="Rsum final.png"; '
        "filename*=UTF-8''R%C3%A9sum%C3%A9%20%22final%22%E7%A0%94%E7%A9%B6.png"
    )


async def test_signed_download_rejects_fragments():
    disposition = 'attachment; filename="notes.txt"; filename*=UTF-8\'\'notes.txt'
    from urllib.parse import quote

    url = (
        f"{_ENDPOINT}/{_BUCKET}/{quote(_FINAL, safe='/')}"
        f"?{_v4_query(77, 300, 'application/octet-stream', disposition)}#frag"
    )
    await _expect_signing_rejected(url, is_query=False)


async def test_signed_download_bounds_ttl_and_sanitizes_the_filename():
    from agentdrive import storage_transfers as st

    calls, signer = _fake_signer()
    _, handler = _recording()
    adapter = _make_adapter(handler, url_signer=signer)

    with pytest.raises(ValueError):
        await adapter.sign_generation_download(
            st.SignedDownloadRequest(
                object_name=_FINAL,
                generation=77,
                media_type="application/octet-stream",
                filename="notes.txt",
                ttl_seconds=0,
            )
        )
    with pytest.raises(ValueError):
        await adapter.sign_generation_download(
            st.SignedDownloadRequest(
                object_name=_FINAL,
                generation=77,
                media_type="application/octet-stream",
                filename="notes.txt",
                ttl_seconds=24 * 3600,
            )
        )
    await adapter.sign_generation_download(
        st.SignedDownloadRequest(
            object_name=_FINAL,
            generation=77,
            media_type="application/octet-stream",
            filename='evil"\r\nname.txt',
            ttl_seconds=300,
        )
    )
    disposition = calls[-1]["disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert 'evil"' not in disposition


# ---------------------------------------------------------------------------
# The legacy browser-resumable claim is REPLACED, not wrapped
# ---------------------------------------------------------------------------


def test_legacy_json_api_browser_resumable_helper_is_removed():
    """`storage.create_resumable_upload_session` initiated via the JSON API
    and documented an origin-parameter browser path; GCS does not enforce
    bucket CORS on JSON API endpoints, so that claim is unsound for B3.
    The XML adapter replaces it — the old helper must be gone so nothing
    can quietly revive the JSON-API browser transport."""
    assert not hasattr(storage, "create_resumable_upload_session")
