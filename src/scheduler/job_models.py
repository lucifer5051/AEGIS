"""SAGE Background Worker & Persistent Task Scheduler -- Domain Models.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.trust.audit import _check_no_secrets


def _utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@unique
class JobStatus(str, Enum):
    """Operational lifecycle states of a background job."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@unique
class WorkerStatus(str, Enum):
    """Health and operational state of a background worker."""

    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    DEAD = "DEAD"


class BackgroundJob(BaseModel):
    """Persistent job representation for autonomous background execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: Optional[str] = None
    schedule_id: Optional[str] = None
    goal_text: str
    action_type: str = "goal_execution"
    status: JobStatus = JobStatus.QUEUED
    priority: int = 10  # 1 = highest, 10 = default, 20 = lowest
    created_at: str = Field(default_factory=_utc_now_iso)
    scheduled_at: str = Field(default_factory=_utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    worker_id: Optional[str] = None
    lease_expires_at: Optional[str] = None
    execution_id: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3
    next_retry_at: Optional[str] = None
    last_error: Optional[str] = None
    configuration_reference: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkerRegistration(BaseModel):
    """Persistent record of an active or historical background worker process."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_id: str
    hostname: str
    process_id: int
    status: WorkerStatus = WorkerStatus.ACTIVE
    started_at: str = Field(default_factory=_utc_now_iso)
    last_heartbeat_at: str = Field(default_factory=_utc_now_iso)
    concurrency_limit: int = 1
    active_jobs_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class JobExecutionResult(BaseModel):
    """Summary of a background worker job execution attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    status: JobStatus
    success: bool
    result_summary: str
    task_id: Optional[str] = None
    execution_duration_ms: float = 0.0
    error: Optional[str] = None
    requires_approval: bool = False
