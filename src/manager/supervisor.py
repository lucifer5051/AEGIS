"""SAGE Supervisor -- Central Execution Coordinator.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 27
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md

Core Invariants:
1. "Intelligence proposes; deterministic authority decides."
2. The Supervisor coordinates planning and tool invocation, but NEVER acts
   as an authorization authority.
3. Every tool invocation must go through TrustAuthority and ToolBroker.
4. Fast Decision is strictly advisory; it has zero authorization privileges.
5. All executions are checkpointed durably to SQLite before dispatch and
   after completion.
6. Fail-closed: denied or ambiguous actions halt task execution and escalate
   to AWAITING_APPROVAL or FAILED.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from src.decision.models import FastDecisionRequest, RequestContext
from src.decision.service import FastDecisionService
from src.manager.briefing import (
    ConfirmationGate,
    ContextBriefing,
    ExecutionBudget,
    ProvenanceType,
)
from src.manager.context import ContextAssembler
from src.manager.errors import (
    StepNotFoundError,
    TaskNotFoundError,
    TaskStateError,
)
from src.manager.models import (
    ExecutionMode,
    StepPlan,
    StepRecord,
    StepState,
    TaskRecord,
    TaskState,
)
from src.manager.observation import StepObservation
from src.manager.persistent_engine import PersistentExecutionEngine
from src.manager.planner import PlanValidationError, StructuredPlanner
from src.manager.research import (
    DiagnosticInvestigator,
    LocalDocResearchProvider,
    ResearchProvider,
)
from src.manager.state_store import StateStore
from src.manager.synthesis import TaskSynthesizer
from src.manager.verifier import StepVerifier, VerificationOutcome
from src.reasoning.base import LocalReasoningProvider
from src.tools.broker import ToolBroker
from src.tools.models import ToolResultStatus
from src.trust.authority import TrustAuthority

logger = logging.getLogger(__name__)


class Supervisor:
    """Central supervisor coordinating task lifecycle, planning, and execution."""

    # Max retry attempts for idempotent steps
    MAX_STEP_RETRIES = 2

    def __init__(
        self,
        state_store: StateStore,
        trust_authority: TrustAuthority,
        tool_broker: ToolBroker,
        fast_decision: Optional[FastDecisionService] = None,
        reasoning_provider: Optional[LocalReasoningProvider] = None,
        planner: Optional[StructuredPlanner] = None,
        verifier: Optional[StepVerifier] = None,
        synthesizer: Optional[TaskSynthesizer] = None,
        context_assembler: Optional[ContextAssembler] = None,
        skill_registry: Optional[Any] = None,
        clarification_gate: Optional[Any] = None,
        admission_controller: Optional[Any] = None,
        experience_service: Optional[Any] = None,
        memory_store: Optional[Any] = None,
        retriever: Optional[Any] = None,
        lesson_manager: Optional[Any] = None,
        confirmation_gate: Optional[ConfirmationGate] = None,
        persistent_engine: Optional[PersistentExecutionEngine] = None,
        research_provider: Optional[ResearchProvider] = None,
        notification_manager: Optional[Any] = None,
    ) -> None:
        """Initialize supervisor with required subsystems."""
        self._store = state_store
        self._authority = trust_authority
        self._tools = tool_broker
        self._decision = fast_decision
        self._reasoning_provider = reasoning_provider
        self._planner = planner or (
            StructuredPlanner(reasoning_provider) if reasoning_provider else None
        )
        self._verifier = verifier or StepVerifier()
        self._synthesizer = synthesizer or (
            TaskSynthesizer(reasoning_provider) if reasoning_provider else None
        )
        self._memory_store = memory_store
        self._retriever = retriever
        self._lesson_manager = lesson_manager
        if context_assembler is not None:
            self._context_assembler = context_assembler
        elif memory_store is not None or retriever is not None or lesson_manager is not None:
            self._context_assembler = ContextAssembler(
                memory_store=memory_store,
                retriever=retriever,
                lesson_manager=lesson_manager,
            )
        else:
            self._context_assembler = ContextAssembler()
        self._skill_registry = skill_registry
        self._clarification_gate = clarification_gate
        self._admission_controller = admission_controller
        self._experience_service = experience_service
        self._notifications = notification_manager
        self._confirmation_gate = confirmation_gate or ConfirmationGate(
            context_assembler=self._context_assembler,
            planner=self._planner,
            tool_broker=self._tools,
            trust_authority=self._authority,
            clarification_gate=self._clarification_gate,
        )
        self._persistent_engine = persistent_engine or PersistentExecutionEngine(
            state_store=self._store,
            trust_authority=self._authority,
            tool_broker=self._tools,
            verifier=self._verifier,
            investigator=DiagnosticInvestigator(
                research_provider=research_provider or LocalDocResearchProvider(),
                lesson_manager=self._lesson_manager,
            ),
            research_provider=research_provider,
            notification_manager=notification_manager,
            experience_service=self._experience_service,
        )

    @property
    def context_assembler(self) -> ContextAssembler:
        """Return the active ContextAssembler."""
        return self._context_assembler

    @property
    def confirmation_gate(self) -> ConfirmationGate:
        """Return the active ConfirmationGate."""
        return self._confirmation_gate

    @property
    def persistent_engine(self) -> PersistentExecutionEngine:
        """Return the active PersistentExecutionEngine."""
        return self._persistent_engine

    @property
    def memory_store(self) -> Optional[Any]:
        """Return the active EKM memory store, if configured."""
        return self._memory_store

    @property
    def lesson_manager(self) -> Optional[Any]:
        """Return the active LessonManager, if configured."""
        return self._lesson_manager

    def assemble_context(
        self,
        goal: str,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        additional_constraints: Optional[List[str]] = None,
    ) -> Any:
        """Assemble bounded ContextIR for reasoning and planning."""
        return self._context_assembler.assemble(
            goal=goal,
            project_id=project_id,
            task_id=task_id,
            additional_constraints=additional_constraints,
        )

    # ==========================================================================
    # TASK CREATION & PLANNING
    # ==========================================================================

    def submit_goal(
        self,
        goal_text: str,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        priority: int = 100,
        metadata: Optional[Dict[str, Any]] = None,
        execution_mode: Optional[Any] = None,
    ) -> TaskRecord:
        """Submit a user goal into the system, creating a durable task in SUBMITTED state.

        Advisory intent classification is optionally queried via FastDecisionService,
        preserving the advisory-only invariant.
        """
        routing_intent: Optional[str] = None
        if self._decision is not None:
            try:
                decision_req = FastDecisionRequest(
                    user_input=goal_text,
                    context=RequestContext(project_scope=project_id),
                )
                eval_result = self._decision.evaluate(decision_req)
                if eval_result and eval_result.route:
                    routing_intent = (
                        eval_result.route.value
                        if hasattr(eval_result.route, "value")
                        else str(eval_result.route)
                    )
            except Exception as exc:
                logger.warning("Advisory Fast Decision evaluation skipped: %s", exc)

        task = self._store.create_task(
            title=goal_text,
            session_id=session_id,
            project_id=project_id,
            priority=priority,
            routing_intent=routing_intent,
            metadata=metadata,
            execution_mode=execution_mode,
        )
        logger.info(
            "Goal submitted: task_id=%r, routing_intent=%r, mode=%s",
            task.task_id,
            routing_intent,
            task.execution_mode.value,
        )
        return task

    def create_task(
        self,
        goal_text: str,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        priority: int = 100,
        metadata: Optional[Dict[str, Any]] = None,
        execution_mode: Optional[Any] = None,
    ) -> TaskRecord:
        """Alias for submit_goal to create a durable task."""
        return self.submit_goal(
            goal_text=goal_text,
            session_id=session_id,
            project_id=project_id,
            priority=priority,
            metadata=metadata,
            execution_mode=execution_mode,
        )

    # ==========================================================================
    # CONTEXT UNDERSTANDING & CONFIRMATION GATE
    # ==========================================================================

    def build_context_briefing(
        self,
        task_id: str,
        initial_requirements: Optional[Sequence[str]] = None,
        initial_constraints: Optional[Sequence[str]] = None,
        deliverables: Optional[Sequence[str]] = None,
        proposed_steps: Optional[Sequence[StepPlan]] = None,
        budgets: Optional[ExecutionBudget] = None,
    ) -> ContextBriefing:
        """Construct and persist a Context Understanding Briefing for a task."""
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        briefing = self._confirmation_gate.build_briefing(
            task=task,
            user_goal=task.title,
            project_id=task.project_id,
            initial_requirements=initial_requirements,
            initial_constraints=initial_constraints,
            deliverables=deliverables,
            proposed_steps=proposed_steps,
            budgets=budgets,
        )
        new_meta = dict(task.metadata)
        new_meta["context_briefing"] = briefing.model_dump()
        self._store.update_task_metadata(task_id, new_meta)
        return briefing

    def confirm_context_briefing(
        self,
        task_id: str,
        confirmation_note: Optional[str] = None,
    ) -> ContextBriefing:
        """Explicitly confirm a briefing, unlocking persistent execution."""
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        briefing_data = task.metadata.get("context_briefing")
        if not briefing_data:
            raise TaskStateError(f"Task '{task_id}' has no Context Briefing to confirm.")

        briefing = ContextBriefing.model_validate(briefing_data)
        confirmed_briefing = self._confirmation_gate.confirm(briefing, confirmation_note)

        new_meta = dict(task.metadata)
        new_meta["context_briefing"] = confirmed_briefing.model_dump()
        self._store.update_task_metadata(task_id, new_meta)

        # If task was paused in AWAITING_APPROVAL, transition to EXECUTING if steps are ready
        if task.state == TaskState.AWAITING_APPROVAL:
            steps = self._store.list_task_steps(task_id)
            if steps:
                self._store.transition_task_state(task_id, TaskState.EXECUTING)
        return confirmed_briefing

    def correct_context_briefing(
        self,
        task_id: str,
        correction_text: str,
        updated_steps: Optional[Sequence[StepPlan]] = None,
        updated_deliverables: Optional[Sequence[str]] = None,
    ) -> ContextBriefing:
        """Apply user correction to briefing, resetting to AWAITING_CONFIRMATION."""
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        briefing_data = task.metadata.get("context_briefing")
        if not briefing_data:
            raise TaskStateError(f"Task '{task_id}' has no Context Briefing to correct.")

        briefing = ContextBriefing.model_validate(briefing_data)
        corrected_briefing = self._confirmation_gate.correct(
            briefing,
            correction_text,
            updated_steps=updated_steps,
            updated_deliverables=updated_deliverables,
        )

        new_meta = dict(task.metadata)
        new_meta["context_briefing"] = corrected_briefing.model_dump()
        self._store.update_task_metadata(task_id, new_meta)
        return corrected_briefing

    def add_context_to_briefing(
        self,
        task_id: str,
        new_context: Sequence[str],
        provenance: ProvenanceType = ProvenanceType.USER_PROVIDED,
    ) -> ContextBriefing:
        """Add context items to briefing and update task metadata."""
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        briefing_data = task.metadata.get("context_briefing")
        if not briefing_data:
            raise TaskStateError(f"Task '{task_id}' has no Context Briefing to supplement.")

        briefing = ContextBriefing.model_validate(briefing_data)
        updated = self._confirmation_gate.add_context(briefing, new_context, provenance)

        new_meta = dict(task.metadata)
        new_meta["context_briefing"] = updated.model_dump()
        self._store.update_task_metadata(task_id, new_meta)
        return updated

    def cancel_context_briefing(
        self,
        task_id: str,
        reason: str,
    ) -> ContextBriefing:
        """Cancel a briefing and transition the task to CANCELLED."""
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        briefing_data = task.metadata.get("context_briefing")
        if not briefing_data:
            raise TaskStateError(f"Task '{task_id}' has no Context Briefing to cancel.")

        briefing = ContextBriefing.model_validate(briefing_data)
        cancelled = self._confirmation_gate.cancel(briefing, reason)

        new_meta = dict(task.metadata)
        new_meta["context_briefing"] = cancelled.model_dump()
        self._store.update_task_metadata(task_id, new_meta)
        if not task.state.is_terminal:
            self._store.transition_task_state(task_id, TaskState.CANCELLED, error_summary=reason)
        return cancelled

    def get_context_briefing(self, task_id: str) -> Optional[ContextBriefing]:
        """Fetch the current Context Briefing for a task, if any."""
        task = self._store.get_task(task_id)
        if not task:
            return None
        briefing_data = task.metadata.get("context_briefing")
        if not briefing_data:
            return None
        return ContextBriefing.model_validate(briefing_data)

    def plan_task(
        self,
        task_id: str,
        steps: Sequence[StepPlan],
    ) -> TaskRecord:
        """Formulate an ordered execution plan for a task and persist all steps.

        Transitions:
            SUBMITTED -> PLANNING -> EXECUTING (or AWAITING_APPROVAL if empty).

        Raises:
            TaskNotFoundError: If task_id is invalid.
            TaskStateError: If task is not in SUBMITTED state.
        """
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        if task.state != TaskState.SUBMITTED:
            raise TaskStateError(
                f"Cannot plan task '{task_id}' in state '{task.state.value}'. Expected 'SUBMITTED'."
            )

        # Transition to PLANNING
        self._store.transition_task_state(task_id, TaskState.PLANNING)

        # Register each planned step
        for idx, step_plan in enumerate(steps):
            self._store.create_step(
                task_id=task_id,
                step_index=idx,
                action_name=step_plan.action_name,
                tool_name=step_plan.tool_name,
                is_idempotent=step_plan.is_idempotent,
                idempotency_key=step_plan.idempotency_key,
                arguments=step_plan.arguments,
            )

        # Checkpoint initial plan
        self._store.create_checkpoint(
            task_id=task_id,
            checkpoint_seq=1,
            snapshot={"event": "PLAN_FORMULATED", "step_count": len(steps)},
        )

        # Transition PLANNING -> EXECUTING
        updated_task = self._store.transition_task_state(task_id, TaskState.EXECUTING)
        logger.info("Task planned: task_id=%r, steps=%d", task_id, len(steps))
        return updated_task

    # ==========================================================================
    # EXECUTION LOOP
    # ==========================================================================

    def execute_task(
        self,
        task_id: str,
        actor: str = "supervisor",
    ) -> TaskRecord:
        """Run the execution loop for an active task.

        Iterates through registered steps, dispatching tool actions through
        ToolBroker and persisting checkpoints before and after each step.

        Raises:
            TaskNotFoundError: If task does not exist.
            TaskStateError: If task is not in EXECUTING state.
        """
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        if task.execution_mode == ExecutionMode.PERSISTENT_COMPLETION:
            return self._persistent_engine.execute_persistent(task_id, actor=actor)

        if task.state != TaskState.EXECUTING:
            raise TaskStateError(
                f"Cannot execute task '{task_id}' in state '{task.state.value}'. "
                "Expected 'EXECUTING'."
            )

        steps = self._store.list_task_steps(task_id)
        if not steps:
            # No steps to execute; complete task
            return self._store.transition_task_state(task_id, TaskState.COMPLETED)

        for step in steps:
            if step.state in (StepState.COMPLETED, StepState.SKIPPED):
                continue

            if step.state == StepState.AWAITING_APPROVAL:
                # Step requires approval; halt loop
                return self._store.transition_task_state(task_id, TaskState.AWAITING_APPROVAL)

            if step.state != StepState.PENDING:
                # Step in unexpected state; fail closed
                return self._store.transition_task_state(
                    task_id,
                    TaskState.FAILED,
                    error_summary=(
                        f"Step {step.step_index} in invalid non-pending state '{step.state.value}'."
                    ),
                )

            # Step is PENDING: execute it
            step_result = self._execute_step(task, step, actor=actor)
            if step_result.state == StepState.FAILED:
                return self._store.transition_task_state(
                    task_id,
                    TaskState.FAILED,
                    error_summary=step_result.result_summary or "Step execution failed.",
                )
            if step_result.state == StepState.AWAITING_APPROVAL:
                return self._store.transition_task_state(
                    task_id,
                    TaskState.AWAITING_APPROVAL,
                    error_summary="Step requires authorization approval.",
                )

        # All steps successfully completed
        completed_task = self._store.transition_task_state(task_id, TaskState.COMPLETED)
        logger.info("Task completed: task_id=%r", task_id)
        return completed_task

    def _execute_step(
        self,
        task: TaskRecord,
        step: StepRecord,
        actor: str,
    ) -> StepRecord:
        """Execute a single step within a task."""
        # 1. Pre-execution checkpoint
        existing_cps = self._store.list_checkpoints(task.task_id)
        next_seq = len(existing_cps) + 1
        self._store.create_checkpoint(
            task_id=task.task_id,
            step_id=step.step_id,
            checkpoint_seq=next_seq,
            snapshot={
                "event": "DISPATCHING_STEP",
                "step_index": step.step_index,
                "action": step.action_name,
                "tool": step.tool_name,
            },
        )

        # 2. Mark step RUNNING
        running_step = self._store.transition_step_state(step.step_id, StepState.RUNNING)

        # 3. Dispatch execution
        if running_step.tool_name:
            # Tool action must go through ToolBroker (which verifies capability via TrustAuthority)
            tool_result = self._tools.invoke(
                tool_name=running_step.tool_name,
                args=running_step.arguments,
                actor=actor,
                project_id=task.project_id,
            )

            if tool_result.status == ToolResultStatus.SUCCESS:
                completed_step = self._store.transition_step_state(
                    step.step_id,
                    StepState.COMPLETED,
                    result_summary=tool_result.output,
                )
                self._store.create_checkpoint(
                    task_id=task.task_id,
                    step_id=step.step_id,
                    checkpoint_seq=next_seq + 1,
                    snapshot={
                        "step_completed": True,
                        "step_index": step.step_index,
                        "output": tool_result.output,
                    },
                )
                return completed_step

            if tool_result.status == ToolResultStatus.DENIED:
                # Authorization denied by Trust Core policy
                logger.warning(
                    "Tool %r denied by policy for step %d: %s",
                    running_step.tool_name,
                    step.step_index,
                    tool_result.error_summary,
                )
                # Escalate to AWAITING_APPROVAL if approval possible, or fail
                escalated_step = self._store.transition_step_state(
                    step.step_id,
                    StepState.AWAITING_APPROVAL,
                    result_summary=f"Policy denied: {tool_result.error_summary}",
                )
                return escalated_step

            # Status is ERROR or other failure
            failed_step = self._store.transition_step_state(
                step.step_id,
                StepState.FAILED,
                result_summary=f"{tool_result.error_type}: {tool_result.error_summary}",
            )
            return failed_step

        # Internal/reasoning step without a tool
        completed_step = self._store.transition_step_state(
            step.step_id,
            StepState.COMPLETED,
            result_summary="Internal execution step completed.",
        )
        self._store.create_checkpoint(
            task_id=task.task_id,
            step_id=step.step_id,
            checkpoint_seq=next_seq + 1,
            snapshot={"step_completed": True, "step_index": step.step_index},
        )
        return completed_step

    # ==========================================================================
    # HUMAN-IN-THE-LOOP & RESUME
    # ==========================================================================

    def approve_step(
        self,
        task_id: str,
        step_id: str,
        actor: str = "human_user",
    ) -> TaskRecord:
        """Approve an escalated step and transition task back to EXECUTING.

        Preconditions:
            Task and step must both be in AWAITING_APPROVAL.
        """
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        step = self._store.get_step(step_id)
        if not step:
            raise StepNotFoundError(f"Step not found: {step_id}")

        if step.state != StepState.AWAITING_APPROVAL:
            raise TaskStateError(
                f"Cannot approve step '{step_id}' in state '{step.state.value}'. "
                "Expected 'AWAITING_APPROVAL'."
            )

        # Transition step to PENDING so resume_task/execute_task can dispatch it
        self._store.transition_step_state(
            step_id,
            StepState.PENDING,
            result_summary=f"User approved by {actor}.",
        )
        updated_task = self._store.transition_task_state(task_id, TaskState.EXECUTING)
        logger.info("Step %r approved by %r for task %r", step_id, actor, task_id)
        return updated_task

    def resume_task(
        self,
        task_id: str,
        actor: str = "supervisor",
    ) -> TaskRecord:
        """Resume execution of an interrupted or approved task."""
        task = self._store.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        if task.state == TaskState.AWAITING_APPROVAL:
            # Check if all steps in AWAITING_APPROVAL have been resolved
            steps = self._store.list_task_steps(task_id)
            if not any(s.state == StepState.AWAITING_APPROVAL for s in steps):
                task = self._store.transition_task_state(task_id, TaskState.EXECUTING)

        if task.state != TaskState.EXECUTING:
            raise TaskStateError(
                f"Cannot resume task '{task_id}' in state '{task.state.value}'. "
                "Expected 'EXECUTING'."
            )

        return self.execute_task(task_id, actor=actor)

    def cancel_task(
        self,
        task_id: str,
        reason: str = "Cancelled by user or supervisor.",
    ) -> TaskRecord:
        """Cancel a non-terminal task."""
        return self._store.transition_task_state(
            task_id,
            TaskState.CANCELLED,
            error_summary=reason,
        )

    # ==========================================================================
    # HIGH-LEVEL AGENT LOOP
    # ==========================================================================

    def execute_goal(
        self,
        goal_text: str,
        actor: str = "user",
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        additional_constraints: Optional[Sequence[str]] = None,
    ) -> "GoalExecutionResult":
        """Execute a natural-language goal end-to-end through the agent loop.

        USER GOAL → CONTEXT (ContextIR) → QWEN LOCAL REASONING → VALIDATED PLAN
            → SUPERVISOR → TRUST CORE → TOOL BROKER → OBSERVATION
            → VERIFICATION → RETRY/REPLAN → SYNTHESIS → USER RESULT

        Args:
            goal_text:    Natural language user goal.
            actor:        Responsible identity for capability issuance.
            project_id:   Optional project scope.
            session_id:   Optional session ID.
            additional_constraints: Optional extra safety constraints injected into ContextIR.

        Returns:
            GoalExecutionResult with task record, synthesis, and observations.

        Raises:
            RuntimeError: If no planner is configured.
        """
        # 0. CLARIFICATION GATE (Deterministic Preflight)
        if self._clarification_gate is not None:
            from src.skills.clarification import ClarificationAction

            clar = self._clarification_gate.evaluate(
                provided_args={"goal": goal_text},
                required_keys=["goal"],
            )
            if clar.action == ClarificationAction.CLARIFY:
                return GoalExecutionResult(
                    goal_text=goal_text,
                    success=False,
                    synthesis=f"Clarification required: {clar.clarification_prompt}",
                    task_record=None,
                    observations=[],
                )

        if self._planner is None:
            raise RuntimeError(
                "No reasoning provider/planner configured. "
                "Provide a LocalReasoningProvider when constructing Supervisor."
            )

        constraints = list(additional_constraints) if additional_constraints else []

        # 1. CONTEXT ASSEMBLY
        context_ir = self._context_assembler.assemble(
            goal=goal_text,
            project_id=project_id,
            additional_constraints=constraints if constraints else None,
        )

        # 2. PLANNING (with one internal repair attempt)
        try:
            plan = self._planner.generate_plan(context_ir, self._tools)
        except PlanValidationError as exc:
            logger.error("Planning failed for goal %r: %s", goal_text, exc)
            return GoalExecutionResult(
                goal_text=goal_text,
                success=False,
                synthesis=f"Planning failed: {exc}",
                task_record=None,
                observations=[],
            )

        # 3. SUBMIT + PERSIST TASK
        task = self.submit_goal(
            goal_text=goal_text,
            session_id=session_id,
            project_id=project_id,
        )

        # Convert Plan steps → StepPlan records and persist
        step_plans = [
            StepPlan(
                action_name=step.tool,
                tool_name=step.tool,
                is_idempotent=step.is_idempotent,
                arguments=dict(step.arguments),
            )
            for step in plan.steps
        ]
        task = self.plan_task(task.task_id, step_plans)

        # 4. EXECUTION LOOP with observation + verification + bounded retry
        all_observations: list[StepObservation] = []
        all_verifications = []
        steps = self._store.list_task_steps(task.task_id)

        for step_record, planned_step in zip(steps, plan.steps, strict=False):
            retry_count = 0
            while True:
                self._store.transition_step_state(step_record.step_id, StepState.RUNNING)
                # Invoke through ToolBroker (Trust Core validates inside)
                tool_result = self._tools.invoke(
                    tool_name=planned_step.tool,
                    args=dict(planned_step.arguments),
                    actor=actor,
                    project_id=project_id,
                )

                obs = StepObservation.from_tool_result(
                    step_number=planned_step.step_number,
                    tool_name=planned_step.tool,
                    result=tool_result,
                )
                all_observations.append(obs)

                # 5. VERIFICATION
                ver = self._verifier.verify(planned_step, obs)
                all_verifications.append(ver)

                if ver.outcome == VerificationOutcome.VERIFIED_SUCCESS:
                    self._store.transition_step_state(
                        step_record.step_id,
                        StepState.COMPLETED,
                        result_summary=obs.output_summary,
                    )
                    break  # Advance to next step

                # Retry only idempotent steps, bounded
                if (
                    ver.can_retry
                    and planned_step.is_idempotent
                    and retry_count < self.MAX_STEP_RETRIES
                ):
                    retry_count += 1
                    logger.warning(
                        "Retrying idempotent step %d (attempt %d): %s",
                        planned_step.step_number,
                        retry_count,
                        ver.rationale,
                    )
                    continue

                # Non-retryable or max retries exceeded
                if ver.requires_approval:
                    self._store.transition_step_state(
                        step_record.step_id,
                        StepState.AWAITING_APPROVAL,
                        result_summary=ver.rationale,
                    )
                    final_task = self._store.transition_task_state(
                        task.task_id, TaskState.AWAITING_APPROVAL
                    )
                    synthesis = (
                        f"Task paused: '{planned_step.description}' requires human approval. "
                        f"{ver.rationale}"
                    )
                    return GoalExecutionResult(
                        goal_text=goal_text,
                        success=False,
                        synthesis=synthesis,
                        task_record=final_task,
                        observations=all_observations,
                    )

                # Fail closed
                self._store.transition_step_state(
                    step_record.step_id,
                    StepState.FAILED,
                    result_summary=ver.rationale,
                )
                final_task = self._store.transition_task_state(
                    task.task_id,
                    TaskState.FAILED,
                    error_summary=ver.rationale,
                )
                synthesis = f"Execution halted: {ver.rationale}"
                return GoalExecutionResult(
                    goal_text=goal_text,
                    success=False,
                    synthesis=synthesis,
                    task_record=final_task,
                    observations=all_observations,
                )

        # 6. SYNTHESIS
        completed_task = self._store.transition_task_state(task.task_id, TaskState.COMPLETED)
        if self._synthesizer is not None:
            synthesis_text = self._synthesizer.synthesize(
                goal=goal_text,
                plan=plan,
                observations=all_observations,
                verifications=all_verifications,
            )
        else:
            synthesis_text = (
                f"Goal '{goal_text}' completed successfully across {len(all_observations)} step(s)."
            )

        return GoalExecutionResult(
            goal_text=goal_text,
            success=True,
            synthesis=synthesis_text,
            task_record=completed_task,
            observations=all_observations,
        )

    # ==========================================================================
    # SKILLS INTEGRATION (PHASE 6)
    # ==========================================================================

    def select_skill(
        self,
        goal_text: str,
        intent_tags: Optional[Sequence[str]] = None,
    ) -> Optional[Any]:
        """Supervisor chooses the Skill deterministically; the skill does not choose itself."""
        if self._skill_registry is None:
            return None
        from src.skills.models import SkillLifecycleState

        candidates = self._skill_registry.search_skills(intent=goal_text, tags=intent_tags)
        if not candidates:
            return None
        selected = candidates[0]
        if selected.current_state == SkillLifecycleState.AVAILABLE:
            return self._skill_registry.transition_lifecycle(
                selected.manifest.skill_id, SkillLifecycleState.SELECTED
            )
        return selected

    def execute_skill(
        self,
        invocation: Any,
        actor: str = "supervisor",
        project_id: Optional[str] = None,
    ) -> Any:
        """Execute a skill following the Supervisor -> Skill Protocol.

        Flow:
        Supervisor
         ↓
        Skill Registry (retrieve skill)
         ↓
        Deterministic Resource Admission
         ↓
        Skill Admission (transition ADMITTED)
         ↓
        Execution via BoundSkillToolBroker
         ↓
        Verification (transition VERIFYING -> SUCCEEDED / FAILED)
         ↓
        Result + Experience Event
        """
        if self._skill_registry is None:
            from src.skills.errors import SkillNotFoundError

            raise SkillNotFoundError("No SkillRegistry configured in Supervisor.")

        from src.skills.errors import SkillExecutionError
        from src.skills.executor import BoundSkillToolBroker
        from src.skills.experience import ExperienceEvent, ExperienceOutcomeType
        from src.skills.models import (
            SkillExecutionStatus,
            SkillLifecycleState,
            SkillResult,
            VerificationStatus,
        )

        registered_skill = self._skill_registry.get_skill(invocation.skill_id)
        manifest = registered_skill.manifest

        if registered_skill.current_state == SkillLifecycleState.FAILED:
            self._skill_registry.transition_lifecycle(
                manifest.skill_id, SkillLifecycleState.AVAILABLE
            )
            registered_skill = self._skill_registry.get_skill(invocation.skill_id)

        if registered_skill.current_state == SkillLifecycleState.AVAILABLE:
            self._skill_registry.transition_lifecycle(
                manifest.skill_id, SkillLifecycleState.SELECTED
            )

        admission = None
        if self._admission_controller is not None:
            admission = self._admission_controller.evaluate_admission(manifest)
            if not admission.admitted:
                self._skill_registry.transition_lifecycle(
                    manifest.skill_id, SkillLifecycleState.FAILED
                )
                return SkillResult(
                    invocation_id=invocation.invocation_id,
                    skill_id=invocation.skill_id,
                    status=SkillExecutionStatus.FAILED,
                    error=(
                        f"Resource admission rejected: {admission.reason} "
                        f"(action={admission.action})"
                    ),
                    verification_status=VerificationStatus.FAILED,
                )

        try:
            self._skill_registry.transition_lifecycle(
                manifest.skill_id, SkillLifecycleState.ADMITTED
            )
            self._skill_registry.transition_lifecycle(
                manifest.skill_id, SkillLifecycleState.EXECUTING
            )

            executor = self._skill_registry.get_executor(manifest.skill_id)
            if executor is None:
                raise SkillExecutionError(
                    f"No executor registered for skill '{manifest.skill_id}'."
                )

            bound_broker = BoundSkillToolBroker(
                broker=self._tools,
                manifest=manifest,
                actor=actor,
                project_id=project_id,
            )

            result = executor.execute(invocation, bound_broker)

            self._skill_registry.transition_lifecycle(
                manifest.skill_id, SkillLifecycleState.VERIFYING
            )

            if result.status == SkillExecutionStatus.SUCCESS:
                self._skill_registry.transition_lifecycle(
                    manifest.skill_id, SkillLifecycleState.SUCCEEDED
                )
                self._skill_registry.transition_lifecycle(
                    manifest.skill_id, SkillLifecycleState.AVAILABLE
                )
                outcome = ExperienceOutcomeType.VERIFIED_SUCCESS
            else:
                self._skill_registry.transition_lifecycle(
                    manifest.skill_id, SkillLifecycleState.FAILED
                )
                outcome = ExperienceOutcomeType.EXECUTION_FAILURE

            if self._experience_service is not None:
                exp_event = ExperienceEvent(
                    task_id=invocation.task_id or invocation.invocation_id,
                    invocation_id=invocation.invocation_id,
                    skill_id=invocation.skill_id,
                    intent=invocation.input.get("goal", manifest.description),
                    selected_tools=manifest.tool_bindings,
                    execution_path=(manifest.skill_id,),
                    verification_result=result.verification_status.value,
                    outcome_type=outcome,
                    latency_ms=result.metrics.get("duration_ms", 0.0),
                    error_or_recovery_info=result.error,
                )
                self._experience_service.record_event(exp_event)
                result = result.model_copy(update={"experience_event": exp_event.model_dump()})

            return result

        finally:
            if admission and admission.lease and self._admission_controller is not None:
                self._admission_controller.release_lease(admission.lease)


class GoalExecutionResult:
    """Result of a complete execute_goal() agent loop."""

    __slots__ = ("goal_text", "success", "synthesis", "task_record", "observations")

    def __init__(
        self,
        goal_text: str,
        success: bool,
        synthesis: str,
        task_record: Optional[TaskRecord],
        observations: list,
    ) -> None:
        self.goal_text = goal_text
        self.success = success
        self.synthesis = synthesis
        self.task_record = task_record
        self.observations = observations

    def __repr__(self) -> str:
        return (
            f"GoalExecutionResult(success={self.success!r}, "
            f"synthesis={self.synthesis!r}, "
            f"steps={len(self.observations)})"
        )
