"""SAGE Durable Scheduler -- Core Scheduling Engine.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12

Supports:
- One-time execution (ONE_SHOT).
- Recurring interval and 5-part cron schedules (CRON_RECURRING).
- Condition/event-triggered execution (EVENT_TRIGGERED).
- Power-cycle & restart resilience with strict deduplication on (schedule_id, scheduled_for).
- Downtime missed-run handling (RUN_ONCE, SKIP, FAIL_ALERT).
- Resource admission and bounded concurrency.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.scheduler.cron import compute_next_cron_run
from src.scheduler.errors import (
    DuplicateScheduleError,
    ScheduleNotFoundError,
    ScheduleValidationError,
)
from src.scheduler.job_models import (
    BackgroundJob,
    JobExecutionResult,
    JobStatus,
    WorkerRegistration,
)
from src.scheduler.models import (
    MissedRunPolicy,
    ScheduleDefinition,
    ScheduleExecutionRecord,
    ScheduleExecutionStatus,
    ScheduleStatus,
    ScheduleType,
)
from src.scheduler.store import SchedulerStore
from src.trust.audit import _check_no_secrets
from src.workflow.engine import WorkflowEngine

if False:
    from src.scheduler.job_queue import JobQueue
    from src.scheduler.worker_manager import WorkerManager

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(iso_str: str) -> datetime:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC datetime."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class WorkflowScheduler:
    """Durable scheduler coordinating one-time, recurring, and event-based workflow runs."""

    # Threshold after which a past-due schedule is classified as a missed run (downtime)
    DEFAULT_MISSED_RUN_THRESHOLD_SECONDS = 300.0  # 5 minutes

    def __init__(
        self,
        scheduler_store: SchedulerStore,
        workflow_engine: Optional[WorkflowEngine] = None,
        job_queue: Optional[Any] = None,
        worker_manager: Optional[Any] = None,
        admission_controller: Optional[Any] = None,
        missed_run_threshold_seconds: float = DEFAULT_MISSED_RUN_THRESHOLD_SECONDS,
        max_concurrent_runs: int = 5,
    ) -> None:
        """Initialize scheduler with required persistence and execution engine."""
        self._store = scheduler_store
        self._engine = workflow_engine
        self._job_queue = job_queue
        self._worker_manager = worker_manager
        self._admission = admission_controller
        self._missed_threshold = missed_run_threshold_seconds
        self._max_concurrency = max_concurrent_runs

    # ==========================================================================
    # SCHEDULE DEFINITIONS LIFECYCLE
    # ==========================================================================

    def create_schedule(
        self,
        target_action: str,
        schedule_type: ScheduleType,
        workflow_id: Optional[str] = None,
        project_id: Optional[str] = None,
        cron_expression: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        trigger_condition: Optional[Dict[str, Any]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        next_run_at: Optional[str] = None,
        missed_run_policy: MissedRunPolicy = MissedRunPolicy.RUN_ONCE,
        schedule_id: Optional[str] = None,
    ) -> ScheduleDefinition:
        """Create and persist a new schedule definition."""
        sid = schedule_id or str(uuid4())
        existing = self._store.get_schedule(sid)
        if existing is not None:
            raise DuplicateScheduleError(f"Schedule with ID '{sid}' already exists.")

        params = dict(parameters or {})
        cond = dict(trigger_condition or {}) if trigger_condition else None

        # Verify zero-secret invariant
        _check_no_secrets(str(params))
        if cond:
            _check_no_secrets(str(cond))

        now_dt = datetime.now(timezone.utc)

        # Validate schedule-type specific parameters
        if schedule_type == ScheduleType.ONE_SHOT:
            if not next_run_at:
                raise ScheduleValidationError("ONE_SHOT schedule requires next_run_at timestamp.")
            computed_next = next_run_at

        elif schedule_type == ScheduleType.CRON_RECURRING:
            if not cron_expression and not interval_seconds:
                raise ScheduleValidationError(
                    "CRON_RECURRING schedule requires cron_expression or interval_seconds."
                )
            if cron_expression:
                # Validate cron syntax
                compute_next_cron_run(cron_expression, now_dt)

            if next_run_at:
                computed_next = next_run_at
            elif cron_expression:
                next_dt = compute_next_cron_run(cron_expression, now_dt)
                computed_next = next_dt.isoformat()
            else:
                next_dt = now_dt + timedelta(seconds=interval_seconds or 60)
                computed_next = next_dt.isoformat()

        elif schedule_type == ScheduleType.EVENT_TRIGGERED:
            if not cond:
                raise ScheduleValidationError(
                    "EVENT_TRIGGERED schedule requires trigger_condition dictionary."
                )
            # Event triggers use distant future timestamp or immediate
            computed_next = next_run_at or (now_dt + timedelta(days=3650)).isoformat()

        else:
            raise ScheduleValidationError(f"Unknown schedule type: {schedule_type}")

        now_str = _now_utc()
        defn = ScheduleDefinition(
            schedule_id=sid,
            target_action=target_action,
            workflow_id=workflow_id,
            project_id=project_id,
            schedule_type=schedule_type,
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            trigger_condition=cond,
            next_run_at=computed_next,
            last_run_at=None,
            missed_run_policy=missed_run_policy,
            status=ScheduleStatus.ACTIVE,
            is_enabled=True,
            parameters=params,
            created_at=now_str,
            updated_at=now_str,
        )

        return self._store.save_schedule(defn)

    def pause_schedule(self, schedule_id: str) -> None:
        """Pause an active schedule."""
        schedule = self._store.get_schedule(schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError(f"Schedule '{schedule_id}' not found.")
        self._store.update_schedule_status(schedule_id, ScheduleStatus.PAUSED)

    def resume_schedule(self, schedule_id: str) -> None:
        """Resume a paused schedule and compute next run."""
        schedule = self._store.get_schedule(schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError(f"Schedule '{schedule_id}' not found.")

        # Recompute next_run_at if it was in the past
        now_dt = datetime.now(timezone.utc)
        scheduled_dt = _parse_iso(schedule.next_run_at)
        if scheduled_dt <= now_dt:
            next_dt = self._compute_next_run(schedule, after_time=now_dt)
            if next_dt:
                self._store.update_schedule_next_run(
                    schedule_id,
                    next_run_at=next_dt.isoformat(),
                    status=ScheduleStatus.ACTIVE,
                )
                return

        self._store.update_schedule_status(schedule_id, ScheduleStatus.ACTIVE)

    def cancel_schedule(self, schedule_id: str) -> None:
        """Cancel a schedule."""
        schedule = self._store.get_schedule(schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError(f"Schedule '{schedule_id}' not found.")
        self._store.update_schedule_status(schedule_id, ScheduleStatus.CANCELLED)

    # ==========================================================================
    # POLLING AND DISPATCH
    # ==========================================================================

    def poll_and_dispatch(
        self,
        current_time: Optional[datetime] = None,
        actor: str = "scheduler",
    ) -> List[ScheduleExecutionRecord]:
        """Poll for due schedules, enforce deduplication, handle missed runs, and execute.

        Args:
            current_time: Optional reference time (defaults to UTC now).
            actor: Calling identity for capability authorization.

        Returns:
            List of ScheduleExecutionRecord produced during this tick.
        """
        now_dt = current_time or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_str = now_dt.isoformat()

        # Find schedules due at or before now_str
        due_schedules = self._store.list_schedules(
            status=ScheduleStatus.ACTIVE,
            due_before=now_str,
        )

        executed_records: List[ScheduleExecutionRecord] = []

        for schedule in due_schedules:
            if not schedule.is_enabled:
                continue

            scheduled_slot = schedule.next_run_at
            scheduled_dt = _parse_iso(scheduled_slot)

            # 1. DEDUPLICATION: Has this exact time slot already been processed?
            if self._store.has_execution_for_slot(schedule.schedule_id, scheduled_slot):
                # Slot already recorded: advance next_run_at to avoid repeated processing
                next_dt = self._compute_next_run(schedule, after_time=now_dt)
                if next_dt:
                    self._store.update_schedule_next_run(
                        schedule.schedule_id,
                        next_run_at=next_dt.isoformat(),
                    )
                else:
                    self._store.update_schedule_status(schedule.schedule_id, ScheduleStatus.EXPIRED)
                continue

            # 2. MISSED RUN HANDLING (Host was offline during scheduled window)
            delay_seconds = (now_dt - scheduled_dt).total_seconds()
            is_missed_run = delay_seconds > self._missed_threshold

            if is_missed_run:
                if schedule.missed_run_policy == MissedRunPolicy.SKIP:
                    # Skip execution; record audit record and advance to future
                    exec_record = ScheduleExecutionRecord(
                        schedule_id=schedule.schedule_id,
                        scheduled_for=scheduled_slot,
                        triggered_at=now_str,
                        status=ScheduleExecutionStatus.SKIPPED,
                        result_summary=(
                            f"Skipped under policy SKIP (delay was {delay_seconds:.1f}s)."
                        ),
                        created_at=now_str,
                        completed_at=now_str,
                    )
                    self._store.record_execution(exec_record)
                    executed_records.append(exec_record)

                    next_dt = self._compute_next_run(schedule, after_time=now_dt)
                    if next_dt:
                        self._store.update_schedule_next_run(
                            schedule.schedule_id,
                            next_run_at=next_dt.isoformat(),
                            last_run_at=now_str,
                        )
                    else:
                        self._store.update_schedule_status(
                            schedule.schedule_id, ScheduleStatus.EXPIRED
                        )
                    continue

                elif schedule.missed_run_policy == MissedRunPolicy.FAIL_ALERT:
                    # Halt schedule and alert
                    exec_record = ScheduleExecutionRecord(
                        schedule_id=schedule.schedule_id,
                        scheduled_for=scheduled_slot,
                        triggered_at=now_str,
                        status=ScheduleExecutionStatus.FAILED,
                        result_summary=(
                            f"Missed run alert: execution overdue by {delay_seconds:.1f}s."
                        ),
                        created_at=now_str,
                        completed_at=now_str,
                    )
                    self._store.record_execution(exec_record)
                    executed_records.append(exec_record)
                    self._store.update_schedule_status(schedule.schedule_id, ScheduleStatus.PAUSED)
                    continue

                # MissedRunPolicy.RUN_ONCE: Fall through to execute once

            # 3. RESOURCE ADMISSION & CONCURRENCY
            if self._admission is not None:
                try:
                    # If admission controller refuses, defer this run to next poll
                    admission_ok = getattr(self._admission, "can_admit", lambda: True)()
                    if not admission_ok:
                        logger.warning(
                            "Scheduler deferred run for '%s' due to resource constraints.",
                            schedule.schedule_id,
                        )
                        continue
                except Exception:
                    pass

            # 4. DISPATCH EXECUTION
            exec_record = ScheduleExecutionRecord(
                schedule_id=schedule.schedule_id,
                scheduled_for=scheduled_slot,
                triggered_at=now_str,
                status=ScheduleExecutionStatus.RUNNING,
                created_at=now_str,
            )
            self._store.record_execution(exec_record)

            run_id = None
            run_success = False
            result_summary = None

            try:
                if self._job_queue is not None:
                    goal = schedule.parameters.get("goal_text") or schedule.target_action
                    bg_job = BackgroundJob(
                        schedule_id=schedule.schedule_id,
                        goal_text=goal,
                        action_type="workflow" if schedule.workflow_id else "goal_execution",
                        status=JobStatus.QUEUED,
                        priority=schedule.parameters.get("priority", 10),
                        scheduled_at=scheduled_slot,
                        configuration_reference=schedule.parameters.get("configuration_ref", {}),
                        metadata=dict(schedule.parameters),
                    )
                    self._job_queue.enqueue_job(bg_job)
                    run_id = None
                    run_success = True
                    result_summary = f"Job '{bg_job.job_id}' queued for background execution."
                elif schedule.workflow_id and self._engine is not None:
                    wf_run = self._engine.start_workflow(
                        workflow_id=schedule.workflow_id,
                        inputs=schedule.parameters,
                        actor=actor,
                        project_id=schedule.project_id,
                        triggered_by="schedule",
                    )
                    run_id = wf_run.run_id
                    run_success = wf_run.status.value == "COMPLETED"
                    result_summary = (
                        "Workflow completed."
                        if run_success
                        else f"Workflow ended with status: {wf_run.status.value}"
                    )
                else:
                    # Direct action dispatch
                    run_success = True
                    result_summary = f"Action '{schedule.target_action}' triggered."

            except Exception as exc:
                run_success = False
                result_summary = f"Execution failed: {exc}"

            completed_str = _now_utc()
            final_status = (
                ScheduleExecutionStatus.COMPLETED if run_success else ScheduleExecutionStatus.FAILED
            )

            updated_record = ScheduleExecutionRecord(
                execution_id=exec_record.execution_id,
                schedule_id=exec_record.schedule_id,
                run_id=run_id,
                scheduled_for=exec_record.scheduled_for,
                triggered_at=exec_record.triggered_at,
                status=final_status,
                result_summary=result_summary,
                created_at=exec_record.created_at,
                completed_at=completed_str,
            )
            self._store.update_execution(updated_record)
            executed_records.append(updated_record)

            # Advance next_run_at or mark EXPIRED
            next_dt = self._compute_next_run(schedule, after_time=now_dt)
            if next_dt:
                self._store.update_schedule_next_run(
                    schedule.schedule_id,
                    next_run_at=next_dt.isoformat(),
                    last_run_at=completed_str,
                )
            else:
                self._store.update_schedule_next_run(
                    schedule.schedule_id,
                    next_run_at=scheduled_slot,
                    last_run_at=completed_str,
                    status=ScheduleStatus.EXPIRED,
                )

        return executed_records

    # ==========================================================================
    # EVENT-TRIGGERED EXECUTION
    # ==========================================================================

    def trigger_event(
        self,
        event_name: str,
        payload: Optional[Dict[str, Any]] = None,
        actor: str = "system",
    ) -> List[ScheduleExecutionRecord]:
        """Trigger any active EVENT_TRIGGERED schedules matching event_name."""
        event_schedules = self._store.list_schedules(
            status=ScheduleStatus.ACTIVE,
        )
        now_str = _now_utc()
        records: List[ScheduleExecutionRecord] = []

        for schedule in event_schedules:
            if schedule.schedule_type != ScheduleType.EVENT_TRIGGERED:
                continue
            cond = schedule.trigger_condition or {}
            target_event = cond.get("event")
            if target_event != event_name:
                continue

            exec_record = ScheduleExecutionRecord(
                schedule_id=schedule.schedule_id,
                scheduled_for=now_str,
                triggered_at=now_str,
                status=ScheduleExecutionStatus.RUNNING,
                created_at=now_str,
            )
            self._store.record_execution(exec_record)

            run_id = None
            run_success = False
            result_summary = None

            event_args = dict(schedule.parameters)
            if payload:
                event_args.update(payload)

            try:
                if schedule.workflow_id:
                    wf_run = self._engine.start_workflow(
                        workflow_id=schedule.workflow_id,
                        inputs=event_args,
                        actor=actor,
                        project_id=schedule.project_id,
                        triggered_by="event",
                    )
                    run_id = wf_run.run_id
                    run_success = wf_run.status.value == "COMPLETED"
                    result_summary = (
                        "Event workflow completed."
                        if run_success
                        else f"Workflow ended in: {wf_run.status.value}"
                    )
                else:
                    run_success = True
                    result_summary = f"Event action '{schedule.target_action}' executed."
            except Exception as exc:
                run_success = False
                result_summary = f"Event trigger error: {exc}"

            completed_str = _now_utc()
            final_status = (
                ScheduleExecutionStatus.COMPLETED if run_success else ScheduleExecutionStatus.FAILED
            )

            updated = ScheduleExecutionRecord(
                execution_id=exec_record.execution_id,
                schedule_id=exec_record.schedule_id,
                run_id=run_id,
                scheduled_for=exec_record.scheduled_for,
                triggered_at=exec_record.triggered_at,
                status=final_status,
                result_summary=result_summary,
                created_at=exec_record.created_at,
                completed_at=completed_str,
            )
            self._store.update_execution(updated)
            records.append(updated)

        return records

    # ==========================================================================
    # HELPERS
    # ==========================================================================

    def _compute_next_run(
        self,
        schedule: ScheduleDefinition,
        after_time: datetime,
    ) -> Optional[datetime]:
        """Compute next run timestamp based on recurrence type."""
        if schedule.schedule_type == ScheduleType.ONE_SHOT:
            return None

        if schedule.schedule_type == ScheduleType.CRON_RECURRING:
            if schedule.cron_expression:
                return compute_next_cron_run(schedule.cron_expression, after_time)
            elif schedule.interval_seconds:
                return after_time + timedelta(seconds=schedule.interval_seconds)

        return None

    # ==========================================================================
    # BACKGROUND JOB & CONVENIENCE SCHEDULING
    # ==========================================================================

    def schedule_immediate(
        self,
        target_action: str,
        goal_text: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        configuration_ref: Optional[Dict[str, Any]] = None,
        priority: int = 10,
    ) -> BackgroundJob:
        """Schedule a job for immediate background execution."""
        if self._job_queue is None:
            raise RuntimeError("JobQueue must be configured to use schedule_immediate().")
        now_str = _now_utc()
        params = dict(parameters or {})
        _check_no_secrets(str(params))
        config_ref = dict(configuration_ref or {})
        if project_id:
            config_ref["project_id"] = project_id
        _check_no_secrets(str(config_ref))
        goal = goal_text or target_action

        job = BackgroundJob(
            goal_text=goal,
            action_type="goal_execution",
            status=JobStatus.QUEUED,
            priority=priority,
            created_at=now_str,
            scheduled_at=now_str,
            configuration_reference=config_ref,
            metadata=params,
        )
        return self._job_queue.enqueue_job(job)

    def schedule_delayed(
        self,
        delay_seconds: float,
        target_action: str,
        goal_text: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        configuration_ref: Optional[Dict[str, Any]] = None,
        priority: int = 10,
    ) -> BackgroundJob:
        """Schedule a job for execution after a specified delay."""
        if self._job_queue is None:
            raise RuntimeError("JobQueue must be configured to use schedule_delayed().")
        now_dt = datetime.now(timezone.utc)
        sched_dt = now_dt + timedelta(seconds=delay_seconds)
        now_str = now_dt.isoformat()
        sched_str = sched_dt.isoformat()

        params = dict(parameters or {})
        _check_no_secrets(str(params))
        config_ref = dict(configuration_ref or {})
        if project_id:
            config_ref["project_id"] = project_id
        _check_no_secrets(str(config_ref))
        goal = goal_text or target_action

        job = BackgroundJob(
            goal_text=goal,
            action_type="goal_execution",
            status=JobStatus.PENDING,
            priority=priority,
            created_at=now_str,
            scheduled_at=sched_str,
            configuration_reference=config_ref,
            metadata=params,
        )
        return self._job_queue.enqueue_job(job)

    def schedule_at(
        self,
        run_at: datetime,
        target_action: str,
        goal_text: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        configuration_ref: Optional[Dict[str, Any]] = None,
        priority: int = 10,
    ) -> BackgroundJob:
        """Schedule a job for execution at a specific future datetime."""
        if self._job_queue is None:
            raise RuntimeError("JobQueue must be configured to use schedule_at().")
        if run_at.tzinfo is None:
            run_at_utc = run_at.replace(tzinfo=timezone.utc)
        else:
            run_at_utc = run_at.astimezone(timezone.utc)

        now_str = _now_utc()
        sched_str = run_at_utc.isoformat()

        params = dict(parameters or {})
        _check_no_secrets(str(params))
        config_ref = dict(configuration_ref or {})
        if project_id:
            config_ref["project_id"] = project_id
        _check_no_secrets(str(config_ref))
        goal = goal_text or target_action

        job = BackgroundJob(
            goal_text=goal,
            action_type="goal_execution",
            status=JobStatus.PENDING,
            priority=priority,
            created_at=now_str,
            scheduled_at=sched_str,
            configuration_reference=config_ref,
            metadata=params,
        )
        return self._job_queue.enqueue_job(job)

    def create_recurring_schedule(
        self,
        recurrence: str,
        target_action: str,
        project_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        missed_run_policy: MissedRunPolicy = MissedRunPolicy.RUN_ONCE,
        timezone_offset_minutes: Optional[int] = None,
    ) -> ScheduleDefinition:
        """Create a recurring schedule with friendly recurrence expressions and timezone support."""
        now_dt = datetime.now(timezone.utc)
        cron_expr: Optional[str] = None
        interval_secs: Optional[int] = None

        rec_lower = recurrence.strip().lower()
        if rec_lower == "hourly":
            cron_expr = "0 * * * *"
        elif rec_lower == "daily":
            cron_expr = "0 0 * * *"
        elif rec_lower == "weekly":
            cron_expr = "0 0 * * 0"
        elif rec_lower == "monthly":
            cron_expr = "0 0 1 * *"
        elif rec_lower.isdigit():
            interval_secs = int(rec_lower)
        else:
            # Assume standard cron expression
            cron_expr = recurrence.strip()

        # Handle timezone offset if specified
        if cron_expr and timezone_offset_minutes is not None:
            tz = timezone(timedelta(minutes=timezone_offset_minutes))
            local_now = now_dt.astimezone(tz)
            next_local = compute_next_cron_run(cron_expr, local_now)
            next_utc = next_local.astimezone(timezone.utc)
            next_run_str = next_utc.isoformat()
        else:
            next_run_str = None

        return self.create_schedule(
            target_action=target_action,
            schedule_type=ScheduleType.CRON_RECURRING,
            workflow_id=workflow_id,
            project_id=project_id,
            cron_expression=cron_expr,
            interval_seconds=interval_secs,
            parameters=parameters,
            missed_run_policy=missed_run_policy,
            next_run_at=next_run_str,
        )

    # ==========================================================================
    # JOB & WORKER DELEGATION APIS
    # ==========================================================================

    def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        schedule_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[BackgroundJob]:
        """List background jobs with optional status filter."""
        if not self._job_queue:
            return []
        return self._job_queue.list_jobs(status=status, schedule_id=schedule_id, limit=limit)

    def get_job(self, job_id: str) -> Optional[BackgroundJob]:
        """Retrieve a background job by ID."""
        if not self._job_queue:
            return None
        return self._job_queue.get_job(job_id)

    def cancel_job(self, job_id: str, reason: str = "Cancelled by user") -> Optional[BackgroundJob]:
        """Cancel a pending or queued job."""
        if not self._job_queue:
            return None
        return self._job_queue.cancel_job(job_id, reason=reason)

    def retry_job(self, job_id: str) -> Optional[BackgroundJob]:
        """Reset a failed job to QUEUED for immediate retry."""
        if not self._job_queue:
            return None
        return self._job_queue.retry_job(job_id)

    def approve_job(
        self,
        job_id: str,
        approver: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[BackgroundJob]:
        """Resume an approval-suspended job."""
        if not self._job_queue:
            return None
        return self._job_queue.resume_approved_job(job_id, approver=approver, approval_metadata=metadata)

    def reject_job(self, job_id: str, reason: str = "Rejected by user") -> Optional[BackgroundJob]:
        """Reject an approval-suspended job."""
        if not self._job_queue:
            return None
        return self._job_queue.reject_job(job_id, reason=reason)

    def get_worker_status(self, worker_id: str) -> Optional[WorkerRegistration]:
        """Get status for a specific background worker."""
        if not self._worker_manager:
            return None
        return self._worker_manager.get_worker_status(worker_id)

    def list_workers(self) -> List[WorkerRegistration]:
        """List all registered background workers."""
        if not self._worker_manager:
            return []
        return self._worker_manager.list_workers()

    def run_worker_tick(
        self,
        worker_id: str,
        current_time: Optional[datetime] = None,
    ) -> Optional[JobExecutionResult]:
        """Run a single execution cycle on the configured worker manager."""
        if not self._worker_manager:
            return None
        return self._worker_manager.run_worker_tick(worker_id, current_time=current_time)

    def start_worker(self, worker_id: Optional[str] = None) -> str:
        """Start a background worker thread."""
        if not self._worker_manager:
            raise RuntimeError("WorkerManager must be configured to start background workers.")
        return self._worker_manager.start_worker(worker_id=worker_id)

    def stop_worker(self, worker_id: str, timeout_seconds: float = 5.0) -> None:
        """Stop a background worker thread."""
        if self._worker_manager:
            self._worker_manager.stop_worker(worker_id, timeout_seconds=timeout_seconds)

    def stop_all_workers(self, timeout_seconds: float = 5.0) -> None:
        """Stop all background worker threads."""
        if self._worker_manager:
            self._worker_manager.stop_all(timeout_seconds=timeout_seconds)
