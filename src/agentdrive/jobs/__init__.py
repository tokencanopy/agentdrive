"""Scheduled job entrypoints (Cloud Run Jobs).

The live v0 job surface, rebuilt for the day-0 schema at B3 packet 1:

  * ``python -m agentdrive.jobs.gc`` — the GC sweeper Terraform schedules
    daily (plus weekly ``--orphan-sweep``). See `agentdrive.core.gc`.
  * ``python -m agentdrive.jobs.reconcile_generations`` — the guarded,
    idempotent backfill of `artifact_versions.storage_bucket` /
    `storage_generation` for legacy CAS rows (supports ``--dry-run``).

The pre-reset job framework (ContinuousWorker, audit tables, lock registry)
remains archived; these entrypoints are deliberately self-contained.
"""
