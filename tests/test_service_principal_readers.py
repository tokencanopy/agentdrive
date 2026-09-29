"""Readers and schema accept a Service principal BEFORE any writer creates one.

Token Canopy service account design (tokencanopy/tokencanopy#331) §7.1, §15
step 6. The ordering is not a preference: a widened writer meeting a check
constraint or a response model that still refuses `service` fails at INSERT or
at serialization, and either one takes down the whole request for whoever is
looking — including humans who have nothing to do with the integration.

So every row here is inserted DIRECTLY, bypassing the domain modules, which is
exactly how a Service row will arrive once the writers land. And the last
group proves the writers still refuse, so this commit widens readers WITHOUT
opening a path.
"""

from __future__ import annotations

import os
import socket

import pytest
import pytest_asyncio

WS = "tcws_0000000000000001"
SERVICE = "tcsvc_0000000000000001"


def _postgres_reachable() -> bool:
    host = os.environ.get("POSTGRES_TEST_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_TEST_PORT", "5432"))
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="local Postgres is not reachable"
)


@pytest_asyncio.fixture
async def conn(app_with_lifespan):
    import asyncpg

    connection = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(app_with_lifespan):
    """Truncate before AND after, like every other suite that writes rows.

    This module writes drives, artifacts and `artifact_versions` rows straight
    into the tables — by design, see the docstring above — and those version
    rows carry no `storage_bucket`/`storage_generation`. Until this fixture
    existed they were never removed, so every later test on the same xdist
    worker ran against a database in which `transfer_readiness()` counted
    unresolved rows, and the readiness-gated suites answered
    `503 TRANSFER_DISABLED` where they expected a 4xx (#537 hardened those
    suites to truncate on entry; this removes the leak at its source).
    """
    from agentdrive.db import conn as db_conn

    statement = (
        "TRUNCATE idempotency_records, storage_reservations, upload_sessions, "
        "workspace_storage, drives RESTART IDENTITY CASCADE"
    )
    async with db_conn() as c:
        await c.execute(statement)
    yield
    async with db_conn() as c:
        await c.execute(statement)


async def _drive(conn) -> tuple[str, str]:
    """A drive and its root folder, written straight to the tables.

    In ONE transaction: the drive -> root-folder FK is DEFERRABLE precisely so
    the pair can be created together, and outside a transaction it fires on
    the first statement.
    """
    from agentdrive.core.ids import new_id

    drive_id = new_id("drv")
    folder_id = new_id("fld")
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO drives (id, workspace_id, name, revision, root_folder_id) "
            "VALUES ($1, $2, 'svc', $3, $4)",
            drive_id, WS, new_id("rev"), folder_id,
        )
        await conn.execute(
            "INSERT INTO folders (id, drive_id, parent_id, name, revision) "
            "VALUES ($1, $2, NULL, NULL, $3)",
            folder_id, drive_id, new_id("rev"),
        )
    return drive_id, folder_id


# ---------------------------------------------------------------------------
# grants
# ---------------------------------------------------------------------------


async def test_a_service_grant_inserts(conn):
    from agentdrive.core.ids import new_id

    drive_id, _ = await _drive(conn)
    await conn.execute(
        "INSERT INTO grants (id, drive_id, resource_type, resource_id, "
        "principal_type, principal_id, role, revision) "
        "VALUES ($1, $2, 'drive', $3, 'service', $4, 'manager', $5)",
        new_id("grn"), drive_id, drive_id, SERVICE, new_id("rev"),
    )
    row = await conn.fetchrow(
        "SELECT principal_type, principal_id FROM grants WHERE drive_id = $1",
        drive_id,
    )
    assert row["principal_type"] == "service"
    assert row["principal_id"] == SERVICE


async def test_a_service_grant_must_name_a_tcsvc_principal(conn):
    """The id-shape constraint is per principal_type. A `service` grant naming
    a `tcagt_` id would be an agent's access recorded under the wrong kind."""
    import asyncpg

    from agentdrive.core.ids import new_id

    drive_id, _ = await _drive(conn)
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await conn.execute(
            "INSERT INTO grants (id, drive_id, resource_type, resource_id, "
            "principal_type, principal_id, role, revision) "
            "VALUES ($1, $2, 'drive', $3, 'service', 'tcagt_0000000000000001', "
            "'manager', $4)",
            new_id("grn"), drive_id, drive_id, new_id("rev"),
        )


