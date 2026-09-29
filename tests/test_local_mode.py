"""AUTH_MODE=local end to end inside the process: the CLI mints an opaque
API key, the `/v0` boundary resolves it to the same actor a Hub token would
yield, revocation and expiry refuse it, the internal ingress introspects it
for the MCP sidecar, and the discovery document says what a self-hosted
install can honestly say.

Runs against the suite's real Postgres (the 0063/0064 tables) with the
settings flipped to local mode per test.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from agentdrive import keys as cli
from agentdrive.api import v0_deps
from agentdrive.api.v0_errors import V0ApiError
from agentdrive.config import settings
from agentdrive.db import conn
from agentdrive.identity.api_keys import KEY_PREFIX, display_id, generate_key, key_hash

ORIGIN = "http://test"


@pytest_asyncio.fixture
async def local_mode(app_with_lifespan, monkeypatch):
    """Flip the running settings into local mode on an empty identity plane."""
    monkeypatch.setattr(settings, "auth_mode", "local")
    v0_deps.reset()
    async with conn() as c:
        await c.execute("DELETE FROM local_api_keys")
        await c.execute("DELETE FROM local_principals")
    yield
    v0_deps.reset()


@pytest_asyncio.fixture
async def http(app_with_lifespan):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app_with_lifespan), base_url=ORIGIN) as ac:
        yield ac


def _create_args(**over) -> argparse.Namespace:
    base = {
        "subject_type": "agent",
        "name": "claude-code",
        "workspace": "default",
        "scopes": "all",
        "expires": None,
        "role": None,
        "subject": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


# ---- the CLI ----------------------------------------------------------------


async def test_init_mints_one_owner_per_workspace(local_mode):
    first = await cli._init("default", "owner")
    again = await cli._init("default", "owner")
    assert first["created"] is True and first["owner"].startswith("tcusr_")
    assert again == {"workspace_id": "default", "owner": first["owner"], "created": False}
    other = await cli._init("other", "owner")
    assert other["owner"] != first["owner"]


async def test_concurrent_inits_mint_exactly_one_owner(local_mode):
    results = await asyncio.gather(*(cli._init("race", "owner") for _ in range(4)))
    owners = {r["owner"] for r in results}
    assert len(owners) == 1
    assert sum(r["created"] for r in results) == 1
    async with conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM local_principals WHERE workspace_id = 'race' "
            "AND workspace_role = 'owner'"
        ) == 1


async def test_create_needs_an_owner_and_an_agent_key_sponsors_it(local_mode):
    with pytest.raises(SystemExit, match="has no owner yet"):
        await cli._create(_create_args())
    owner = (await cli._init("default", "owner"))["owner"]
    minted = await cli._create(_create_args())
    assert minted["principal_type"] == "agent"
    assert minted["subject"].startswith("tcagt_")
    assert minted["key"].startswith(KEY_PREFIX)
    assert minted["id"] == display_id(minted["key"])
    assert minted["expires_at"] is None, "keys do not expire unless --expires was given"
    # Through the real dependency: the key works, and its sponsor is the
    # owner `init` minted, which is what mints a sponsor grant on the drives
    # it creates.
    actor = await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert actor.subject == minted["subject"]
    assert actor.is_agent
    assert actor.sponsor_id == owner
    assert actor.workspace_id == "default"
    assert set(actor.scopes) == set(minted["scopes"])
    assert actor.token_id == minted["id"]


async def test_the_key_is_stored_only_as_a_hash(local_mode):
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())
    async with conn() as c:
        row = await c.fetchrow("SELECT * FROM local_api_keys WHERE id = $1", minted["id"])
    stored = " ".join(str(v) for v in row.values())
    assert minted["key"] not in stored
    assert row["key_hash"] == key_hash(minted["key"])
    # The id is a prefix of the key, not the key: 8 of 40 secret characters.
    assert minted["key"].startswith(row["id"])
    assert len(row["id"]) < len(minted["key"])


async def test_an_owner_user_key_is_a_workspace_admin(local_mode):
    owner = (await cli._init("default", "owner"))["owner"]
    minted = await cli._create(_create_args(subject_type="user", name="me", role="owner"))
    assert minted["subject"] == owner
    actor = await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert actor.subject_type == "user"
    assert actor.is_workspace_admin
    assert actor.sponsor_id is None and actor.credential_id is None
    member = await cli._create(_create_args(subject_type="user", name="them"))
    assert not (await v0_deps.resolve_actor(f"Bearer {member['key']}")).is_workspace_admin


async def test_role_is_refused_on_an_agent_key(local_mode):
    await cli._init("default", "owner")
    with pytest.raises(ValueError, match="--role applies to user keys"):
        await cli._create(_create_args(role="admin"))


async def test_a_plain_create_mints_its_own_principal(local_mode):
    await cli._init("default", "owner")
    first = await cli._create(_create_args())
    second = await cli._create(_create_args())
    assert first["subject"] != second["subject"]
    assert first["reused_subject"] is False


async def test_rotation_keeps_the_subject_so_its_grants_survive(local_mode):
    """`--subject` is what makes "revoke and reissue" — the ONLY sanctioned
    way to change a key's scopes (§8 decision 11), and the answer to a leak —
    a rotation rather than a replacement. Grants and drive ownership key on
    the subject, so minting a fresh one would silently orphan everything the
    agent owned, and there would be nothing to see afterwards but an agent
    that had lost its drives.

    Two keys for one agent are then two CREDENTIALS, exactly as at Hub: same
    subject, different `credential_id`, different `token_id`."""
    await cli._init("default", "owner")
    first = await cli._create(_create_args(scopes="drives:read"))
    rotated = await cli._create(
        _create_args(subject=first["subject"], scopes="drives:read,drives:write")
    )
    assert rotated["subject"] == first["subject"]
    assert rotated["reused_subject"] is True
    assert rotated["key"] != first["key"]
    a = await v0_deps.resolve_actor(f"Bearer {first['key']}")
    b = await v0_deps.resolve_actor(f"Bearer {rotated['key']}")
    assert a.subject == b.subject
    assert a.credential_id != b.credential_id
    assert a.token_id != b.token_id
    assert set(b.scopes) == {"drives:read", "drives:write"}
    # Retiring the old key leaves the new one — and the subject — working.
    await cli._revoke(first["id"])
    assert (await v0_deps.resolve_actor(f"Bearer {rotated['key']}")).subject == a.subject
    async with conn() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM local_principals WHERE principal_type = 'agent'"
        ) == 1


async def test_rotation_refuses_a_principal_that_is_not_this_one(local_mode):
    await cli._init("default", "owner")
    await cli._init("other", "owner")
    agent = await cli._create(_create_args())
    user = await cli._create(_create_args(subject_type="user", name="them"))
    with pytest.raises(SystemExit, match="no principal"):
        await cli._create(_create_args(subject="tcagt_" + "0" * 16))
    with pytest.raises(ValueError, match="not a user subject"):
        await cli._create(_create_args(subject_type="user", name="x", subject=agent["subject"]))
    with pytest.raises(ValueError, match="not a agent subject"):
        await cli._create(_create_args(subject=user["subject"]))
    with pytest.raises(SystemExit, match="belongs to workspace"):
        await cli._create(_create_args(workspace="other", subject=agent["subject"]))
    with pytest.raises(ValueError, match="exclusive"):
        await cli._create(
            _create_args(subject_type="user", name="x", subject=user["subject"], role="owner")
        )


async def test_scopes_are_a_subset_and_are_validated(local_mode):
    await cli._init("default", "owner")
    minted = await cli._create(_create_args(scopes="drives:read,content:read"))
    actor = await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert set(actor.scopes) == {"drives:read", "content:read"}
    assert actor.can("drives:read") and not actor.can("drives:write")
    with pytest.raises(ValueError, match="unknown scopes"):
        await cli._create(_create_args(scopes="drives:read,drives:destroy"))


async def test_a_failed_create_leaves_no_orphan_principal(local_mode):
    await cli._init("default", "owner")
    with pytest.raises(ValueError, match="3650d"):
        await cli._create(_create_args(expires="99999999999d"))
    async with conn() as c:
        assert await c.fetchval("SELECT count(*) FROM local_principals") == 1  # the owner
        assert await c.fetchval("SELECT count(*) FROM local_api_keys") == 0


async def test_list_shows_the_id_and_never_the_key(local_mode):
    await cli._init("default", "owner")
    minted = await cli._create(_create_args(name="claude-code"))
    listed = await cli._list("default")
    assert [k["id"] for k in listed] == [minted["id"]]
    assert listed[0]["name"] == "claude-code"
    assert listed[0]["scopes"] == " ".join(minted["scopes"])
    assert "key_hash" not in listed[0]
    assert minted["key"] not in cli.json_dumps(listed)
    assert await cli._list("other") == []


def test_expiry_parsing():
    assert cli.parse_expires("30m") == 1800
    assert cli.parse_expires("2d") == 172800
    assert cli.parse_expires("3650d") == cli.MAX_EXPIRES_SECONDS
    for bad in ("", "10", "1w", "0h", "-5m", "3651d", "99999999999d"):
        with pytest.raises(ValueError):
            cli.parse_expires(bad)


def test_workspace_ids_are_labels_not_blanks():
    assert cli.parse_workspace_id(" team-a ") == "team-a"
    for bad in ("", "   ", "a b", "-x", "x" * 129):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.parse_workspace_id(bad)


def test_the_cli_refuses_to_mint_under_hub(monkeypatch):
    assert settings.auth_mode == "hub"
    for command in (["init"], ["list"], ["revoke", "adk_x"]):
        with pytest.raises(SystemExit, match="AUTH_MODE=local"):
            cli.main(command)


def _statements_writing_keys() -> list[tuple[str, str]]:
    """Every statement in the tree that could write `local_api_keys`.

    Whitespace-normalised first, because Python splits long SQL across
    adjacent string literals and lines; `(file, statement)` so a failure names
    where to look. The corpus is the whole package plus every migration and
    the baseline, not the three files that write the table today — a future
    `0065`, or a module that does not exist yet, is exactly the thing this
    guard is for.
    """
    package = Path(cli.__file__).parent
    app = package.parent.parent
    sources = sorted(package.rglob("*.py"))
    sources += sorted((app / "migrations").glob("*.sql")) + [app / "schema.sql"]
    found: list[tuple[str, str]] = []
    for source in sources:
        text = source.read_text()
        # Comments out first — the prose around this table says "UPDATE" and
        # "scopes" constantly, and a guard that trips on its own rationale is
        # a guard someone deletes.
        text = re.sub(r"(--|#)[^\n]*", " ", text)
        # Adjacent string literals and line breaks both vanish, so a statement
        # Python split across either is one statement here.
        text = re.sub(r"['\"]\s*['\"]", "", text)
        text = re.sub(r"[\s\\]+", " ", text)
        for match in re.finditer(
            r"\bUPDATE\b[^;]*?\blocal_api_keys\b[^;]*|"
            r"\blocal_api_keys\b[^;]*?\bDO UPDATE\b[^;]*",
            text,
            re.IGNORECASE,
        ):
            found.append((source.name, match.group(0)))
    return found


def test_no_code_path_widens_a_key_s_scopes():
    """§8 decision 11: a key's scopes are fixed at creation, so revoke plus
    create is the only way to change what a client may do — which is also why
    a leaked key cannot be widened by whoever leaked it.

    Enforced by ABSENCE, which nothing in the type system or the schema can
    notice, so it is enforced here instead: no statement anywhere in the app
    that writes this table may mention `scopes`.
    """
    statements = _statements_writing_keys()
    assert statements, "expected at least `revoke`'s UPDATE to be found"
    assert any("revoked_at" in s for _, s in statements), "the revoke statement went missing"
    for source, statement in statements:
        assert "scopes" not in statement.lower(), f"{source}: {statement}"


def test_the_scope_guard_catches_the_ways_round_it(tmp_path, monkeypatch):
    """The guard is a grep, so its own blind spots are the risk. Each of these
    is a real way to spell a scope UPDATE that an earlier, narrower pattern
    let through: a statement split across adjacent string literals, an upsert,
    and one with unusual whitespace."""
    package = Path(cli.__file__).parent
    for name, sql in {
        "split.py": (
            'await c.execute("UPDATE local_api_keys SET revoked_at = now(), "\n'
            '                "scopes = $2 WHERE id = $1")'
        ),
        "upsert.py": (
            '"INSERT INTO local_api_keys (id) VALUES ($1) '
            'ON CONFLICT (id) DO UPDATE SET scopes = EXCLUDED.scopes"'
        ),
        "spaced.py": '"UPDATE   local_api_keys\n SET scopes = $1"',
    }.items():
        planted = package / name
        planted.write_text(sql)
        try:
            with pytest.raises(AssertionError):
                test_no_code_path_widens_a_key_s_scopes()
        finally:
            planted.unlink()


# ---- resolution -------------------------------------------------------------


async def test_revocation_refuses_the_key_on_the_next_request(local_mode):
    await cli._init("default", "owner")
    minted = await cli._create(_create_args(scopes="drives:read"))
    await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    revoked = await cli._revoke(minted["id"])
    assert revoked["id"] == minted["id"] and revoked["revoked_at"] is not None
    with pytest.raises(V0ApiError) as exc:
        await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert exc.value.status_code == 401
    # The row stays, so `list` can still name it.
    listed = await cli._list("default")
    assert listed[0]["revoked_at"] is not None
    with pytest.raises(SystemExit, match="no API key"):
        await cli._revoke("adk_missing1")


async def test_an_expired_key_is_refused_and_its_row_remains(local_mode):
    await cli._init("default", "owner")
    minted = await cli._create(_create_args(expires="1h"))
    await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    async with conn() as c:
        await c.execute(
            "UPDATE local_api_keys SET expires_at = $2 WHERE id = $1",
            minted["id"], datetime.now(UTC) - timedelta(seconds=1),
        )
    with pytest.raises(V0ApiError) as exc:
        await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert exc.value.status_code == 401
    assert (await cli._list("default"))[0]["id"] == minted["id"]


async def test_an_unknown_or_malformed_bearer_is_refused(local_mode):
    await cli._init("default", "owner")
    for bearer in (
        generate_key(),                      # well-formed, never issued
        "adk_short",                          # our prefix, wrong length
        "not-a-key",
        "eyJhbGciOiJSUzI1NiJ9.e30.x",         # a JWT: nothing here verifies one
    ):
        with pytest.raises(V0ApiError) as exc:
            await v0_deps.resolve_actor(f"Bearer {bearer}")
        assert exc.value.status_code == 401, bearer
        assert exc.value.headers["WWW-Authenticate"].startswith('Bearer error="invalid_token"')
    with pytest.raises(V0ApiError) as exc:
        await v0_deps.resolve_actor(None)
    assert exc.value.status_code == 401
    assert "error=" not in exc.value.headers["WWW-Authenticate"]


async def test_an_agent_key_whose_workspace_lost_its_owner_is_refused(local_mode):
    """A Hub-issued agent token always carries a sponsor. Admitting an
    unsponsored agent would silently drop the sponsor grant on every drive
    it creates, so the boundary refuses instead."""
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())
    async with conn() as c:
        await c.execute(
            "UPDATE local_principals SET workspace_role = 'admin' "
            "WHERE workspace_role = 'owner' AND workspace_id = 'default'"
        )
    with pytest.raises(V0ApiError) as exc:
        await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert exc.value.status_code == 401


async def test_a_database_outage_during_resolution_is_a_503(local_mode, monkeypatch):
    """Our auth boundary being down is `AUTH_UNAVAILABLE`, like an
    unreachable JWKS — not a 500 from an unhandled driver error, and never
    fail-open."""
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())

    class _Down:
        async def __aenter__(self):
            raise ConnectionError("pool gone")

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("agentdrive.db.conn", lambda: _Down())
    with pytest.raises(V0ApiError) as exc:
        await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert exc.value.status_code == 503
    assert exc.value.code == "AUTH_UNAVAILABLE"
    assert exc.value.headers["Retry-After"]


async def test_one_key_works_on_every_surface(local_mode):
    """No `api`-versus-`mcp` audience split in local mode (§4.2): the same
    key resolves for `/v0` and for the internal ingress's `/mcp` audience,
    because an operator who minted it IS the principal."""
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())
    product = await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    mcp = await v0_deps.resolve_actor(
        f"Bearer {minted['key']}", audience=settings.hub_mcp_audience
    )
    assert product == mcp


# ---- hosted behaviour is untouched -----------------------------------------


async def test_hub_mode_refuses_an_adk_bearer_and_never_reads_the_key_table(
    app_with_lifespan, hub_jwks, hub_token, monkeypatch
):
    """The hosted boundary: an `adk_` bearer is refused exactly as any other
    non-JWT is, and a real Hub token still verifies."""
    assert settings.auth_mode == "hub"
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    v0_deps.reset()
    try:
        with pytest.raises(V0ApiError) as exc:
            await v0_deps.resolve_actor(f"Bearer {generate_key()}")
        assert exc.value.status_code == 401
        actor = await v0_deps.resolve_actor(f"Bearer {hub_token()}")
        assert actor.subject.startswith("tcagt_")
    finally:
        v0_deps.reset()


async def test_a_live_key_is_refused_under_hub_mode(local_mode, monkeypatch, hub_jwks):
    """The same key that works in local mode must be worthless the moment the
    deployment is a hosted one — and not merely refused: the credential table
    must never be CONSULTED. The pool is made to raise, so a hub-mode request
    that touched it would be a 503 rather than the 401 asserted here."""
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())
    monkeypatch.setattr(settings, "auth_mode", "hub")
    monkeypatch.setattr(v0_deps, "_fetch_jwks", lambda issuer: hub_jwks.public_jwks)
    v0_deps.reset()

    class _Tripwire:
        async def __aenter__(self):
            raise AssertionError("hub mode consulted the local credential store")

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("agentdrive.db.conn", lambda: _Tripwire())
    with pytest.raises(V0ApiError) as exc:
        await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert exc.value.status_code == 401


async def test_a_bearer_that_is_not_key_shaped_never_reaches_postgres(local_mode, monkeypatch):
    """The shape check runs before the pool is touched. Hub-mode verification
    is pure CPU, so an unauthenticated flood costs a hosted deployment
    nothing; here every bearer would otherwise borrow a Postgres connection
    before anything looked at it, and the cheapest bad bearer to send is one
    that was never going to be a key."""

    class _Tripwire:
        async def __aenter__(self):
            raise AssertionError("a malformed bearer reached the pool")

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("agentdrive.db.conn", lambda: _Tripwire())
    for bearer in ("adk_short", "not-a-key", "eyJhbGciOiJSUzI1NiJ9.e30.x", "adk_" + "x" * 41):
        with pytest.raises(V0ApiError) as exc:
            await v0_deps.resolve_actor(f"Bearer {bearer}")
        assert exc.value.status_code == 401, bearer


async def test_missing_credential_tables_are_a_boot_error_not_a_permanent_503(
    local_mode, monkeypatch
):
    """§4.2's failure semantics: with no key file and no fetch, the only
    boot-time auth check local mode has left is that the migrations ran.
    Without it an install that skipped `apply_schema` answers 503 to every
    request forever while `/health` stays green."""
    await v0_deps.check_local_credentials()  # the tables exist here

    class _NoTables:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, *_):
            raise asyncpg.exceptions.UndefinedTableError("relation does not exist")

    monkeypatch.setattr("agentdrive.db.conn", lambda: _NoTables())
    with pytest.raises(asyncpg.exceptions.UndefinedTableError):
        await v0_deps.check_local_credentials()
    monkeypatch.setattr(settings, "auth_mode", "hub")
    await v0_deps.check_local_credentials()  # a no-op under Hub


async def test_a_key_whose_principal_lives_in_another_workspace_is_refused(local_mode):
    """Nothing in the schema holds `local_api_keys.workspace_id` equal to its
    principal's, and the resolver takes the workspace from the key row and the
    role from the principal row. A row that disagrees must be a 401, not an
    actor acting in one workspace with another's principal."""
    await cli._init("default", "owner")
    await cli._init("other", "owner")
    minted = await cli._create(_create_args())
    await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    async with conn() as c:
        await c.execute(
            "UPDATE local_api_keys SET workspace_id = 'other' WHERE id = $1", minted["id"]
        )
    with pytest.raises(V0ApiError) as exc:
        await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert exc.value.status_code == 401


