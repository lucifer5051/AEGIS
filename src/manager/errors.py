"""SAGE Supervisor & State Store -- Exception Hierarchy.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 27

All exceptions in this module encode fail-closed execution semantics.
Never leak secrets, passwords, tokens, or raw prompts in exception messages.
"""

from __future__ import annotations


class ManagerError(Exception):
    """Base exception for all supervisor and state-management failures."""


class TaskStateError(ManagerError):
    """Raised when an illegal or non-monotonic state transition is attempted,
    or when attempting to mutate a task or step in a terminal state.
    """


class TaskNotFoundError(ManagerError):
    """Raised when a referenced task does not exist in the state store."""


class SessionNotFoundError(ManagerError):
    """Raised when a referenced supervisor session does not exist in the state store."""


class StepNotFoundError(ManagerError):
    """Raised when a referenced task step does not exist in the state store."""


class ReconciliationError(ManagerError):
    """Raised when execution state recovery encounters unrecoverable corruption
    or irreconcilable step invariants.
    """


class StateStoreError(ManagerError):
    """Raised when a state store persistence or transaction operation fails."""


class SupervisorExecutionError(ManagerError):
    """Raised when an unrecoverable execution failure halts the supervisor loop."""
