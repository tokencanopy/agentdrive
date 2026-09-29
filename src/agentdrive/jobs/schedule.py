"""When the maintenance jobs run: the one record of the cadence.

A deployment must run these, or it never reclaims storage: deleted artifacts
stay in the database and their bytes stay in the store, abandoned uploads keep
their reserved bytes, and usage is never finalized. A self-hosted install runs
them with `python -m agentdrive.jobs.scheduler`, which the process
supervisor starts inside the API container when `SCHEDULER_ENABLED=true`
(`compose.selfhost.yml` sets it); a deployment with its own scheduler (cron, a
Kubernetes CronJob, a cloud scheduler) runs the same commands on the same
cadence, listed by `python -m agentdrive.jobs.scheduler --list`.

Times are UTC, in five-field cron syntax. Each job is
`python -m <module> <args>` against the same settings as the API.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    cron: str
    module: str
    args: tuple[str, ...]
    # The scheduler kills a run that outlives this. The GC stops itself at
    # `GCSweeper.HARD_TIMEOUT_S` (50 min), so its kill comes after that.
    timeout_s: int
    description: str = ""


SCHEDULE: tuple[ScheduledJob, ...] = (
    ScheduledJob(
        name="gc-hourly",
        cron="0 * * * *",
        module="agentdrive.jobs.gc",
        args=("--sessions-only",),
        timeout_s=3600,
        description=(
            "Session phases only: terminalize abandoned uploads and release "
            "their reserved bytes. No object-store listing."
        ),
    ),
    ScheduledJob(
        name="gc-daily",
        cron="0 3 * * *",
        module="agentdrive.jobs.gc",
        args=(),
        timeout_s=3600,
        description=(
            "Full sweep: purge expired soft-deletes, mark-sweep unreferenced "
            "content blobs, clean up scratch."
        ),
    ),
    ScheduledJob(
        name="gc-weekly",
        cron="0 4 * * 0",
        module="agentdrive.jobs.gc",
        args=("--orphan-sweep",),
        timeout_s=3600,
        description="The full sweep plus the orphan sweep over purged drives' prefixes.",
    ),
    ScheduledJob(
        name="usage-snapshot",
        cron="*/15 * * * *",
        module="agentdrive.jobs.usage_snapshot",
        args=("--no-notify",),
        timeout_s=900,
        description="Finalize usage reservations and prune bounded usage history.",
    ),
)
