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
from src.manager.supervisor import Supervisor
from src.tools.broker import ToolBroker
from src.trust.authority import TrustAuthority

__all__ = [
    # Models & Enums
    "CheckpointSnapshot",
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
) -> Supervisor:
    """Convenience factory to wire up a Supervisor with a fresh StateStore."""
    store = StateStore(conn)
    return Supervisor(
        state_store=store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        fast_decision=fast_decision,
    )