async def test_a_service_grant_may_not_be_public_shaped(conn):
    import asyncpg

    from agentdrive.core.ids import new_id

    drive_id, _ = await _drive(conn)
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await conn.execute(
            "INSERT INTO grants (id, drive_id, resource_type, resource_id, "
            "principal_type, principal_id, role, revision) "
            "VALUES ($1, $2, 'drive', $3, 'service', NULL, 'viewer', $4)",
            new_id("grn"), drive_id, drive_id, new_id("rev"),
        )


@pytest.mark.parametrize(
    "principal_type,principal_id",
    [
        ("agent", "tcagt_0000000000000001"),
        ("user", "tcusr_0000000000000001"),
        ("workspace", WS),
        ("public", None),
    ],
)
async def test_the_existing_grant_principal_types_still_insert(
    conn, principal_type, principal_id
):
    from agentdrive.core.ids import new_id

    drive_id, _ = await _drive(conn)
    role = "viewer" if principal_type == "public" else "manager"
    await conn.execute(
        "INSERT INTO grants (id, drive_id, resource_type, resource_id, "
        "principal_type, principal_id, role, revision) "
        "VALUES ($1, $2, 'drive', $3, $4, $5, $6, $7)",
        new_id("grn"), drive_id, drive_id, principal_type, principal_id,
        role, new_id("rev"),
    )


# ---------------------------------------------------------------------------
# actor columns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("actor_type", ["agent", "user", "system", "service"])
async def test_drive_changes_accepts_every_actor_type(conn, actor_type):
    """`system` is the maintenance actor and is NOT dropped by the widening —
    the additive migration adds a value, it does not replace the set."""
    from agentdrive.core.ids import new_id

    drive_id, folder_id = await _drive(conn)
    await conn.execute(
        "INSERT INTO drive_changes (id, drive_id, sequence, change_set_id, "
        "type, actor_type, actor_id, resource_type, resource_id) "
        "VALUES ($1, $2, 1, $3, 'created', $4, $5, 'folder', $6)",
        new_id("chg"), drive_id, new_id("cset"), actor_type,
        SERVICE if actor_type == "service" else None, folder_id,
    )
    row = await conn.fetchrow(
        "SELECT actor_type FROM drive_changes WHERE drive_id = $1", drive_id
    )
    assert row["actor_type"] == actor_type


async def test_an_unknown_actor_type_is_still_refused(conn):
    """The widening is to a CLOSED set, not to anything."""
    import asyncpg

    from agentdrive.core.ids import new_id

    drive_id, folder_id = await _drive(conn)
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await conn.execute(
            "INSERT INTO drive_changes (id, drive_id, sequence, change_set_id, "
            "type, actor_type, actor_id, resource_type, resource_id) "
            "VALUES ($1, $2, 1, $3, 'created', 'robot', NULL, 'folder', $4)",
            new_id("chg"), drive_id, new_id("cset"), folder_id,
        )


async def test_artifact_versions_accepts_a_service_actor(conn):
    from agentdrive.core.ids import new_id

    drive_id, folder_id = await _drive(conn)
    artifact_id = new_id("art")
    await conn.execute(
        "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
        "VALUES ($1, $2, $3, 'a.txt', $4)",
        artifact_id, drive_id, folder_id, new_id("rev"),
    )
    version_id = new_id("ver")
    await conn.execute(
        "INSERT INTO artifact_versions (id, artifact_id, checksum, content_type, "
        "size_bytes, storage_object, actor_type, actor_id, ordinal) "
        "VALUES ($1, $2, 'sha256:0', 'text/plain', 0, $3, 'service', $4, 1)",
        version_id, artifact_id, f"objects/{version_id}", SERVICE,
    )
    row = await conn.fetchrow(
        "SELECT actor_type, actor_id FROM artifact_versions WHERE id = $1",
        version_id,
    )
    assert (row["actor_type"], row["actor_id"]) == ("service", SERVICE)


async def test_the_immutable_version_trigger_still_guards_actor_type(conn):
    """`actor_type` is in the append-only trigger's frozen column list. The
    widening must not have taken it out."""
    import asyncpg

    from agentdrive.core.ids import new_id

    drive_id, folder_id = await _drive(conn)
    artifact_id = new_id("art")
    await conn.execute(
        "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
        "VALUES ($1, $2, $3, 'a.txt', $4)",
        artifact_id, drive_id, folder_id, new_id("rev"),
    )
    version_id = new_id("ver")
    await conn.execute(
        "INSERT INTO artifact_versions (id, artifact_id, checksum, content_type, "
        "size_bytes, storage_object, actor_type, actor_id, ordinal) "
        "VALUES ($1, $2, 'sha256:0', 'text/plain', 0, $3, 'service', $4, 1)",
        version_id, artifact_id, f"objects/{version_id}", SERVICE,
    )
    with pytest.raises(asyncpg.RestrictViolationError):
        await conn.execute(
            "UPDATE artifact_versions SET actor_type = 'user' WHERE id = $1",
            version_id,
        )


