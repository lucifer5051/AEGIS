"""SAGE Durable Scheduler Subsystem.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12
"""

from __future__ import annotations

from src.scheduler.cron import compute_next_cron_run
from src.scheduler.errors import (
    DuplicateScheduleError,
    MissedRunError,
    ScheduleNotFoundError,
    SchedulerError,
    ScheduleValidationError,
)
from src.scheduler.job_models import (
    BackgroundJob,
    JobExecutionResult,
    JobStatus,
    WorkerRegistration,
    WorkerStatus,
)
from src.scheduler.job_queue import JobQueue
from src.scheduler.models import (
    MissedRunPolicy,
    ScheduleDefinition,
    ScheduleExecutionRecord,
    ScheduleExecutionStatus,
    ScheduleStatus,
    ScheduleType,
)
from src.scheduler.scheduler import WorkflowScheduler
from src.scheduler.store import SchedulerStore
from src.scheduler.worker_manager import WorkerManager

__all__ = [
    "WorkflowScheduler",
    "SchedulerStore",
    "JobQueue",
    "WorkerManager",
    "compute_next_cron_run",
    "BackgroundJob",
    "JobStatus",
    "WorkerRegistration",
    "WorkerStatus",
    "JobExecutionResult",
    "ScheduleDefinition",
    "ScheduleExecutionRecord",
    "ScheduleType",
    "ScheduleStatus",
    "MissedRunPolicy",
    "ScheduleExecutionStatus",
    "SchedulerError",
    "ScheduleNotFoundError",
    "DuplicateScheduleError",
    "ScheduleValidationError",
    "MissedRunError",
]
