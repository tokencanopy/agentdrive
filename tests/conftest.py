"""Shared pytest infrastructure.

Two layers of tests:
  * Pure unit tests (test_paths, test_snippets, test_folders, the original
    test_extract / test_render / test_renderers / test_schemas / test_sessions)
    — no fixtures needed, always runnable.
  * Integration tests (test_integration) — depend on the `client` and `drive`
    fixtures, which require Postgres on localhost:5432 and fake-gcs-server
    on :4443. If Postgres isn't reachable, the integration tests skip cleanly.

Tests run against a per-invocation database (`agentdrive_test_<pid>`)
so the dev data in `agentdrive` is never touched AND concurrent pytest
runs never share state. The DB is created + migrated at session start
and dropped at session end; orphans from killed runs are reaped by the
next run. Set AGENTDRIVE_TEST_DB to pin a fixed name instead (that DB
is then left in place after the run, for inspection).
"""

# Environment override MUST happen before any `import agentdrive`.
import contextlib
import json
import os
import secrets
import time

_DEV_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://agentdrive:dev@localhost:5432/agentdrive"
)
_HOST_CREDS = _DEV_DB_URL.rsplit("/", 1)[0]
# Every pytest invocation gets its own database: two runners sharing
# one test DB TRUNCATE each other's fixtures mid-test — the failures
# look like flakes (multi-second lock-wait "slowness", cross-process
# corruption) but are really two suites interleaving. Keying the name
# to this process's PID isolates concurrent sessions, background
# full-suite runs, and future xdist workers (each worker is its own
# process) for free. An explicit AGENTDRIVE_TEST_DB pins a fixed name
# instead — that DB is exempt from the session-end drop and the orphan
# reaper, which is the keep-it-around-for-inspection escape hatch.
_TEST_DB_BASE = "agentdrive_test"
_TEST_DB_NAME = os.environ.get("AGENTDRIVE_TEST_DB")
_OWNS_TEST_DB = _TEST_DB_NAME is None  # auto-named ⇒ we create + drop it
if _TEST_DB_NAME is None:
    _TEST_DB_NAME = f"{_TEST_DB_BASE}_{os.getpid()}"
_TEST_DB_URL = f"{_HOST_CREDS}/{_TEST_DB_NAME}"
_ADMIN_DB_URL = f"{_HOST_CREDS}/postgres"

os.environ["DATABASE_URL"] = _TEST_DB_URL
# Same per-invocation isolation for blob storage: a fixed bucket name
# means concurrent suites interleave objects AND the emulator's state
# grows without bound (nothing ever deleted blobs — the shared bucket
# accumulated weeks of test artifacts and drove fake-gcs to 100% CPU).
# The app lifespan's ensure_store() creates the per-pid bucket; the
# session teardown deletes it; orphans are reaped at session start.
# An explicit GCS_BUCKET env var pins a name and opts out of all three.
_TEST_BUCKET_BASE = "agentdrive-test"


def _is_auto_bucket(name: str) -> bool:
    suffix = name.removeprefix(f"{_TEST_BUCKET_BASE}-")
    return suffix != name and suffix.isdigit()


# "GCS_BUCKET present in env" is NOT a reliable pin signal: under
# pytest-xdist the controller's conftest import sets its auto name,
# and every worker inherits it — workers would all share the
# controller's bucket and nobody would own the cleanup (the
# controller never runs the app lifespan). So an inherited *auto*
# name (exactly `agentdrive-test-<digits>`) is re-claimed per
# process; only a non-auto-shaped name counts as a pin.
_env_bucket = os.environ.get("GCS_BUCKET")
_OWNS_BUCKET = _env_bucket is None or _is_auto_bucket(_env_bucket)
if _OWNS_BUCKET:
    os.environ["GCS_BUCKET"] = f"{_TEST_BUCKET_BASE}-{os.getpid()}"
_TEST_BUCKET = os.environ["GCS_BUCKET"]
os.environ.setdefault("GCS_EMULATOR_HOST", "http://localhost:4443")

