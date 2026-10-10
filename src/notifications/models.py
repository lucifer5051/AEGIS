"""SAGE Notification Subsystem -- Typed Event Notification Models.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 9

Design constraints:
- Notifications are strictly advisory -- they NEVER approve, grant, or
  execute any action automatically.
- Notification content must be free of secrets (passwords, tokens, API
  keys, private keys). The zero-secret invariant is enforced at write time.
- Priority levels P0-P4 mirror SAGE urgency tiers (P0 = highest urgency).
- All datetimes are ISO-8601 UTC strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any, Dict, Optional

# ==============================================================================
# ENUMERATIONS
# ==============================================================================


@unique
class NotificationEventType(str, Enum):
    """Typed notification event classification (matches DB CHECK constraint)."""

    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    WORKFLOW_FAILED = "WORKFLOW_FAILED"
    WORKFLOW_PAUSED = "WORKFLOW_PAUSED"
    SCHEDULE_FAILED = "SCHEDULE_FAILED"
    SCHEDULE_MISSED = "SCHEDULE_MISSED"
    RESOURCE_CONSTRAINT = "RESOURCE_CONSTRAINT"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
    INTERVENTION_REQUIRED = "INTERVENTION_REQUIRED"
    GENERAL_INFO = "GENERAL_INFO"


@unique
class NotificationPriority(str, Enum):
    """Notification urgency tier (P0 = most urgent)."""

    P0 = "P0"  # Critical, user must be notified immediately
    P1 = "P1"  # High urgency: pending approval, execution failure
    P2 = "P2"  # Standard: workflow completion, schedule ran
    P3 = "P3"  # Low: informational
    P4 = "P4"  # Trace: background diagnostics


@unique
class NotificationStatus(str, Enum):
    """Delivery lifecycle state (matches DB CHECK constraint)."""

    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    DISMISSED = "DISMISSED"
    SUPPRESSED = "SUPPRESSED"  # Suppressed by quiet hours or deduplication


@unique
class NotificationSourceType(str, Enum):
    """Source subsystem that emitted the notification."""

    WORKFLOW = "workflow"
    SCHEDULE = "schedule"
    APPROVAL = "approval"
    SYSTEM = "system"


# ==============================================================================
# DATA CLASSES
# ==============================================================================


@dataclass(frozen=True)
class Notification:
    """Immutable representation of a single notification record.

    Secrets must never appear in title, body, or metadata_json.
    All string fields are sanitized at creation time by NotificationManager.
    """

    notification_id: str
    event_type: NotificationEventType
    priority: NotificationPriority
    title: str
    body: str
    status: NotificationStatus
    created_at: str
    updated_at: str
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    project_id: Optional[str] = None
    dedup_key: Optional[str] = None
    delivery_attempts: int = 0
    max_delivery_attempts: int = 3
    acknowledged_at: Optional[str] = None
    dismissed_at: Optional[str] = None
    expires_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NotificationAttempt:
    """Immutable record of a single delivery attempt."""

    attempt_id: str
    notification_id: str
    attempt_number: int
    adapter: str
    attempted_at: str
    succeeded: bool
    error_message: Optional[str] = None
    response_summary: Optional[str] = None


@dataclass(frozen=True)
class DeliveryResult:
    """Result returned by a delivery adapter's send() call."""

    succeeded: bool
    adapter: str
    error_message: Optional[str] = None
    response_summary: Optional[str] = None
