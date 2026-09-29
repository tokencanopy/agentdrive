"""GCS transfer-storage adapter for B3 direct upload sessions (packet 2).

Governing contract: TokenCanopy
``docs/superpowers/specs/2026-08-14-agentdrive-direct-transfer-session-design.md``
§5.6/§6/§7/§8. Upload controls and download capability minting are its
callers; the runtime readiness gate keeps both fail-closed when direct
transfer is disabled.

Why this module exists instead of extending ``storage.py``: the browser
transport must be the GCS **XML API** — GCS does not enforce bucket CORS on
JSON API endpoints, so the old ``storage.create_resumable_upload_session``
(JSON API via the google client, origin passed per call) documented a
browser path that CORS could never actually gate. That helper is REPLACED
by this adapter, not wrapped.

Measured amendment (2026-08-20, probed against real GCS — see TokenCanopy
``docs/superpowers/specs/2026-08-20-agentdrive-browser-initiated-transfer-amendment.md``):
server-side XML initiation is ALSO dead to browsers. A resumable session
initiated by an authenticated server-side POST gets **no CORS blessing**,
whatever ``Origin`` the initiation carried — chunk PUT responses on the
session URI never carry ``Access-Control-Allow-Origin`` (browsers see bare
503s) while non-browser clients succeed on the identical requests, and
bucket CORS demonstrably works on plain object paths of the same bucket.
Only a genuine CORS-context initiation performed by the BROWSER, from the
real origin, yields a CORS-usable session. This adapter therefore no longer
initiates at all: it V4-SIGNS the initiation request (POST +
``x-goog-resumable: start`` + content type + adoption marker as signed
headers) and the client performs the initiation itself, reading the session
URI from the ``Location`` header.

Security posture, enforced here and pinned by tests/test_storage_transfers.py:

  * The bucket and endpoints come ONLY from trusted deployment
    configuration handed to the constructor; no request value can steer
    them, and ``XmlResumableRequest`` has no such field. The canonical
    browser origin remains validated deployment configuration (the CORS
    coupling pin) but is no longer sent on any request.
  * Signing mints NO provider-side state (IAM ``signBlob`` is pure
    computation), so a signing failure is always safe to retry; the
    at-most-one rule that matters — one DISCLOSURE per session, marked by
    ``provider_attempted_at`` — is the caller's, not a provider property.
  * ``X-Upload-Content-Length`` is never sent (§7): the declared size is an
    AgentDrive contract enforced by reservation + completion stat, not a
    provider header claim.
  * The signed initiation URL is validated FAIL-CLOSED before return
    (closed V4 grammar, the exact signed-header set, freshness, expiry ==
    TTL), returned once to the caller, and appears in no log record;
    provider exceptions are SEVERED from their httpx causes so no traceback
    formatter can resurrect a URL or query string.
  * Rewrite pins the exact source generation and destination
    ``ifGenerationMatch=0`` (create-only, §6 step 3); delete is
    generation-pinned; signed GETs pin generation + response type +
    disposition and FAIL CLOSED — never the legacy signer's soft ``None``.

Layer rule: imports ``core.v0_uploads`` only for the shared, credential-free
``ObjectObservation`` shape (this adapter satisfies its ``TransferStorage``
protocol); never ``agentdrive.api``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import re
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .config import MAX_DOWNLOAD_CAPABILITY_TTL_SECONDS, validate_exact_origin
from .content_disposition import build_content_disposition
from .core.v0_uploads import ObjectObservation

# Object-metadata keys carrying the server-owned adoption identity (§6).
# The XML surface reads/writes them as `x-goog-meta-<key>` headers; the JSON
# rewrite writes them under `metadata`. One constant each so the initiation,
# stat, and rewrite spellings cannot drift apart.
ADOPTION_MARKER_METADATA_KEY = "adoption-marker"
SOURCE_FINGERPRINT_METADATA_KEY = "source-fingerprint"

# Hard ceiling on a signed-download TTL accepted at the signer boundary.
# THE SAME constant the config validator fences
# DIRECT_DOWNLOAD_CAPABILITY_TTL_SECONDS against (packet-4 follow-up: one
# authoritative bound, re-exported here under the adapter's historical name).
MAX_DOWNLOAD_TTL_SECONDS = MAX_DOWNLOAD_CAPABILITY_TTL_SECONDS

# Adapter-owned transport policy (packet-2 rereview blocker): every provider
# request is finitely timed and NEVER follows a redirect, regardless of how
# the injected client was configured. An unbounded call would hang a future
# begin/completion lease; a followed redirect would carry the trusted Origin
# and metadata (and, same-origin, the credential) off the configured endpoint.
PROVIDER_TIMEOUT_SECONDS = 30.0
_PROVIDER_TIMEOUT = httpx.Timeout(PROVIDER_TIMEOUT_SECONDS)

# Lifetime of a V4-signed initiation URL (2026-08-20 amendment §4). A
# PROTOCOL constant, not a B8 launch value: it bounds how long the
# initiation capability itself lives — long enough for the console to POST
# it (including one bounded retry and one session-lost re-initiation),
# short enough that a leaked bundle dies in minutes. The session URI the
# POST mints keeps GCS's own ~1-week lifetime, exactly as before.
INITIATION_TTL_SECONDS = 600

# The EXACT signed-header set of a valid initiation URL, in V4's canonical
# form (lowercase, sorted, semicolon-joined). Anything else is a credential
# with different powers than the contract discloses and is refused.
INITIATION_SIGNED_HEADERS = (
    "content-type;host;x-goog-meta-adoption-marker;x-goog-resumable"
)

# Bound on a provider rewrite continuation token the adapter will accept or
# resend; anything longer/empty/control-bearing is not a provable token.
MAX_CONTINUATION_LENGTH = 4096

# GCS bucket-name syntax (lowercase letters, digits, dots, dashes,
# underscores; 3–222 chars; alnum at both ends).
_BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")

_V4_DATE = re.compile(r"\d{8}T\d{6}Z")

_CANONICAL_CRC32C = re.compile(r"[A-Za-z0-9+/]{6}==")


class InvalidChecksumError(ValueError):
    """A CRC32C value that is not the canonical GCS metadata form."""


def canonical_crc32c(value: str) -> str:
    """Strictly validate a declared CRC32C (§5.2) and return it unchanged.

    Not regex-only: decode with the standard alphabet, require exactly four
    bytes, re-encode with standard alphabet + padding, and require
    byte-for-byte equality — rejecting unpadded, URL-safe, overlong,
    non-canonical-trailing-bit, and malformed encodings."""
    if not _CANONICAL_CRC32C.fullmatch(value):
        raise InvalidChecksumError("CRC32C must be 4 bytes as padded standard base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidChecksumError("CRC32C is not valid base64") from exc
    if len(decoded) != 4:
        raise InvalidChecksumError("CRC32C must decode to exactly 4 bytes")
    if base64.b64encode(decoded).decode("ascii") != value:
        raise InvalidChecksumError("CRC32C encoding is not canonical")
    return value


class TransferProviderError(Exception):
    """Base for provider-boundary failures.

    Deliberately coordinate-free: ``str()``/``repr()`` show only the safe
    classification and HTTP status, never a URL, query value, object key, or
    provider message — these strings end up in logs and error envelopes."""

    def __init__(self, classification: str, *, status: int | None = None) -> None:
        self.classification = classification
        self.status = status
        rendered = classification if status is None else f"{classification} ({status})"
        super().__init__(rendered)


class InitiationSigningUnavailableError(TransferProviderError):
    """Initiation signing failed or produced an unvalidatable URL. FAIL
    CLOSED (503 TRANSFER_UNAVAILABLE at the wire). Signing mints no
    provider state, so the caller's session stays retryable."""


