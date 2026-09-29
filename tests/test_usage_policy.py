from agentdrive.core.usage.models import Metric, Period, ScopeType
from agentdrive.core.usage.policy import default_drive_limits, for_actor
from agentdrive.identity.actor import V0ActorContext


def test_policy_builds_closed_actor_limits():
    actor = V0ActorContext(
        subject="tcusr_synthetic",
        subject_type="user",
        workspace_id="tcws_synthetic",
        membership_id="tcmem_synthetic",
        token_id="tctok_synthetic",
        scopes=frozenset(),
        drive_limits=default_drive_limits(),
    )

    policy = for_actor(actor)

    assert policy.upload == (
        policy.upload[0].__class__(
            metric=Metric.UPLOAD_BYTES,
            scope_type=ScopeType.PRINCIPAL,
            scope_id="tcusr_synthetic",
            period=Period.HOUR,
            limit=10 * 1024**3,
        ),
        policy.upload[1].__class__(
            metric=Metric.UPLOAD_BYTES,
            scope_type=ScopeType.WORKSPACE,
            scope_id="tcws_synthetic",
            period=Period.HOUR,
            limit=25 * 1024**3,
        ),
    )