# The filesystem backend gets the same per-process isolation the bucket has:
# `STORAGE_BACKEND=fs STORAGE_FS_ROOT=/tmp/x` becomes `/tmp/x-<pid>` in every
# worker (an inherited `-<digits>` suffix is re-claimed, exactly like the
# auto bucket name), and the process removes its own root at exit. Without
# this, one worker's mark-sweep would list every other worker's objects.
if os.environ.get("STORAGE_BACKEND", "").strip().lower() == "fs":
    import atexit
    import re as _re
    import shutil

    _fs_base = _re.sub(r"-\d+$", "", os.environ.get("STORAGE_FS_ROOT", "").rstrip("/"))
    if _fs_base:
        _fs_root = f"{_fs_base}-{os.getpid()}"
        os.environ["STORAGE_FS_ROOT"] = _fs_root
        atexit.register(shutil.rmtree, _fs_root, True)
os.environ["SESSION_SECRET"] = secrets.token_urlsafe(48)
os.environ.setdefault("PUBLIC_BASE_URL", "http://test")
# The broad integration suite exercises the staged superset. Dedicated gate
# tests launch isolated processes with both values and prove production's
# fail-closed 51-operation surface separately.
os.environ.setdefault("SHEET_SESSIONS_ENABLED", "true")

# Now safe to import the rest. Agentdrive is imported only inside fixtures
# (lazy) so the env mutations above are guaranteed to be in effect.
import socket

import pytest
import pytest_asyncio


def _db_available() -> bool:
    try:
        host_port = _HOST_CREDS.rsplit("@", 1)[1]  # "localhost:5432"
        host, port_s = host_port.split(":")
        with socket.create_connection((host, int(port_s)), timeout=1):
            return True
    except (OSError, ValueError, IndexError):
        return False


# PID-keyed ephemeral DB families this suite creates. The reaper drops
# members whose owning process is dead. `agentdrive_schema_replay_<pid>`
# comes from test_schema_forward_compat's replay fixture.
_PID_DB_PREFIXES = (_TEST_DB_BASE, "agentdrive_schema_replay")


async def _reap_orphan_test_dbs(admin) -> None:
    """Drop PID-keyed test DBs (`<prefix>_<pid>`) whose owning pytest
    process is gone — leftovers from killed or crashed runs that never
    reached their own session-end drop. Liveness is a kill(pid, 0)
    probe: alive (or recycled to a live process, or owned by another
    user) ⇒ skip; the next run after that pid dies reaps it. Names with
    a non-numeric suffix are someone's pinned AGENTDRIVE_TEST_DB —
    never touched."""
    import asyncpg

    for prefix in _PID_DB_PREFIXES:
        rows = await admin.fetch(
            "SELECT datname FROM pg_database WHERE datname LIKE $1",
            f"{prefix}\\_%",
        )
        for row in rows:
            # Only exactly `<prefix>_<digits>` is ours to manage.
            suffix = row["datname"].removeprefix(f"{prefix}_")
            if not suffix.isdigit():
                continue
            try:
                os.kill(int(suffix), 0)  # liveness probe, delivers no signal
                continue
            except ProcessLookupError:
                pass  # owner gone — reap below
            except PermissionError:
                continue  # alive under another user
            # A failure here means a concurrent run's reaper won the
            # race — fine, nothing left to do.
            with contextlib.suppress(asyncpg.PostgresError):
                await admin.execute(
                    f'DROP DATABASE "{row["datname"]}" WITH (FORCE)'
                )


async def _ensure_test_db() -> None:
    """Create the test database if missing and bring it fully up to
    date via the SAME apply_all() the prod migrate job runs — pending
    migrations first, then the schema.sql baseline. One code path for
    every database means every integration test session exercises the
    runner. Also reaps orphaned per-pid test DBs from dead runs."""
    import asyncpg

    from agentdrive.scripts.apply_schema import apply_all

    admin = await asyncpg.connect(_ADMIN_DB_URL)
    try:
        await _reap_orphan_test_dbs(admin)
        exists = await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", _TEST_DB_NAME,
        )
        if not exists:
            # Identifier, not a value — can't be parameterized. The name
            # comes from our own env var with a safe default; quote it.
            await admin.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
    finally:
        await admin.close()

    await apply_all(_TEST_DB_URL)