# ---------------------------------------------------------------------------
# principal-bound sessions
# ---------------------------------------------------------------------------


async def test_a_direct_upload_session_accepts_a_service_principal(conn):
    """A Service holding the existing content-write scopes uses the standard
    large-upload path (§7.1)."""
    from agentdrive.core.ids import new_id

    drive_id, folder_id = await _drive(conn)
    await conn.execute(
        "INSERT INTO upload_sessions (id, workspace_id, drive_id, "
        "principal_type, principal_id, target_kind, parent_folder_id, "
        "artifact_name, declared_size_bytes, declared_media_type, "
        "declared_crc32c, adoption_marker, scratch_object, final_object, "
        "expires_at) "
        "VALUES ($1, $2, $3, 'service', $4, 'artifact', $5, 'a.txt', 1, "
        "'text/plain', 'AAAAAA==', 'marker', 'scratch', 'final', "
        "now() + interval '1 hour')",
        new_id("upld"), WS, drive_id, SERVICE, folder_id,
    )
    row = await conn.fetchrow(
        "SELECT principal_type FROM upload_sessions WHERE drive_id = $1", drive_id
    )
    assert row["principal_type"] == "service"


async def test_a_private_viewer_session_refuses_a_service_principal(conn):
    """Deliberately NOT widened (§7.1). A viewer session is a narrow browser
    console capability minted by the Human BFF path — it is not a backend
    service interface, and a Service Account has no browser."""
    import asyncpg

    from agentdrive.core.ids import new_id

    drive_id, folder_id = await _drive(conn)
    artifact_id = new_id("art")
    await conn.execute(
        "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
        "VALUES ($1, $2, $3, 'a.txt', $4)",
        artifact_id, drive_id, folder_id, new_id("rev"),
    )
    version_id = new_id("ver")
    await conn.execute(
        "INSERT INTO artifact_versions (id, artifact_id, checksum, content_type, "
        "size_bytes, storage_object, actor_type, actor_id, ordinal) "
        "VALUES ($1, $2, 'sha256:0', 'text/plain', 0, $3, 'user', "
        "'tcusr_0000000000000001', 1)",
        version_id, artifact_id, f"objects/{version_id}",
    )
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await conn.execute(
            "INSERT INTO viewer_sessions (id, drive_id, artifact_id, version_id, "
            "workspace_id, principal_type, principal_id, credential_hash, "
            "expires_at) VALUES ($1, $2, $3, $4, $5, 'service', $6, $7, "
            "now() + interval '1 hour')",
            new_id("vwr"), drive_id, artifact_id, version_id, WS, SERVICE,
            "hash-" + new_id("vwr"),
        )


# ---------------------------------------------------------------------------
# response models
# ---------------------------------------------------------------------------


def test_the_grant_model_accepts_a_service_principal_type():
    from agentdrive.api.v0_models import GrantOut

    grant = GrantOut.model_validate(
        {
            "id": "grn_0000000000000001",
            "drive_id": "drv_0000000000000001",
            "resource_type": "drive",
            "resource_id": "drv_0000000000000001",
            "principal_type": "service",
            "principal_id": SERVICE,
            "role": "manager",
            "revision": "rev_0000000000000001",
            "state": "active",
            "expires_at": None,
            "created_at": "2026-08-25T20:00:00Z",
            "revoked_at": None,
        }
    )
    assert grant.principal_type == "service"


def test_the_change_actor_model_accepts_a_service_actor():
    from agentdrive.api.v0_models import ChangeActorOut

    assert ChangeActorOut.model_validate({"type": "service", "id": SERVICE}).type == (
        "service"
    )


@pytest.mark.parametrize("kind", ["agent", "user", "system"])
def test_the_change_actor_model_keeps_the_existing_kinds(kind):
    from agentdrive.api.v0_models import ChangeActorOut

    assert ChangeActorOut.model_validate({"type": kind, "id": None}).type == kind


def test_the_change_actor_model_still_refuses_an_unknown_kind():
    import pydantic

    from agentdrive.api.v0_models import ChangeActorOut

    with pytest.raises(pydantic.ValidationError):
        ChangeActorOut.model_validate({"type": "robot", "id": None})


