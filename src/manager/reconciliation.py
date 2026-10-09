"""SAGE Supervisor & State Store -- Startup Reconciliation Engine.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 27:
    "Startup Reconciliation:
     - Upon startup, the Reconciliation Engine scans for in-flight tasks marked RUNNING.
     - Incomplete tasks are inspected against idempotent checkpoints and either resumed,
       safely rolled back, or flagged for user attention."

Invariants:
- Deterministic, fail-closed recovery.
- Zero network access.
- Ambiguous in-flight side effects are NEVER blindly replayed; they must escalate
  to AWAITING_APPROVAL.
- Idempotent steps with completion checkpoints recover as COMPLETED.
- Unstarted/safe idempotent steps recover as PENDING and resume.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from src.manager.models import (
    CheckpointSnapshot,
    ReconciledTaskResult,
    ReconciliationReport,
    RecoveryDisposition,
    StepRecord,
    StepState,
    TaskRecord,
    TaskState,
)
from src.manager.state_store import StateStore

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


class ReconciliationEngine:
    """Discovers interrupted tasks on startup and reconciles them into safe states."""

    def __init__(self, state_store: StateStore) -> None:
        """Initialize with an active state store."""
        self._store = state_store

    def reconcile_startup(self) -> ReconciliationReport:
        """Scan state store for in-flight tasks and reconcile each task deterministically.

        Returns:
            Structured ReconciliationReport summarizing recovery actions.
        """
        in_flight_tasks = self._store.get_in_flight_tasks()
        reconciled_results: List[ReconciledTaskResult] = []

        resumed_count = 0
        escalated_count = 0
        failed_count = 0

        for task in in_flight_tasks:
            result = self._reconcile_single_task(task)
            reconciled_results.append(result)

            if result.disposition == RecoveryDisposition.RESUME:
                resumed_count += 1
            elif result.disposition == RecoveryDisposition.ESCALATE_TO_USER:
                escalated_count += 1
            elif result.disposition == RecoveryDisposition.FAIL_TERMINAL:
                failed_count += 1

        report = ReconciliationReport(
            report_id=str(uuid4()),
            timestamp=_now_utc(),
            tasks_discovered=len(in_flight_tasks),
            tasks_resumed=resumed_count,
            tasks_escalated=escalated_count,
            tasks_failed=failed_count,
            reconciled_tasks=reconciled_results,
        )

        logger.info(
            "Reconciliation complete: discovered=%d, resumed=%d, escalated=%d, failed=%d",
            report.tasks_discovered,
            report.tasks_resumed,
            report.tasks_escalated,
            report.tasks_failed,
        )
        return report

    # Backward-compatible alias
    reconcile_on_startup = reconcile_startup

    def _reconcile_single_task(self, task: TaskRecord) -> ReconciledTaskResult:
        """Reconcile an individual in-flight task."""
        try:
            if task.state == TaskState.SUBMITTED:
                return ReconciledTaskResult(
                    task_id=task.task_id,
                    disposition=RecoveryDisposition.RESUME,
                    previous_state=task.state,
                    new_state=task.state,
                    action_summary="Task is submitted and ready for planning.",
                )

            if task.state == TaskState.AWAITING_APPROVAL:
                return ReconciledTaskResult(
                    task_id=task.task_id,
                    disposition=RecoveryDisposition.ESCALATE_TO_USER,
                    previous_state=task.state,
                    new_state=task.state,
                    action_summary="Task continues awaiting user approval.",
                )

            if task.state == TaskState.PLANNING:
                steps = self._store.list_task_steps(task.task_id)
                if not steps:
                    return ReconciledTaskResult(
                        task_id=task.task_id,
                        disposition=RecoveryDisposition.RESUME,
                        previous_state=task.state,
                        new_state=task.state,
                        action_summary="Planning incomplete; ready to resume plan formation.",
                    )
                # If all steps exist and are PENDING, advance to EXECUTING
                all_pending = all(s.state == StepState.PENDING for s in steps)
                if all_pending:
                    updated = self._store.transition_task_state(task.task_id, TaskState.EXECUTING)
                    return ReconciledTaskResult(
                        task_id=task.task_id,
                        disposition=RecoveryDisposition.RESUME,
                        previous_state=task.state,
                        new_state=updated.state,
                        action_summary="Plan steps verified; advanced to EXECUTING.",
                    )
                return ReconciledTaskResult(
                    task_id=task.task_id,
                    disposition=RecoveryDisposition.RESUME,
                    previous_state=task.state,
                    new_state=task.state,
                    action_summary="Planning task in progress; ready to resume.",
                )

            if task.state == TaskState.EXECUTING:
                return self._reconcile_executing_task(task)

            # Fallback for any other non-terminal state
            return ReconciledTaskResult(
                task_id=task.task_id,
                disposition=RecoveryDisposition.RESUME,
                previous_state=task.state,
                new_state=task.state,
                action_summary=f"Task preserved in state '{task.state.value}'.",
            )

        except Exception as exc:
            logger.error("Failed to reconcile task '%s': %s", task.task_id, exc, exc_info=True)
            try:
                self._store.transition_task_state(
                    task.task_id,
                    TaskState.FAILED,
                    error_summary=f"Reconciliation error: {exc}",
                )
            except Exception:
                pass
            return ReconciledTaskResult(
                task_id=task.task_id,
                disposition=RecoveryDisposition.FAIL_TERMINAL,
                previous_state=task.state,
                new_state=TaskState.FAILED,
                action_summary=f"Reconciliation failed with error: {exc}",
            )

    def _reconcile_executing_task(self, task: TaskRecord) -> ReconciledTaskResult:
        """Reconcile a task that was actively EXECUTING when the crash occurred."""
        steps = self._store.list_task_steps(task.task_id)
        checkpoints = self._store.list_checkpoints(task.task_id)

        if not steps:
            # No steps registered; fail closed
            updated = self._store.transition_task_state(
                task.task_id,
                TaskState.FAILED,
                error_summary="Task marked EXECUTING without registered execution steps.",
            )
            return ReconciledTaskResult(
                task_id=task.task_id,
                disposition=RecoveryDisposition.FAIL_TERMINAL,
                previous_state=task.state,
                new_state=updated.state,
                action_summary="Failed closed: executing task had no steps.",
            )

        running_steps = [s for s in steps if s.state == StepState.RUNNING]

        if not running_steps:
            # No steps were mid-flight
            all_completed = all(s.state == StepState.COMPLETED for s in steps)
            if all_completed:
                updated = self._store.transition_task_state(task.task_id, TaskState.COMPLETED)
                return ReconciledTaskResult(
                    task_id=task.task_id,
                    disposition=RecoveryDisposition.RESUME,
                    previous_state=task.state,
                    new_state=updated.state,
                    action_summary="All steps were completed; task marked COMPLETED.",
                )

            # Check if any step is awaiting approval
            if any(s.state == StepState.AWAITING_APPROVAL for s in steps):
                updated = self._store.transition_task_state(
                    task.task_id, TaskState.AWAITING_APPROVAL
                )
                return ReconciledTaskResult(
                    task_id=task.task_id,
                    disposition=RecoveryDisposition.ESCALATE_TO_USER,
                    previous_state=task.state,
                    new_state=updated.state,
                    action_summary=(
                        "Step awaiting approval; task transitioned to AWAITING_APPROVAL."
                    ),
                )

            # Remaining steps are PENDING; safely resume
            first_pending = next((s for s in steps if s.state == StepState.PENDING), None)
            step_idx = first_pending.step_index if first_pending else 0
            return ReconciledTaskResult(
                task_id=task.task_id,
                disposition=RecoveryDisposition.RESUME,
                previous_state=task.state,
                new_state=task.state,
                action_summary=(
                    f"Clean checkpoint found; ready to resume from step index {step_idx}."
                ),
                step_id=first_pending.step_id if first_pending else None,
            )

        # There is an in-flight step
        in_flight_step = running_steps[0]

        # Check if an idempotent completion checkpoint exists for this step
        completion_checkpoint = self._find_completion_checkpoint(in_flight_step, checkpoints)

        if completion_checkpoint:
            # Step actually finished before process crash
            self._store.transition_step_state(
                in_flight_step.step_id,
                StepState.COMPLETED,
                result_summary="Recovered from completion checkpoint.",
            )
            # Re-check if remaining steps exist
            all_done = all(
                s.step_id == in_flight_step.step_id or s.state == StepState.COMPLETED for s in steps
            )
            if all_done:
                updated = self._store.transition_task_state(task.task_id, TaskState.COMPLETED)
                return ReconciledTaskResult(
                    task_id=task.task_id,
                    disposition=RecoveryDisposition.RESUME,
                    previous_state=task.state,
                    new_state=updated.state,
                    action_summary="In-flight step had completion checkpoint; all steps complete.",
                    step_id=in_flight_step.step_id,
                )

            return ReconciledTaskResult(
                task_id=task.task_id,
                disposition=RecoveryDisposition.RESUME,
                previous_state=task.state,
                new_state=task.state,
                action_summary="In-flight step had completion checkpoint; advanced to COMPLETED.",
                step_id=in_flight_step.step_id,
            )

        # No completion checkpoint.
        if in_flight_step.is_idempotent:
            # Idempotent step can safely be reset to PENDING for re-execution
            self._store.transition_step_state(
                in_flight_step.step_id,
                StepState.PENDING,
                result_summary="Crash recovery: reset idempotent in-flight step to PENDING.",
            )
            return ReconciledTaskResult(
                task_id=task.task_id,
                disposition=RecoveryDisposition.RESUME,
                previous_state=task.state,
                new_state=task.state,
                action_summary=(
                    f"Idempotent step {in_flight_step.step_index} "
                    f"({in_flight_step.action_name}) reset to PENDING for safe retry."
                ),
                step_id=in_flight_step.step_id,
            )

        # Ambiguous in-flight side effect! Non-idempotent action must NEVER be blindly replayed.
        self._store.transition_step_state(
            in_flight_step.step_id,
            StepState.AWAITING_APPROVAL,
            result_summary=(
                "Crash recovery: non-idempotent in-flight step requires user verification."
            ),
        )
        updated = self._store.transition_task_state(
            task.task_id,
            TaskState.AWAITING_APPROVAL,
            error_summary=(
                f"Interrupted in-flight non-idempotent step {in_flight_step.step_index} "
                f"('{in_flight_step.action_name}') requires manual verification before resume."
            ),
        )
        return ReconciledTaskResult(
            task_id=task.task_id,
            disposition=RecoveryDisposition.ESCALATE_TO_USER,
            previous_state=task.state,
            new_state=updated.state,
            action_summary=(
                f"Non-idempotent step {in_flight_step.step_index} ('{in_flight_step.action_name}') "
                "interrupted mid-execution; escalated to AWAITING_APPROVAL."
            ),
            step_id=in_flight_step.step_id,
        )

    def _find_completion_checkpoint(
        self,
        step: StepRecord,
        checkpoints: List[CheckpointSnapshot],
    ) -> Optional[CheckpointSnapshot]:
        """Check if any checkpoint indicates this step was completed."""
        for cp in checkpoints:
            if cp.step_id == step.step_id and cp.snapshot.get("step_completed") is True:
                return cp
        return None
