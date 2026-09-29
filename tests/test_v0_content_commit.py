"""The shared immutable-commit/accounting seam itself (B3 §7/§9).

`tests/test_v0_logical_accounting.py` proves every PRODUCER crosses the
seam; this file pins the seam's own contract: argument validation, the
exactly-once conversion guard, cross-drive/workspace rejection, the parity
helper, and the single-source version column list.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentdrive.core import v0_content_commit as acct
from agentdrive.db import conn

pytestmark = pytest.mark.asyncio

WS = "tcws_0000000000000001"
AGENT = "tcagt_0000000000000001"
DRIVE_A = "drv_00000000000000a7"
ROOT_A = "fld_0000000000000a71"
DRIVE_B = "drv_00000000000000a8"
ROOT_B = "fld_0000000000000a81"


@pytest_asyncio.fixture(autouse=True)
async def _clean(app_with_lifespan):
    # Clean BEFORE too: suites that predate B3 truncate drives but not the
    # workspace accounting row, so committed bytes would leak in.
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, storage_reservations, "
            "upload_sessions, workspace_storage, drives "
            "RESTART IDENTITY CASCADE"
        )
    yield
    async with conn() as c:
        await c.execute(
            "TRUNCATE idempotency_records, storage_reservations, "
            "upload_sessions, workspace_storage, drives "
            "RESTART IDENTITY CASCADE"
        )


async def _seed(c, drive_id: str, root_id: str) -> None:
    await c.execute(
        "INSERT INTO drives (id, workspace_id, name, revision) VALUES "
        "($1, $2, 'seam-fixture', 'rev_00000000000000a7') "
        "ON CONFLICT (id) DO NOTHING",
        drive_id, WS,
    )
    await c.execute(
        "INSERT INTO folders (id, drive_id, parent_id, name, revision) VALUES "
        "($1, $2, NULL, NULL, 'rev_00000000000000a8') "
        "ON CONFLICT (id) DO NOTHING",
        root_id, drive_id,
    )
    await c.execute(
        "UPDATE drives SET root_folder_id = $2 WHERE id = $1", drive_id, root_id
    )


def _commit_request(**over) -> acct.ImmutableVersionCommit:
    fields = dict(
        drive_id=DRIVE_A,
        workspace_id=WS,
        artifact_id="art_0000000000000a71",
        version_id="ver_0000000000000a71",
        parent_version_id=None,
        ordinal=1,
        checksum="sha256:a71",
        content_type="text/plain",
        size_bytes=5,
        storage_object="cas/seam/a71",
        storage_bucket="bucket-demo",
        storage_generation=3,
        actor_type="agent",
        actor_id=AGENT,
        reservation_id="",
    )
    fields.update(over)
    return acct.ImmutableVersionCommit(**fields)


async def test_reserve_rejects_unknown_drive_and_negative_size(app_with_lifespan):
    async with conn() as c:
        with pytest.raises(acct.AccountingError):
            await acct.reserve_version_bytes(
                c, workspace_id=WS, drive_id="drv_000000000000dead",
                principal_id=AGENT, upload_id=None, size_bytes=1,
            )


async def test_storage_limit_applies_without_direct_transfer(app_with_lifespan):
    async with conn() as c, c.transaction():
        await _seed(c, DRIVE_A, ROOT_A)
        await c.execute(
            "UPDATE drives SET storage_bytes=9 WHERE id=$1",
            DRIVE_A,
        )
        await c.execute(
            "INSERT INTO workspace_storage (workspace_id, committed_bytes) "
            "VALUES ($1, 9)",
            WS,
        )
        with pytest.raises(acct.QuotaExceededError):
            await acct.reserve_version_bytes(
                c,
                workspace_id=WS,
                drive_id=DRIVE_A,
                principal_id=AGENT,
                upload_id=None,
                size_bytes=2,
                workspace_limit_bytes=100,
                drive_limit_bytes=10,
            )
        await _seed(c, DRIVE_A, ROOT_A)
        with pytest.raises(acct.AccountingError):
            await acct.reserve_version_bytes(
                c, workspace_id=WS, drive_id=DRIVE_A,
                principal_id=AGENT, upload_id=None, size_bytes=-1,
            )


async def test_release_argument_validation(app_with_lifespan):
    async with conn() as c:
        with pytest.raises(acct.AccountingError):
            await acct.release_version_reservation(c)
        with pytest.raises(acct.AccountingError):
            await acct.release_version_reservation(
                c, reservation_id="rsv_0000000000000001",
                upload_id="upld_0000000000000001",
            )
        # releasing something that never existed is a detected no-op
        assert await acct.release_version_reservation(
            c, reservation_id="rsv_00000000000000ff"
        ) is False


async def test_commit_requires_a_live_matching_reservation(app_with_lifespan):
    async with conn() as c:
        await _seed(c, DRIVE_A, ROOT_A)
        await _seed(c, DRIVE_B, ROOT_B)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000a71', $1, $2, 'seam.bin', "
            "'rev_0000000000000a72')",
            DRIVE_A, ROOT_A,
        )
        # (a) a consumed reservation cannot convert twice
        async with c.transaction():
            rsv = await acct.reserve_version_bytes(
                c, workspace_id=WS, drive_id=DRIVE_A, principal_id=AGENT,
                upload_id=None, size_bytes=5,
            )
            await acct.commit_immutable_version(c, _commit_request(reservation_id=rsv))
        with pytest.raises(acct.AccountingError):
            await acct.commit_immutable_version(
                c,
                _commit_request(
                    reservation_id=rsv, version_id="ver_0000000000000a72",
                    ordinal=2,
                ),
            )
        # (b) a reservation made for another DRIVE cannot back this commit
        with pytest.raises(acct.AccountingError):
            async with c.transaction():
                other = await acct.reserve_version_bytes(
                    c, workspace_id=WS, drive_id=DRIVE_B, principal_id=AGENT,
                    upload_id=None, size_bytes=5,
                )
                await acct.commit_immutable_version(
                    c,
                    _commit_request(
                        reservation_id=other, version_id="ver_0000000000000a73",
                        ordinal=2,
                    ),
                )


async def test_commit_returns_wire_payload_without_storage_coordinates(
    app_with_lifespan,
):
    async with conn() as c:
        await _seed(c, DRIVE_A, ROOT_A)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000a74', $1, $2, 'payload.bin', "
            "'rev_0000000000000a74')",
            DRIVE_A, ROOT_A,
        )
        async with c.transaction():
            rsv = await acct.reserve_version_bytes(
                c, workspace_id=WS, drive_id=DRIVE_A, principal_id=AGENT,
                upload_id=None, size_bytes=5,
            )
            payload = await acct.commit_immutable_version(
                c,
                _commit_request(
                    reservation_id=rsv,
                    artifact_id="art_0000000000000a74",
                    version_id="ver_0000000000000a74",
                ),
            )
        assert payload["id"] == "ver_0000000000000a74"
        assert payload["size_bytes"] == 5
        assert payload["hash"] == "sha256:a71"
        # storage coordinates never cross the wire shape
        for secret in ("storage_object", "storage_bucket", "storage_generation"):
            assert secret not in payload


async def test_parity_helper_reports_counter_and_live_sum(app_with_lifespan):
    async with conn() as c:
        await _seed(c, DRIVE_A, ROOT_A)
        counter, live_sum = await acct.storage_bytes_parity(c, DRIVE_A)
        assert (counter, live_sum) == (0, 0)
        await c.execute(
            "INSERT INTO artifacts (id, drive_id, parent_id, name, revision) "
            "VALUES ('art_0000000000000a75', $1, $2, 'parity.bin', "
            "'rev_0000000000000a75')",
            DRIVE_A, ROOT_A,
        )
        async with c.transaction():
            rsv = await acct.reserve_version_bytes(
                c, workspace_id=WS, drive_id=DRIVE_A, principal_id=AGENT,
                upload_id=None, size_bytes=5,
            )
            await acct.commit_immutable_version(
                c,
                _commit_request(
                    reservation_id=rsv,
                    artifact_id="art_0000000000000a75",
                    version_id="ver_0000000000000a75",
                ),
            )
        counter, live_sum = await acct.storage_bytes_parity(c, DRIVE_A)
        assert counter == live_sum == 5
        # a hand-tampered counter is REPORTED as divergent, not equalized
        await c.execute(
            "UPDATE drives SET storage_bytes = 9 WHERE id = $1", DRIVE_A
        )
        counter, live_sum = await acct.storage_bytes_parity(c, DRIVE_A)
        assert (counter, live_sum) == (9, 5)


async def test_version_columns_are_single_sourced():
    """Producer SELECTs import the seam's column list, so propagation reads
    can never drift from the one INSERT."""
    from agentdrive.core import v0_artifacts

    assert v0_artifacts._VERSION_COLUMNS is acct.VERSION_COLUMNS
    for column in ("storage_bucket", "storage_generation"):
        assert column in acct.VERSION_COLUMNS
