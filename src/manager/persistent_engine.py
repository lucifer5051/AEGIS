"""SAGE Persistent Execution Engine.

Canonical Reference:
    PROMPT 029: Persistent Completion Mode Execution Loop
    Principle: Understand -> Explain -> Confirm -> Execute ->
               Observe -> Diagnose -> Recover -> Verify -> Complete.

Invariants:
- Requires a confirmed ContextBriefing before persistent execution begins.
- Routine bounded retries proceed autonomously within confirmed scope.
- Material context changes (new tools, policy approval, stuck loops) PAUSE and escalate.
- All actions must be authorized by Trust Core before dispatch.
- Checkpoints are saved before and after every execution, diagnosis, and recovery action.
- Zero-Secret Invariant: Checkpoints, diagnosis records, and metadata never leak credentials.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.manager.briefing import BriefingStatus, ContextBriefing, ExecutionBudget
from src.manager.errors import (
    BudgetExhaustedError,
    ConfirmationRequiredError,
    StuckLoopDetectedError,
    TaskNotFoundError,
    TaskStateError,
)
from src.manager.models import (
    StepPlan,
    StepRecord,
    StepState,
    TaskRecord,
    TaskState,
)
from src.manager.observation import StepObservation
from src.manager.planner import PlannedStep
from src.manager.research import (
    DiagnosticInvestigator,
    ErrorDiagnosis,
    RecoveryStrategyType,
    ResearchProvider,
)
from src.manager.state_store import StateStore
from src.manager.verifier import StepVerifier, VerificationOutcome
from src.notifications.models import NotificationEventType
from src.tools.broker import ToolBroker
from src.tools.models import ToolResultStatus
from src.trust.audit import _check_no_secrets
from src.trust.authority import TrustAuthority

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


class PersistentExecutionEngine:
    """Orchestrates persistent, resilient execution under Persistent Completion Mode."""

    def __init__(
        self,
        state_store: StateStore,
        trust_authority: TrustAuthority,
        tool_broker: ToolBroker,
        verifier: Optional[StepVerifier] = None,
        investigator: Optional[DiagnosticInvestigator] = None,
        research_provider: Optional[ResearchProvider] = None,
        online_provider: Optional[ResearchProvider] = None,
        network_permitted: bool = False,
        notification_manager: Optional[Any] = None,
        experience_service: Optional[Any] = None,
    ) -> None:
        self._store = state_store
        self._authority = trust_authority
        self._tools = tool_broker
        self._verifier = verifier or StepVerifier()
        self._investigator = investigator or DiagnosticInvestigator(
            research_provider=research_provider,
            online_provider=online_provider,
            experience_service=experience_service,
            network_permitted=network_permitted,
        )
        self._notifications = notification_manager
        self._experience = experience_service

    def execute_persistent(
        self,
        task_id: str,
        actor: str = "persistent_engine",
    ) -> TaskRecord:
        """Run the persistent execution loop for a task with confirmed briefing.

        Raises:
            TaskNotFoundError: If task does not exist.
            TaskStateError: If task is not in EXECUTING state.
            ConfirmationRequiredError: If task lacks a confirmed ContextBriefing.
            BudgetExhaustedError: If runtime or iteration limits are exceeded.
            StuckLoopDetectedError: If oscillating strategies or unchanged repeated failures occur.
        """
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        if task.state != TaskState.EXECUTING:
            raise TaskStateError(
                f"Cannot execute task '{task_id}' in state '{task.state.value}'. "
                "Expected 'EXECUTING'."
            )

        # 1. Enforce Confirmation Gate: Briefing MUST be CONFIRMED
        briefing_data = task.metadata.get("context_briefing")
        if not briefing_data:
            raise ConfirmationRequiredError(
                f"Task '{task_id}' is in Persistent Completion Mode but has no Context Briefing."
            )

        briefing = ContextBriefing.model_validate(briefing_data)
        if briefing.status != BriefingStatus.CONFIRMED:
            raise ConfirmationRequiredError(
                f"Task '{task_id}' cannot execute: Context Briefing is in state "
                f"'{briefing.status.value}'. Explicit user confirmation is required."
            )

        budget: ExecutionBudget = briefing.budgets
        start_time = time.monotonic()
        iteration_count = 0
        recovery_cycle_count = 0
        tool_call_count = 0

        consecutive_failures = 0
        recent_signatures: List[str] = []
        strategy_history: List[RecoveryStrategyType] = []
        disproven_per_step: Dict[str, List[str]] = {}

        logger.info(
            "Entering Persistent Execution Loop for task=%r (v%d)",
            task_id,
            briefing.version,
        )

        while True:
            iteration_count += 1
            elapsed_time = time.monotonic() - start_time

            # 2. Budget checks
            if iteration_count > budget.max_iterations:
                err_msg = (
                    f"Iteration budget exceeded ({iteration_count} > {budget.max_iterations})."
                )
                self._record_budget_exhaustion(task_id, err_msg)
                raise BudgetExhaustedError(err_msg)

            if elapsed_time > budget.max_time_seconds:
                err_msg = (
                    f"Time budget exceeded ({elapsed_time:.1f}s > {budget.max_time_seconds}s)."
                )
                self._record_budget_exhaustion(task_id, err_msg)
                raise BudgetExhaustedError(err_msg)

            if recovery_cycle_count > budget.max_recovery_cycles:
                err_msg = (
                    f"Recovery cycle budget exceeded "
                    f"({recovery_cycle_count} > {budget.max_recovery_cycles})."
                )
                self._record_budget_exhaustion(task_id, err_msg)
                raise BudgetExhaustedError(err_msg)

            # 3. Retrieve next pending step
            steps = self._store.list_task_steps(task_id)
            pending_step: Optional[StepRecord] = None
            for s in steps:
                if s.state == StepState.PENDING:
                    pending_step = s
                    break

            if not pending_step:
                # Check if all steps completed
                all_done = all(s.state in (StepState.COMPLETED, StepState.SKIPPED) for s in steps)
                if all_done:
                    # Verified completion!
                    return self._complete_task(task_id, briefing)
                else:
                    # Check for waiting approval
                    awaiting = any(s.state == StepState.AWAITING_APPROVAL for s in steps)
                    if awaiting:
                        return self._store.transition_task_state(
                            task_id,
                            TaskState.AWAITING_APPROVAL,
                            error_summary="Step requires authorization approval.",
                        )
                    # No pending steps left but not all completed
                    return self._store.transition_task_state(
                        task_id,
                        TaskState.FAILED,
                        error_summary="Execution ended with incomplete non-pending steps.",
                    )

            # 4. Execute the pending step
            step_plan = StepPlan(
                action_name=pending_step.action_name,
                tool_name=pending_step.tool_name,
                is_idempotent=pending_step.is_idempotent,
                idempotency_key=pending_step.idempotency_key,
                arguments=pending_step.arguments,
            )

            # Pre-step checkpoint
            next_seq = len(self._store.list_checkpoints(task_id)) + 1
            self._store.create_checkpoint(
                task_id=task_id,
                step_id=pending_step.step_id,
                checkpoint_seq=next_seq,
                snapshot={
                    "event": "PERSISTENT_STEP_DISPATCH",
                    "step_index": pending_step.step_index,
                    "action": pending_step.action_name,
                    "iteration": iteration_count,
                    "recovery_cycles": recovery_cycle_count,
                },
            )

            self._store.transition_step_state(pending_step.step_id, StepState.RUNNING)
            tool_call_count += 1

            # Dispatch tool
            if pending_step.tool_name:
                tool_result = self._tools.invoke(
                    tool_name=pending_step.tool_name,
                    args=pending_step.arguments,
                    actor=actor,
                    project_id=task.project_id,
                )

                if tool_result.status == ToolResultStatus.SUCCESS:
                    # Verify output via verifier
                    obs = StepObservation.from_tool_result(
                        step_number=pending_step.step_index,
                        tool_name=pending_step.tool_name or pending_step.action_name,
                        result=tool_result,
                    )
                    ps = PlannedStep(
                        step_number=pending_step.step_index,
                        description=pending_step.action_name,
                        tool=pending_step.tool_name or pending_step.action_name,
                        arguments=pending_step.arguments,
                        is_idempotent=step_plan.is_idempotent if step_plan else True,
                        acceptance_criteria="Execution completes successfully.",
                    )
                    ver_outcome = self._verifier.verify(step=ps, observation=obs)
                    if ver_outcome.outcome == VerificationOutcome.VERIFIED_SUCCESS:
                        # Success
                        self._store.transition_step_state(
                            pending_step.step_id,
                            StepState.COMPLETED,
                            result_summary=tool_result.output,
                        )
                        consecutive_failures = 0
                        self._store.create_checkpoint(
                            task_id=task_id,
                            step_id=pending_step.step_id,
                            checkpoint_seq=next_seq + 1,
                            snapshot={
                                "step_completed": True,
                                "step_index": pending_step.step_index,
                                "output": tool_result.output,
                            },
                        )
                        continue

                    # Step verification assertion failed
                    reason = ver_outcome.rationale or "assertions failed"
                    error_summary = f"Step verification failed: {reason}"
                    error_type = "VerificationError"
                elif tool_result.status == ToolResultStatus.DENIED:
                    error_summary = f"Policy denied: {tool_result.error_summary}"
                    error_type = "PolicyDeniedError"
                else:
                    error_summary = tool_result.error_summary or "Tool invocation failed"
                    error_type = tool_result.error_type or "ToolError"
            else:
                # Internal non-tool step
                error_summary = "Non-tool steps unsupported in persistent loop"
                error_type = "StepExecutionError"

            # 5. Handle Failure: CLASSIFY -> INVESTIGATE -> RESEARCH -> RECOVER
            consecutive_failures += 1
            recovery_cycle_count += 1

            step_disproven = disproven_per_step.get(pending_step.step_id, [])
            diagnosis: ErrorDiagnosis = self._investigator.diagnose(
                step_id=pending_step.step_id,
                step_index=pending_step.step_index,
                step_plan=step_plan,
                error_type=error_type,
                error_summary=error_summary,
                consecutive_failures=consecutive_failures,
                previous_strategies=strategy_history,
                disproven_hypotheses=step_disproven,
            )

            # Record hypothesis into disproven set for this step in case subsequent retry fails
            if diagnosis.testable_hypothesis:
                if pending_step.step_id not in disproven_per_step:
                    disproven_per_step[pending_step.step_id] = []
                if diagnosis.testable_hypothesis not in disproven_per_step[pending_step.step_id]:
                    disproven_per_step[pending_step.step_id].append(diagnosis.testable_hypothesis)

            strategy_history.append(diagnosis.chosen_strategy)
            recent_signatures.append(diagnosis.error_signature)

            # Persist checkpoint recording the error, diagnosis, and research provenance
            self._store.create_checkpoint(
                task_id=task_id,
                step_id=pending_step.step_id,
                checkpoint_seq=next_seq + 1,
                snapshot={
                    "event": "ERROR_DIAGNOSIS",
                    "error_summary": error_summary,
                    "diagnosis": diagnosis.model_dump(),
                    "testable_hypothesis": diagnosis.testable_hypothesis,
                    "verification_method": diagnosis.verification_method,
                    "research_provider": (
                        diagnosis.research_record.provider_type
                        if diagnosis.research_record
                        else None
                    ),
                    "consecutive_failures": consecutive_failures,
                    "recovery_cycles": recovery_cycle_count,
                },
            )

            # Stuck loop check (3 identical error signatures without progress)
            if (
                len(recent_signatures) >= 3
                and recent_signatures[-1] == recent_signatures[-2] == recent_signatures[-3]
            ):
                stuck_msg = (
                    f"Stuck loop detected on signature '{diagnosis.error_signature}'. "
                    f"Repeated failures without progress."
                )
                self._pause_for_reconfirmation(
                    task_id=task_id,
                    step_id=pending_step.step_id,
                    reason=stuck_msg,
                )
                raise StuckLoopDetectedError(stuck_msg)

            # Check if user reconfirmation or human approval is required
            if diagnosis.requires_user_reconfirmation or diagnosis.chosen_strategy in (
                RecoveryStrategyType.ESCALATE_APPROVAL,
                RecoveryStrategyType.ABORT_BLOCKER,
            ):
                reason = (
                    diagnosis.reconfirmation_reason
                    or "Material context change requires reconfirmation."
                )
                return self._pause_for_reconfirmation(
                    task_id=task_id,
                    step_id=pending_step.step_id,
                    reason=reason,
                )

            # Routine recovery within confirmed scope
            if diagnosis.chosen_strategy == RecoveryStrategyType.PARAM_ADJUSTMENT:
                # Update arguments of pending step for next attempt
                logger.info(
                    "Applying routine recovery PARAM_ADJUSTMENT for step %d: %s",
                    pending_step.step_index,
                    diagnosis.recovery_arguments,
                )
                self._store.transition_step_state(
                    pending_step.step_id,
                    StepState.PENDING,
                    result_summary=f"Recovering via {diagnosis.chosen_strategy.value}",
                )
                # Update step arguments in state store
                self._store.update_step_arguments(
                    pending_step.step_id,
                    diagnosis.recovery_arguments,
                )
                continue

            elif diagnosis.chosen_strategy == RecoveryStrategyType.RETRY_WITH_BACKOFF:
                logger.info(
                    "Retrying step %d with backoff (%d consecutive failures)",
                    pending_step.step_index,
                    consecutive_failures,
                )
                self._store.transition_step_state(
                    pending_step.step_id,
                    StepState.PENDING,
                    result_summary=f"Retrying: attempt {consecutive_failures + 1}",
                )
                continue

            else:
                # Strategy not handled autonomously -> pause for user intervention
                return self._pause_for_reconfirmation(
                    task_id=task_id,
                    step_id=pending_step.step_id,
                    reason=(
                        f"Recovery strategy {diagnosis.chosen_strategy.value} "
                        "requires user guidance."
                    ),
                )

    def _complete_task(self, task_id: str, briefing: ContextBriefing) -> TaskRecord:
        """Mark task completed, notify user, and persist final checkpoint."""
        now = _now_utc()
        completed = self._store.transition_task_state(task_id, TaskState.COMPLETED)
        cps = self._store.list_checkpoints(task_id)
        self._store.create_checkpoint(
            task_id=task_id,
            checkpoint_seq=len(cps) + 1,
            snapshot={
                "event": "PERSISTENT_COMPLETION_VERIFIED",
                "goal": briefing.goal,
                "acceptance_criteria": briefing.acceptance_criteria,
                "completed_at": now,
            },
        )
        if self._notifications is not None:
            try:
                if hasattr(self._notifications, "notify"):
                    self._notifications.notify(
                        event_type=NotificationEventType.WORKFLOW_COMPLETED,
                        title=f"Workflow Completed: {task_id}",
                        body=(f"Workflow '{task_id}' completed successfully in Persistent Mode."),
                        priority=None,
                        source_type="task",
                        source_id=task_id,
                    )
                elif hasattr(self._notifications, "notify_workflow_completed"):
                    self._notifications.notify_workflow_completed(
                        workflow_id=task_id,
                        run_id=task_id,
                    )
            except Exception as exc:
                logger.warning("Notification on task completion failed: %s", exc)

        logger.info("Persistent Completion verified for task=%r", task_id)
        return completed

    def _pause_for_reconfirmation(
        self,
        task_id: str,
        step_id: str,
        reason: str,
    ) -> TaskRecord:
        """Pause execution, set AWAITING_APPROVAL, and notify user."""
        _check_no_secrets(reason)
        self._store.transition_step_state(
            step_id,
            StepState.AWAITING_APPROVAL,
            result_summary=reason,
        )
        task = self._store.transition_task_state(
            task_id,
            TaskState.AWAITING_APPROVAL,
            error_summary=reason,
        )
        cps = self._store.list_checkpoints(task_id)
        self._store.create_checkpoint(
            task_id=task_id,
            step_id=step_id,
            checkpoint_seq=len(cps) + 1,
            snapshot={
                "event": "PAUSED_FOR_USER_CONFIRMATION",
                "reason": reason,
            },
        )
        if self._notifications is not None:
            try:
                if hasattr(self._notifications, "notify"):
                    self._notifications.notify(
                        event_type=NotificationEventType.APPROVAL_REQUIRED,
                        title=f"Approval Required: {task_id}",
                        body=reason,
                        source_type="task",
                        source_id=task_id,
                    )
                elif hasattr(self._notifications, "notify_approval_required"):
                    self._notifications.notify_approval_required(
                        action_name="persistent_execution.reconfirm",
                        request_id=task_id,
                        actor="persistent_engine",
                    )
            except Exception as exc:
                logger.warning("Notification on reconfirmation pause failed: %s", exc)

        logger.info("Task %r paused for user confirmation: %s", task_id, reason)
        return task

    def _record_budget_exhaustion(self, task_id: str, error_summary: str) -> None:
        """Record budget exhaustion in task state, checkpoint, and notifications."""
        _check_no_secrets(error_summary)
        cps = self._store.list_checkpoints(task_id)
        self._store.create_checkpoint(
            task_id=task_id,
            checkpoint_seq=len(cps) + 1,
            snapshot={
                "event": "BUDGET_EXHAUSTED",
                "error": error_summary,
            },
        )
        self._store.transition_task_state(
            task_id,
            TaskState.FAILED,
            error_summary=error_summary,
        )
        if self._notifications is not None:
            try:
                if hasattr(self._notifications, "notify"):
                    self._notifications.notify(
                        event_type=NotificationEventType.WORKFLOW_FAILED,
                        title=f"Persistent Execution Failed: {task_id}",
                        body=error_summary,
                        source_type="task",
                        source_id=task_id,
                    )
                elif hasattr(self._notifications, "notify_recovery_exhausted"):
                    self._notifications.notify_recovery_exhausted(
                        task_id=task_id,
                        error_summary=error_summary,
                    )
            except Exception as exc:
                logger.warning("Notification on budget exhaustion failed: %s", exc)
