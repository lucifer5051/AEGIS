"""SAGE Notification Subsystem -- Public API.

Import from here. Do not import directly from sub-modules in application code.
"""

from __future__ import annotations

from src.notifications.adapters import (
    DeliveryAdapter,
    LocalLogAdapter,
    build_default_adapters,
)
from src.notifications.errors import (
    DeliveryAdapterError,
    NotificationAlreadyAcknowledgedError,
    NotificationDeliveryError,
    NotificationError,
    NotificationNotFoundError,
    NotificationSecretLeakError,
    NotificationStorageError,
)
from src.notifications.manager import NotificationManager
from src.notifications.models import (
    DeliveryResult,
    Notification,
    NotificationAttempt,
    NotificationEventType,
    NotificationPriority,
    NotificationSourceType,
    NotificationStatus,
)
from src.notifications.store import NotificationStore

__all__ = [
    # Manager
    "NotificationManager",
    # Store
    "NotificationStore",
    # Models
    "Notification",
    "NotificationAttempt",
    "NotificationEventType",
    "NotificationPriority",
    "NotificationSourceType",
    "NotificationStatus",
    "DeliveryResult",
    # Adapters
    "DeliveryAdapter",
    "LocalLogAdapter",
    "build_default_adapters",
    # Errors
    "NotificationError",
    "NotificationStorageError",
    "NotificationSecretLeakError",
    "NotificationDeliveryError",
    "NotificationNotFoundError",
    "NotificationAlreadyAcknowledgedError",
    "DeliveryAdapterError",
]
