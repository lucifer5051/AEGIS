"""SAGE Durable Workflow Engine -- Domain Models & State Enums.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 11
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 11

All definitions and execution models enforce strict typing and fail-closed state machines.
"""

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Dict, FrozenSet, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


@unique
class WorkflowStatus(str, Enum):
    """Lifecycle status of a workflow instance run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Return True if this status represents a finished execution."""
        return self in _TERMINAL_WORKFLOW_STATUSES

    def can_transition_to(self, target: WorkflowStatus) -> bool:
        """Check if transition from self to target status is valid."""
        return target in VALID_WORKFLOW_TRANSITIONS.get(self, frozenset())


_TERMINAL_WORKFLOW_STATUSES: FrozenSet[WorkflowStatus] = frozenset(
    {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    }
)

VALID_WORKFLOW_TRANSITIONS: Dict[WorkflowStatus, FrozenSet[WorkflowStatus]] = {
    WorkflowStatus.PENDING: frozenset(
        {
            WorkflowStatus.RUNNING,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.RUNNING: frozenset(
        {
            WorkflowStatus.PAUSED,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.PAUSED: frozenset(
        {
            WorkflowStatus.RUNNING,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.FAILED,
        }
    ),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
}


@unique
class WorkflowStepStatus(str, Enum):
    """Execution status of an individual step in a workflow run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"

    @property
    def is_terminal(self) -> bool:
        """Return True if step has terminated."""
        return self in {
            WorkflowStepStatus.COMPLETED,
            WorkflowStepStatus.FAILED,
            WorkflowStepStatus.SKIPPED,
        }


@unique
class FailurePolicy(str, Enum):
    """Strategy for handling a step execution failure."""

    STOP = "STOP"
    RETRY = "RETRY"
    IGNORE_AND_CONTINUE = "IGNORE_AND_CONTINUE"


@unique
class VerificationPolicy(str, Enum):
    """Level of verification required for step outputs."""

    STRICT = "STRICT"
    RELAXED = "RELAXED"
    NONE = "NONE"


class WorkflowStepDefinition(BaseModel):
    """Specification of an individual step in a workflow DAG."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    title: str
    skill_id: Optional[str] = None
    tool_name: Optional[str] = None
    action_name: Optional[str] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)
    dependencies: List[str] = Field(default_factory=list)
    is_idempotent: bool = False
    idempotency_key: Optional[str] = None
    max_retries: int = 2
    timeout_seconds: Optional[int] = None
    failure_policy: FailurePolicy = FailurePolicy.STOP
    verification_policy: VerificationPolicy = VerificationPolicy.STRICT
    required_capabilities: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowBudget(BaseModel):
    """Resource and execution limits configured for a workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_execution_seconds: Optional[int] = None
    max_total_retries: int = 5
    max_concurrency: int = 1


class WorkflowDefinition(BaseModel):
    """Versioned immutable definition of a workflow Directed Acyclic Graph (DAG)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    name: str
    version: int = 1
    project_id: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    steps: List[WorkflowStepDefinition]
    budget: WorkflowBudget = Field(default_factory=WorkflowBudget)
    created_at: str
    updated_at: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowStepExecutionRecord(BaseModel):
    """Durable record of a single step execution within a workflow run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    status: WorkflowStepStatus = WorkflowStepStatus.PENDING
    retry_count: int = 0
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    result_summary: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class WorkflowRun(BaseModel):
    """Durable instance and execution state of a workflow run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(default_factory=lambda: str(uuid4()))
    workflow_id: str
    workflow_version: int = 1
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    status: WorkflowStatus = WorkflowStatus.PENDING
    triggered_by: str = "user"
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    step_states: Dict[str, WorkflowStepExecutionRecord] = Field(default_factory=dict)
    error_summary: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None
