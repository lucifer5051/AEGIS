"""SAGE Remote Control Gateway -- SQLite Persistent Store.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
    migrations/0009_channel_gateway_schema.sql
    PROMPT 032: SAGE Secure Remote-Control Gateway (Telegram-First)

Invariants:
- Transactional atomic writes using BEGIN IMMEDIATE.
- Strict update deduplication via UNIQUE(channel, channel_update_id).
- Zero-secret invariant: raises on detected credentials.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from src.gateway.errors import GatewayError
from src.gateway.models import (
    InboundUpdate,
    OutboundMessage,
    OutboundStatus,
    UpdateLifecycleState,
)
from src.storage.database import transaction
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class GatewayStore:
    """SQLite persistence layer for remote gateway updates and outbound messages."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record_inbound_update(
        self,
        channel: str,
        channel_update_id: str,
        sender_id: str,
        chat_id: str,
        chat_type: str = "private",
        message_type: str = "text",
        raw_text: Optional[str] = None,
        state: UpdateLifecycleState = UpdateLifecycleState.RECEIVED,
        task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[InboundUpdate, bool]:
        """Record an inbound update.

        Returns:
            Tuple of (InboundUpdate, is_duplicate).
            If (channel, channel_update_id) already exists, returns (existing_update, True).
        """
        if raw_text is not None:
            _check_no_secrets(raw_text)

        meta = dict(metadata or {})
        meta_json = json.dumps(meta, sort_keys=True)
        _check_no_secrets(meta_json)

        update_id = str(uuid4())
        now = _now_utc()

        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    INSERT INTO gateway_updates (
                        update_id, channel, channel_update_id, sender_id, chat_id,
                        chat_type, message_type, raw_text, state, task_id,
                        retry_count, max_retries, error_summary, metadata_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 3, NULL, ?, ?, ?);
                    """,
                    (
                        update_id,
                        channel,
                        channel_update_id,
                        sender_id,
                        chat_id,
                        chat_type,
                        message_type,
                        raw_text,
                        state.value,
                        task_id,
                        meta_json,
                        now,
                        now,
                    ),
                )
        except (sqlite3.IntegrityError, Exception) as exc:
            if "UNIQUE constraint failed" in str(exc) or isinstance(exc, sqlite3.IntegrityError):
                # Duplicate update ID detected!
                existing = self.get_update_by_channel_id(channel, channel_update_id)
                if existing is not None:
                    return existing, True
            raise GatewayError(
                f"Error recording update {channel_update_id}: {exc}"
            ) from exc

        return (
            InboundUpdate(
                update_id=update_id,
                channel=channel,
                channel_update_id=channel_update_id,
                sender_id=sender_id,
                chat_id=chat_id,
                chat_type=chat_type,
                message_type=message_type,
                raw_text=raw_text,
                state=state,
                task_id=task_id,
                retry_count=0,
                max_retries=3,
                error_summary=None,
                metadata=meta,
                created_at=now,
                updated_at=now,
            ),
            False,
        )

    def get_inbound_update(self, update_id: str) -> Optional[InboundUpdate]:
        """Fetch an inbound update by its internal UUID."""
        cursor = self._conn.execute(
            """
            SELECT update_id, channel, channel_update_id, sender_id, chat_id,
                   chat_type, message_type, raw_text, state, task_id,
                   retry_count, max_retries, error_summary, metadata_json,
                   created_at, updated_at
            FROM gateway_updates
            WHERE update_id = ?;
            """,
            (update_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_inbound_update(row)

    def get_update_by_channel_id(
        self, channel: str, channel_update_id: str
    ) -> Optional[InboundUpdate]:
        """Fetch an inbound update by channel and external update ID."""
        cursor = self._conn.execute(
            """
            SELECT update_id, channel, channel_update_id, sender_id, chat_id,
                   chat_type, message_type, raw_text, state, task_id,
                   retry_count, max_retries, error_summary, metadata_json,
                   created_at, updated_at
            FROM gateway_updates
            WHERE channel = ? AND channel_update_id = ?;
            """,
            (channel, channel_update_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_inbound_update(row)

    def transition_update_state(
        self,
        update_id: str,
        new_state: UpdateLifecycleState,
        error_summary: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> InboundUpdate:
        """Atomically transition the processing state of an inbound update."""
        if error_summary is not None:
            _check_no_secrets(error_summary)

        now = _now_utc()
        with transaction(self._conn, immediate=True) as cur:
            if task_id is not None:
                cur.execute(
                    """
                    UPDATE gateway_updates
                    SET state = ?, error_summary = ?, task_id = ?, updated_at = ?
                    WHERE update_id = ?;
                    """,
                    (new_state.value, error_summary, task_id, now, update_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE gateway_updates
                    SET state = ?, error_summary = ?, updated_at = ?
                    WHERE update_id = ?;
                    """,
                    (new_state.value, error_summary, now, update_id),
                )

        updated = self.get_inbound_update(update_id)
        if not updated:
            raise GatewayError(f"Update '{update_id}' missing after state transition.")
        return updated

    def record_outbound_message(
        self,
        channel: str,
        recipient_id: str,
        content: str,
        inbound_update_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> OutboundMessage:
        """Record an outbound message queued for delivery."""
        _check_no_secrets(content)
        msg_id = str(uuid4())
        now = _now_utc()

        with transaction(self._conn, immediate=True) as cur:
            cur.execute(
                """
                INSERT INTO gateway_outbound_messages (
                    message_id, channel, recipient_id, inbound_update_id,
                    task_id, content, status, attempts, max_attempts,
                    last_attempt_at, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, 3, NULL, NULL, ?, ?);
                """,
                (
                    msg_id,
                    channel,
                    recipient_id,
                    inbound_update_id,
                    task_id,
                    content,
                    now,
                    now,
                ),
            )

        return OutboundMessage(
            message_id=msg_id,
            channel=channel,
            recipient_id=recipient_id,
            content=content,
            inbound_update_id=inbound_update_id,
            task_id=task_id,
            status=OutboundStatus.PENDING,
            attempts=0,
            max_attempts=3,
            last_attempt_at=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )

    def update_outbound_status(
        self,
        message_id: str,
        status: OutboundStatus,
        error_message: Optional[str] = None,
    ) -> OutboundMessage:
        """Update delivery status and attempt count of an outbound message."""
        if error_message is not None:
            _check_no_secrets(error_message)

        now = _now_utc()
        with transaction(self._conn, immediate=True) as cur:
            cur.execute(
                """
                UPDATE gateway_outbound_messages
                SET status = ?, attempts = attempts + 1, last_attempt_at = ?,
                    error_message = ?, updated_at = ?
                WHERE message_id = ?;
                """,
                (status.value, now, error_message, now, message_id),
            )

        cursor = self._conn.execute(
            """
            SELECT message_id, channel, recipient_id, inbound_update_id,
                   task_id, content, status, attempts, max_attempts,
                   last_attempt_at, error_message, created_at, updated_at
            FROM gateway_outbound_messages
            WHERE message_id = ?;
            """,
            (message_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise GatewayError(f"Outbound message '{message_id}' missing after status update.")
        return self._row_to_outbound_message(row)

    def get_outbound_message(self, message_id: str) -> Optional[OutboundMessage]:
        """Fetch an outbound message by its internal UUID."""
        cursor = self._conn.execute(
            """
            SELECT message_id, channel, recipient_id, inbound_update_id,
                   task_id, content, status, attempts, max_attempts,
                   last_attempt_at, error_message, created_at, updated_at
            FROM gateway_outbound_messages
            WHERE message_id = ?;
            """,
            (message_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_outbound_message(row)

    def list_inbound_updates(
        self,
        channel: Optional[str] = None,
        state: Optional[UpdateLifecycleState] = None,
        limit: int = 50,
    ) -> List[InboundUpdate]:
        """Query recent inbound updates."""
        query = (
            "SELECT update_id, channel, channel_update_id, sender_id, chat_id, "
            "chat_type, message_type, raw_text, state, task_id, "
            "retry_count, max_retries, error_summary, metadata_json, "
            "created_at, updated_at FROM gateway_updates WHERE 1=1"
        )
        params: List[Any] = []
        if channel:
            query += " AND channel = ?"
            params.append(channel)
        if state:
            query += " AND state = ?"
            params.append(state.value)
        query += " ORDER BY created_at DESC LIMIT ?;"
        params.append(limit)

        cursor = self._conn.execute(query, params)
        return [self._row_to_inbound_update(r) for r in cursor.fetchall()]

    def list_outbound_messages(
        self,
        channel: Optional[str] = None,
        status: Optional[OutboundStatus] = None,
        limit: int = 50,
    ) -> List[OutboundMessage]:
        """Query recent outbound messages."""
        query = (
            "SELECT message_id, channel, recipient_id, inbound_update_id, "
            "task_id, content, status, attempts, max_attempts, "
            "last_attempt_at, error_message, created_at, updated_at "
            "FROM gateway_outbound_messages WHERE 1=1"
        )
        params: List[Any] = []
        if channel:
            query += " AND channel = ?"
            params.append(channel)
        if status:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY created_at DESC LIMIT ?;"
        params.append(limit)

        cursor = self._conn.execute(query, params)
        return [self._row_to_outbound_message(r) for r in cursor.fetchall()]

    def _row_to_inbound_update(self, row: Any) -> InboundUpdate:
        return InboundUpdate(
            update_id=row[0],
            channel=row[1],
            channel_update_id=row[2],
            sender_id=row[3],
            chat_id=row[4],
            chat_type=row[5],
            message_type=row[6],
            raw_text=row[7],
            state=UpdateLifecycleState(row[8]),
            task_id=row[9],
            retry_count=row[10],
            max_retries=row[11],
            error_summary=row[12],
            metadata=json.loads(row[13]) if row[13] else {},
            created_at=row[14],
            updated_at=row[15],
        )

    def _row_to_outbound_message(self, row: Any) -> OutboundMessage:
        return OutboundMessage(
            message_id=row[0],
            channel=row[1],
            recipient_id=row[2],
            inbound_update_id=row[3],
            task_id=row[4],
            content=row[5],
            status=OutboundStatus(row[6]),
            attempts=row[7],
            max_attempts=row[8],
            last_attempt_at=row[9],
            error_message=row[10],
            created_at=row[11],
            updated_at=row[12],
        )
