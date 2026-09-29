from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Metric(StrEnum):
    UPLOAD_BYTES = "upload_bytes"
    DOWNLOAD_BYTES = "download_bytes"
    PUBLIC_BYTES = "public_bytes"
    REQUESTS = "requests"


class ScopeType(StrEnum):
    WORKSPACE = "workspace"
    DRIVE = "drive"
    PRINCIPAL = "principal"
    SHARE = "share"
    SHARE_IP = "share_ip"


class Period(StrEnum):
    TEN_SECONDS = "ten_seconds"
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"


@dataclass(frozen=True)
class DriveLimitsV1:
    version: Literal[1]
    storage_bytes_drive: int
    storage_bytes_workspace: int
    upload_bytes_hour_principal: int
    upload_bytes_hour_workspace: int
    download_bytes_day_workspace: int
    download_bytes_month_workspace: int
    public_share_bytes_day: int


@dataclass(frozen=True)
class EffectiveLimit:
    metric: Metric
    scope_type: ScopeType
    scope_id: str
    period: Period | None
    limit: int
