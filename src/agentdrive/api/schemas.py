"""Health-probe wire shapes.

This module once held the response models for the pre-reset REST surface. The
2026-07-30 v0 contract reset moved every request/response model next to its
router (`api/v0_models.py` and the `api/v0_*.py` modules), and the models left
here became unreachable — but they kept the *same class names* as their v0
replacements (`ArtifactOut`, `DriveOut`, `GrantOut`, and sixteen more), so a
mistaken import produced a legacy shape carrying fields the contract no longer
has (`restore_url`, `trash_bytes`, `purge_at`) with nothing to catch it. They
are deleted; git history holds them.

What remains is genuinely live: `/health` predates the `/v0` error envelope and
is consumed by load balancers, so it keeps its own shapes rather than adopting
the envelope. Do not add v0 models here — they belong beside their router.
"""

from typing import Literal

from pydantic import BaseModel


class HealthOut(BaseModel):
    status: Literal["ok"]


class HealthDegradedDetail(BaseModel):
    status: Literal["degraded"]
    error: str


class HealthDegradedResponse(BaseModel):
    """Legacy health-probe failure shape.

    Health predates the `/v0` error envelope and is consumed by load
    balancers. PR 1 documents the wire shape without changing it; convergence
    on the canonical API envelope is a separately reviewed compatibility
    decision.
    """

    detail: HealthDegradedDetail
