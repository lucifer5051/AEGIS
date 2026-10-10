"""SAGE Notification Subsystem -- Notification Manager.

The NotificationManager is the single authoritative entry point for all
notification creation, delivery, retry, escalation, quiet-hours enforcement,
deduplication, and acknowledgement.

Security invariants (strictly enforced):
1. Notifications are purely advisory -- they NEVER approve actions automatically.
2. Only the Trust Core ApprovalStore can change approval state.
3. Notification delivery failure never bypasses approval requirements.
4. Untrusted event content is never interpreted as executable instructions.
5. Notification content (title, body, metadata) is sanitized for zero-secret
   invariant before any persistence or delivery attempt.
6. Expired or revoked approvals remain invalid regardless of notification state.

Delivery behavior:
- Attempts delivery through all registered adapters in order.
- Records each attempt (succeeded/failed) in NotificationAttempt table.
- Retries transient failures up to max_delivery_attempts (bounded).
- After all attempts exhausted: marks notification as FAILED.
- A FAILED notification never blocks workflow state or task completion.

Quiet hours enforcement:
- During quiet hours, P2/P3/P4 notifications are SUPPRESSED (recorded, not delivered).
- P0 and P1 notifications bypass quiet hours regardless of configuration.

Deduplication:
- Before creating a notification, checks whether an active notification with
  the same dedup_key already exists within the deduplication window.
- If a duplicate is detected, the new notification is created with status
  SUPPRESSED and delivery is skipped.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.config.models import NotificationConfig
from src.notifications.adapters import DeliveryAdapter, build_default_adapters
from src.notifications.errors import NotificationAlreadyAcknowledgedError, NotificationNotFoundError
from src.notifications.models import (
    Notification,
    NotificationEventType,
    NotificationPriority,
    NotificationStatus,
)
from src.notifications.store import NotificationStore
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)

# Priorities that bypass quiet hours (critical and high urgency)
_BYPASS_QUIET_HOURS = {NotificationPriority.P0, NotificationPriority.P1}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


class NotificationManager:
    """Manages notification lifecycle: creation, delivery, retry, escalation, acknowledgement.

    Thread safety: Each operation acquires its own SQLite transaction via
    NotificationStore. Concurrent access is safe under SQLite WAL mode.
    """

    def __init__(
        self,
        store: NotificationStore,
        config: Optional[NotificationConfig] = None,
        adapters: Optional[List[DeliveryAdapter]] = None,
    ) -> None:
        self._store = store
        self._config = config or NotificationConfig()
        self._adapters = adapters if adapters is not None else build_default_adapters()

    # ==========================================================================
    # PUBLIC API: NOTIFICATION CREATION
    # ==========================================================================

    def notify(
        self,
        event_type: NotificationEventType,
        title: str,
        body: str,
        priority: Optional[NotificationPriority] = None,
        source_type: Optional[str] = None,
        source_id: Optional[str] = None,
        project_id: Optional[str] = None,
        dedup_key: Optional[str] = None,
        max_delivery_attempts: Optional[int] = None,
        expires_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        notification_id: Optional[str] = None,
    ) -> Notification:
        """Create and attempt delivery of a new notification.

        Returns the persisted Notification (status may be DELIVERED, FAILED,
        or SUPPRESSED depending on quiet hours and deduplication).

        Never raises on delivery failure -- persists the failure state instead.
        """
        resolved_priority = priority or self._default_priority()
        notification_id = notification_id or str(uuid4())
        max_attempts = (
            max_delivery_attempts
            if max_delivery_attempts is not None
            else self._config.max_delivery_attempts
            if hasattr(self._config, "max_delivery_attempts")
            else 3
        )  # noqa: E501

        # --- 1. Deduplication check ---
        if dedup_key and self._store.exists_with_dedup_key(dedup_key, active_only=True):
            logger.debug(
                "Notification suppressed (dedup): key=%s, event=%s",
                dedup_key,
                event_type.value,
            )
            notification = self._store.create_notification(
                notification_id=notification_id,
                event_type=event_type,
                priority=resolved_priority,
                title=title,
                body=body,
                source_type=source_type,
                source_id=source_id,
                project_id=project_id,
                dedup_key=dedup_key,
                max_delivery_attempts=max_attempts,
                expires_at=expires_at,
                metadata=metadata,
            )
            return self._store.update_status(notification_id, NotificationStatus.SUPPRESSED)

        # --- 2. Persist notification (PENDING) ---
        notification = self._store.create_notification(
            notification_id=notification_id,
            event_type=event_type,
            priority=resolved_priority,
            title=title,
            body=body,
            source_type=source_type,
            source_id=source_id,
            project_id=project_id,
            dedup_key=dedup_key,
            max_delivery_attempts=max_attempts,
            expires_at=expires_at,
            metadata=metadata,
        )

        # --- 3. Quiet hours enforcement ---
        if self._is_quiet_hours() and resolved_priority not in _BYPASS_QUIET_HOURS:
            logger.debug(
                "Notification suppressed (quiet hours): id=%s priority=%s",
                notification_id,
                resolved_priority.value,
            )
            return self._store.update_status(notification_id, NotificationStatus.SUPPRESSED)

        # --- 4. Attempt delivery ---
        return self._deliver(notification)

    def notify_approval_required(
        self,
        action_name: str,
        request_id: str,
        actor: str,
        project_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> Notification:
        """Emit an APPROVAL_REQUIRED notification.

        Security: This notification ONLY informs the user. It does not
        grant, approve, or change the approval state in any way.
        """
        title = f"Approval Required: {action_name}"
        body = (
            f"Action '{action_name}' requested by '{actor}' requires explicit approval. "
            f"Request ID: {request_id}. "
            f"Use the Trust Core approval interface to approve or reject."
        )
        return self.notify(
            event_type=NotificationEventType.APPROVAL_REQUIRED,
            title=title,
            body=body,
            priority=NotificationPriority.P1,
            source_type="approval",
            source_id=request_id,
            project_id=project_id,
            dedup_key=f"approval_required:{request_id}",
            expires_at=expires_at,
        )

    def notify_approval_expired(
        self,
        action_name: str,
        request_id: str,
    ) -> Notification:
        """Emit an APPROVAL_EXPIRED notification."""
        title = f"Approval Expired: {action_name}"
        body = (
            f"The pending approval for '{action_name}' (request: {request_id}) "
            f"has expired. The action will not be executed. Submit a new request if needed."
        )
        return self.notify(
            event_type=NotificationEventType.APPROVAL_EXPIRED,
            title=title,
            body=body,
            priority=NotificationPriority.P2,
            source_type="approval",
            source_id=request_id,
            dedup_key=f"approval_expired:{request_id}",
        )

    def notify_workflow_completed(
        self,
        workflow_id: str,
        run_id: str,
        project_id: Optional[str] = None,
    ) -> Notification:
        """Emit a WORKFLOW_COMPLETED notification."""
        return self.notify(
            event_type=NotificationEventType.WORKFLOW_COMPLETED,
            title=f"Workflow Completed: {workflow_id}",
            body=f"Workflow '{workflow_id}' (run: {run_id}) completed successfully.",
            priority=NotificationPriority.P2,
            source_type="workflow",
            source_id=run_id,
            project_id=project_id,
            dedup_key=f"workflow_completed:{run_id}",
        )

    def notify_workflow_failed(
        self,
        workflow_id: str,
        run_id: str,
        error_summary: str,
        project_id: Optional[str] = None,
    ) -> Notification:
        """Emit a WORKFLOW_FAILED notification."""
        # Ensure error summary does not leak secrets
        _check_no_secrets(error_summary)
        return self.notify(
            event_type=NotificationEventType.WORKFLOW_FAILED,
            title=f"Workflow Failed: {workflow_id}",
            body=f"Workflow '{workflow_id}' (run: {run_id}) failed: {error_summary}",
            priority=NotificationPriority.P1,
            source_type="workflow",
            source_id=run_id,
            project_id=project_id,
            dedup_key=f"workflow_failed:{run_id}",
        )

    def notify_workflow_paused(
        self,
        workflow_id: str,
        run_id: str,
        reason: str,
        project_id: Optional[str] = None,
    ) -> Notification:
        """Emit a WORKFLOW_PAUSED (intervention required) notification."""
        _check_no_secrets(reason)
        return self.notify(
            event_type=NotificationEventType.WORKFLOW_PAUSED,
            title=f"Workflow Paused — Intervention Required: {workflow_id}",
            body=(
                f"Workflow '{workflow_id}' (run: {run_id}) is paused and requires "
                f"intervention: {reason}"
            ),
            priority=NotificationPriority.P1,
            source_type="workflow",
            source_id=run_id,
            project_id=project_id,
            dedup_key=f"workflow_paused:{run_id}",
        )

    def notify_schedule_failed(
        self,
        schedule_id: str,
        target_action: str,
        error_summary: str,
    ) -> Notification:
        """Emit a SCHEDULE_FAILED notification."""
        _check_no_secrets(error_summary)
        return self.notify(
            event_type=NotificationEventType.SCHEDULE_FAILED,
            title=f"Scheduled Task Failed: {target_action}",
            body=(
                f"Scheduled task '{target_action}' (schedule: {schedule_id}) "
                f"failed: {error_summary}"
            ),
            priority=NotificationPriority.P1,
            source_type="schedule",
            source_id=schedule_id,
            dedup_key=f"schedule_failed:{schedule_id}",
        )

    def notify_schedule_missed(
        self,
        schedule_id: str,
        target_action: str,
        delay_seconds: float,
    ) -> Notification:
        """Emit a SCHEDULE_MISSED notification."""
        return self.notify(
            event_type=NotificationEventType.SCHEDULE_MISSED,
            title=f"Scheduled Task Missed: {target_action}",
            body=(
                f"Scheduled task '{target_action}' (schedule: {schedule_id}) was missed. "
                f"Execution was overdue by {delay_seconds:.0f}s."
            ),
            priority=NotificationPriority.P2,
            source_type="schedule",
            source_id=schedule_id,
            dedup_key=f"schedule_missed:{schedule_id}",
        )

    def notify_resource_constraint(
        self,
        resource_name: str,
        detail: str,
        source_id: Optional[str] = None,
    ) -> Notification:
        """Emit a RESOURCE_CONSTRAINT notification."""
        _check_no_secrets(detail)
        return self.notify(
            event_type=NotificationEventType.RESOURCE_CONSTRAINT,
            title=f"Resource Constraint: {resource_name}",
            body=f"Resource '{resource_name}' prevented execution: {detail}",
            priority=NotificationPriority.P2,
            source_type="system",
            source_id=source_id,
            dedup_key=f"resource_constraint:{resource_name}:{source_id or 'global'}",
        )

    def notify_recovery_exhausted(
        self,
        task_id: str,
        error_summary: str,
    ) -> Notification:
        """Emit a RECOVERY_EXHAUSTED notification."""
        _check_no_secrets(error_summary)
        return self.notify(
            event_type=NotificationEventType.RECOVERY_EXHAUSTED,
            title="Recovery Exhausted — User Input Required",
            body=(
                f"All automatic recovery attempts for task '{task_id}' have been exhausted. "
                f"Error: {error_summary}. Manual intervention is required."
            ),
            priority=NotificationPriority.P0,
            source_type="system",
            source_id=task_id,
            dedup_key=f"recovery_exhausted:{task_id}",
        )

    # ==========================================================================
    # PUBLIC API: ACKNOWLEDGEMENT & DISMISSAL
    # ==========================================================================

    def acknowledge(self, notification_id: str) -> Notification:
        """Mark a notification as acknowledged by the user.

        Acknowledgement is informational only. It does NOT approve any action.
        """
        notification = self._store.get_notification(notification_id)
        if notification is None:
            raise NotificationNotFoundError(f"Notification '{notification_id}' not found.")
        if notification.status == NotificationStatus.ACKNOWLEDGED:
            raise NotificationAlreadyAcknowledgedError(
                f"Notification '{notification_id}' already acknowledged."
            )
        return self._store.update_status(
            notification_id,
            NotificationStatus.ACKNOWLEDGED,
            acknowledged_at=_now_iso(),
        )

    def dismiss(self, notification_id: str) -> Notification:
        """Mark a notification as dismissed (user chose to ignore it).

        Dismissal is informational only. It does NOT affect approval state.
        """
        notification = self._store.get_notification(notification_id)
        if notification is None:
            raise NotificationNotFoundError(f"Notification '{notification_id}' not found.")
        return self._store.update_status(
            notification_id,
            NotificationStatus.DISMISSED,
            dismissed_at=_now_iso(),
        )

    # ==========================================================================
    # PUBLIC API: PENDING NOTIFICATION DELIVERY (retry loop)
    # ==========================================================================

    def retry_pending_deliveries(self) -> List[Notification]:
        """Retry delivery for all PENDING notifications.

        Called periodically (e.g. by a background poll loop) to recover
        from transient delivery failures. Bounded by max_delivery_attempts.
        """
        pending = self._store.list_notifications(status=NotificationStatus.PENDING)
        results: List[Notification] = []
        for notification in pending:
            # Skip if max attempts already reached (will be marked FAILED here)
            if notification.delivery_attempts >= notification.max_delivery_attempts:
                failed = self._store.update_status(
                    notification.notification_id, NotificationStatus.FAILED
                )
                results.append(failed)
                continue
            delivered = self._deliver(notification)
            results.append(delivered)
        return results

    def list_notifications(
        self,
        status: Optional[NotificationStatus] = None,
        priority: Optional[NotificationPriority] = None,
        event_type: Optional[NotificationEventType] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Notification]:
        """Query persisted notifications with optional filters."""
        return self._store.list_notifications(
            status=status,
            priority=priority,
            event_type=event_type,
            project_id=project_id,
            limit=limit,
        )

    def get_notification(self, notification_id: str) -> Optional[Notification]:
        """Retrieve a single notification by ID."""
        return self._store.get_notification(notification_id)

    # ==========================================================================
    # INTERNAL DELIVERY
    # ==========================================================================

    def _deliver(self, notification: Notification) -> Notification:
        """Attempt delivery through registered adapters, record each attempt.

        Returns updated notification with final status (DELIVERED or FAILED).
        Delivery failure NEVER propagates as an exception.
        """
        attempt_number = notification.delivery_attempts + 1
        final_notification = notification

        for adapter in self._adapters:
            try:
                result = adapter.send(notification)
            except Exception as exc:
                # Adapter violated its contract (must not raise), but we absorb the error.
                result_error = f"Adapter {adapter.adapter_name} raised unexpectedly: {exc}"
                logger.error(result_error)
                self._store.record_attempt(
                    notification_id=notification.notification_id,
                    attempt_number=attempt_number,
                    adapter=adapter.adapter_name,
                    succeeded=False,
                    error_message=result_error,
                )
                continue

            self._store.record_attempt(
                notification_id=notification.notification_id,
                attempt_number=attempt_number,
                adapter=result.adapter,
                succeeded=result.succeeded,
                error_message=result.error_message,
                response_summary=result.response_summary,
            )

            if result.succeeded:
                final_notification = self._store.update_status(
                    notification.notification_id,
                    NotificationStatus.DELIVERED,
                    delivery_attempts=attempt_number,
                )
                return final_notification

        # All adapters failed this attempt
        final_attempts = attempt_number
        if final_attempts >= notification.max_delivery_attempts:
            final_notification = self._store.update_status(
                notification.notification_id,
                NotificationStatus.FAILED,
                delivery_attempts=final_attempts,
            )
            logger.warning(
                "Notification delivery permanently failed after %d attempts: id=%s event=%s",
                final_attempts,
                notification.notification_id,
                notification.event_type.value,
            )
        else:
            final_notification = self._store.update_status(
                notification.notification_id,
                NotificationStatus.PENDING,
                delivery_attempts=final_attempts,
            )

        return final_notification

    # ==========================================================================
    # HELPERS
    # ==========================================================================

    def _default_priority(self) -> NotificationPriority:
        """Map config default_priority string to NotificationPriority enum."""
        p = getattr(self._config, "default_priority", "P2")
        try:
            return NotificationPriority(p)
        except ValueError:
            return NotificationPriority.P2

    def _is_quiet_hours(self) -> bool:
        """Return True if current UTC time falls within configured quiet hours."""
        if not self._config.quiet_hours_enabled:
            return False
        try:
            now = _now_utc()
            start_h, start_m = [int(x) for x in self._config.quiet_hours_start_utc.split(":")]
            end_h, end_m = [int(x) for x in self._config.quiet_hours_end_utc.split(":")]

            now_minutes = now.hour * 60 + now.minute
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m

            if start_minutes <= end_minutes:
                # Same-day window e.g. 08:00-22:00
                return start_minutes <= now_minutes < end_minutes
            else:
                # Overnight window e.g. 22:00-08:00
                return now_minutes >= start_minutes or now_minutes < end_minutes
        except Exception:
            return False
