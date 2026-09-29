import pytest

from agentdrive.core.usage.gate import LimitExceeded, LimitGate
from agentdrive.core.usage.meter import LimitDecision, UsageCharge
from agentdrive.core.usage.models import EffectiveLimit, Metric, Period, ScopeType


class RefusingMeter:
    async def charge(self, connection, request):
        return LimitDecision(
            allowed=False,
            metric=request.metric,
            scope_type="workspace",
            limit=10,
            requested=request.amount,
        )


@pytest.mark.asyncio
async def test_gate_turns_refusal_into_typed_exception():
    gate = LimitGate(RefusingMeter())
    request = UsageCharge(
        "req_gate",
        Metric.DOWNLOAD_BYTES,
        11,
        (
            EffectiveLimit(
                Metric.DOWNLOAD_BYTES,
                ScopeType.WORKSPACE,
                "tcws_synthetic",
                Period.DAY,
                10,
            ),
        ),
    )
    with pytest.raises(LimitExceeded) as refused:
        await gate.charge(None, request)
    assert refused.value.decision.requested == 11