class TransferProviderUnavailableError(TransferProviderError):
    """Transient or AMBIGUOUS provider outcome — retryable/reconcilable,
    never proof of failure (503 TRANSFER_UNAVAILABLE at the wire)."""


class TransferPreconditionFailedError(TransferProviderError):
    """A generation precondition fired: destination already exists, source
    generation gone, or a pinned delete missed. The fenced caller — not this
    adapter — classifies what that means for the session."""


class DownloadSigningUnavailableError(TransferProviderError):
    """Signing failed or produced an unvalidatable URL. FAIL CLOSED (503
    DOWNLOAD_SIGNING_UNAVAILABLE) — there is no proxy/stream fallback."""


@dataclass(frozen=True)
class XmlResumableRequest:
    """One XML resumable initiation to SIGN. Deliberately narrow: the
    bucket and endpoint are TRUSTED CONFIGURATION on the adapter — a caller
    (and therefore any request-derived value) cannot supply them."""

    object_name: str
    content_type: str
    adoption_marker: str


@dataclass(frozen=True)
class SignedInitiation:
    """One validated V4-signed initiation target (2026-08-20 amendment).

    The URL alone is inert: the signature covers the header VALUES in
    ``required_headers``, so a holder must send exactly them for GCS to
    accept the POST. ``expires_at`` is derived from the URL's own
    X-Goog-Date + X-Goog-Expires so the disclosed lifetime can never claim
    more than the credential has."""

    url: str
    required_headers: Mapping[str, str]
    expires_at: datetime


@dataclass(frozen=True)
class ObjectGeneration:
    """Exact provider coordinates of one observed object generation."""

    object_name: str
    generation: int


@dataclass(frozen=True)
class RewriteResult:
    """One step of the guarded rewrite. ``done`` with the final generation,
    or a ``continuation`` token the CALLER durably commits before the next
    call (§6: continuation is server-only recovery state; this adapter never
    hides a multi-call rewrite behind an internal loop)."""

    done: bool
    generation: int | None = None
    continuation: str | None = None


@dataclass(frozen=True)
class SignedDownloadRequest:
    """The transfer adapter's signing input: exact object coordinates and
    response semantics for one V4 GET over the transfer bucket."""

    object_name: str
    generation: int
    media_type: str
    filename: str
    ttl_seconds: int


_OBJECT_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f?#]")
# Values that ride as raw HTTP header values (Content-Type, x-goog-meta-*).
# Rejecting control characters HERE is defense in depth against header
# smuggling/metadata forgery (adversarial review finding 2): today's h11
# transport would refuse a CRLF anyway, but the substrate must not depend
# on whichever transport a caller injects.
_HEADER_VALUE_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")


# Refcounted silencing state for _provider_logs_silenced. Module-global on
# purpose: logger levels are process-global, so the bookkeeping must be too.
_SILENCED_LOGGER_NAMES = ("httpx", "httpcore")
_silence_depth = 0
_silence_prior: list[tuple[logging.Logger, int]] = []


@contextlib.contextmanager
def _provider_logs_silenced():
    """Raise httpx/httpcore logger thresholds to WARNING for the duration of
    a provider call. Their INFO/DEBUG records interpolate the full request
    URL — which for a transfer call names the bucket and object key that §8
    redaction forbids in logs. (Level on the "httpcore" parent covers its
    child loggers via effective-level inheritance.)

    REFCOUNTED, because levels are process-global shared state: a naive
    save/restore pair lets one finishing call restore the verbose level
    while a concurrent call's request is still in flight (adversarial
    review finding 1) — the first entrant saves + raises, only the last
    exit restores, so overlapping transfer calls stay silenced end to end.
    Depth arithmetic runs synchronously (no await inside the manager), so
    it is atomic under asyncio's single-threaded scheduling."""
    global _silence_depth, _silence_prior
    if _silence_depth == 0:
        _silence_prior = [
            (logging.getLogger(name), logging.getLogger(name).level)
            for name in _SILENCED_LOGGER_NAMES
        ]
        for lg, level in _silence_prior:
            lg.setLevel(max(level, logging.WARNING))
    _silence_depth += 1
    try:
        yield
    finally:
        _silence_depth -= 1
        if _silence_depth == 0:
            for lg, level in _silence_prior:
                lg.setLevel(level)
            _silence_prior = []


