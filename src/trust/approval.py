"""SAGE Trust Core -- Approval Lifecycle Management.

Canonical Reference:
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 16
    migrations/0005_trust_core_schema.sql

Invariants:
1. All escalation actions produce persistent approval records in SQLite.
2. Capability issuance for escalated actions requires a verified, unexpired
   approval record in 'APPROVED' status.
3. UUIDs alone are NEVER accepted as proof of approval.
4. Expirations are enforced deterministically using ISO 8601 UTC timestamps.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import uuid4

from src.trust.errors import TrustError


@dataclass(frozen=True)
class ApprovalRequestRecord:
    """Immutable representation of a persisted approval request."""

    request_id: str
    task_id: str
    action_name: str
    action_risk_level: str
    target_resource: str
    parameters_summary: str
    status: str  # 'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'REVOKED'
    expires_at: str
    project_id: Optional[str] = None
    decision_actor: Optional[str] = None
    decision_at: Optional[str] = None

    def is_expired(self) -> bool:
        """Check if request has passed its expiration time."""
        now_utc = datetime.now(timezone.utc).isoformat()
        return self.expires_at <= now_utc


class ApprovalStore:
    """Persistent SQLite store for human approval requests."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_request(
        self,
        action_name: str,
        action_risk_level: str = "MEDIUM",
        target_resource: str = "*",
        parameters_summary: str = "{}",
        task_id: str = "ad-hoc",
        project_id: Optional[str] = None,
        ttl_seconds: float = 300.0,
    ) -> ApprovalRequestRecord:
        """Create and persist a new PENDING approval request."""
        request_id = str(uuid4())
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        status = "PENDING"

        query = """
        INSERT INTO approval_requests (
            request_id, task_id, project_id, action_name, action_risk_level,
            target_resource, parameters_summary, status, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        try:
            self._conn.execute(
                query,
                (
                    request_id,
                    task_id,
                    project_id,
                    action_name,
                    action_risk_level,
                    target_resource,
                    parameters_summary,
                    status,
                    expires_at,
                ),
            )
        except sqlite3.Error as exc:
            raise TrustError(f"Failed to persist approval request: {exc}") from exc

        return ApprovalRequestRecord(
            request_id=request_id,
            task_id=task_id,
            project_id=project_id,
            action_name=action_name,
            action_risk_level=action_risk_level,
            target_resource=target_resource,
            parameters_summary=parameters_summary,
            status=status,
            expires_at=expires_at,
        )

    def get_request(self, request_id: str) -> Optional[ApprovalRequestRecord]:
        """Fetch an approval request by its ID."""
        query = """
        SELECT request_id, task_id, project_id, action_name, action_risk_level,
               target_resource, parameters_summary, status, decision_actor,
               decision_at, expires_at
        FROM approval_requests
        WHERE request_id = ?;
        """
        cursor = self._conn.execute(query, (request_id,))
        row = cursor.fetchone()
        if not row:
            return None

        # Check for dynamic expiration
        rec = ApprovalRequestRecord(
            request_id=row[0],
            task_id=row[1],
            project_id=row[2],
            action_name=row[3],
            action_risk_level=row[4],
            target_resource=row[5],
            parameters_summary=row[6],
            status=row[7],
            decision_actor=row[8],
            decision_at=row[9],
            expires_at=row[10],
        )

        if rec.status == "PENDING" and rec.is_expired():
            self._conn.execute(
                "UPDATE approval_requests SET status = 'EXPIRED' WHERE request_id = ?;",
                (request_id,),
            )
            return ApprovalRequestRecord(
                request_id=rec.request_id,
                task_id=rec.task_id,
                project_id=rec.project_id,
                action_name=rec.action_name,
                action_risk_level=rec.action_risk_level,
                target_resource=rec.target_resource,
                parameters_summary=rec.parameters_summary,
                status="EXPIRED",
                decision_actor=rec.decision_actor,
                decision_at=rec.decision_at,
                expires_at=rec.expires_at,
            )

        return rec

    def approve_request(self, request_id: str, actor: str) -> ApprovalRequestRecord:
        """Approve a pending approval request."""
        req = self.get_request(request_id)
        if not req:
            raise TrustError(f"Approval request '{request_id}' not found.")
        if req.status != "PENDING":
            raise TrustError(
                f"Cannot approve request '{request_id}' in non-pending status '{req.status}'."
            )

        now_utc = datetime.now(timezone.utc).isoformat()
        query = """
        UPDATE approval_requests
        SET status = 'APPROVED', decision_actor = ?, decision_at = ?
        WHERE request_id = ?;
        """
        self._conn.execute(query, (actor, now_utc, request_id))
        return self.get_request(request_id)  # type: ignore[return-value]

    def reject_request(
        self, request_id: str, actor: str, reason: str = ""
    ) -> ApprovalRequestRecord:
        """Reject a pending approval request."""
        req = self.get_request(request_id)
        if not req:
            raise TrustError(f"Approval request '{request_id}' not found.")
        if req.status != "PENDING":
            raise TrustError(
                f"Cannot reject request '{request_id}' in non-pending status '{req.status}'."
            )

        now_utc = datetime.now(timezone.utc).isoformat()
        query = """
        UPDATE approval_requests
        SET status = 'REJECTED', decision_actor = ?, decision_at = ?
        WHERE request_id = ?;
        """
        self._conn.execute(query, (actor, now_utc, request_id))
        return self.get_request(request_id)  # type: ignore[return-value]

    def list_pending(self, task_id: Optional[str] = None) -> List[ApprovalRequestRecord]:
        """List active pending approval requests."""
        self.expire_pending_requests()
        if task_id:
            query = """
            SELECT request_id, task_id, project_id, action_name, action_risk_level,
                   target_resource, parameters_summary, status, decision_actor,
                   decision_at, expires_at
            FROM approval_requests
            WHERE status = 'PENDING' AND task_id = ?;
            """
            cursor = self._conn.execute(query, (task_id,))
        else:
            query = """
            SELECT request_id, task_id, project_id, action_name, action_risk_level,
                   target_resource, parameters_summary, status, decision_actor,
                   decision_at, expires_at
            FROM approval_requests
            WHERE status = 'PENDING';
            """
            cursor = self._conn.execute(query)

        return [
            ApprovalRequestRecord(
                request_id=row[0],
                task_id=row[1],
                project_id=row[2],
                action_name=row[3],
                action_risk_level=row[4],
                target_resource=row[5],
                parameters_summary=row[6],
                status=row[7],
                decision_actor=row[8],
                decision_at=row[9],
                expires_at=row[10],
            )
            for row in cursor.fetchall()
        ]

    def expire_pending_requests(self) -> int:
        """Mark all expired pending requests as EXPIRED."""
        now_utc = datetime.now(timezone.utc).isoformat()
        cursor = self._conn.execute(
            "UPDATE approval_requests SET status = 'EXPIRED' "
            "WHERE status = 'PENDING' AND expires_at <= ?;",
            (now_utc,),
        )
        return cursor.rowcount
