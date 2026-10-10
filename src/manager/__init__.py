"""SAGE Supervisor & State Store Subsystem.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 27
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md

Public API for:
- Durable state persistence (StateStore)
- Startup state discovery and idempotent recovery (ReconciliationEngine)
- Central execution orchestration (Supervisor)
- Task, step, and session lifecycle models and errors
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from src.decision.service import FastDecisionService
from src.manager.errors import (
    ManagerError,
    ReconciliationError,
    SessionNotFoundError,
    StateStoreError,
    StepNotFoundError,
    SupervisorExecutionError,
    TaskNotFoundError,
    TaskStateError,
)
from src.manager.models import (
    VALID_STEP_TRANSITIONS,
    VALID_TASK_TRANSITIONS,
    CheckpointSnapshot,
    ReconciledTaskResult,
    ReconciliationReport,
    RecoveryDisposition,
    SessionRecord,
    SessionStatus,
    StepPlan,
    StepRecord,
    StepState,
    TaskRecord,
    TaskState,
)
from src.manager.reconciliation import ReconciliationEngine
from src.manager.state_store import StateStore
from src.manager.supervisor import GoalExecutionResult, Supervisor
from src.tools.broker import ToolBroker
from src.trust.authority import TrustAuthority

__all__ = [
    # Models & Enums
    "CheckpointSnapshot",
    "GoalExecutionResult",
    "ReconciledTaskResult",
    "ReconciliationReport",
    "RecoveryDisposition",
    "SessionRecord",
    "SessionStatus",
    "StepPlan",
    "StepRecord",
    "StepState",
    "TaskRecord",
    "TaskState",
    "VALID_STEP_TRANSITIONS",
    "VALID_TASK_TRANSITIONS",
    # Errors
    "ManagerError",
    "ReconciliationError",
    "SessionNotFoundError",
    "StateStoreError",
    "StepNotFoundError",
    "SupervisorExecutionError",
    "TaskNotFoundError",
    "TaskStateError",
    # Subsystem Components
    "ReconciliationEngine",
    "StateStore",
    "Supervisor",
    # Builders
    "build_supervisor",
]


def build_supervisor(
    conn: sqlite3.Connection,
    trust_authority: TrustAuthority,
    tool_broker: ToolBroker,
    fast_decision: Optional[FastDecisionService] = None,
    skill_registry: Optional[object] = None,
    clarification_gate: Optional[object] = None,
    admission_controller: Optional[object] = None,
    experience_service: Optional[object] = None,
    memory_store: Optional[object] = None,
    retriever: Optional[object] = None,
    lesson_manager: Optional[object] = None,
) -> Supervisor:
    """Convenience factory to wire up a Supervisor with a fresh StateStore.

    All skills-related subsystems are optional; omit them for plain
    tool-broker-only operation.
    """
    store = StateStore(conn)
    return Supervisor(
        state_store=store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        fast_decision=fast_decision,
        skill_registry=skill_registry,
        clarification_gate=clarification_gate,
        admission_controller=admission_controller,
        experience_service=experience_service,
        memory_store=memory_store,
        retriever=retriever,
        lesson_manager=lesson_manager,
    )