async def test_a_scope_outside_the_vocabulary_is_dropped_not_carried(local_mode):
    """`from_claims` intersects a Hub token's `scope` claim with the
    vocabulary; the key path must not differ. A row can only hold an unknown
    scope if it was hand-written or the vocabulary shrank — either way the
    actor must look like one a Hub token would have produced."""
    await cli._init("default", "owner")
    minted = await cli._create(_create_args(scopes="drives:read"))
    async with conn() as c:
        await c.execute(
            "UPDATE local_api_keys SET scopes = 'drives:read drives:destroy' WHERE id = $1",
            minted["id"],
        )
    actor = await v0_deps.resolve_actor(f"Bearer {minted['key']}")
    assert set(actor.scopes) == {"drives:read"}


# ---- over HTTP --------------------------------------------------------------


async def test_a_request_with_a_local_key_reaches_v0(local_mode, http):
    """Over the ASGI surface, not just the dependency: create a drive with a
    CLI-minted key and list it back."""
    await cli._init("default", "owner")
    minted = await cli._create(_create_args())
    auth = {"Authorization": f"Bearer {minted['key']}"}
    created = await http.post(
        "/v0/drives", json={"name": "Local drive"},
        headers={**auth, "Idempotency-Key": "local-mode-1"},
    )
    assert created.status_code == 201, created.text
    listed = await http.get("/v0/drives", headers=auth)
    assert listed.status_code == 200
    assert created.json()["id"] in [d["id"] for d in listed.json()["items"]]
    unauthenticated = await http.get("/v0/drives")
    assert unauthenticated.status_code == 401


async def test_discovery_names_no_authorization_server_and_serves_no_jwks(local_mode, http):
    doc = (await http.get("/.well-known/oauth-protected-resource")).json()
    assert doc["authorization_servers"] == []
    assert doc["bearer_methods_supported"] == ["header"]
    # The retired issuer's endpoint is gone in BOTH modes: nothing signs.
    assert (await http.get("/jwks")).status_code == 404


async def test_hub_mode_still_names_hub_in_discovery(http):
    assert settings.auth_mode == "hub"
    doc = (await http.get("/.well-known/oauth-protected-resource")).json()
    assert doc["authorization_servers"] == [settings.hub_issuer.rstrip("/")]
    assert (await http.get("/jwks")).status_code == 404
