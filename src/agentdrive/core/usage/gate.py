from __future__ import annotations

from dataclasses import dataclass

from agentdrive.config import settings
from agentdrive.db import DBConn

from .meter import LimitDecision, UsageCharge, UsageMeter, UsageReservation
from .models import Metric
from .policy import for_actor


class LimitExceeded(RuntimeError):
    def __init__(self, decision: LimitDecision):
        super().__init__("usage limit exceeded")
        self.decision = decision


@dataclass(frozen=True)
class LimitGate:
    meter: UsageMeter

    async def charge(self, connection: DBConn, charge: UsageCharge) -> LimitDecision:
        decision = await self.meter.charge(connection, charge)
        if not decision.allowed:
            raise LimitExceeded(decision)
        return decision

    async def reserve(
        self, connection: DBConn, reservation: UsageReservation
    ) -> LimitDecision:
        decision = await self.meter.reserve(connection, reservation)
        if not decision.allowed:
            raise LimitExceeded(decision)
        return decision

    async def charge_upload_authorized_bytes(
        self,
        connection: DBConn,
        *,
        actor,
        operation_key: str,
        size_bytes: int,
    ) -> LimitDecision:
        if settings.upload_byte_limit_mode == "off":
            return LimitDecision(allowed=True, metric=Metric.UPLOAD_BYTES)
        return await self.charge(
            connection,
            UsageCharge(
                operation_key=operation_key,
                metric=Metric.UPLOAD_BYTES,
                amount=size_bytes,
                limits=for_actor(actor).upload,
                enforce=settings.upload_byte_limit_mode == "enforce",
            ),
        )

    async def charge_private_download(
        self,
        connection: DBConn,
        *,
        actor,
        drive_id: str,
        operation_key: str,
        size_bytes: int,
    ) -> LimitDecision:
        if settings.private_download_limit_mode == "off":
            return LimitDecision(allowed=True, metric=Metric.DOWNLOAD_BYTES)
        decision = await self.charge(
            connection,
            UsageCharge(
                operation_key=operation_key,
                metric=Metric.DOWNLOAD_BYTES,
                amount=size_bytes,
                limits=for_actor(actor).private_download,
                enforce=settings.private_download_limit_mode == "enforce",
            ),
        )
        updated = await connection.execute(
            "UPDATE drives SET retrieval_bytes=retrieval_bytes+$2, updated_at=now() "
            "WHERE id=$1 AND workspace_id=$3 AND deleted_at IS NULL",
            drive_id,
            size_bytes,
            actor.workspace_id,
        )
        if updated != "UPDATE 1":
            raise RuntimeError("download accounting target disappeared")
        return decision


usage_gate = LimitGate(UsageMeter())