def test_the_grant_writer_accepts_service_in_its_type_list():
    from agentdrive.core.v0_grants import PRINCIPAL_TYPES

    assert "service" in PRINCIPAL_TYPES
    # Additive: the four that were there stay there.
    for existing in ("agent", "user", "workspace", "public"):
        assert existing in PRINCIPAL_TYPES


# ---------------------------------------------------------------------------
# no writer opened
# ---------------------------------------------------------------------------


async def test_the_grant_writer_still_refuses_a_service_principal(conn):
    """Widened readers, NO writer (§15 step 6 vs step 7).

    A `service` grant is now REPRESENTABLE — the check constraints accept one,
    the models render one — and still uncreatable through the API, because
    creating a grant needs manager authority on the target and a Service actor
    holds none: it matches no workspace grant (#500) and has no `service`
    grant of its own. The refusal is the ordinary non-oracle one, which is
    also the answer a stranger gets.
    """
    from agentdrive.core.v0_grants import GrantNotFoundError, create_grant
    from agentdrive.identity.actor import V0ActorContext

    drive_id, _ = await _drive(conn)
    actor = V0ActorContext(
        subject=SERVICE,
        subject_type="service",
        workspace_id=WS,
        membership_id=None,
        token_id="tctok_0000000000000001",
        scopes=frozenset({"sharing:write", "drives:read"}),
        credential_id="tck_0000000000000001",
    )

    # Target a USER grant, not a service one: a service TARGET is refused by
    # the create allowlist above, which would mask what this asserts — that a
    # service CALLER has no authority here either.
    with pytest.raises(GrantNotFoundError):
        await create_grant(
            conn,
            actor,
            drive_id,
            resource_type="drive",
            resource_id=drive_id,
            principal_type="user",
            principal_id="tcusr_0000000000000001",
            role="manager",
            expires_at=None,
        )


async def test_a_service_actor_reaches_no_drive_through_a_workspace_grant(conn):
    """The exclusion from #500, restated at the reader level now that a
    `service` grant is representable: a workspace grant covering this
    workspace still gives a Service nothing."""
    from agentdrive.core.ids import new_id

    drive_id, _ = await _drive(conn)
    await conn.execute(
        "INSERT INTO grants (id, drive_id, resource_type, resource_id, "
        "principal_type, principal_id, role, revision) "
        "VALUES ($1, $2, 'drive', $3, 'workspace', $4, 'manager', $5)",
        new_id("grn"), drive_id, drive_id, WS, new_id("rev"),
    )

    matched = await conn.fetchval(
        "SELECT _principal_matches('service', $1, $2, principal_type, principal_id) "
        "FROM grants WHERE drive_id = $3",
        SERVICE, WS, drive_id,
    )
    assert matched is False


async def test_a_human_manager_can_create_a_service_grant_as_of_this_slice(conn):
    """The reader slice gated this; the writer slice opens it.

    Representable-before-creatable was the point of the gate: a `service` row
    had to serialize everywhere before a human manager could make one, and the
    rules governing that access — explicit grants only, no workspace-grant
    match, the manager invariant — arrive with the custody slice. They are
    here now, so §7.3's "active Service Accounts are selectable in Drive
    sharing" is expressible.
    """
    from agentdrive.core.ids import new_id
    from agentdrive.core.v0_grants import create_grant
    from agentdrive.identity.actor import V0ActorContext

    drive_id, _ = await _drive(conn)
    human = "tcusr_0000000000000001"
    await conn.execute(
        "INSERT INTO grants (id, drive_id, resource_type, resource_id, "
        "principal_type, principal_id, role, revision) "
        "VALUES ($1, $2, 'drive', $3, 'user', $4, 'manager', $5)",
        new_id("grn"), drive_id, drive_id, human, new_id("rev"),
    )
    actor = V0ActorContext(
        subject=human,
        subject_type="user",
        workspace_id=WS,
        membership_id="tcmem_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset({"sharing:write", "drives:read"}),
        workspace_role="admin",
    )

    grant = await create_grant(
        conn, actor, drive_id,
        principal_type="service", principal_id=SERVICE,
        resource_type="drive", resource_id=drive_id,
        role="viewer", expires_at=None,
    )
    assert grant["principal_type"] == "service"


@pytest.mark.parametrize(
    "kind", ["agent", "user", "service", "workspace", "public"]
)
def test_every_principal_type_is_creatable_again(kind):
    from agentdrive.core.v0_grants import CREATABLE_PRINCIPAL_TYPES

    assert kind in CREATABLE_PRINCIPAL_TYPES
