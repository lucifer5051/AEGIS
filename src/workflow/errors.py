"""SAGE Durable Workflow Engine -- Error Hierarchy.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 11
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 11
"""

from __future__ import annotations


class WorkflowError(Exception):
    """Base exception for all workflow subsystem errors."""


class WorkflowValidationError(WorkflowError):
    """Raised when a workflow definition fails structural or semantic validation."""


class CyclicDependencyError(WorkflowValidationError):
    """Raised when a dependency cycle is detected in a workflow DAG."""


class WorkflowNotFoundError(WorkflowError):
    """Raised when a requested workflow definition or run does not exist."""


class WorkflowStateError(WorkflowError):
    """Raised on an invalid workflow or step state transition."""


class WorkflowExecutionError(WorkflowError):
    """Raised when workflow step execution halts due to an unrecoverable failure."""


class WorkflowBudgetExceededError(WorkflowExecutionError):
    """Raised when a workflow execution exceeds its configured budget or deadline."""


class WorkflowSecurityError(WorkflowError):
    """Raised when a workflow violates Trust Core boundaries or secret invariants."""