async def _drop_own_test_db() -> None:
    """Session-end cleanup for the auto-named per-pid database. FORCE
    (PG13+) kicks any straggler connections so the drop can't hang
    behind a leaked pool conn. Runs that die before reaching this are
    covered by _reap_orphan_test_dbs() on the next invocation."""
    import asyncpg

    admin = await asyncpg.connect(_ADMIN_DB_URL)
    try:
        await admin.execute(f'DROP DATABASE "{_TEST_DB_NAME}" WITH (FORCE)')
    finally:
        await admin.close()


def _delete_bucket_sync(name: str) -> None:
    """Delete every blob in `name`, then the bucket itself. Individual
    blob deletes (not batch) — fake-gcs support for the batch endpoint
    is spotty across versions, and per-run buckets stay small enough
    that one-by-one is seconds, not minutes."""
    from agentdrive.storage import gcs as storage

    client = storage._client_singleton()
    bucket = client.bucket(name)
    if not bucket.exists():
        return
    for blob in client.list_blobs(name):
        blob.delete()
    bucket.delete()


def _reap_orphan_buckets_sync() -> None:
    """Bucket twin of _reap_orphan_test_dbs(): drop `agentdrive-test-
    <pid>` buckets whose owning pytest process is dead. Pinned bucket
    names (explicit GCS_BUCKET, or any non-numeric suffix — including
    the bare legacy `agentdrive-test`) are never touched."""
    from agentdrive.storage import gcs as storage

    client = storage._client_singleton()
    for bucket in client.list_buckets():
        suffix = bucket.name.removeprefix(f"{_TEST_BUCKET_BASE}-")
        if suffix == bucket.name or not suffix.isdigit():
            continue
        try:
            os.kill(int(suffix), 0)
            continue  # owner alive
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        # Concurrent reaper or mid-delete race — the next run retries.
        with contextlib.suppress(Exception):
            _delete_bucket_sync(bucket.name)


@pytest.fixture(autouse=True)
def _reset_v0_rate_limiter():
    """Drop the v0 per-principal rate-limit buckets before every test.

    The /v0 routers gate on a per-principal per-minute budget (api/v0_rate_limit,
    in-memory storage). The whole suite drives hundreds of requests as the same
    test principal, so without a reset the budget leaks across tests and later,
    unrelated tests start 429ing. The root `client` fixture also resets it (for
    tests that never reach this autouse), but this guarantees it for every test
    regardless of which HTTP fixture it uses.
    """
    from agentdrive.api.v0_rate_limit import reset as v0_rate_limit_reset
    from agentdrive.public.routes import reset_public_prefilter

    v0_rate_limit_reset()
    reset_public_prefilter()
    yield
    v0_rate_limit_reset()
    reset_public_prefilter()


@pytest_asyncio.fixture(autouse=True)
async def _reset_transfer_rate_windows():
    """Drop the direct-transfer rate windows before every test.

    Same leak the fixture above prevents, one storage layer down: those
    windows moved from a module dict into `transfer_rate_windows`, so
    clearing them is now DB work and cannot be done from the sync fixtures
    (`fake_storage`, `fake_signer`) that used to call the in-memory reset.

    Deliberately tolerant of there being no pool. This is autouse for the
    WHOLE suite, and most of it — renderers, sanitizers, id and schema
    tests — never starts the app. Depending on `app_with_lifespan` to
    guarantee a pool would make every one of those tests skip on a machine
    without Postgres instead of running as they do today.
    """
    from agentdrive import db

    async def _truncate() -> None:
        if db._pool is None:
            return
        from agentdrive.core.v0_transfer_rate import reset

        async with db.conn() as c:
            await reset(c)

    await _truncate()
    yield
    await _truncate()


@pytest_asyncio.fixture(scope="session")
async def app_with_lifespan():
    if not _db_available():
        pytest.skip(
            "Postgres on localhost:5432 not reachable "
            "(start with `docker compose up -d`)"
        )
    await _ensure_test_db()
    import asyncio

    await asyncio.to_thread(_reap_orphan_buckets_sync)
    from asgi_lifespan import LifespanManager

    from agentdrive.app import app

    async with LifespanManager(app):
        yield app

    # Pinned names (AGENTDRIVE_TEST_DB / GCS_BUCKET) survive the run for
    # inspection; auto-named per-pid resources are this run's to remove.
    if _OWNS_TEST_DB:
        await _drop_own_test_db()
    if _OWNS_BUCKET:
        await asyncio.to_thread(_delete_bucket_sync, _TEST_BUCKET)


