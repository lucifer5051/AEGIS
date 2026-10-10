"""SAGE Notification Subsystem -- Typed Error Hierarchy.

All notification-related errors extend NotificationError, which extends RuntimeError.
No notification error must corrupt workflow state or grant capabilities.
"""

from __future__ import annotations


class NotificationError(RuntimeError):
    """Base class for all SAGE notification subsystem errors."""


class NotificationStorageError(NotificationError):
    """Raised when a durable store read or write fails."""


class NotificationSecretLeakError(NotificationError):
    """Raised when a notification payload contains a secret-class value."""


class NotificationDeliveryError(NotificationError):
    """Raised when all delivery attempts are exhausted."""


class NotificationNotFoundError(NotificationError):
    """Raised when a notification ID does not exist."""


class NotificationAlreadyAcknowledgedError(NotificationError):
    """Raised when trying to acknowledge an already-acknowledged notification."""


class DeliveryAdapterError(NotificationError):
    """Raised by a delivery adapter on a transient or permanent failure."""
