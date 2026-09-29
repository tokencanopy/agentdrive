"""Regression guard: `schema.sql` must apply cleanly to a database
that has never seen the schema before.

This is the test that should have existed before PR #94: it's the
only thing standing between the schema and the silently-failing-deploy
bug class that motivated the 2026-06-05 schema reset. CI's pytest
applies schema against a fresh container; this test gives the same
guarantee locally, in a few hundred ms, every time you run the suite.

What it does:
  1. `CREATE DATABASE`s an ephemeral DB with a random suffix
  2. Applies the full `schema.sql`
  3. Asserts a handful of post-apply invariants
  4. `DROP DATABASE`s the ephemeral DB

A failure here means a fresh deploy (CI's test stage, the prod
migrate Cloud Run Job's first run on a new Cloud SQL instance,
any contributor's fresh `docker compose up` + pytest) will fail.
Don't weaken this test to make a forward-reference go away; fix
the schema ordering so the apply genuinely is clean.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import socket

import pytest

_DEV_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://agentdrive:dev@localhost:5432/agentdrive"
)
_HOST_CREDS = _DEV_DB_URL.rsplit("/", 1)[0]
_ADMIN_DB_URL = f"{_HOST_CREDS}/postgres"
_SCHEMA_PATH = pathlib.Path(__file__).parent.parent / "schema.sql"


def _db_available() -> bool:
    try:
        host_port = _HOST_CREDS.rsplit("@", 1)[1]
        host, port_s = host_port.split(":")
        with socket.create_connection((host, int(port_s)), timeout=1):
            return True
    except (OSError, ValueError, IndexError):
        return False


@pytest.mark.asyncio
async def test_schema_applies_cleanly_to_empty_database():
    """Apply `schema.sql` to a database that has never seen it."""
    if not _db_available():
        pytest.skip(
            "Postgres on localhost:5432 not reachable "
            "(start with `docker compose up -d`)"
        )

    import asyncpg

    # Unique DB name per test invocation — concurrent pytest workers
    # or iterative re-runs while debugging shouldn't collide.
    db_name = f"agentdrive_fresh_{secrets.token_hex(4)}"
    target_dsn = f"{_HOST_CREDS}/{db_name}"
    sql = _SCHEMA_PATH.read_text()

    admin = await asyncpg.connect(_ADMIN_DB_URL)
    try:
        # CREATE DATABASE can't run inside a transaction; asyncpg's
        # default execute() honors that. Quoted identifier as
        # defense-in-depth (db_name is locally-minted from token_hex
        # so SQLi isn't really possible).
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()

    try:
        conn = await asyncpg.connect(target_dsn)
        try:
            # apply_schema.py wraps execute() in conn.transaction();
            # mirror that here so the test path matches production.
            async with conn.transaction():
                await conn.execute(sql)
            # Applied a second time on purpose. `apply_all` runs the baseline
            # as an unconditional idempotent tail on EVERY invocation, not
            # just the fresh path -- so a bare `CREATE TABLE` anywhere in this
            # file makes the second `apply_schema` of any database fail. That
            # is exactly the defect this caught when the v0 baseline was
            # first written.
            async with conn.transaction():
                await conn.execute(sql)
            await _assert_invariants(conn)
        finally:
            await conn.close()
    finally:
        # Re-acquire admin conn — can't drop a DB you're connected
        # to. WITH (FORCE) terminates leftover sessions (PG 13+).
        admin = await asyncpg.connect(_ADMIN_DB_URL)
        try:
            await admin.execute(
                f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'
            )
        finally:
            await admin.close()


async def _assert_invariants(conn) -> None:
    """The §12A invariants, proven to BITE rather than merely to exist.

    `tests/test_schema_shape.py` reads the file and asserts each constraint
    is written down. That cannot tell a working constraint from a typo'd
    one, or catch a generated expression Postgres silently accepts but never
    populates. These drive real rows at a real database.

    Every rejection case below is a state some future handler will
    eventually try to write. The point of putting them here rather than in
    a handler test is that they must hold for writers nobody has written.
    """
    # A valid two-drive fixture: each drive with its structural root, one
    # artifact with a head version. Everything below mutates against it.
    await conn.execute(
        """
        INSERT INTO drives (id, workspace_id, name, revision) VALUES
          ('drv_00000000000000a1','ws_test','A','rev_00000000000000a1'),
          ('drv_00000000000000b1','ws_test','B','rev_00000000000000b1');
        INSERT INTO folders (id, drive_id, parent_id, name, revision) VALUES
          ('fld_0000000000000a01','drv_00000000000000a1',NULL,NULL,'rev_0000000000000a01'),
          ('fld_0000000000000b01','drv_00000000000000b1',NULL,NULL,'rev_0000000000000b01');
        UPDATE drives SET root_folder_id='fld_0000000000000a01'
          WHERE id='drv_00000000000000a1';
        UPDATE drives SET root_folder_id='fld_0000000000000b01'
          WHERE id='drv_00000000000000b1';
        INSERT INTO artifacts (id,drive_id,parent_id,name,revision) VALUES
          ('art_0000000000000a01','drv_00000000000000a1','fld_0000000000000a01',
           'notes.md','rev_0000000000000a11');
        INSERT INTO artifact_versions
          (id,artifact_id,checksum,content_type,size_bytes,storage_object,
           actor_type,actor_id,ordinal)
        VALUES
          ('ver_0000000000000a01','art_0000000000000a01','sha256:x',
           'text/markdown',10,'cas/x','agent','tcagt_x',1);
        UPDATE artifacts SET head_version_id='ver_0000000000000a01'
          WHERE id='art_0000000000000a01';
        """
    )

    async def rejects(label: str, sql: str) -> None:
        import asyncpg as _pg

        try:
            async with conn.transaction():
                await conn.execute(sql)
        except _pg.PostgresError:
            return
        raise AssertionError(f"schema permits {label} -- invariant is not enforced")

    # §6.2 -- the structural root is the only parent-less folder in a drive.
    await rejects(
        "a second root folder in one drive",
        "INSERT INTO folders (id,drive_id,parent_id,name,revision) VALUES "
        "('fld_0000000000000a02','drv_00000000000000a1',NULL,NULL,"
        "'rev_0000000000000a02')",
    )
    # §4.1 -- an id that does not match its table's prefix cannot be stored.
    await rejects(
        "a malformed id prefix",
        "INSERT INTO folders (id,drive_id,parent_id,name,revision) VALUES "
        "('bogus_1','drv_00000000000000a1','fld_0000000000000a01','x',"
        "'rev_0000000000000a03')",
    )
    # §4 -- the composite FK is what makes a cross-drive parent
    # unrepresentable. A plain FK on parent_id alone would allow this.
    await rejects(
        "a parent in another drive",
        "INSERT INTO artifacts (id,drive_id,parent_id,name,revision) VALUES "
        "('art_0000000000000a09','drv_00000000000000a1','fld_0000000000000b01',"
        "'x','rev_0000000000000a04')",
    )
    # The folder-tree half of the same composite FK. The artifact case alone
    # does not cover it: the two FKs are separate statements in the file, so
    # one could be dropped or weakened while the artifact probe stays green.
    await rejects(
        "a folder whose parent is in another drive",
        "INSERT INTO folders (id,drive_id,parent_id,name,revision) VALUES "
        "('fld_0000000000000a09','drv_00000000000000a1','fld_0000000000000b01',"
        "'x','rev_0000000000000a09')",
    )
    # §4 -- the root half of the same §12A row as the parent case above. The
    # fixture already had two drives with their own roots; only the assertion
    # was missing, which is how a bare FK survived review.
    await rejects(
        "a drive rooted at another drive's folder",
        "UPDATE drives SET root_folder_id='fld_0000000000000b01' "
        "WHERE id='drv_00000000000000a1'",
    )
    # §6.2 -- one collision domain across BOTH tables. Postgres has no
    # cross-table unique index, so this is the trigger's half; without it a
    # folder and an artifact could share a name and §6.11's "at most one
    # item" would be false.
    await rejects(
        "a folder taking an artifact's sibling name",
        "INSERT INTO folders (id,drive_id,parent_id,name,revision) VALUES "
        "('fld_0000000000000a07','drv_00000000000000a1','fld_0000000000000a01',"
        "'notes.md','rev_0000000000000a07')",
    )
    # A real sibling folder to collide with. The insert above was rejected, so
    # without this the rename would find nothing and pass vacuously -- which
    # is exactly the shape of mistake this whole review pass was about.
    await conn.execute(
        "INSERT INTO folders (id,drive_id,parent_id,name,revision) VALUES "
        "('fld_0000000000000a08','drv_00000000000000a1','fld_0000000000000a01',"
        "'reports','rev_0000000000000a08')"
    )
    await rejects(
        "a rename onto a sibling of the other kind",
        "UPDATE artifacts SET name='reports' WHERE id='art_0000000000000a01'",
    )
    # §6.2 -- names are never an upsert key; a duplicate sibling is a conflict.
    await rejects(
        "a duplicate sibling name",
        "INSERT INTO artifacts (id,drive_id,parent_id,name,revision) VALUES "
        "('art_0000000000000a02','drv_00000000000000a1','fld_0000000000000a01',"
        "'notes.md','rev_0000000000000a05')",
    )
    # §6.1 -- a negative counter is the signature of a double-decrement.
    await rejects(
        "a negative byte counter",
        "UPDATE drives SET storage_bytes=-1 WHERE id='drv_00000000000000a1'",
    )
    # §6.4 -- versions are immutable, held by a trigger because no CHECK can
    # express "no UPDATE ever".
    await rejects(
        "an UPDATE of a stored version",
        "UPDATE artifact_versions SET checksum='sha256:tampered' "
        "WHERE id='ver_0000000000000a01'",
    )
    # §6.8 -- public is the publication mechanism, and publication is read-only.
    await rejects(
        "a public grant above viewer",
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,role,revision) VALUES ('grn_0000000000000a01',"
        "'drv_00000000000000a1','drive','drv_00000000000000a1','public','editor','rev_0000000000000a01')",
    )
    await rejects(
        "a public grant carrying a principal id",
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000a02',"
        "'drv_00000000000000a1','drive','drv_00000000000000a1','public',"
        "'tcusr_x','viewer','rev_0000000000000a02')",
    )
    await rejects(
        "an agent principal holding a tcusr_ id",
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000a03',"
        "'drv_00000000000000a1','drive','drv_00000000000000a1','agent',"
        "'tcusr_x','viewer','rev_0000000000000a03')",
    )
    # §6.8/§12A -- a grant is drive-scoped, and so is the resource it names.
    # `resource_id` is polymorphic, so no composite FK can pin it; the
    # `reject_out_of_drive_resource` trigger is what makes a cross-drive
    # grant unrepresentable. A bare id alone would let drive A's grant name
    # drive B's folder -- the same class of state the composite FKs refuse.
    await rejects(
        "a grant on a folder in another drive",
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000a04',"
        "'drv_00000000000000a1','folder','fld_0000000000000b01','user',"
        "'tcusr_x','viewer','rev_0000000000000a04')",
    )
    await rejects(
        "a grant on an artifact in another drive",
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000a05',"
        "'drv_00000000000000b1','artifact','art_0000000000000a01','user',"
        "'tcusr_x','viewer','rev_0000000000000a05')",
    )
    await rejects(
        "a drive grant naming a different drive",
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000a06',"
        "'drv_00000000000000a1','drive','drv_00000000000000b1','user',"
        "'tcusr_x','viewer','rev_0000000000000a06')",
    )
    await rejects(
        "a grant on a resource that does not exist",
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000a07',"
        "'drv_00000000000000a1','folder','fld_0000000000000ccc','user',"
        "'tcusr_x','viewer','rev_0000000000000a07')",
    )
    await rejects(
        "a share on an artifact in another drive",
        "INSERT INTO shares (id,drive_id,resource_type,resource_id,"
        "secret_hash,revision) VALUES ('shr_0000000000000a01','drv_00000000000000b1',"
        "'artifact','art_0000000000000a01','sha256:a01','rev_0000000000000a01')",
    )
    await rejects(
        "a share on a version in another drive",
        "INSERT INTO shares (id,drive_id,resource_type,resource_id,"
        "secret_hash,revision) VALUES ('shr_0000000000000a02','drv_00000000000000b1',"
        "'artifact_version','ver_0000000000000a01','sha256:x','rev_0000000000000a02')",
    )
    await rejects(
        "a share on a resource that does not exist",
        "INSERT INTO shares (id,drive_id,resource_type,resource_id,"
        "secret_hash,revision) VALUES ('shr_0000000000000a03','drv_00000000000000a1',"
        "'folder','fld_0000000000000ccc','sha256:a03','rev_0000000000000a03')",
    )
    # B3 direct transfers (migration 0049): the session table's publication /
    # cleanup enumerations, target discriminator, and completed-result shape
    # must bite on a FRESH baseline apply too, not only on the migrated path
    # covered by tests/test_v0_upload_state.py.
    await rejects(
        "an upload session in an unknown publication state",
        "INSERT INTO upload_sessions (id,workspace_id,drive_id,principal_type,"
        "principal_id,target_kind,parent_folder_id,artifact_name,"
        "declared_size_bytes,declared_media_type,declared_crc32c,"
        "adoption_marker,scratch_object,final_object,expires_at,state) VALUES "
        "('upld_0000000000000a01','ws_test','drv_00000000000000a1','agent',"
        "'tcagt_x','artifact','fld_0000000000000a01','up.txt',1,'text/plain',"
        "'yZRlqg==','m','s','f',now() + interval '1 hour','uploading')",
    )
    await rejects(
        "an upload session whose target mixes both discriminator arms",
        "INSERT INTO upload_sessions (id,workspace_id,drive_id,principal_type,"
        "principal_id,target_kind,parent_folder_id,artifact_name,artifact_id,"
        "declared_size_bytes,declared_media_type,declared_crc32c,"
        "adoption_marker,scratch_object,final_object,expires_at) VALUES "
        "('upld_0000000000000a02','ws_test','drv_00000000000000a1','agent',"
        "'tcagt_x','artifact','fld_0000000000000a01','up.txt',"
        "'art_0000000000000a01',1,'text/plain','yZRlqg==','m','s','f',"
        "now() + interval '1 hour')",
    )
    await rejects(
        "a completed upload session without immutable result coordinates",
        "INSERT INTO upload_sessions (id,workspace_id,drive_id,principal_type,"
        "principal_id,target_kind,parent_folder_id,artifact_name,"
        "declared_size_bytes,declared_media_type,declared_crc32c,"
        "adoption_marker,scratch_object,final_object,expires_at,state,"
        "terminal_at) VALUES "
        "('upld_0000000000000a03','ws_test','drv_00000000000000a1','agent',"
        "'tcagt_x','artifact','fld_0000000000000a01','up.txt',1,'text/plain',"
        "'yZRlqg==','m','s','f',now() + interval '1 hour','completed',now())",
    )
    await rejects(
        "a negative workspace storage counter",
        "INSERT INTO workspace_storage (workspace_id,committed_bytes) "
        "VALUES ('ws_test',-1)",
    )
    await rejects(
        "a released reservation without a release kind",
        "INSERT INTO storage_reservations (id,workspace_id,drive_id,"
        "principal_id,size_bytes,released_at) VALUES "
        "('rsv_0000000000000a01','ws_test','drv_00000000000000a1','tcagt_x',"
        "1,now())",
    )

    # §6.7 -- the retention floor may sit one past the head (an empty,
    # fully-trimmed feed) but never beyond it.
    await rejects(
        "a retention floor beyond the head",
        "INSERT INTO drive_change_heads (drive_id,last_sequence,"
        "retained_from_sequence) VALUES ('drv_00000000000000a1',5,7)",
    )
    # §6.7 -- total order within one drive means the sequence is dense and
    # unique per drive.
    await rejects(
        "a duplicate change sequence in one drive",
        "INSERT INTO drive_changes (id,drive_id,sequence,change_set_id,type,"
        "actor_type,resource_type,resource_id) VALUES "
        "('chg_0000000000000a01','drv_00000000000000a1',1,'cset_1',"
        "'artifact.created','agent','artifact','art_0000000000000a01'),"
        "('chg_0000000000000a02','drv_00000000000000a1',1,'cset_1',"
        "'artifact.created','agent','artifact','art_0000000000000a01')",
    )

    # A constraint that rejects VALID state is equally a bug, so the happy
    # paths are asserted too.
    head = await conn.fetchval(
        "SELECT head_version_id FROM artifacts WHERE id='art_0000000000000a01'"
    )
    assert head == "ver_0000000000000a01", (
        "the composite head-version FK rejected an artifact's own version"
    )

    # §4.3 -- parent_version_id belongs to the SAME artifact. A bare
    # self-reference (REFERENCES artifact_versions(id)) only guarantees "some
    # version"; the composite (artifact_id, parent_version_id) FK is what makes
    # a cross-artifact history edge unrepresentable -- the version-level
    # analogue of the head_version_id case just above. Needs a SECOND artifact
    # with its own version to aim the bad edge at.
    await conn.execute(
        """
        INSERT INTO artifacts (id,drive_id,parent_id,name,revision) VALUES
          ('art_0000000000000c01','drv_00000000000000a1','fld_0000000000000a01',
           'other.md','rev_0000000000000c01');
        INSERT INTO artifact_versions
          (id,artifact_id,checksum,content_type,size_bytes,storage_object,
           actor_type,actor_id,ordinal)
        VALUES
          ('ver_0000000000000c01','art_0000000000000c01','sha256:y',
           'text/markdown',10,'cas/y','agent','tcagt_y',1);
        """
    )
    # Assert the EXACT asyncpg type, not merely "some error": the guarantee is
    # a foreign-key violation from artifact_versions_parent_is_own, and pinning
    # the type stops a future NOT NULL / CHECK from masquerading as it holding.
    import asyncpg as _fkpg

    try:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO artifact_versions "
                "(id,artifact_id,parent_version_id,checksum,content_type,"
                "size_bytes,storage_object,actor_type,actor_id,ordinal) VALUES "
                "('ver_0000000000000a02','art_0000000000000a01',"
                "'ver_0000000000000c01','sha256:z','text/markdown',10,'cas/z',"
                "'agent','tcagt_z',2)"
            )
    except _fkpg.ForeignKeyViolationError:
        pass
    else:
        raise AssertionError(
            "schema permits a version whose parent belongs to ANOTHER artifact "
            "-- artifact_versions_parent_is_own is not enforced"
        )
    # The same-artifact parent is the valid case the FK must still accept: a
    # constraint that rejects legal state is as much a bug as one that permits
    # illegal state.
    await conn.execute(
        "INSERT INTO artifact_versions "
        "(id,artifact_id,parent_version_id,checksum,content_type,"
        "size_bytes,storage_object,actor_type,actor_id,ordinal) VALUES "
        "('ver_0000000000000a02','art_0000000000000a01',"
        "'ver_0000000000000a01','sha256:z','text/markdown',10,'cas/z',"
        "'agent','tcagt_z',2)"
    )

    # Same-drive grants and shares must be accepted -- the drive-scoping
    # trigger exists to reject cross-drive references, not to make valid
    # drive-scoped ones impossible.
    for _label, sql in (
        (
            "a drive grant naming its own drive",
            "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
            "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000a08',"
            "'drv_00000000000000a1','drive','drv_00000000000000a1','user',"
            "'tcusr_x','viewer','rev_0000000000000a08')",
        ),
        (
            "a grant on a folder in its own drive",
            "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
            "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000a09',"
            "'drv_00000000000000a1','folder','fld_0000000000000a01','user',"
            "'tcusr_x','viewer','rev_0000000000000a09')",
        ),
        (
            "a grant on an artifact in its own drive",
            "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
            "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000a0a',"
            "'drv_00000000000000a1','artifact','art_0000000000000a01','user',"
            "'tcusr_x','viewer','rev_0000000000000a0a')",
        ),
        (
            "a share on an artifact in its own drive",
            "INSERT INTO shares (id,drive_id,resource_type,resource_id,"
            "secret_hash,revision,expires_at,daily_byte_limit) VALUES "
            "('shr_0000000000000a04','drv_00000000000000a1',"
            "'artifact','art_0000000000000a01','sha256:a04','rev_0000000000000a04',"
            "now()+interval '1 day',5368709120)",
        ),
        (
            "a share on a version in its own drive",
            "INSERT INTO shares (id,drive_id,resource_type,resource_id,"
            "secret_hash,revision,expires_at,daily_byte_limit) VALUES "
            "('shr_0000000000000a05','drv_00000000000000a1',"
            "'artifact_version','ver_0000000000000a01','sha256:x','rev_0000000000000a05',"
            "now()+interval '1 day',5368709120)",
        ),
        (
            "a share on a folder in its own drive",
            "INSERT INTO shares (id,drive_id,resource_type,resource_id,"
            "secret_hash,revision,expires_at,daily_byte_limit) VALUES "
            "('shr_0000000000000a06','drv_00000000000000a1',"
            "'folder','fld_0000000000000a01','sha256:a06','rev_0000000000000a06',"
            "now()+interval '1 day',5368709120)",
        ),
    ):
        await conn.execute(sql)

    # §6.8 -- "one live grant per principal" is enforced by the schema's
    # partial unique index, and ONLY by the `revoked_at IS NULL` arm of its
    # predicate. This pins what the schema does and does not promise:
    #   * a second LIVE grant for the same (resource, principal) is rejected
    #     by the index;
    #   * an EXPIRED grant still occupies its index slot, so a raw re-insert
    #     is rejected too -- an index predicate cannot be time-dependent
    #     (Postgres requires IMMUTABLE functions there), so aging a grant out
    #     is a write-path responsibility (`_supersede_expired_grant` in
    #     v0-core). If this second rejection ever fails, someone moved
    #     expiry-awareness into the schema, and the Layer-3 write path must
    #     be revisited, not celebrated.
    #
    # Two principals so the cases don't overlap: tcusr_y proves the live-slot
    # rejection, tcusr_z proves the expired-slot one (its only row is dead).
    await conn.execute(
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000b01',"
        "'drv_00000000000000a1','drive','drv_00000000000000a1','user',"
        "'tcusr_y','manager','rev_0000000000000b01')"
    )
    await rejects(
        "a second live grant for the same principal",
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000b02',"
        "'drv_00000000000000a1','drive','drv_00000000000000a1','user',"
        "'tcusr_y','manager','rev_0000000000000b02')",
    )
    await conn.execute(
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision,expires_at) VALUES "
        "('grn_0000000000000b03','drv_00000000000000a1','drive',"
        "'drv_00000000000000a1','user','tcusr_z','manager',"
        "'rev_0000000000000b03',now() - interval '1 day')"
    )
    await rejects(
        "a raw re-insert while an expired grant still holds the slot",
        "INSERT INTO grants (id,drive_id,resource_type,resource_id,"
        "principal_type,principal_id,role,revision) VALUES ('grn_0000000000000b04',"
        "'drv_00000000000000a1','drive','drv_00000000000000a1','user',"
        "'tcusr_z','manager','rev_0000000000000b03')",
    )
    tsv = await conn.fetchval(
        "SELECT search_tsv::text FROM artifacts WHERE id='art_0000000000000a01'"
    )
    assert "'note':1A" in tsv, f"search_tsv did not index the name: {tsv!r}"

    # The whole point of GENERATED: a writer that never mentions search_tsv
    # still cannot leave it stale.
    await conn.execute(
        "UPDATE artifacts SET labels=ARRAY['quarterly'] "
        "WHERE id='art_0000000000000a01'"
    )
    tsv = await conn.fetchval(
        "SELECT search_tsv::text FROM artifacts WHERE id='art_0000000000000a01'"
    )
    # `quarter`, not `quarterly`: labels are stemmed like the rest of the
    # document, so a search for the stem finds the fuller word. Asserting the
    # stem is what makes this a test of the expression rather than of the
    # string that went in.
    assert "'quarter'" in tsv, (
        f"search_tsv went stale after a label update -- B3 is not fixed: {tsv!r}"
    )

    # Blocker B3 (finding 2 in schema-integrity): the GENERATED search_tsv
    # must not turn a legal-sized row into an unrecoverable write error.
    # A tsvector is capped at 1 MiB; `metadata` is unbounded JSONB and the
    # v0 inline-body ceiling is 20 MiB, so before this fix a metadata
    # document well under the product limit made every INSERT/UPDATE of the
    # row fail at the storage layer with "string is too long for tsvector".
    # Each arm of the generated expression must truncate its input so the
    # derived tsvector can never blow past the storage cap. A writer that
    # stores a large-but-legal value (at ~2 MiB, an order of magnitude under
    # the §6.5 ceiling) must still succeed, and the bounded arms -- name and
    # content_preview -- must remain searchable.
    # ~2 MiB of *distinct* tokens: a single repetitive string collapses to one
    # lexeme and would never trip the 1 MiB tsvector cap. Distinct words are
    # what force a lexeme per token, turning an ordinary metadata write into
    # the unrecoverable storage-layer error this test pins.
    big_meta = '{"notes": "' + " ".join(f"w{i}" for i in range(500_000)) + '"}'
    await conn.execute(
        """
        INSERT INTO artifacts (id, drive_id, parent_id, name,
                               content_preview, metadata, revision)
        VALUES ('art_0000000000000b01', 'drv_00000000000000a1',
                'fld_0000000000000a01', 'boundary.md',
                'distinct searchable preview', $1::jsonb,
                'rev_0000000000000b01')
        """,
        big_meta,
    )
    tsv = await conn.fetchval(
        "SELECT search_tsv::text FROM artifacts "
        "WHERE id='art_0000000000000b01'"
    )
    assert "'boundari':1A" in tsv, f"search_tsv lost the name arm: {tsv!r}"
    assert "'preview'" in tsv, "search_tsv lost the content_preview arm"

    # v0 substrate happy paths: the upload-session and job tables accept
    # valid rows. The artifact target keeps its parent_id (the schema refuses
    # an artifact-targeted session with no parent), reusing the existing
    # drive/folder fixture. `change_cursors` is deliberately absent -- D14
    # seals the client's position in the cursor token itself (ruling 1).
    await conn.execute(
        """
        INSERT INTO v0_uploads (id, drive_id, target_kind, parent_id, source, state)
        VALUES ('upld_0000000000000a01', 'drv_00000000000000a1', 'artifact',
                'fld_0000000000000a01', 'api', 'active')
        """
    )
    await conn.execute(
        """
        INSERT INTO v0_jobs (id, drive_id, kind, state, revision)
        VALUES ('job_0000000000000a01', 'drv_00000000000000a1', 'folder.copy',
                'queued', 'rev_0000000000000a01')
        """
    )
    await conn.execute(
        """
        INSERT INTO v0_job_object_refs (job_id, drive_id, gcs_object)
        VALUES ('job_0000000000000a01', 'drv_00000000000000a1', 'cas/upl/x')
        """
    )
