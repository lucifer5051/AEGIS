"""SAGE Persistent Job Queue -- SQLite Job Lifecycle and Lease Management.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.scheduler.job_models import (
    BackgroundJob,
    JobStatus,
    WorkerRegistration,
    WorkerStatus,
    _utc_now_iso,
)
from src.storage.database import transaction
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _sanitize_and_serialize(data: Dict[str, Any]) -> str:
    """Serialize dictionary to JSON while verifying zero raw secrets."""
    serialized = json.dumps(data, sort_keys=True)
    _check_no_secrets(serialized)
    return serialized


def _parse_iso(iso_str: str) -> datetime:
    """Parse ISO-8601 string to timezone-aware UTC datetime."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class JobQueue:
    """Persistent transactional job queue supporting atomic claiming, leases, and recovery."""

    TABLE_JOBS = "background_jobs"
    TABLE_WORKERS = "worker_registrations"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create auxiliary SQLite tables if they do not already exist."""
        sql = f"""
        CREATE TABLE IF NOT EXISTS {self.TABLE_JOBS} (
            job_id TEXT PRIMARY KEY,
            task_id TEXT NULL,
            schedule_id TEXT NULL,
            goal_text TEXT NOT NULL,
            action_type TEXT NOT NULL DEFAULT 'goal_execution',
            status TEXT NOT NULL CHECK (status IN (
                'PENDING', 'QUEUED', 'RUNNING', 'WAITING_APPROVAL',
                'RETRY_WAIT', 'COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED'
            )),
            priority INTEGER NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            started_at TEXT NULL,
            completed_at TEXT NULL,
            worker_id TEXT NULL,
            lease_expires_at TEXT NULL,
            execution_id TEXT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_retry_at TEXT NULL,
            last_error TEXT NULL,
            configuration_reference_json TEXT NOT NULL DEFAULT '{{}}',
            metadata_json TEXT NOT NULL DEFAULT '{{}}'
        );
        CREATE INDEX IF NOT EXISTS idx_background_jobs_status_sched 
            ON {self.TABLE_JOBS} (status, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_background_jobs_worker 
            ON {self.TABLE_JOBS} (worker_id, status);
        CREATE INDEX IF NOT EXISTS idx_background_jobs_task_id 
            ON {self.TABLE_JOBS} (task_id);

        CREATE TABLE IF NOT EXISTS {self.TABLE_WORKERS} (
            worker_id TEXT PRIMARY KEY,
            hostname TEXT NOT NULL,
            process_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'IDLE', 'STOPPING', 'STOPPED', 'DEAD')),
            started_at TEXT NOT NULL,
            last_heartbeat_at TEXT NOT NULL,
            concurrency_limit INTEGER NOT NULL DEFAULT 1,
            active_jobs_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{{}}'
        );
        CREATE INDEX IF NOT EXISTS idx_worker_registrations_heartbeat 
            ON {self.TABLE_WORKERS} (status, last_heartbeat_at);
        """
        try:
            self._conn.executescript(sql)
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning("JobQueue schema creation warning: %s", exc)

    # =========================================================================
    # JOB ENQUEUE AND CLAIM
    # =========================================================================

    def enqueue_job(self, job: BackgroundJob) -> BackgroundJob:
        """Enqueue a background job into persistent storage."""
        config_json = _sanitize_and_serialize(job.configuration_reference)
        meta_json = _sanitize_and_serialize(job.metadata)

        sql = f"""
        INSERT INTO {self.TABLE_JOBS} (
            job_id, task_id, schedule_id, goal_text, action_type,
            status, priority, created_at, scheduled_at, started_at,
            completed_at, worker_id, lease_expires_at, execution_id,
            attempt_count, max_attempts, next_retry_at, last_error,
            configuration_reference_json, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            task_id = excluded.task_id,
            schedule_id = excluded.schedule_id,
            goal_text = excluded.goal_text,
            action_type = excluded.action_type,
            status = excluded.status,
            priority = excluded.priority,
            scheduled_at = excluded.scheduled_at,
            started_at = excluded.started_at,
            completed_at = excluded.completed_at,
            worker_id = excluded.worker_id,
            lease_expires_at = excluded.lease_expires_at,
            execution_id = excluded.execution_id,
            attempt_count = excluded.attempt_count,
            max_attempts = excluded.max_attempts,
            next_retry_at = excluded.next_retry_at,
            last_error = excluded.last_error,
            configuration_reference_json = excluded.configuration_reference_json,
            metadata_json = excluded.metadata_json;
        """
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(
                sql,
                (
                    job.job_id,
                    job.task_id,
                    job.schedule_id,
                    job.goal_text,
                    job.action_type,
                    job.status.value,
                    job.priority,
                    job.created_at,
                    job.scheduled_at,
                    job.started_at,
                    job.completed_at,
                    job.worker_id,
                    job.lease_expires_at,
                    job.execution_id,
                    job.attempt_count,
                    job.max_attempts,
                    job.next_retry_at,
                    job.last_error,
                    config_json,
                    meta_json,
                ),
            )
        return job

    def claim_next_job(
        self,
        worker_id: str,
        lease_duration_seconds: float = 60.0,
        current_time: Optional[datetime] = None,
    ) -> Optional[BackgroundJob]:
        """Atomically claim the next eligible job for execution by worker_id."""
        now_dt = current_time or datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        lease_expires_dt = now_dt + timedelta(seconds=lease_duration_seconds)
        lease_str = lease_expires_dt.isoformat()

        with transaction(self._conn, immediate=True) as cursor:
            # Select highest priority eligible job
            select_sql = f"""
            SELECT job_id FROM {self.TABLE_JOBS}
            WHERE (status = 'QUEUED'
                   OR (status = 'PENDING' AND scheduled_at <= ?)
                   OR (status = 'RETRY_WAIT' AND (next_retry_at IS NULL OR next_retry_at <= ?)))
            ORDER BY priority ASC, scheduled_at ASC
            LIMIT 1;
            """
            cursor.execute(select_sql, (now_str, now_str))
            row = cursor.fetchone()
            if not row:
                return None

            job_id = row[0]

            # Atomically claim the selected job
            update_sql = f"""
            UPDATE {self.TABLE_JOBS}
            SET status = 'RUNNING',
                worker_id = ?,
                started_at = ?,
                lease_expires_at = ?,
                attempt_count = attempt_count + 1
            WHERE job_id = ? AND status IN ('PENDING', 'QUEUED', 'RETRY_WAIT');
            """
            cursor.execute(update_sql, (worker_id, now_str, lease_str, job_id))
            if cursor.rowcount == 0:
                # Race condition: claimed by another worker simultaneously
                return None

        return self.get_job(job_id)

    def heartbeat_lease(
        self,
        job_id: str,
        worker_id: str,
        extension_seconds: float = 60.0,
    ) -> bool:
        """Extend the lease expiration for an actively running job."""
        now_dt = datetime.now(timezone.utc)
        new_lease_str = (now_dt + timedelta(seconds=extension_seconds)).isoformat()

        sql = f"""
        UPDATE {self.TABLE_JOBS}
        SET lease_expires_at = ?
        WHERE job_id = ? AND worker_id = ? AND status = 'RUNNING';
        """
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (new_lease_str, job_id, worker_id))
            return cursor.rowcount > 0

    # =========================================================================
    # JOB STATE TRANSITIONS
    # =========================================================================

    def complete_job(
        self,
        job_id: str,
        task_id: Optional[str] = None,
        result_summary: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BackgroundJob:
        """Mark a job as successfully completed."""
        now_str = _utc_now_iso()
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        updated_meta = dict(job.metadata)
        if metadata:
            updated_meta.update(metadata)
        if result_summary:
            updated_meta["result_summary"] = result_summary

        meta_json = _sanitize_and_serialize(updated_meta)

        sql = f"""
        UPDATE {self.TABLE_JOBS}
        SET status = 'COMPLETED',
            task_id = COALESCE(?, task_id),
            completed_at = ?,
            lease_expires_at = NULL,
            metadata_json = ?
        WHERE job_id = ?;
        """
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (task_id, now_str, meta_json, job_id))

        return self.get_job(job_id)  # type: ignore

    def fail_job(
        self,
        job_id: str,
        error: str,
        is_retryable: bool = False,
        backoff_base_seconds: float = 5.0,
    ) -> BackgroundJob:
        """Mark a job as failed, calculating exponential backoff if retryable."""
        _check_no_secrets(error)
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        if is_retryable and job.attempt_count < job.max_attempts:
            # Exponential backoff calculation
            backoff_secs = backoff_base_seconds * (2 ** (job.attempt_count - 1))
            next_retry_str = (now_dt + timedelta(seconds=backoff_secs)).isoformat()

            sql = f"""
            UPDATE {self.TABLE_JOBS}
            SET status = 'RETRY_WAIT',
                worker_id = NULL,
                lease_expires_at = NULL,
                next_retry_at = ?,
                last_error = ?
            WHERE job_id = ?;
            """
            with transaction(self._conn, immediate=True) as cursor:
                cursor.execute(sql, (next_retry_str, error, job_id))
        else:
            # Permanent failure
            sql = f"""
            UPDATE {self.TABLE_JOBS}
            SET status = 'FAILED',
                completed_at = ?,
                lease_expires_at = NULL,
                last_error = ?
            WHERE job_id = ?;
            """
            with transaction(self._conn, immediate=True) as cursor:
                cursor.execute(sql, (now_str, error, job_id))

        return self.get_job(job_id)  # type: ignore

    def suspend_for_approval(
        self,
        job_id: str,
        approval_reason: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BackgroundJob:
        """Suspend job execution pending human approval."""
        _check_no_secrets(approval_reason)
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        updated_meta = dict(job.metadata)
        if metadata:
            updated_meta.update(metadata)
        updated_meta["approval_reason"] = approval_reason
        meta_json = _sanitize_and_serialize(updated_meta)

        sql = f"""
        UPDATE {self.TABLE_JOBS}
        SET status = 'WAITING_APPROVAL',
            lease_expires_at = NULL,
            last_error = ?,
            metadata_json = ?
        WHERE job_id = ?;
        """
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (f"ApprovalRequired: {approval_reason}", meta_json, job_id))

        return self.get_job(job_id)  # type: ignore

    def resume_approved_job(
        self,
        job_id: str,
        approver: str,
        approval_metadata: Optional[Dict[str, Any]] = None,
    ) -> BackgroundJob:
        """Resume an approved job by returning it to QUEUED status."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        updated_meta = dict(job.metadata)
        if approval_metadata:
            updated_meta.update(approval_metadata)
        updated_meta["approved_by"] = approver
        updated_meta["approved_at"] = _utc_now_iso()
        meta_json = _sanitize_and_serialize(updated_meta)

        sql = f"""
        UPDATE {self.TABLE_JOBS}
        SET status = 'QUEUED',
            worker_id = NULL,
            lease_expires_at = NULL,
            metadata_json = ?
        WHERE job_id = ? AND status = 'WAITING_APPROVAL';
        """
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (meta_json, job_id))

        return self.get_job(job_id)  # type: ignore

    def reject_job(self, job_id: str, reason: str = "Rejected by approver") -> BackgroundJob:
        """Reject and cancel a job in WAITING_APPROVAL status."""
        _check_no_secrets(reason)
        now_str = _utc_now_iso()

        sql = f"""
        UPDATE {self.TABLE_JOBS}
        SET status = 'CANCELLED',
            completed_at = ?,
            lease_expires_at = NULL,
            last_error = ?
        WHERE job_id = ?;
        """
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (now_str, f"ApprovalRejected: {reason}", job_id))

        return self.get_job(job_id)  # type: ignore

    def cancel_job(self, job_id: str, reason: str = "User cancelled") -> BackgroundJob:
        """Explicitly cancel a job."""
        _check_no_secrets(reason)
        now_str = _utc_now_iso()

        sql = f"""
        UPDATE {self.TABLE_JOBS}
        SET status = 'CANCELLED',
            completed_at = ?,
            lease_expires_at = NULL,
            last_error = ?
        WHERE job_id = ? AND status NOT IN ('COMPLETED', 'FAILED');
        """
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (now_str, reason, job_id))

        return self.get_job(job_id)  # type: ignore

    def retry_job(self, job_id: str) -> BackgroundJob:
        """Manually reset a failed or cancelled job to QUEUED."""
        sql = f"""
        UPDATE {self.TABLE_JOBS}
        SET status = 'QUEUED',
            attempt_count = 0,
            worker_id = NULL,
            lease_expires_at = NULL,
            next_retry_at = NULL,
            last_error = NULL,
            completed_at = NULL
        WHERE job_id = ?;
        """
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (job_id,))

        return self.get_job(job_id)  # type: ignore

    # =========================================================================
    # RECOVERY AND LEASE CLEANUP
    # =========================================================================

    def recover_abandoned_jobs(
        self,
        current_time: Optional[datetime] = None,
        backoff_base_seconds: float = 5.0,
    ) -> int:
        """Recover jobs whose worker lease has expired without a completion or heartbeat."""
        now_dt = current_time or datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        sql_find = f"""
        SELECT job_id, attempt_count, max_attempts FROM {self.TABLE_JOBS}
        WHERE status = 'RUNNING' AND lease_expires_at < ?;
        """
        recovered_count = 0

        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql_find, (now_str,))
            stale_jobs = cursor.fetchall()

            for job_id, attempt_count, max_attempts in stale_jobs:
                if attempt_count < max_attempts:
                    backoff = backoff_base_seconds * (2 ** max(0, attempt_count - 1))
                    next_retry_str = (now_dt + timedelta(seconds=backoff)).isoformat()
                    cursor.execute(
                        f"""
                        UPDATE {self.TABLE_JOBS}
                        SET status = 'RETRY_WAIT',
                            worker_id = NULL,
                            lease_expires_at = NULL,
                            next_retry_at = ?,
                            last_error = 'Worker lease expired; re-queued for recovery'
                        WHERE job_id = ?;
                        """,
                        (next_retry_str, job_id),
                    )
                else:
                    cursor.execute(
                        f"""
                        UPDATE {self.TABLE_JOBS}
                        SET status = 'FAILED',
                            completed_at = ?,
                            worker_id = NULL,
                            lease_expires_at = NULL,
                            last_error = 'Worker lease expired and max attempts exceeded'
                        WHERE job_id = ?;
                        """,
                        (now_str, job_id),
                    )
                recovered_count += 1

        return recovered_count

    # =========================================================================
    # QUERYING AND LOOKUP
    # =========================================================================

    def get_job(self, job_id: str) -> Optional[BackgroundJob]:
        """Fetch a single background job by ID."""
        sql = f"""
        SELECT job_id, task_id, schedule_id, goal_text, action_type,
               status, priority, created_at, scheduled_at, started_at,
               completed_at, worker_id, lease_expires_at, execution_id,
               attempt_count, max_attempts, next_retry_at, last_error,
               configuration_reference_json, metadata_json
        FROM {self.TABLE_JOBS}
        WHERE job_id = ?;
        """
        cursor = self._conn.cursor()
        cursor.execute(sql, (job_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_job(row)

    def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        schedule_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[BackgroundJob]:
        """Query jobs with optional status and schedule filters."""
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status.value)
        if schedule_id:
            clauses.append("schedule_id = ?")
            params.append(schedule_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT job_id, task_id, schedule_id, goal_text, action_type,
               status, priority, created_at, scheduled_at, started_at,
               completed_at, worker_id, lease_expires_at, execution_id,
               attempt_count, max_attempts, next_retry_at, last_error,
               configuration_reference_json, metadata_json
        FROM {self.TABLE_JOBS}
        {where}
        ORDER BY priority ASC, scheduled_at DESC
        LIMIT ?;
        """
        params.append(limit)
        cursor = self._conn.cursor()
        cursor.execute(sql, tuple(params))
        return [self._row_to_job(r) for r in cursor.fetchall()]

    def _row_to_job(self, row: Any) -> BackgroundJob:
        """Convert a database row into a BackgroundJob model."""
        return BackgroundJob(
            job_id=row[0],
            task_id=row[1],
            schedule_id=row[2],
            goal_text=row[3],
            action_type=row[4],
            status=JobStatus(row[5]),
            priority=row[6],
            created_at=row[7],
            scheduled_at=row[8],
            started_at=row[9],
            completed_at=row[10],
            worker_id=row[11],
            lease_expires_at=row[12],
            execution_id=row[13],
            attempt_count=row[14],
            max_attempts=row[15],
            next_retry_at=row[16],
            last_error=row[17],
            configuration_reference=json.loads(row[18] or "{}"),
            metadata=json.loads(row[19] or "{}"),
        )
