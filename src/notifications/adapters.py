"""SAGE Notification Subsystem -- Delivery Adapter Interface & Local Adapter.

Defines the provider-independent DeliveryAdapter base class and implements
LocalLogAdapter -- a safe, always-available adapter that writes to the
Python logging system.

Design invariants:
- Adapters MUST NOT approve or execute any action.
- Adapters MUST NOT store raw secrets. Payload content was sanitized by
  NotificationManager before dispatch.
- A delivery failure by an adapter MUST NOT affect notification store
  state beyond recording the failed attempt.
- Adapters are replaceable: future adapters (desktop toast, etc.) satisfy
  the same interface without changing NotificationManager.

Future adapters to register here:
  - DesktopToastAdapter (Windows toast notifications via win10toast or similar)
  - WhatsAppAdapter (PROPOSED-FUTURE, external integration)
  - EmailAdapter (PROPOSED-FUTURE, external integration)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from src.notifications.errors import DeliveryAdapterError
from src.notifications.models import DeliveryResult, Notification

logger = logging.getLogger(__name__)


# ==============================================================================
# ABSTRACT ADAPTER INTERFACE
# ==============================================================================


class DeliveryAdapter(ABC):
    """Provider-independent delivery adapter interface.

    Concrete adapters implement send() and return a DeliveryResult.
    They must never raise exceptions that propagate beyond NotificationManager;
    instead they encode errors in DeliveryResult.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Unique stable name identifying this adapter (e.g. 'local_log')."""

    @abstractmethod
    def send(self, notification: Notification) -> DeliveryResult:
        """Attempt delivery of a notification.

        Args:
            notification: The notification to deliver. Content is sanitized.

        Returns:
            DeliveryResult with succeeded=True on success, succeeded=False on
            transient or permanent failure.

        Must not raise any exception. Catch all errors internally and encode
        them in DeliveryResult.error_message.
        """


# ==============================================================================
# LOCAL LOG ADAPTER (always available, safe default)
# ==============================================================================


class LocalLogAdapter(DeliveryAdapter):
    """Delivers notifications by writing structured log lines.

    This adapter is always available regardless of platform.  It ensures
    that no notification is silently dropped even when richer delivery
    mechanisms are unavailable.

    Security:
    - Only logs the title, body, event_type, priority, source_id, and
      notification_id.  No metadata fields are logged (they might accumulate
      operational context the user did not intend to expose in plain logs).
    - Never logs secrets; payload sanitization happens upstream in
      NotificationManager before reaching here.
    """

    ADAPTER_NAME = "local_log"

    @property
    def adapter_name(self) -> str:
        return self.ADAPTER_NAME

    def send(self, notification: Notification) -> DeliveryResult:
        """Write notification to Python logging system."""
        try:
            log_level = self._priority_to_log_level(notification.priority.value)
            logger.log(
                log_level,
                "[NOTIFICATION/%s/%s] %s | %s | source=%s:%s | id=%s",
                notification.priority.value,
                notification.event_type.value,
                notification.title,
                notification.body,
                notification.source_type or "-",
                notification.source_id or "-",
                notification.notification_id,
            )
            return DeliveryResult(
                succeeded=True,
                adapter=self.ADAPTER_NAME,
                response_summary=f"Logged at level {log_level}",
            )
        except Exception as exc:
            return DeliveryResult(
                succeeded=False,
                adapter=self.ADAPTER_NAME,
                error_message=f"Log adapter error: {exc}",
            )

    @staticmethod
    def _priority_to_log_level(priority: str) -> int:
        """Map notification priority to Python logging level."""
        mapping = {
            "P0": logging.CRITICAL,
            "P1": logging.ERROR,
            "P2": logging.WARNING,
            "P3": logging.INFO,
            "P4": logging.DEBUG,
        }
        return mapping.get(priority, logging.INFO)


# ==============================================================================
# FACTORY
# ==============================================================================


def build_default_adapters() -> list[DeliveryAdapter]:
    """Return the ordered list of default delivery adapters.

    LocalLogAdapter is always first. Future adapters (desktop toast, etc.)
    are appended after it in priority order.
    """
    adapters: list[DeliveryAdapter] = [LocalLogAdapter()]
    # PROPOSED-FUTURE: DesktopToastAdapter(), if win10toast/winsdk available
    return adapters


__all__ = [
    "DeliveryAdapter",
    "LocalLogAdapter",
    "DeliveryAdapterError",
    "build_default_adapters",
]
