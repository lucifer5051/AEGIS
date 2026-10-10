"""SAGE Durable Scheduler -- Error Hierarchy.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12
"""

from __future__ import annotations


class SchedulerError(Exception):
    """Base exception for all scheduler errors."""


class ScheduleNotFoundError(SchedulerError):
    """Raised when a requested schedule definition does not exist."""


class DuplicateScheduleError(SchedulerError):
    """Raised when attempting to create a schedule with a conflicting ID."""


class ScheduleValidationError(SchedulerError):
    """Raised when schedule parameters, cron expressions, or conditions are invalid."""


class MissedRunError(SchedulerError):
    """Raised when a missed run cannot be safely resolved under policy."""