class XmlTransferStorage:
    """The one storage adapter for direct-transfer objects.

    Scoped to a single configured transfer bucket: every method touches only
    ``{endpoint}/{bucket}/…``, so an observation or deletion can never name a
    foreign bucket (the property ``core.v0_uploads.adoption_fingerprint``
    relies on). Satisfies that module's ``TransferStorage`` protocol.

    ``client`` is the injected HTTP seam (tests use ``httpx.MockTransport``);
    ``token_provider`` supplies a bearer for server-to-server provider calls
    (``None`` for the anonymous local emulator); ``url_signer`` is the V4
    download-signing seam — its output is validated before it is returned,
    an absent signer raises ``DownloadSigningUnavailableError``, and
    production wires the IAM-credentials ``_production_url_signer``
    (packet 4). ``initiation_signer`` is the V4 INITIATION-signing seam
    (2026-08-20 amendment): same posture — validated fail-closed output,
    absent signer raises ``InitiationSigningUnavailableError``, production
    wires ``_production_initiation_signer`` and the emulator lane wires the
    shape-only local signer (fake-gcs verifies no signatures)."""

    def __init__(
        self,
        *,
        bucket: str,
        upload_endpoint: str,
        download_endpoint: str,
        canonical_origin: str,
        scratch_prefix: str,
        immutable_prefix: str,
        client: httpx.AsyncClient,
        token_provider: Callable[[], Awaitable[str | None]] | None = None,
        url_signer: Callable[..., str] | None = None,
        initiation_signer: Callable[..., str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not _BUCKET_NAME.fullmatch(bucket):
            raise ValueError("transfer bucket must be a valid GCS bucket name")
        for prefix, name in (
            (scratch_prefix, "scratch_prefix"),
            (immutable_prefix, "immutable_prefix"),
        ):
            if (
                not prefix
                or not prefix.endswith("/")
                or prefix.startswith("/")
                or "//" in prefix
                or _OBJECT_NAME_FORBIDDEN.search(prefix)
            ):
                raise ValueError(f"{name} must be a relative, slash-terminated namespace")
        if scratch_prefix.startswith(immutable_prefix) or immutable_prefix.startswith(
            scratch_prefix
        ):
            raise ValueError("scratch and immutable prefixes must be disjoint namespaces")
        self._bucket = bucket
        # The SAME exact-origin validator `Settings` uses (config.py) — a
        # direct constructor cannot weaken the boundary that config enforces.
        self._upload_endpoint = validate_exact_origin(
            upload_endpoint, setting="upload_endpoint"
        )
        # The DOWNLOAD endpoint hosts disclosed bearer targets: HTTPS-only,
        # loopback included (the upload endpoint keeps the loopback-HTTP
        # exception for the fake-GCS emulator).
        self._download_endpoint = validate_download_endpoint(
            download_endpoint, setting="download_endpoint"
        )
        # Still validated deployment configuration (the infra contract test
        # pins it equal to a bucket CORS origin), but since the 2026-08-20
        # amendment it is SENT nowhere: the browser's own Origin at its
        # signed initiation POST is what GCS binds session CORS to.
        self._canonical_origin = validate_exact_origin(
            canonical_origin, setting="canonical browser origin"
        )
        self._scratch_prefix = scratch_prefix
        self._immutable_prefix = immutable_prefix
        self._client = client
        self._token_provider = token_provider
        self._url_signer = url_signer
        self._initiation_signer = initiation_signer
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- shared plumbing ---------------------------------------------------

    def _object_path(self, endpoint: str, object_name: str) -> str:
        return f"{endpoint}/{self._bucket}/" + urllib.parse.quote(
            object_name, safe="/"
        )

    def _require_object_name(self, object_name: str, *, prefix: str | None) -> str:
        """Server-selected keys only: relative, control-free, no traversal,
        and (when a namespace applies) inside the expected prefix."""
        if (
            not object_name
            or object_name.startswith("/")
            or ".." in object_name.split("/")
            or _OBJECT_NAME_FORBIDDEN.search(object_name)
        ):
            raise ValueError("invalid transfer object name")
        if prefix is not None and not object_name.startswith(prefix):
            raise ValueError("transfer object name is outside its namespace")
        return object_name

    def _require_transfer_namespace(self, object_name: str) -> str:
        """Stat/delete are confined to the configured scratch OR immutable
        namespace — the adapter can never observe or delete a foreign key
        (rereview should-fix: namespace scoping was bucket-wide)."""
        name = self._require_object_name(object_name, prefix=None)
        if not (
            name.startswith(self._scratch_prefix)
            or name.startswith(self._immutable_prefix)
        ):
            raise ValueError("transfer object name is outside the transfer namespaces")
        return name

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: dict | None = None,
        classification: str,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if self._token_provider is not None:
            # Credential acquisition inside its own fail-closed, severed
            # boundary: a token-provider exception may interpolate a
            # credential endpoint or secret, so it must never escape untyped
            # (rereview blocker). Silenced like every provider interaction.
            token = None
            credential_failed = False
            with _provider_logs_silenced():
                try:
                    # Adapter-owned deadline on the CREDENTIAL leg too: a
                    # provider that hangs (not just one that raises) must not
                    # hold a future begin/completion lease indefinitely.
                    token = await asyncio.wait_for(
                        self._token_provider(), PROVIDER_TIMEOUT_SECONDS
                    )
                except Exception:
                    credential_failed = True
            if credential_failed:
                raise TransferProviderUnavailableError("credential_unavailable")
            if token:
                request_headers["Authorization"] = f"Bearer {token}"
        # Swallow the httpx error entirely and raise OUTSIDE the handler:
        # httpx exception messages interpolate the URL, and even a suppressed
        # `__context__` would keep it reachable to log introspection.
        # Transport policy is ADAPTER-owned per request: never follow a
        # redirect and never wait unboundedly, whatever the injected
        # client's defaults say.
        response = None
        with _provider_logs_silenced(), contextlib.suppress(httpx.HTTPError):
            response = await self._client.request(
                method,
                url,
                headers=request_headers,
                params=params,
                json=json_body,
                timeout=_PROVIDER_TIMEOUT,
                follow_redirects=False,
            )
        if response is None:
            raise TransferProviderUnavailableError(classification)
        if 300 <= response.status_code < 400:
            # A provider redirect is refused, never followed: honoring it
            # would send the trusted Origin/metadata (and, same-origin, the
            # credential) somewhere the configuration never named.
            raise TransferProviderUnavailableError(
                "redirect_refused", status=response.status_code
            )
        return response

    # -- V4-signed XML initiation (§5.6 as amended 2026-08-20) -------------

    async def sign_resumable_initiation(
        self, request: XmlResumableRequest
    ) -> SignedInitiation:
        """Mint ONE validated, V4-signed XML initiation target for a
        server-selected scratch key — the single allowed disclosure path.

        No provider request is made here: signing is pure computation (IAM
        ``signBlob``-backed in production), so a failure provably leaves no
        provider state and the caller's session stays retryable. FAIL
        CLOSED on every axis — signer error, missing signer, or output
        that does not verify against the configured endpoint, bucket,
        exact object, the EXACT signed-header set, freshness, and expiry
        == INITIATION_TTL_SECONDS."""
        object_name = self._require_object_name(
            request.object_name, prefix=self._scratch_prefix
        )
        for value, label in (
            (request.content_type, "content type"),
            (request.adoption_marker, "adoption marker"),
        ):
            if not value or _HEADER_VALUE_FORBIDDEN.search(value):
                raise ValueError(f"invalid {label} for a transfer initiation")
        url = await _run_initiation_signer(
            self._initiation_signer,
            bucket=self._bucket,
            object_name=object_name,
            content_type=request.content_type,
            adoption_marker=request.adoption_marker,
            ttl_seconds=INITIATION_TTL_SECONDS,
        )
        try:
            validate_signed_initiation_url(
                url,
                upload_endpoint=self._upload_endpoint,
                bucket=self._bucket,
                object_name=object_name,
                ttl_seconds=INITIATION_TTL_SECONDS,
                now=self._clock(),
            )
            expires_at = _signed_url_expiry(url)
        except InitiationSigningUnavailableError:
            raise
        except Exception:
            # Belt under the validator's own checks: hostile signer OUTPUT
            # must never surface as anything but the typed refusal.
            raise InitiationSigningUnavailableError("signed_url_invalid") from None
        return SignedInitiation(
            url=url,
            # The exact headers the signature covers — computed HERE, from
            # the same values that were signed, so the disclosed set and
            # the signed set cannot drift apart.
            required_headers={
                "x-goog-resumable": "start",
                "Content-Type": request.content_type,
                f"x-goog-meta-{ADOPTION_MARKER_METADATA_KEY}": (
                    request.adoption_marker
                ),
            },
            expires_at=expires_at,
        )

    # -- finalized-object stat (§6 completion step 2 / reconciliation) -----

    async def stat_generation(
        self, object_name: str, generation: int | None = None
    ) -> ObjectObservation | None:
        """Metadata-only observation of one object (optionally an exact
        generation). Unobservable fields stay ``None`` — the caller treats
        them as AMBIGUOUS, never as a match."""
        object_name = self._require_transfer_namespace(object_name)
        if generation is not None and generation <= 0:
            raise ValueError("a pinned stat requires a positive generation")
        params = {"generation": str(generation)} if generation is not None else None
        response = await self._request(
            "HEAD",
            self._object_path(self._upload_endpoint, object_name),
            params=params,
            classification="stat_unavailable",
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise TransferProviderUnavailableError(
                "stat_unavailable", status=response.status_code
            )
        headers = response.headers
        # Fields a provider DID return must be well-formed and coherent —
        # a malformed or contradictory identity is an AMBIGUOUS observation,
        # surfaced typed, never a "valid" ObjectObservation (rereview
        # blocker). Absent fields stay None, which callers treat as
        # unverifiable, never as a match.
        crc32c = None
        for token in headers.get("x-goog-hash", "").split(","):
            name, _, value = token.strip().partition("=")
            if name == "crc32c" and value:
                try:
                    crc32c = canonical_crc32c(value)
                except InvalidChecksumError:
                    raise TransferProviderUnavailableError("stat_malformed") from None
                break
        size_header = headers.get(
            "x-goog-stored-content-length", headers.get("Content-Length")
        )
        observed_generation = headers.get("x-goog-generation")
        try:
            generation_value = int(observed_generation) if observed_generation else None
            size_value = int(size_header) if size_header is not None else None
        except ValueError:
            raise TransferProviderUnavailableError("stat_malformed") from None
        if generation_value is not None and generation_value <= 0:
            raise TransferProviderUnavailableError("stat_malformed")
        if size_value is not None and size_value < 0:
            raise TransferProviderUnavailableError("stat_malformed")
        if (
            generation is not None
            and generation_value is not None
            and generation_value != generation
        ):
            # A generation-pinned stat answering with a DIFFERENT generation
            # is provider corruption, not an observation to hand upward.
            raise TransferProviderUnavailableError("stat_mismatch")
        return ObjectObservation(
            object_name=object_name,
            generation=generation_value,
            size=size_value,
            crc32c=crc32c,
            content_type=headers.get("Content-Type"),
            adoption_marker=headers.get(
                f"x-goog-meta-{ADOPTION_MARKER_METADATA_KEY}"
            ),
            source_fingerprint=headers.get(
                f"x-goog-meta-{SOURCE_FINGERPRINT_METADATA_KEY}"
            ),
        )

    async def stat_object(self, object_name: str) -> ObjectObservation | None:
        """``core.v0_uploads.TransferStorage`` protocol shim: the live
        (unpinned) observation the reconciler verifies against the session's
        durable adoption identity."""
        return await self.stat_generation(object_name, None)

    # -- guarded scratch → immutable adoption (§6 completion step 3) -------

    async def rewrite_generation_create_only(
        self,
        source: ObjectGeneration,
        destination_name: str,
        *,
        content_type: str,
        adoption_marker: str,
        source_fingerprint: str,
        continuation: str | None = None,
    ) -> RewriteResult:
        """One step of the create-only adoption rewrite.

        Pins the EXACT source generation and destination
        ``ifGenerationMatch=0`` so an existing destination is never
        overwritten, and stamps the server-owned identity metadata the
        reconciler later verifies. A ``continuation`` is returned — not
        looped — because the caller must durably commit it before the next
        call (§6). Server-to-server JSON API is fine here: the XML-only rule
        exists for the browser CORS path, which this call is not."""
        source_name = self._require_object_name(
            source.object_name, prefix=self._scratch_prefix
        )
        destination = self._require_object_name(
            destination_name, prefix=self._immutable_prefix
        )
        if source.generation <= 0:
            raise ValueError("rewrite requires a positive observed source generation")
        for value, label in (
            (content_type, "content type"),
            (adoption_marker, "adoption marker"),
            (source_fingerprint, "source fingerprint"),
        ):
            if not value or _HEADER_VALUE_FORBIDDEN.search(value):
                raise ValueError(f"invalid {label} for a rewrite")
        if continuation is not None and (
            not continuation
            or len(continuation) > MAX_CONTINUATION_LENGTH
            or _HEADER_VALUE_FORBIDDEN.search(continuation)
        ):
            raise ValueError("invalid rewrite continuation token")
        params = {
            "sourceGeneration": str(source.generation),
            "ifGenerationMatch": "0",
        }
        if continuation is not None:
            params["rewriteToken"] = continuation
        url = (
            f"{self._upload_endpoint}/storage/v1/b/{self._bucket}/o/"
            + urllib.parse.quote(source_name, safe="")
            + f"/rewriteTo/b/{self._bucket}/o/"
            + urllib.parse.quote(destination, safe="")
        )
        response = await self._request(
            "POST",
            url,
            params=params,
            json_body={
                "contentType": content_type,
                "metadata": {
                    ADOPTION_MARKER_METADATA_KEY: adoption_marker,
                    SOURCE_FINGERPRINT_METADATA_KEY: source_fingerprint,
                },
            },
            classification="rewrite_unavailable",
        )
        if response.status_code in (404, 412):
            raise TransferPreconditionFailedError(
                "rewrite_precondition", status=response.status_code
            )
        if response.status_code != 200:
            raise TransferProviderUnavailableError(
                "rewrite_unavailable", status=response.status_code
            )
        # STRICT response shapes only (rereview blocker): `done` must be the
        # exact boolean; done=True demands a positive integer generation;
        # done=False demands a nonempty bounded string token. Anything else —
        # truthy strings, null tokens, negative generations, a top-level
        # list — cannot PROVE an adoption outcome and is AMBIGUOUS.
        try:
            payload = response.json()
        except ValueError:
            raise TransferProviderUnavailableError("rewrite_uncertain") from None
        if isinstance(payload, dict):
            done = payload.get("done")
            if done is True:
                resource = payload.get("resource")
                raw = resource.get("generation") if isinstance(resource, dict) else None
                # `isdecimal()` (not `isdigit()`): the latter is true for
                # unicode digits like "²"/"½" that int() then rejects with an
                # uncaught ValueError — every malformed shape must stay typed.
                if isinstance(raw, str) and raw.isdecimal():
                    raw = int(raw)
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    return RewriteResult(done=True, generation=raw)
            elif done is False:
                token = payload.get("rewriteToken")
                if (
                    isinstance(token, str)
                    and token
                    and len(token) <= MAX_CONTINUATION_LENGTH
                    and not _HEADER_VALUE_FORBIDDEN.search(token)
                ):
                    return RewriteResult(done=False, continuation=token)
        raise TransferProviderUnavailableError("rewrite_uncertain")

    # -- generation-pinned delete (GC / cleanup) ---------------------------

    async def delete_generation(self, object_name: str, generation: int) -> None:
        """Delete exactly one observed generation; a vanished object is
        success (idempotent cleanup), a changed one is a typed precondition
        failure the GC restats after grace — never a blind delete."""
        object_name = self._require_transfer_namespace(object_name)
        if generation <= 0:
            raise ValueError("delete requires a positive observed generation")
        response = await self._request(
            "DELETE",
            self._object_path(self._upload_endpoint, object_name),
            headers={"x-goog-if-generation-match": str(generation)},
            params={"generation": str(generation)},
            classification="delete_unavailable",
        )
        if response.status_code in (200, 204, 404):
            return
        if response.status_code == 412:
            raise TransferPreconditionFailedError(
                "delete_precondition", status=response.status_code
            )
        raise TransferProviderUnavailableError(
            "delete_unavailable", status=response.status_code
        )

    # -- generation-pinned V4 signed GET (§5.7) ----------------------------

    async def sign_generation_download(self, request: SignedDownloadRequest) -> str:
        """Mint one direct, generation-pinned signed GET target.

        FAIL CLOSED on every axis — signer error, missing signer, or an
        output that does not verify against the configured endpoint, bucket,
        exact generation, and semantic response parameters. Never a soft
        ``None``: the wire surface (packet 4) maps failures to
        ``503 DOWNLOAD_SIGNING_UNAVAILABLE`` with no stream fallback."""
        object_name = self._require_object_name(
            request.object_name, prefix=self._immutable_prefix
        )
        if not 0 < request.ttl_seconds <= MAX_DOWNLOAD_TTL_SECONDS:
            raise ValueError(
                f"download ttl must be within (0, {MAX_DOWNLOAD_TTL_SECONDS}]"
            )
        if not request.media_type:
            raise ValueError("a response content type is required")
        disposition = build_content_disposition("attachment", request.filename)
        url = await _run_url_signer(
            self._url_signer,
            bucket=self._bucket,
            object_name=object_name,
            generation=request.generation,
            ttl_seconds=request.ttl_seconds,
            media_type=request.media_type,
            disposition=disposition,
        )
        validate_signed_download_url(
            url,
            download_endpoint=self._download_endpoint,
            bucket=self._bucket,
            object_name=object_name,
            generation=request.generation,
            media_type=request.media_type,
            disposition=disposition,
            ttl_seconds=request.ttl_seconds,
            now=self._clock(),
        )
        return url


# The CLOSED set of query keys a valid V4 signed GET carries here: the
# three semantic parameters this design pins plus the complete mandatory
# V4 field set — each exactly once, nothing else.
_SIGNED_QUERY_KEYS = (
    "generation",
    "response-content-type",
    "response-content-disposition",
    "X-Goog-Algorithm",
    "X-Goog-Credential",
    "X-Goog-Date",
    "X-Goog-Expires",
    "X-Goog-SignedHeaders",
    "X-Goog-Signature",
)

# The CLOSED query-key set of a V4-signed initiation URL: exactly the
# mandatory V4 field set. No semantic parameters — the initiation's
# semantics (method, resumable intent, content type, marker) ride in the
# SIGNED HEADERS, and no `upload_id` — that parameter belongs to the
# session URI GCS mints in the Location of the client's POST.
_INITIATION_QUERY_KEYS = (
    "X-Goog-Algorithm",
    "X-Goog-Credential",
    "X-Goog-Date",
    "X-Goog-Expires",
    "X-Goog-SignedHeaders",
    "X-Goog-Signature",
)

# Bound on a signed URL accepted at this boundary — real V4 URLs with a
# 2048-bit RSA signature and a long disposition sit well under 4 KiB; an
# unbounded value has no honest use and must be refused before parsing.
MAX_SIGNED_URL_LENGTH = 8192

# Raw bytes a signed URL may never carry: controls, space, DEL. Checked on
# the RAW string BEFORE urlsplit, which would otherwise silently strip
# leading/embedded newlines and normalize the comparison away.
_URL_FORBIDDEN = re.compile(r"[\x00-\x20\x7f]")

# GCS V4 credential scope: AUTHORIZER/DATE/auto/storage/goog4_request with
# a nonempty bounded identity and the scope date equal to X-Goog-Date's
# calendar date (validated against the captured group).
_V4_CREDENTIAL = re.compile(r"[^/\s]{1,128}/(\d{8})/auto/storage/goog4_request")

# A provider RSA signature: ASCII hex, even length. 512 hex chars for the
# 2048-bit service-account keys signBlob uses today; the bound admits
# 1024–8192-bit keys without accepting a token-sized stub.
_V4_SIGNATURE = re.compile(r"[0-9a-fA-F]{256,2048}")

# Validation tolerance between this service's clock and the signer's
# signing instant. A protocol-validation constant like
# PROVIDER_TIMEOUT_SECONDS — not a B8 launch value: it bounds how stale or
# future a signer's OWN timestamp may be, not any client-facing policy.
DOWNLOAD_SIGNING_SKEW_SECONDS = 60


async def _run_url_signer(
    url_signer: Callable[..., str] | None,
    *,
    bucket: str,
    object_name: str,
    generation: int,
    ttl_seconds: int,
    media_type: str,
    disposition: str,
    timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
) -> str:
    """Invoke the V4 signing callable off the event loop, exceptions
    SEVERED and the wait BOUNDED: the production signer does blocking
    credential/IAM-signBlob work whose library-side retries can outlive any
    caller, and its failures may interpolate hosts or credential sources.

    The deadline reuses the adapter's provider-timeout authority. A timed
    out worker thread cannot be killed, but its eventual return value is
    simply discarded by the cancelled awaiter — it mutates no shared or
    durable state (the production signer touches only the idempotent
    credential cache), so a late signature can never alter a response or
    row."""
    if url_signer is None:
        raise DownloadSigningUnavailableError("signer_not_configured")
    # Raise OUTSIDE the except handlers (the `_request` pattern): `from
    # None` only suppresses display — it leaves `__context__` pointing at
    # the original signer exception, whose message can interpolate a
    # credential host. Severing at the source keeps the typed error's
    # chain empty for any future formatter or introspection.
    url: str | None = None
    failure: str | None = None
    try:
        url = await asyncio.wait_for(
            asyncio.to_thread(
                url_signer,
                bucket,
                object_name,
                generation,
                ttl_seconds,
                media_type,
                disposition,
            ),
            timeout_seconds,
        )
    except TimeoutError:
        failure = "signing_timeout"
    except Exception:
        failure = "signing_failed"
    if failure is not None or url is None:
        raise DownloadSigningUnavailableError(failure or "signing_failed")
    return url


async def _run_initiation_signer(
    initiation_signer: Callable[..., str] | None,
    *,
    bucket: str,
    object_name: str,
    content_type: str,
    adoption_marker: str,
    ttl_seconds: int,
    timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
) -> str:
    """The ``_run_url_signer`` posture for the initiation signer: invoked
    off the event loop, exceptions SEVERED at the source (a signer failure
    can interpolate credential hosts), and the wait BOUNDED by the
    adapter's provider-timeout authority. A timed-out worker thread's late
    return value is discarded by the cancelled awaiter and mutates nothing
    durable — signing is pure computation."""
    if initiation_signer is None:
        raise InitiationSigningUnavailableError("signer_not_configured")
    url: str | None = None
    failure: str | None = None
    try:
        url = await asyncio.wait_for(
            asyncio.to_thread(
                initiation_signer,
                bucket,
                object_name,
                content_type,
                adoption_marker,
                ttl_seconds,
            ),
            timeout_seconds,
        )
    except TimeoutError:
        failure = "signing_timeout"
    except Exception:
        failure = "signing_failed"
    if failure is not None or url is None:
        raise InitiationSigningUnavailableError(failure or "signing_failed")
    return url


def validate_download_endpoint(value: str, *, setting: str) -> str:
    """The HTTPS-only origin rule for the download-capability boundary.

    Deliberately STRICTER than the shared ``validate_exact_origin`` (which
    keeps its loopback-HTTP exception for the upload/emulator surface): a
    signed download target is a disclosed bearer, and the accepted contract
    generates only HTTPS V4 GET targets — a plaintext loopback bearer is
    not a useful development exception, it is a leak."""
    origin = validate_exact_origin(value, setting=setting)
    if not origin.startswith("https://"):
        raise ValueError(
            f"{setting} must be https — signed download bearers are "
            "HTTPS-only, loopback included"
        )
    return origin


def validate_signed_download_url(
    url: str,
    *,
    download_endpoint: str,
    bucket: str,
    object_name: str,
    generation: int,
    media_type: str,
    disposition: str,
    ttl_seconds: int,
    now: datetime,
) -> None:
    """§8: validate our own signing helper's output before returning it —
    a CLOSED V4 grammar, not field presence.

    Rereview blockers (both task-4 reports): the previous membership/
    nonempty checks accepted extra signed headers (contradicting
    ``required_headers: {}``), garbage credentials, one-character
    signatures, decades-stale or future signing instants, and raw control
    characters that urlsplit silently strips. This validator therefore:

      * bounds and pre-screens the RAW url (length, ASCII, no controls)
        before any parsing can normalize hostile bytes away;
      * requires ``https`` unconditionally — independent of the endpoint
        configuration, which is itself HTTPS-only at this boundary;
      * requires the closed literal query-key set, each key exactly once,
        raw wire tokens equal to their decoded forms;
      * requires ``X-Goog-SignedHeaders`` to be exactly ``host`` — any
        extra signed header would oblige the caller to send request
        headers the public response says are not required;
      * parses the credential scope closed
        (identity/DATE/auto/storage/goog4_request) with the scope date
        equal to ``X-Goog-Date``'s calendar date;
      * requires a canonical ASCII-hex, even-length, provider-sized RSA
        signature;
      * requires the signing instant to be FRESH against the injected
        clock (± the documented small skew) and the resulting expiry to be
        in the future and within ``ttl_seconds`` + skew — a stale target
        is a knowingly dead capability, a future-dated one a lifetime the
        configured short TTL never granted; and
      * keeps the exact host/bucket/path/generation/type/disposition and
        expiry==TTL pins.

    Module-level and endpoint/bucket-parameterized because two surfaces
    share it: the transfer adapter's ``sign_generation_download`` and the
    packet-4 ``GenerationDownloadSigner`` covering both content buckets."""
    if (
        len(url) > MAX_SIGNED_URL_LENGTH
        or not url.isascii()
        or _URL_FORBIDDEN.search(url)
    ):
        raise DownloadSigningUnavailableError("signed_url_invalid")
    expected = urllib.parse.urlsplit(download_endpoint)
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    keys = [key for key, _ in pairs]
    # The DECODED keys must also be the LITERAL wire tokens (review
    # hardening): parse_qsl decodes percent-escapes, so a raw
    # `%67eneration=` key would otherwise satisfy the closed key set while
    # the URL literally carries no `generation=` parameter.
    raw_keys = (
        [token.partition("=")[0] for token in parts.query.split("&")]
        if parts.query
        else []
    )
    query = dict(pairs)
    expires = query.get("X-Goog-Expires", "")
    date_value = query.get("X-Goog-Date", "")
    signed_at = None
    if _V4_DATE.fullmatch(date_value):
        try:
            # Regex shape AND a real calendar instant (a regex-valid
            # `20261399T996099Z` must be the typed refusal, never an
            # escaping strptime ValueError).
            signed_at = datetime.strptime(
                date_value, "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=UTC)
        except ValueError:
            signed_at = None
    # ASCII-only digits: `isdigit()` alone admits unicode digits like "²"
    # that int() then rejects with an escaping ValueError. The length bound
    # matters too: int() itself raises past 4300 digits, so an absurd
    # digit string must be refused before conversion (12 digits comfortably
    # covers any honest TTL).
    expires_ok = (
        len(expires) <= 12
        and expires.isascii()
        and expires.isdigit()
        and int(expires) == ttl_seconds
    )
    fresh = False
    if signed_at is not None and expires_ok:
        skew = timedelta(seconds=DOWNLOAD_SIGNING_SKEW_SECONDS)
        expires_at = signed_at + timedelta(seconds=int(expires))
        fresh = (
            now - skew <= signed_at <= now + skew
            and expires_at > now
            # Implied by expires==ttl plus the signed_at upper bound, kept
            # as an explicit belt so the lifetime cap survives independent
            # edits to either term.
            and expires_at <= now + timedelta(seconds=ttl_seconds) + skew
        )
    credential_match = _V4_CREDENTIAL.fullmatch(
        query.get("X-Goog-Credential", "")
    )
    signature = query.get("X-Goog-Signature", "")
    ok = (
        parts.scheme == "https"
        and parts.netloc == expected.netloc
        and not parts.fragment
        and "%2f" not in parts.path.lower()
        and urllib.parse.unquote(parts.path) == f"/{bucket}/{object_name}"
        and sorted(keys) == sorted(_SIGNED_QUERY_KEYS)
        and raw_keys == keys
        and query["generation"] == str(generation)
        and query["response-content-type"] == media_type
        and query["response-content-disposition"] == disposition
        and query["X-Goog-Algorithm"] == "GOOG4-RSA-SHA256"
        and credential_match is not None
        and credential_match.group(1) == date_value[:8]
        # Exactly `host`: the public response promises required_headers={}
        # and GCS demands every signed header, so ANY additional signed
        # header is an unusable-as-advertised capability.
        and query["X-Goog-SignedHeaders"] == "host"
        and _V4_SIGNATURE.fullmatch(signature) is not None
        and len(signature) % 2 == 0
        and fresh
    )
    if not ok:
        raise DownloadSigningUnavailableError("signed_url_invalid")


def validate_signed_initiation_url(
    url: str,
    *,
    upload_endpoint: str,
    bucket: str,
    object_name: str,
    ttl_seconds: int,
    now: datetime,
) -> None:
    """Validate our own initiation signer's output before disclosure —
    the ``validate_signed_download_url`` posture applied to the 2026-08-20
    amendment's credential class:

      * bounds and pre-screens the RAW url (length, ASCII, no controls)
        before any parsing can normalize hostile bytes away;
      * requires the CONFIGURED upload endpoint exactly (scheme + netloc —
        which keeps the loopback-HTTP emulator exception exactly where the
        endpoint validator already grants it, and nowhere else);
      * requires the exact ``/{bucket}/{object}`` path, no fragment, no
        encoded-slash ambiguity;
      * requires the closed 6-key V4 query set, each key exactly once, raw
        wire tokens equal to their decoded forms;
      * requires ``X-Goog-SignedHeaders`` to be EXACTLY
        ``content-type;host;x-goog-meta-adoption-marker;x-goog-resumable``
        — a URL signed over any other header set is a credential with
        different powers than the contract disclosed (replayable as a
        plain PUT, or with forgeable metadata) and is refused;
      * parses the credential scope closed with the scope date equal to
        ``X-Goog-Date``'s calendar date; canonical hex, even-length,
        provider-sized signature;
      * requires the signing instant FRESH (± the signing skew) and the
        expiry in the future and EQUAL to ``ttl_seconds`` — the minutes-
        scale lifetime is the credential's defining bound.

    Failures are the typed fail-closed ``InitiationSigningUnavailableError``
    (503 TRANSFER_UNAVAILABLE at the wire; the session stays retryable)."""
    if (
        len(url) > MAX_SIGNED_URL_LENGTH
        or not url.isascii()
        or _URL_FORBIDDEN.search(url)
    ):
        raise InitiationSigningUnavailableError("signed_url_invalid")
    expected = urllib.parse.urlsplit(upload_endpoint)
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    keys = [key for key, _ in pairs]
    raw_keys = (
        [token.partition("=")[0] for token in parts.query.split("&")]
        if parts.query
        else []
    )
    query = dict(pairs)
    expires = query.get("X-Goog-Expires", "")
    date_value = query.get("X-Goog-Date", "")
    signed_at = None
    if _V4_DATE.fullmatch(date_value):
        try:
            signed_at = datetime.strptime(
                date_value, "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=UTC)
        except ValueError:
            signed_at = None
    expires_ok = (
        len(expires) <= 12
        and expires.isascii()
        and expires.isdigit()
        and int(expires) == ttl_seconds
    )
    fresh = False
    if signed_at is not None and expires_ok:
        skew = timedelta(seconds=DOWNLOAD_SIGNING_SKEW_SECONDS)
        expires_at = signed_at + timedelta(seconds=int(expires))
        fresh = (
            now - skew <= signed_at <= now + skew
            and expires_at > now
            and expires_at <= now + timedelta(seconds=ttl_seconds) + skew
        )
    credential_match = _V4_CREDENTIAL.fullmatch(
        query.get("X-Goog-Credential", "")
    )
    signature = query.get("X-Goog-Signature", "")
    ok = (
        parts.scheme == expected.scheme
        and parts.netloc == expected.netloc
        and not parts.fragment
        and "%2f" not in parts.path.lower()
        and urllib.parse.unquote(parts.path) == f"/{bucket}/{object_name}"
        and sorted(keys) == sorted(_INITIATION_QUERY_KEYS)
        and raw_keys == keys
        and query["X-Goog-Algorithm"] == "GOOG4-RSA-SHA256"
        and credential_match is not None
        and credential_match.group(1) == date_value[:8]
        and query["X-Goog-SignedHeaders"] == INITIATION_SIGNED_HEADERS
        and _V4_SIGNATURE.fullmatch(signature) is not None
        and len(signature) % 2 == 0
        and fresh
    )
    if not ok:
        raise InitiationSigningUnavailableError("signed_url_invalid")


def _signed_url_expiry(url: str) -> datetime:
    """The instant a VALIDATED signed URL stops working: its own signing
    time plus its own signed expiry — never a server-clock guess. Callable
    only after ``validate_signed_download_url`` proved both fields."""
    query = dict(
        urllib.parse.parse_qsl(
            urllib.parse.urlsplit(url).query, keep_blank_values=True
        )
    )
    signed_at = datetime.strptime(
        query["X-Goog-Date"], "%Y%m%dT%H%M%SZ"
    ).replace(tzinfo=UTC)
    return signed_at + timedelta(seconds=int(query["X-Goog-Expires"]))


@dataclass(frozen=True)
class SignedCapability:
    """One validated, generation-pinned signed GET target (B3 §5.7).

    ``expires_at`` is derived from the signed URL's own X-Goog-Date +
    X-Goog-Expires so the product response can never claim a lifetime the
    external bearer does not have."""

    url: str
    disposition: str
    expires_at: datetime


class GenerationDownloadSigner:
    """The packet-4 download-capability signer.

    Distinct from ``XmlTransferStorage`` (which is scoped to the single
    transfer bucket) because committed version rows live in TWO configured
    namespaces: inline SHA-256 CAS content in the artifact bucket and
    adopted direct content under the transfer bucket's immutable prefix.
    ``namespaces`` is that CLOSED bucket → required-prefix map — a persisted
    coordinate outside it is never signed, host-only trust is never enough,
    and every failure is the typed fail-closed
    ``DownloadSigningUnavailableError`` (503 at the wire, no fallback)."""

    def __init__(
        self,
        *,
        download_endpoint: str,
        namespaces: Mapping[str, str],
        url_signer: Callable[..., str] | None = None,
        clock: Callable[[], datetime] | None = None,
        signer_timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        if not namespaces:
            raise ValueError("a download signer needs at least one namespace")
        for bucket, prefix in namespaces.items():
            if not _BUCKET_NAME.fullmatch(bucket):
                raise ValueError("download bucket must be a valid GCS bucket name")
            if (
                not prefix
                or not prefix.endswith("/")
                or prefix.startswith("/")
                or "//" in prefix
                or _OBJECT_NAME_FORBIDDEN.search(prefix)
            ):
                raise ValueError(
                    "download namespace prefix must be a relative, "
                    "slash-terminated namespace"
                )
        # HTTPS-only, loopback included — a bearer target is never plaintext.
        self._download_endpoint = validate_download_endpoint(
            download_endpoint, setting="download_endpoint"
        )
        # The deadline must fit inside the freshness skew: a signer allowed
        # to run longer than the skew could return a SUCCESSFUL signature
        # whose signing instant the validator then refuses as stale — a
        # silent 503 on a healthy path.
        if signer_timeout_seconds > DOWNLOAD_SIGNING_SKEW_SECONDS:
            raise ValueError(
                "signer_timeout_seconds must not exceed "
                f"DOWNLOAD_SIGNING_SKEW_SECONDS ({DOWNLOAD_SIGNING_SKEW_SECONDS})"
            )
        self._namespaces = dict(namespaces)
        self._url_signer = url_signer
        # Injectable UTC clock: the freshness window in the validator needs
        # a testable "now"; production uses the real clock.
        self._clock = clock or (lambda: datetime.now(UTC))
        self._signer_timeout_seconds = signer_timeout_seconds

    async def sign_capability(
        self,
        *,
        bucket: str,
        object_name: str,
        generation: int,
        media_type: str,
        filename: str,
        ttl_seconds: int,
    ) -> SignedCapability:
        """Mint one validated signed GET for a persisted coordinate triple.

        Every check fails CLOSED as ``DownloadSigningUnavailableError`` —
        the coordinates come from durable rows, not callers, so a violation
        here is unavailable configuration/state, never a 4xx."""
        prefix = self._namespaces.get(bucket)
        if prefix is None:
            raise DownloadSigningUnavailableError("bucket_not_configured")
        # The persisted name must be a REAL member of the namespace, not
        # the namespace marker itself and not an ambiguous form: reject a
        # bare/short prefix match, empty segments (`cas//x`, trailing
        # slash), dot segments, percent/backslash ambiguity, traversal,
        # and control characters. Server-generated keys never contain any
        # of these; a row that does is corruption, and the signer is the
        # last fail-closed boundary over persisted coordinates.
        segments = object_name.split("/")
        if (
            not object_name.startswith(prefix)
            or len(object_name) <= len(prefix)
            or "" in segments
            or "." in segments
            or ".." in segments
            or "%" in object_name
            or "\\" in object_name
            or _OBJECT_NAME_FORBIDDEN.search(object_name)
        ):
            raise DownloadSigningUnavailableError("object_outside_namespace")
        if generation <= 0:
            raise DownloadSigningUnavailableError("generation_unavailable")
        if not media_type or _HEADER_VALUE_FORBIDDEN.search(media_type):
            raise DownloadSigningUnavailableError("media_type_invalid")
        if not 0 < ttl_seconds <= MAX_DOWNLOAD_TTL_SECONDS:
            raise DownloadSigningUnavailableError("ttl_out_of_bounds")
        disposition = build_content_disposition("attachment", filename)
        url = await _run_url_signer(
            self._url_signer,
            bucket=bucket,
            object_name=object_name,
            generation=generation,
            ttl_seconds=ttl_seconds,
            media_type=media_type,
            disposition=disposition,
            timeout_seconds=self._signer_timeout_seconds,
        )
        try:
            validate_signed_download_url(
                url,
                download_endpoint=self._download_endpoint,
                bucket=bucket,
                object_name=object_name,
                generation=generation,
                media_type=media_type,
                disposition=disposition,
                ttl_seconds=ttl_seconds,
                now=self._clock(),
            )
            expires_at = _signed_url_expiry(url)
        except DownloadSigningUnavailableError:
            raise
        except Exception:
            # Belt under the validator's own checks: hostile signer OUTPUT
            # must never surface as anything but the typed refusal.
            raise DownloadSigningUnavailableError("signed_url_invalid") from None
        return SignedCapability(
            url=url,
            disposition=disposition,
            expires_at=expires_at,
        )


def _production_url_signer(
    bucket: str,
    object_name: str,
    generation: int,
    ttl_seconds: int,
    media_type: str,
    disposition: str,
) -> str:
    """The production V4 signing callable (blocking; run via
    ``_run_url_signer``'s thread).

    Signs WITHOUT a local private key by delegating to the IAM Credentials
    API (runtime SA email + fresh access token → ``signBlob``), the same
    identity path as the legacy artifact signer — but HARD-FAILING where
    that one returned a soft ``None``: any missing identity, emulator, or
    provider failure raises and becomes the typed fail-closed error. The
    emulator refusal is honest, not a shortcut — fake-gcs cannot verify a
    real V4 signature, so a "signed" URL against it would be a lie."""
    from .config import settings

    if settings.gcs_emulator_host:
        raise RuntimeError("the GCS emulator cannot verify V4 signatures")
    from .storage import gcs as artifact_storage

    creds = artifact_storage._get_signing_creds()
    sa_email = getattr(creds, "service_account_email", None)
    token = getattr(creds, "token", None)
    if not sa_email or not token:
        raise RuntimeError("ADC has no IAM signing identity")
    blob = artifact_storage._client_singleton().bucket(bucket).blob(object_name)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=ttl_seconds),
        method="GET",
        generation=generation,
        response_type=media_type,
        response_disposition=disposition,
        api_access_endpoint=settings.direct_transfer_download_endpoint,
        service_account_email=sa_email,
        access_token=token,
    )


def _production_initiation_signer(
    bucket: str,
    object_name: str,
    content_type: str,
    adoption_marker: str,
    ttl_seconds: int,
) -> str:
    """The production V4 initiation-signing callable (blocking; run via
    ``_run_initiation_signer``'s thread).

    ``method="RESUMABLE"`` is the library's documented initiation form: it
    signs the request as a POST with ``x-goog-resumable: start`` in the
    signed headers; ``content_type`` and the adoption-marker metadata
    header join the signed set, which is exactly what pins the credential
    to this session's declared identity. Same IAM-``signBlob`` identity
    path as the download signer, same hard-fail posture."""
    from .config import settings

    if settings.gcs_emulator_host:
        raise RuntimeError("the GCS emulator cannot verify V4 signatures")
    from .storage import gcs as artifact_storage

    creds = artifact_storage._get_signing_creds()
    sa_email = getattr(creds, "service_account_email", None)
    token = getattr(creds, "token", None)
    if not sa_email or not token:
        raise RuntimeError("ADC has no IAM signing identity")
    blob = artifact_storage._client_singleton().bucket(bucket).blob(object_name)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=ttl_seconds),
        method="RESUMABLE",
        content_type=content_type,
        headers={f"x-goog-meta-{ADOPTION_MARKER_METADATA_KEY}": adoption_marker},
        api_access_endpoint=settings.direct_transfer_upload_endpoint,
        service_account_email=sa_email,
        access_token=token,
    )


def _emulator_initiation_signer(
    bucket: str,
    object_name: str,
    content_type: str,
    adoption_marker: str,
    ttl_seconds: int,
) -> str:
    """The local-emulator initiation signer: a SHAPE-only V4 URL against
    the configured (loopback) upload endpoint. fake-gcs verifies no
    signatures, and the POST it receives is byte-identical to the one the
    old server-side path sent — unlike a download, where a fake signature
    would be a lie (the emulator refusal there stands), an unauthenticated
    local initiation is exactly what the emulator lane always did. Only
    wired when ``gcs_emulator_host`` is set; the production signer above
    refuses that configuration outright."""
    del content_type, adoption_marker  # signed by shape only — see above
    from .config import settings

    date_value = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    query = urllib.parse.urlencode(
        [
            ("X-Goog-Algorithm", "GOOG4-RSA-SHA256"),
            (
                "X-Goog-Credential",
                f"emulator@local.test/{date_value[:8]}/auto/storage/goog4_request",
            ),
            ("X-Goog-Date", date_value),
            ("X-Goog-Expires", str(ttl_seconds)),
            ("X-Goog-SignedHeaders", INITIATION_SIGNED_HEADERS),
            ("X-Goog-Signature", "ab" * 256),
        ],
        quote_via=urllib.parse.quote,
    )
    endpoint = settings.direct_transfer_upload_endpoint
    path = urllib.parse.quote(object_name, safe="/")
    return f"{endpoint}/{bucket}/{path}?{query}"


def build_download_signer() -> GenerationDownloadSigner:
    """Construct the configured capability signer from trusted deployment
    settings: the artifact bucket's CAS namespace plus the dedicated
    transfer bucket's immutable namespace — the complete closed set of
    places a committed version row's coordinates may point. Only callable
    when the transfer configuration is complete (boot validation guarantees
    that whenever ``direct_transfer_enabled`` is true)."""
    from .config import settings
    from .storage import CAS_PREFIX

    return GenerationDownloadSigner(
        download_endpoint=settings.direct_transfer_download_endpoint,
        namespaces={
            settings.gcs_bucket: CAS_PREFIX,
            settings.direct_transfer_bucket: settings.direct_transfer_immutable_prefix,
        },
        url_signer=_production_url_signer,
    )


def build_transfer_storage() -> XmlTransferStorage:
    """Construct the configured adapter from trusted deployment settings.

    Shared by the API router and the GC job so both surfaces speak to the
    same bucket with the same posture. Only callable when the transfer
    configuration is complete (boot validation guarantees that whenever
    ``direct_transfer_enabled`` is true)."""
    from .config import settings

    token_provider = None
    if not settings.gcs_emulator_host:
        async def token_provider() -> str | None:  # pragma: no cover - prod path
            import asyncio

            def _fetch() -> str | None:
                import google.auth
                import google.auth.transport.requests

                credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
                )
                credentials.refresh(google.auth.transport.requests.Request())
                return credentials.token

            return await asyncio.to_thread(_fetch)

    return XmlTransferStorage(
        bucket=settings.direct_transfer_bucket,
        upload_endpoint=settings.direct_transfer_upload_endpoint,
        download_endpoint=settings.direct_transfer_download_endpoint,
        canonical_origin=settings.direct_transfer_canonical_browser_origin,
        scratch_prefix=settings.direct_transfer_scratch_prefix,
        immutable_prefix=settings.direct_transfer_immutable_prefix,
        client=httpx.AsyncClient(),
        token_provider=token_provider,
        url_signer=_production_url_signer,
        initiation_signer=(
            _emulator_initiation_signer
            if settings.gcs_emulator_host
            else _production_initiation_signer
        ),
    )
