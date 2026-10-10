"""SAGE Notification Subsystem -- Durable SQLite Store.

Stores notifications and delivery attempts. All writes are atomic (ACID).
Notification content is validated for zero-secret invariant before persistence.

The store is decoupled from delivery adapters. Delivery failures never corrupt
notification state; the store only records what was attempted and the outcome.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.notifications.errors import NotificationNotFoundError, NotificationStorageError
from src.notifications.models import (
    Notification,
    NotificationAttempt,
    NotificationEventType,
    NotificationPriority,
    NotificationStatus,
)
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class NotificationStore:
    """Durable, append-friendly persistence for notifications and delivery attempts.

    Uses a shared sqlite3.Connection. Callers must provide a connection
    opened with WAL mode (initialize_runtime_database).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ==========================================================================
    # NOTIFICATION CRUD
    # ==========================================================================

    def create_notification(
        self,
        notification_id: str,
        event_type: NotificationEventType,
        priority: NotificationPriority,
        title: str,
        body: str,
        source_type: Optional[str] = None,
        source_id: Optional[str] = None,
        project_id: Optional[str] = None,
        dedup_key: Optional[str] = None,
        max_delivery_attempts: int = 3,
        expires_at: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Notification:
        """Persist a new notification record atomically.

        Raises NotificationStorageError on DB failure.
        Raises NotificationSecretLeakError if title/body/metadata contain secrets.
        """
        meta = metadata or {}
        meta_json = json.dumps(meta, sort_keys=True)

        # Zero-secret invariant enforcement (decoupled from Trust Core audit)
        _check_no_secrets(title)
        _check_no_secrets(body)
        _check_no_secrets(meta_json)

        now = _now_utc()
        notification = Notification(
            notification_id=notification_id,
            event_type=event_type,
            priority=priority,
            title=title,
            body=body,
            status=NotificationStatus.PENDING,
            source_type=source_type,
            source_id=source_id,
            project_id=project_id,
            dedup_key=dedup_key,
            delivery_attempts=0,
            max_delivery_attempts=max_delivery_attempts,
            expires_at=expires_at,
            metadata=meta,
            created_at=now,
            updated_at=now,
        )

        try:
            self._conn.execute("BEGIN IMMEDIATE;")
            self._conn.execute(
                """
                INSERT INTO notifications (
                    notification_id, event_type, priority, title, body,
                    source_type, source_id, project_id, dedup_key,
                    status, delivery_attempts, max_delivery_attempts,
                    expires_at, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    event_type.value,
                    priority.value,
                    title,
                    body,
                    source_type,
                    source_id,
                    project_id,
                    dedup_key,
                    NotificationStatus.PENDING.value,
                    0,
                    max_delivery_attempts,
                    expires_at,
                    meta_json,
                    now,
                    now,
                ),
            )
            self._conn.execute("COMMIT;")
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise NotificationStorageError(
                f"Failed to persist notification '{notification_id}': {exc}"
            ) from exc

        return notification

    def get_notification(self, notification_id: str) -> Optional[Notification]:
        """Retrieve a single notification by ID. Returns None if not found."""
        row = self._conn.execute(
            """
            SELECT notification_id, event_type, priority, title, body,
                   source_type, source_id, project_id, dedup_key,
                   status, delivery_attempts, max_delivery_attempts,
                   acknowledged_at, dismissed_at, expires_at,
                   metadata_json, created_at, updated_at
            FROM notifications
            WHERE notification_id = ?
            """,
            (notification_id,),
        ).fetchone()

        if row is None:
            return None
        return self._row_to_notification(row)

    def list_notifications(
        self,
        status: Optional[NotificationStatus] = None,
        priority: Optional[NotificationPriority] = None,
        event_type: Optional[NotificationEventType] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Notification]:
        """Query notifications with optional filters, newest first."""
        query = """
            SELECT notification_id, event_type, priority, title, body,
                   source_type, source_id, project_id, dedup_key,
                   status, delivery_attempts, max_delivery_attempts,
                   acknowledged_at, dismissed_at, expires_at,
                   metadata_json, created_at, updated_at
            FROM notifications
            WHERE 1=1
        """
        params: List[Any] = []

        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        if priority is not None:
            query += " AND priority = ?"
            params.append(priority.value)
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type.value)
        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_notification(r) for r in rows]

    def update_status(
        self,
        notification_id: str,
        status: NotificationStatus,
        delivery_attempts: Optional[int] = None,
        acknowledged_at: Optional[str] = None,
        dismissed_at: Optional[str] = None,
    ) -> Notification:
        """Atomically update notification status and optional fields."""
        existing = self.get_notification(notification_id)
        if existing is None:
            raise NotificationNotFoundError(f"Notification '{notification_id}' not found.")

        now = _now_utc()
        new_attempts = (
            delivery_attempts if delivery_attempts is not None else existing.delivery_attempts
        )

        try:
            self._conn.execute("BEGIN IMMEDIATE;")
            self._conn.execute(
                """
                UPDATE notifications
                SET status = ?,
                    delivery_attempts = ?,
                    acknowledged_at = ?,
                    dismissed_at = ?,
                    updated_at = ?
                WHERE notification_id = ?
                """,
                (
                    status.value,
                    new_attempts,
                    acknowledged_at or existing.acknowledged_at,
                    dismissed_at or existing.dismissed_at,
                    now,
                    notification_id,
                ),
            )
            self._conn.execute("COMMIT;")
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise NotificationStorageError(
                f"Failed to update notification '{notification_id}': {exc}"
            ) from exc

        return self.get_notification(notification_id)  # type: ignore[return-value]

    def exists_with_dedup_key(
        self,
        dedup_key: str,
        active_only: bool = True,
    ) -> bool:
        """Check whether a non-dismissed notification with this dedup key exists.

        Used to prevent duplicate notifications within the deduplication window.
        """
        query = """
            SELECT 1 FROM notifications
            WHERE dedup_key = ?
        """
        params: list = [dedup_key]
        if active_only:
            query += " AND status NOT IN ('DISMISSED', 'ACKNOWLEDGED', 'SUPPRESSED')"
        row = self._conn.execute(query, params).fetchone()
        return row is not None

    # ==========================================================================
    # DELIVERY ATTEMPT RECORDS
    # ==========================================================================

    def record_attempt(
        self,
        notification_id: str,
        attempt_number: int,
        adapter: str,
        succeeded: bool,
        error_message: Optional[str] = None,
        response_summary: Optional[str] = None,
    ) -> NotificationAttempt:
        """Persist an individual delivery attempt record."""
        attempt_id = str(uuid4())
        now = _now_utc()

        attempt = NotificationAttempt(
            attempt_id=attempt_id,
            notification_id=notification_id,
            attempt_number=attempt_number,
            adapter=adapter,
            attempted_at=now,
            succeeded=succeeded,
            error_message=error_message,
            response_summary=response_summary,
        )

        try:
            self._conn.execute("BEGIN IMMEDIATE;")
            self._conn.execute(
                """
                INSERT INTO notification_attempts (
                    attempt_id, notification_id, attempt_number, adapter,
                    attempted_at, succeeded, error_message, response_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    notification_id,
                    attempt_number,
                    adapter,
                    now,
                    1 if succeeded else 0,
                    error_message,
                    response_summary,
                ),
            )
            self._conn.execute("COMMIT;")
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK;")
            except Exception:
                pass
            logger.warning(
                "Failed to persist notification attempt for '%s': %s",
                notification_id,
                exc,
            )
            # Attempt persistence failure does NOT re-raise: we must not corrupt
            # notification state because of an attempt log failure.

        return attempt

    def list_attempts(self, notification_id: str) -> List[NotificationAttempt]:
        """Retrieve all delivery attempts for a notification."""
        rows = self._conn.execute(
            """
            SELECT attempt_id, notification_id, attempt_number, adapter,
                   attempted_at, succeeded, error_message, response_summary
            FROM notification_attempts
            WHERE notification_id = ?
            ORDER BY attempt_number ASC
            """,
            (notification_id,),
        ).fetchall()
        return [
            NotificationAttempt(
                attempt_id=r[0],
                notification_id=r[1],
                attempt_number=r[2],
                adapter=r[3],
                attempted_at=r[4],
                succeeded=bool(r[5]),
                error_message=r[6],
                response_summary=r[7],
            )
            for r in rows
        ]

    # ==========================================================================
    # HELPERS
    # ==========================================================================

    @staticmethod
    def _row_to_notification(row: tuple) -> Notification:
        (
            notification_id,
            event_type,
            priority,
            title,
            body,
            source_type,
            source_id,
            project_id,
            dedup_key,
            status,
            delivery_attempts,
            max_delivery_attempts,
            acknowledged_at,
            dismissed_at,
            expires_at,
            metadata_json,
            created_at,
            updated_at,
        ) = row

        try:
            meta = json.loads(metadata_json or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}

        return Notification(
            notification_id=notification_id,
            event_type=NotificationEventType(event_type),
            priority=NotificationPriority(priority),
            title=title,
            body=body,
            status=NotificationStatus(status),
            source_type=source_type,
            source_id=source_id,
            project_id=project_id,
            dedup_key=dedup_key,
            delivery_attempts=delivery_attempts,
            max_delivery_attempts=max_delivery_attempts,
            acknowledged_at=acknowledged_at,
            dismissed_at=dismissed_at,
            expires_at=expires_at,
            metadata=meta,
            created_at=created_at,
            updated_at=updated_at,
        )
