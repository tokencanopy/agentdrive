"""artifact_versions immutability trigger — scoped to content identity (§6.4).

The `reject_artifact_version_update()` trigger no longer blanket-rejects every
UPDATE. It freezes the version's CONTENT IDENTITY (the columns a reader, the
ETag/checksum comparison, and the GC mark-sweep's live-blob set derive from)
while PERMITTING the one harmless transition the planned version cap needs:
`parent_version_id` being cleared to NULL by the `ON DELETE SET NULL` tail when
an old parent version is pruned. Re-pointing a parent to a different non-NULL
version is history rewriting and is still rejected.

These are DB-level tests over real Postgres. They insert a drive, its root
folder, one artifact, and two chained versions directly (deferrable FKs let the
whole graph land in one transaction) using SYNTHETIC ids only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pytest

from agentdrive.db import conn

pytestmark = pytest.mark.asyncio

# Synthetic ids satisfying the schema shape CHECKs — no real data.
DRIVE = "drv_00000000000000bb"
FOLDER = "fld_00000000000000bb"
ART = "art_00000000000000bb"
V1 = "ver_00000000000000b1"  # ordinal 1, parent NULL
V2 = "ver_00000000000000b2"  # ordinal 2, parent = V1
REV = "rev_00000000000000a1"
WS = "tcws_0000000000000001"

# Content-identity columns that must stay frozen, with a shape-valid new value.
FROZEN_COLUMNS = [
    ("id", "ver_0000000000000fff"),
    ("artifact_id", "art_0000000000000fff"),
    ("checksum", "sha256:changed"),
    ("content_type", "text/plain"),
    ("size_bytes", 999),
    ("storage_object", "cas/other.bin"),
    ("actor_type", "user"),
    ("actor_id", "tcusr_0000000000000009"),
    ("ordinal", 99),
    ("created_at", datetime(2000, 1, 1, tzinfo=UTC)),
]


@pytest.fixture
async def _versions(app_with_lifespan):
    """Build drive → root folder → artifact → V1 ← V2 directly, then tear it
    down. Yields nothing; tests reference the module-level ids. Depends on
    ``app_with_lifespan`` so the shared asyncpg pool (``core.db.conn``) exists."""
    async with conn() as c, c.transaction():
        await c.execute("SET CONSTRAINTS ALL DEFERRED")
        await c.execute(
            "INSERT INTO drives (id, workspace_id, name, revision, root_folder_id) "
            "VALUES ($1, $2, $3, $4, $5)",
            DRIVE, WS, "imm", REV, FOLDER,
        )
        await c.execute(
            "INSERT INTO folders (id, drive_id, parent_id, name, revision) "
            "VALUES ($1, $2, NULL, NULL, $3)",
            FOLDER, DRIVE, REV,
        )
        await c.execute(
            "INSERT INTO artifacts "
            "(id, drive_id, parent_id, name, revision, head_version_id) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            ART, DRIVE, FOLDER, "a.txt", REV, V2,
        )
        for vid, ordinal, parent in ((V1, 1, None), (V2, 2, V1)):
            await c.execute(
                "INSERT INTO artifact_versions "
                "(id, artifact_id, parent_version_id, checksum, content_type, "
                "size_bytes, storage_object, actor_type, actor_id, ordinal) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                vid, ART, parent, "sha256:1", "application/octet-stream",
                10, f"cas/{vid}.bin", "agent", "tcagt_0000000000000001", ordinal,
            )
    try:
        yield
    finally:
        async with conn() as c:
            await c.execute("DELETE FROM drives WHERE id = $1", DRIVE)


@pytest.mark.parametrize("column, new_value", FROZEN_COLUMNS)
async def test_frozen_content_identity_columns_reject_update(_versions, column, new_value):
    """Changing any content-identity column raises restrict_violation (23001)."""
    async with conn() as c:
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await c.execute(
                f"UPDATE artifact_versions SET {column} = $1 WHERE id = $2",
                new_value, V1,
            )


async def test_repointing_parent_to_different_nonnull_is_rejected(_versions):
    """parent_version_id -> a different non-NULL version is history rewriting."""
    async with conn() as c:
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            # V1 currently has parent NULL; pointing it at V2 is a non-NULL change.
            await c.execute(
                "UPDATE artifact_versions SET parent_version_id = $1 WHERE id = $2",
                V2, V1,
            )


async def test_clearing_parent_to_null_is_permitted(_versions):
    """parent_version_id -> NULL succeeds: the ON DELETE SET NULL prune tail."""
    async with conn() as c:
        # V2's parent is V1; clearing it to NULL is the permitted transition.
        await c.execute(
            "UPDATE artifact_versions SET parent_version_id = NULL WHERE id = $1",
            V2,
        )
        row = await c.fetchrow(
            "SELECT parent_version_id FROM artifact_versions WHERE id = $1", V2
        )
        assert row["parent_version_id"] is None


async def test_noop_update_is_permitted(_versions):
    """An UPDATE that changes nothing is allowed (no frozen column differs)."""
    async with conn() as c:
        await c.execute(
            "UPDATE artifact_versions SET checksum = checksum WHERE id = $1", V1
        )
