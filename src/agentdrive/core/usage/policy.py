from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from agentdrive.config import settings

from .models import DriveLimitsV1, EffectiveLimit, Metric, Period, ScopeType

GIB = 1024**3


def default_drive_limits() -> DriveLimitsV1:
    return DriveLimitsV1(
        version=1,
        storage_bytes_drive=10 * GIB,
        storage_bytes_workspace=50 * GIB,
        upload_bytes_hour_principal=10 * GIB,
        upload_bytes_hour_workspace=25 * GIB,
        download_bytes_day_workspace=50 * GIB,
        download_bytes_month_workspace=250 * GIB,
        public_share_bytes_day=5 * GIB,
    )


def deployment_safety_ceilings() -> dict[str, int]:
    return {
        "storage_bytes_drive": settings.drive_limit_safety_storage_bytes_drive,
        "storage_bytes_workspace": settings.drive_limit_safety_storage_bytes_workspace,
        "upload_bytes_hour_principal": (
            settings.drive_limit_safety_upload_bytes_hour_principal
        ),
        "upload_bytes_hour_workspace": (
            settings.drive_limit_safety_upload_bytes_hour_workspace
        ),
        "download_bytes_day_workspace": (
            settings.drive_limit_safety_download_bytes_day_workspace
        ),
        "download_bytes_month_workspace": (
            settings.drive_limit_safety_download_bytes_month_workspace
        ),
        "public_share_bytes_day": settings.drive_limit_safety_public_share_bytes_day,
    }


def parse_drive_limits_v1(
    value: Any, *, safety_ceilings: Mapping[str, int] | None = None
) -> DriveLimitsV1:
    if not isinstance(value, dict):
        raise ValueError("drive_limits must be an object")
    names = {field.name for field in fields(DriveLimitsV1)}
    if set(value) != names:
        raise ValueError("drive_limits fields do not match version 1")
    if value.get("version") != 1 or isinstance(value.get("version"), bool):
        raise ValueError("drive_limits version must be 1")
    ceilings = safety_ceilings or deployment_safety_ceilings()
    parsed: dict[str, int] = {"version": 1}
    for name in names - {"version"}:
        amount = value[name]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError(f"drive_limits.{name} must be a positive integer")
        ceiling = ceilings.get(name)
        if ceiling is None or amount > ceiling:
            raise ValueError(f"drive_limits.{name} exceeds the deployment ceiling")
        parsed[name] = amount
    return DriveLimitsV1(**parsed)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ActorLimitPolicy:
    storage_drive: int
    storage_workspace: int
    upload: tuple[EffectiveLimit, ...]
    private_download: tuple[EffectiveLimit, ...]


def for_actor(actor: Any) -> ActorLimitPolicy:
    limits = actor.drive_limits
    return ActorLimitPolicy(
        storage_drive=limits.storage_bytes_drive,
        storage_workspace=limits.storage_bytes_workspace,
        upload=(
            EffectiveLimit(
                metric=Metric.UPLOAD_BYTES,
                scope_type=ScopeType.PRINCIPAL,
                scope_id=actor.subject,
                period=Period.HOUR,
                limit=limits.upload_bytes_hour_principal,
            ),
            EffectiveLimit(
                metric=Metric.UPLOAD_BYTES,
                scope_type=ScopeType.WORKSPACE,
                scope_id=actor.workspace_id,
                period=Period.HOUR,
                limit=limits.upload_bytes_hour_workspace,
            ),
        ),
        private_download=(
            EffectiveLimit(
                metric=Metric.DOWNLOAD_BYTES,
                scope_type=ScopeType.WORKSPACE,
                scope_id=actor.workspace_id,
                period=Period.DAY,
                limit=limits.download_bytes_day_workspace,
            ),
            EffectiveLimit(
                metric=Metric.DOWNLOAD_BYTES,
                scope_type=ScopeType.WORKSPACE,
                scope_id=actor.workspace_id,
                period=Period.MONTH,
                limit=limits.download_bytes_month_workspace,
            ),
        ),
    )
