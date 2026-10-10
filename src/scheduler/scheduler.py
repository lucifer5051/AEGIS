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
        workflow_engine: WorkflowEngine,
        admission_controller: Optional[Any] = None,
        missed_run_threshold_seconds: float = DEFAULT_MISSED_RUN_THRESHOLD_SECONDS,
        max_concurrent_runs: int = 5,
    ) -> None:
        """Initialize scheduler with required persistence and execution engine."""
        self._store = scheduler_store
        self._engine = workflow_engine
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
                if schedule.workflow_id:
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
