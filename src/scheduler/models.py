"""SAGE Durable Scheduler -- Domain Models & State Enums.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12
"""

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Dict, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


@unique
class ScheduleType(str, Enum):
    """Triggering mechanism for a schedule."""

    ONE_SHOT = "ONE_SHOT"
    CRON_RECURRING = "CRON_RECURRING"
    EVENT_TRIGGERED = "EVENT_TRIGGERED"


@unique
class ScheduleStatus(str, Enum):
    """Operational lifecycle status of a schedule."""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@unique
class MissedRunPolicy(str, Enum):
    """Policy for handling scheduled runs that were missed during host downtime."""

    RUN_ONCE = "RUN_ONCE"
    SKIP = "SKIP"
    FAIL_ALERT = "FAIL_ALERT"


@unique
class ScheduleExecutionStatus(str, Enum):
    """Status of an individual schedule trigger execution."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ScheduleDefinition(BaseModel):
    """Persistent definition of a scheduled task or workflow execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: str
    target_action: str
    workflow_id: Optional[str] = None
    project_id: Optional[str] = None
    schedule_type: ScheduleType
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    trigger_condition: Optional[Dict[str, Any]] = None
    next_run_at: str
    last_run_at: Optional[str] = None
    missed_run_policy: MissedRunPolicy = MissedRunPolicy.RUN_ONCE
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    is_enabled: bool = True
    parameters: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class ScheduleExecutionRecord(BaseModel):
    """Persistent, deduplicated record of an individual scheduled execution attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str = Field(default_factory=lambda: str(uuid4()))
    schedule_id: str
    run_id: Optional[str] = None
    scheduled_for: str
    triggered_at: str
    status: ScheduleExecutionStatus = ScheduleExecutionStatus.PENDING
    result_summary: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None
