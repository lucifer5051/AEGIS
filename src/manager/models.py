"""SAGE Supervisor & State Store -- Domain Models.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 27
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md

All domain records are frozen and immutable (ConfigDict(frozen=True, extra="forbid")).
"""

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Dict, FrozenSet, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# ==============================================================================
# ENUMERATIONS
# ==============================================================================


@unique
class SessionStatus(str, Enum):
    """Lifecycle status of a supervisor session."""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"


@unique
class TaskState(str, Enum):
    """Durable state machine states for tasks."""

    SUBMITTED = "SUBMITTED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Return True if this is a terminal state."""
        return self in _TERMINAL_TASK_STATES

    def can_transition_to(self, target: TaskState) -> bool:
        """Check if transitioning from self to target is valid."""
        return target in VALID_TASK_TRANSITIONS.get(self, frozenset())


_TERMINAL_TASK_STATES: FrozenSet[TaskState] = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    }
)


@unique
class ExecutionMode(str, Enum):
    """User-selectable task execution mode."""

    STANDARD = "STANDARD"
    PERSISTENT_COMPLETION = "PERSISTENT_COMPLETION"


VALID_TASK_TRANSITIONS: Dict[TaskState, FrozenSet[TaskState]] = {
    TaskState.SUBMITTED: frozenset({TaskState.PLANNING, TaskState.CANCELLED}),
    TaskState.PLANNING: frozenset(
        {
            TaskState.EXECUTING,
            TaskState.AWAITING_APPROVAL,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.AWAITING_APPROVAL: frozenset(
        {
            TaskState.EXECUTING,
            TaskState.PLANNING,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.EXECUTING: frozenset(
        {
            TaskState.AWAITING_APPROVAL,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


@unique
class StepState(str, Enum):
    """Durable state machine states for task steps."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

    @property
    def is_terminal(self) -> bool:
        """Return True if this step state is terminal."""
        return self in _TERMINAL_STEP_STATES

    def can_transition_to(self, target: StepState) -> bool:
        """Check if transitioning from self to target is valid."""
        return target in VALID_STEP_TRANSITIONS.get(self, frozenset())


_TERMINAL_STEP_STATES: FrozenSet[StepState] = frozenset(
    {
        StepState.COMPLETED,
        StepState.FAILED,
        StepState.SKIPPED,
    }
)


VALID_STEP_TRANSITIONS: Dict[StepState, FrozenSet[StepState]] = {
    StepState.PENDING: frozenset(
        {
            StepState.RUNNING,
            StepState.SKIPPED,
            StepState.FAILED,
        }
    ),
    StepState.RUNNING: frozenset(
        {
            StepState.COMPLETED,
            StepState.FAILED,
            StepState.AWAITING_APPROVAL,
            StepState.PENDING,  # Permitted on crash recovery for idempotent unconfirmed steps
        }
    ),
    StepState.AWAITING_APPROVAL: frozenset(
        {
            StepState.PENDING,
            StepState.RUNNING,
            StepState.FAILED,
            StepState.SKIPPED,
            StepState.COMPLETED,
        }
    ),
    StepState.COMPLETED: frozenset(),
    StepState.FAILED: frozenset(),
    StepState.SKIPPED: frozenset(),
}


@unique
class RecoveryDisposition(str, Enum):
    """Disposition assigned to an interrupted task/step during reconciliation."""

    RESUME = "RESUME"
    ROLLBACK = "ROLLBACK"
    ESCALATE_TO_USER = "ESCALATE_TO_USER"
    FAIL_TERMINAL = "FAIL_TERMINAL"


# ==============================================================================
# DOMAIN MODELS
# ==============================================================================


class SessionRecord(BaseModel):
    """Durable supervisor session record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    status: SessionStatus
    created_at: str
    updated_at: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    """Durable task record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    title: str
    state: TaskState
    priority: int = 100
    routing_intent: Optional[str] = None
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None
    error_summary: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def execution_mode(self) -> ExecutionMode:
        """Return the execution mode configured on this task (default STANDARD)."""
        raw = self.metadata.get("execution_mode", ExecutionMode.STANDARD.value)
        try:
            return ExecutionMode(raw)
        except ValueError:
            return ExecutionMode.STANDARD


class StepPlan(BaseModel):
    """Specification of an execution step when forming a plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_name: str
    tool_name: Optional[str] = None
    is_idempotent: bool = False
    idempotency_key: Optional[str] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)


class StepRecord(BaseModel):
    """Durable task step record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    task_id: str
    step_index: int
    action_name: str
    tool_name: Optional[str] = None
    state: StepState
    is_idempotent: bool = False
    idempotency_key: Optional[str] = None
    capability_id: Optional[str] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result_summary: Optional[str] = None
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None


class CheckpointSnapshot(BaseModel):
    """Durable checkpoint snapshot capturing state at a critical boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: str
    task_id: str
    step_id: Optional[str] = None
    checkpoint_seq: int
    snapshot: Dict[str, Any]
    created_at: str


class ReconciledTaskResult(BaseModel):
    """Outcome of reconciling a single in-flight task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    disposition: RecoveryDisposition
    previous_state: TaskState
    new_state: TaskState
    action_summary: str
    step_id: Optional[str] = None


class ReconciliationReport(BaseModel):
    """Structured report produced by the ReconciliationEngine on startup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: str
    tasks_discovered: int
    tasks_resumed: int
    tasks_escalated: int
    tasks_failed: int
    reconciled_tasks: List[ReconciledTaskResult] = Field(default_factory=list)