# The pre-reset `client` fixture lived here. It was removed at the public-
# surface work (2026-08-07): it imported agentdrive.core.quota and
# core.usage (archived at the v0 reset) and truncated ten tables the day-0
# schema does not have, so it raised on first use. Nothing noticed because
# its only consumer was a module-skipped stub. v0 test files define their
# own `http` / `override_actor` / `_clean_tables` fixtures locally — copy
# that pattern (see tests/test_v0_shares.py) rather than reviving this.

# ---------------------------------------------------------------------------
# Hub product-token fixtures
#
# AgentDrive validates Hub-issued tokens offline against Hub's JWKS, so tests
# need a stand-in Hub that can sign — and a second, unrelated key so "signed
# by the wrong party" is a case we can actually exercise rather than assume.
# ---------------------------------------------------------------------------


class _FakeJwks:
    """An RSA keypair plus the public JWKS document a verifier would fetch."""

    def __init__(self, kid: str):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.kid = kid
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._algo = jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256)
        pub = json.loads(self._algo.to_jwk(self._private.public_key()))
        pub.update({"kid": kid, "use": "sig", "alg": "RS256"})
        self.public_jwks = {"keys": [pub]}

    def sign(self, claims: dict, *, kid: str | None = None) -> str:
        """`kid` overrides the header, so a foreign key can CLAIM Hub's kid.

        Without that override every foreign-key test is rejected at the kid
        lookup with zero calls to `jwt.decode` -- the signature check is never
        reached, and a test named for the trust model exercises none of it.
        """
        import jwt

        return jwt.encode(
            claims, self._private, algorithm="RS256",
            headers={"kid": kid or self.kid},
        )


@pytest.fixture(scope="session")
def hub_jwks():
    return _FakeJwks("hub-key-1")


@pytest.fixture(scope="session")
def rotated_hub_jwks():
    """A SECOND Hub key, as after a rotation: the old kid is retired and the
    new one (``hub-key-2``) is what Hub now signs with."""
    return _FakeJwks("hub-key-2")


@pytest.fixture(scope="session")
def foreign_jwks():
    """A key Hub does not publish — someone else's signature."""
    return _FakeJwks("not-hub-1")


def _hub_claims(**over):
    """Hub-shaped product-token claims against the REAL settings defaults.

    ``aud`` defaults to ``settings.hub_product_audience`` (and ``iss`` to
    ``settings.hub_issuer``) so the suite exercises the shipped product-token
    audience — never the archived sign-in client id ("agentdrive")."""
    from agentdrive.config import settings

    now = int(time.time())
    claims = {
        "iss": settings.hub_issuer,
        "sub": "tcagt_0000000000000001",
        "aud": settings.hub_product_audience,
        "scope": "drives:read drives:write content:read content:write",
        "iat": now,
        "exp": now + 3600,
        "jti": "tctok_0000000000000001",
        "workspace_id": "tcws_0000000000000001",
        "membership_id": "tcagm_0000000000000001",
        "client_id": "tccred_0000000000000001",
        "credential_id": "tccred_0000000000000001",
        "runtime_id": "tcrun_0000000000000001",
        "sponsor_id": "tcusr_0000000000000009",
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


@pytest.fixture
def hub_token(hub_jwks):
    """Mint a Hub-shaped bearer accepted by the /v0 verifier under the real
    settings defaults (issuer + `hub_product_audience`). Overrides pass
    through to the claims (e.g. ``aud="agentdrive"`` to exercise rejection)."""

    def _sign(**over) -> str:
        return hub_jwks.sign(_hub_claims(**over))

    return _sign


@pytest.fixture
def hub_claims():
    """The claims factory itself, so a test can sign with any key (e.g. the
    rotated one) while still exercising the real settings defaults."""
    return _hub_claims
