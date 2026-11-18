"""SAGE Autonomous Background Worker Manager.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12

Orchestrates background worker processes, concurrency limits, heartbeats,
TrustCore session authorization, configuration resolution, and execution through Supervisor.
"""

from __future__ import annotations

import logging
import os
import platform
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.manager.supervisor import Supervisor
from src.scheduler.job_models import (
    BackgroundJob,
    JobExecutionResult,
    JobStatus,
    WorkerRegistration,
    WorkerStatus,
    _utc_now_iso,
)
from src.scheduler.job_queue import JobQueue
from src.storage.database import transaction
from src.trust.audit import _check_no_secrets
from src.trust.authority import TrustAuthority
from src.trust.errors import PolicyDeniedError, TrustError

logger = logging.getLogger(__name__)


class WorkerManager:
    """Manages background workers, lease lifecycles, and dispatches jobs to Supervisor."""

    def __init__(
        self,
        job_queue: JobQueue,
        supervisor: Supervisor,
        trust_authority: Optional[TrustAuthority] = None,
        config_manager: Optional[Any] = None,
        memory_engine: Optional[Any] = None,
        model_gateway: Optional[Any] = None,
        lease_duration_seconds: float = 60.0,
        backoff_base_seconds: float = 5.0,
        max_workers: int = 4,
    ) -> None:
        self._queue = job_queue
        self._supervisor = supervisor
        self._trust_authority = trust_authority
        self._config_manager = config_manager
        self._memory_engine = memory_engine
        self._model_gateway = model_gateway
        self._lease_duration = lease_duration_seconds
        self._backoff_base = backoff_base_seconds
        self._max_workers = max_workers

        self._active_threads: Dict[str, threading.Thread] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # =========================================================================
    # WORKER REGISTRATION & HEARTBEAT
    # =========================================================================

    def register_worker(
        self,
        worker_id: Optional[str] = None,
        concurrency_limit: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> WorkerRegistration:
        """Register a worker in persistent storage."""
        wid = worker_id or f"worker-{uuid4().hex[:8]}"
        hostname = platform.node() or "localhost"
        pid = os.getpid()
        meta = metadata or {}
        meta_json = json_str = "{}"
        if meta:
            import json
            meta_json = json.dumps(meta)
            _check_no_secrets(meta_json)

        now_str = _utc_now_iso()
        sql = f"""
        INSERT INTO {JobQueue.TABLE_WORKERS} (
            worker_id, hostname, process_id, status, started_at,
            last_heartbeat_at, concurrency_limit, active_jobs_count, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(worker_id) DO UPDATE SET
            status = excluded.status,
            last_heartbeat_at = excluded.last_heartbeat_at,
            metadata_json = excluded.metadata_json;
        """
        with transaction(self._queue._conn, immediate=True) as cursor:
            cursor.execute(
                sql,
                (
                    wid,
                    hostname,
                    pid,
                    WorkerStatus.ACTIVE.value,
                    now_str,
                    now_str,
                    concurrency_limit,
                    meta_json,
                ),
            )

        return WorkerRegistration(
            worker_id=wid,
            hostname=hostname,
            process_id=pid,
            status=WorkerStatus.ACTIVE,
            started_at=now_str,
            last_heartbeat_at=now_str,
            concurrency_limit=concurrency_limit,
            active_jobs_count=0,
            metadata=meta,
        )

    def heartbeat_worker(self, worker_id: str) -> None:
        """Update last heartbeat timestamp for a registered worker."""
        now_str = _utc_now_iso()
        sql = f"""
        UPDATE {JobQueue.TABLE_WORKERS}
        SET last_heartbeat_at = ?, status = ?
        WHERE worker_id = ?;
        """
        with transaction(self._queue._conn, immediate=True) as cursor:
            cursor.execute(sql, (now_str, WorkerStatus.ACTIVE.value, worker_id))

    def unregister_worker(self, worker_id: str) -> None:
        """Mark a worker as STOPPED upon graceful shutdown."""
        sql = f"""
        UPDATE {JobQueue.TABLE_WORKERS}
        SET status = ?
        WHERE worker_id = ?;
        """
        with transaction(self._queue._conn, immediate=True) as cursor:
            cursor.execute(sql, (WorkerStatus.STOPPED.value, worker_id))

    def get_worker_status(self, worker_id: str) -> Optional[WorkerRegistration]:
        """Fetch worker registration details."""
        sql = f"""
        SELECT worker_id, hostname, process_id, status, started_at,
               last_heartbeat_at, concurrency_limit, active_jobs_count, metadata_json
        FROM {JobQueue.TABLE_WORKERS}
        WHERE worker_id = ?;
        """
        cursor = self._queue._conn.cursor()
        cursor.execute(sql, (worker_id,))
        row = cursor.fetchone()
        if not row:
            return None
        import json
        return WorkerRegistration(
            worker_id=row[0],
            hostname=row[1],
            process_id=row[2],
            status=WorkerStatus(row[3]),
            started_at=row[4],
            last_heartbeat_at=row[5],
            concurrency_limit=row[6],
            active_jobs_count=row[7],
            metadata=json.loads(row[8] or "{}"),
        )

    def list_workers(self) -> List[WorkerRegistration]:
        """List all registered workers."""
        sql = f"""
        SELECT worker_id, hostname, process_id, status, started_at,
               last_heartbeat_at, concurrency_limit, active_jobs_count, metadata_json
        FROM {JobQueue.TABLE_WORKERS}
        ORDER BY started_at DESC;
        """
        cursor = self._queue._conn.cursor()
        cursor.execute(sql)
        import json
        return [
            WorkerRegistration(
                worker_id=r[0],
                hostname=r[1],
                process_id=r[2],
                status=WorkerStatus(r[3]),
                started_at=r[4],
                last_heartbeat_at=r[5],
                concurrency_limit=r[6],
                active_jobs_count=r[7],
                metadata=json.loads(r[8] or "{}"),
            )
            for r in cursor.fetchall()
        ]

    # =========================================================================
    # SINGLE TICK EXECUTION (DETERMINISTIC)
    # =========================================================================

    def run_worker_tick(
        self,
        worker_id: str,
        current_time: Optional[datetime] = None,
    ) -> Optional[JobExecutionResult]:
        """Execute a single atomic claim-and-run cycle.

        Args:
            worker_id: ID of the claiming worker.
            current_time: Optional simulated timestamp.

        Returns:
            JobExecutionResult if a job was executed, None if queue was idle.
        """
        # 1. Recover any abandoned jobs
        self._queue.recover_abandoned_jobs(
            current_time=current_time,
            backoff_base_seconds=self._backoff_base,
        )

        # 2. Worker heartbeat
        self.heartbeat_worker(worker_id)

        # 3. Claim next job
        job = self._queue.claim_next_job(
            worker_id=worker_id,
            lease_duration_seconds=self._lease_duration,
            current_time=current_time,
        )
        if not job:
            return None

        start_time = time.perf_counter()
        actor = job.configuration_reference.get("actor", "automation_worker")
        project_id = job.configuration_reference.get("project_id")
        mode = job.configuration_reference.get("mode", "automation")
        task_overrides = job.configuration_reference.get("task_overrides", {})

        # 4. TrustCore Authorization Verification
        if self._trust_authority is not None:
            # Verify that background actor / action is not hard-denied
            try:
                # Validate base capability for autonomous action dispatch
                policy_result = self._trust_authority._policy.evaluate(
                    f"automation.{job.action_type}",
                    project_id=project_id,
                )
                from src.trust.policy import PolicyDecision
                if policy_result.decision == PolicyDecision.DENY:
                    logger.warning("Job %s hard denied by TrustCore: %s", job.job_id, policy_result.rationale)
                    updated = self._queue.fail_job(
                        job.job_id,
                        error=f"TrustCoreDenied: {policy_result.rationale}",
                        is_retryable=False,  # Security denials are never retried
                    )
                    return JobExecutionResult(
                        job_id=job.job_id,
                        status=updated.status,
                        success=False,
                        result_summary=f"TrustCoreDenied: {policy_result.rationale}",
                        error=f"TrustCoreDenied: {policy_result.rationale}",
                    )
            except Exception as exc:
                logger.error("TrustCore evaluation error for job %s: %s", job.job_id, exc)
                updated = self._queue.fail_job(
                    job.job_id,
                    error=f"TrustCoreError: {str(exc)}",
                    is_retryable=False,
                )
                return JobExecutionResult(
                    job_id=job.job_id,
                    status=updated.status,
                    success=False,
                    result_summary=f"TrustCoreError: {str(exc)}",
                    error=str(exc),
                )

        # 5. Configuration Resolution & Execution Bounds
        max_total_steps = 15
        max_replan_attempts = 3
        if self._config_manager is not None:
            try:
                resolved = self._config_manager.resolve_config(
                    active_mode=mode,
                    task_overrides=task_overrides,
                )
                if resolved.execution_profile:
                    max_total_steps = resolved.execution_profile.max_total_steps
                    max_replan_attempts = resolved.execution_profile.max_replan_attempts
            except Exception as exc:
                logger.warning("Config resolution fallback for job %s: %s", job.job_id, exc)

        # 6. Execution via Supervisor
        try:
            goal_res = self._supervisor.execute_goal(
                goal_text=job.goal_text,
                actor=actor,
                project_id=project_id,
                max_total_steps=max_total_steps,
                max_replan_attempts=max_replan_attempts,
                mode=mode,
                task_overrides=task_overrides,
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            task_id = goal_res.task_record.task_id if goal_res.task_record else None

            # Check if any observation required approval
            requires_approval = False
            for obs in goal_res.observations:
                if getattr(obs, "status", None) and obs.status.value == "DENIED":
                    pol_dec = obs.metadata.get("policy_decision")
                    if pol_dec == "REQUIRE_APPROVAL":
                        requires_approval = True
                        break

            if requires_approval:
                updated = self._queue.suspend_for_approval(
                    job.job_id,
                    approval_reason="Action required human approval gate.",
                    metadata={"task_id": task_id},
                )
                return JobExecutionResult(
                    job_id=job.job_id,
                    status=updated.status,
                    success=False,
                    result_summary="Suspended pending human approval.",
                    task_id=task_id,
                    execution_duration_ms=elapsed_ms,
                    requires_approval=True,
                )

            if goal_res.success:
                updated = self._queue.complete_job(
                    job.job_id,
                    task_id=task_id,
                    result_summary=goal_res.synthesis,
                    metadata={"observations_count": len(goal_res.observations)},
                )
                # Unified Memory Engine integration
                if self._memory_engine is not None and task_id:
                    try:
                        self._memory_engine.record_task_outcome(
                            task_id=task_id,
                            goal=job.goal_text,
                            success=True,
                            synthesis=goal_res.synthesis,
                            project_id=project_id,
                            actor=actor,
                        )
                    except Exception as exc:
                        logger.warning("Memory engine recording error: %s", exc)

                return JobExecutionResult(
                    job_id=job.job_id,
                    status=updated.status,
                    success=True,
                    result_summary=goal_res.synthesis,
                    task_id=task_id,
                    execution_duration_ms=elapsed_ms,
                )
            else:
                # Goal failed
                error_msg = goal_res.synthesis or "Execution halted or failed."
                # Classify retryability: security/policy errors are NOT retryable
                is_retryable = True
                if "PolicyDenied" in error_msg or "TrustCore" in error_msg:
                    is_retryable = False

                updated = self._queue.fail_job(
                    job.job_id,
                    error=error_msg,
                    is_retryable=is_retryable,
                    backoff_base_seconds=self._backoff_base,
                )
                if self._memory_engine is not None and task_id:
                    try:
                        self._memory_engine.record_task_outcome(
                            task_id=task_id,
                            goal=job.goal_text,
                            success=False,
                            synthesis=error_msg,
                            error=error_msg,
                            project_id=project_id,
                            actor=actor,
                        )
                    except Exception as exc:
                        logger.warning("Memory engine failure recording error: %s", exc)

                return JobExecutionResult(
                    job_id=job.job_id,
                    status=updated.status,
                    success=False,
                    result_summary=error_msg,
                    task_id=task_id,
                    execution_duration_ms=elapsed_ms,
                    error=error_msg,
                )

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error("Unhandled worker error for job %s: %s", job.job_id, exc)
            updated = self._queue.fail_job(
                job.job_id,
                error=f"WorkerCrash: {str(exc)}",
                is_retryable=True,
                backoff_base_seconds=self._backoff_base,
            )
            return JobExecutionResult(
                job_id=job.job_id,
                status=updated.status,
                success=False,
                result_summary=f"WorkerCrash: {str(exc)}",
                execution_duration_ms=elapsed_ms,
                error=str(exc),
            )

    # =========================================================================
    # BACKGROUND THREAD POOL MANAGEMENT
    # =========================================================================

    def start_worker(self, worker_id: Optional[str] = None) -> str:
        """Start a background worker thread."""
        with self._lock:
            if len(self._active_threads) >= self._max_workers:
                raise RuntimeError(f"Maximum concurrent workers limit ({self._max_workers}) reached.")

            reg = self.register_worker(worker_id=worker_id)
            wid = reg.worker_id

            stop_evt = threading.Event()
            self._stop_events[wid] = stop_evt

            def _loop() -> None:
                while not stop_evt.is_set():
                    try:
                        res = self.run_worker_tick(wid)
                        if res is None:
                            # Idle wait
                            stop_evt.wait(0.2)
                    except Exception as exc:
                        logger.error("Worker thread %s error: %s", wid, exc)
                        stop_evt.wait(0.5)

                self.unregister_worker(wid)

            t = threading.Thread(target=_loop, name=f"SAGE-Worker-{wid}", daemon=True)
            self._active_threads[wid] = t
            t.start()
            return wid

    def stop_worker(self, worker_id: str, timeout_seconds: float = 5.0) -> None:
        """Stop an individual worker gracefully."""
        with self._lock:
            stop_evt = self._stop_events.get(worker_id)
            thread = self._active_threads.get(worker_id)

        if stop_evt:
            stop_evt.set()
        if thread:
            thread.join(timeout=timeout_seconds)

        with self._lock:
            self._stop_events.pop(worker_id, None)
            self._active_threads.pop(worker_id, None)

    def stop_all(self, timeout_seconds: float = 5.0) -> None:
        """Stop all running background worker threads."""
        with self._lock:
            worker_ids = list(self._active_threads.keys())

        for wid in worker_ids:
            self.stop_worker(wid, timeout_seconds=timeout_seconds)
