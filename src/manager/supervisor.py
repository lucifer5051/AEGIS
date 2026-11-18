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
import time
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from src.decision.models import FastDecisionRequest, RequestContext
from src.decision.routing import (
    ExecutionRouter,
    ExecutionStrategy,
    RoutingDecision,
)
from src.decision.service import FastDecisionService
from src.manager.briefing import (
    ConfirmationGate,
    ContextBriefing,
    ExecutionBudget,
    ProvenanceType,
)
from src.manager.context import ContextAssembler, ContextIR
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
from src.manager.observation import ObservationStatus, StepObservation
from src.manager.persistent_engine import PersistentExecutionEngine
from src.manager.planner import PlanValidationError, StructuredPlanner
from src.manager.research import (
    DiagnosticInvestigator,
    LocalDocResearchProvider,
    ResearchProvider,
)
from src.manager.state_store import StateStore
from src.manager.synthesis import (
    SynthesisDegradationPolicy,
    SynthesisError,
    SynthesisResult,
    TaskSynthesizer,
)
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
    # Bounded observation-driven replan limits
    MAX_REPLAN_ATTEMPTS = 3
    MAX_TOTAL_EXECUTED_STEPS = 20

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
        unified_memory: Optional[Any] = None,
        config_manager: Optional[Any] = None,
        confirmation_gate: Optional[ConfirmationGate] = None,
        persistent_engine: Optional[PersistentExecutionEngine] = None,
        research_provider: Optional[ResearchProvider] = None,
        notification_manager: Optional[Any] = None,
        reasoning_providers: Optional[Dict[str, LocalReasoningProvider]] = None,
        model_gateway: Optional[Any] = None,
        strict_synthesis: bool = False,
        synthesis_policy: Optional[SynthesisDegradationPolicy] = None,
    ) -> None:
        """Initialize supervisor with required subsystems."""
        self._store = state_store
        self._authority = trust_authority
        self._tools = tool_broker
        self._decision = fast_decision
        self._model_gateway = model_gateway
        self._reasoning_provider = reasoning_provider
        self._reasoning_providers = dict(reasoning_providers) if reasoning_providers else {}
        if self._reasoning_provider is None and self._model_gateway is not None:
            try:
                from src.models.provider_adapter import GatewayReasoningProvider
                self._reasoning_provider = GatewayReasoningProvider(self._model_gateway)
            except Exception:
                pass
        self._planner = planner or (
            StructuredPlanner(self._reasoning_provider) if self._reasoning_provider else None
        )
        self._verifier = verifier or StepVerifier()
        self._strict_synthesis = strict_synthesis
        self._synthesizer = synthesizer or (
            TaskSynthesizer(self._reasoning_provider) if self._reasoning_provider else None
        )
        if self._synthesizer is not None:
            if synthesis_policy is not None:
                self._synthesizer.policy = synthesis_policy
            elif strict_synthesis:
                self._synthesizer.policy = SynthesisDegradationPolicy.STRICT
        self._memory_store = memory_store
        self._retriever = retriever
        self._lesson_manager = lesson_manager
        self._unified_memory = unified_memory
        self._config_manager = config_manager

        # Auto-wire memory & experience subsystems if conn available and not explicitly provided
        conn = getattr(state_store, "_conn", None) if state_store else None
        if self._config_manager is None:
            try:
                from src.config.manager import SageConfigurationManager

                self._config_manager = SageConfigurationManager(
                    conn=conn,
                    trustcore_policy=getattr(self._authority, "_policy", None),
                )
            except Exception:
                self._config_manager = None

        if self._unified_memory is None and conn is not None:
            try:
                from src.memory.engine import UnifiedMemoryEngine

                self._unified_memory = UnifiedMemoryEngine(conn)
            except Exception:
                self._unified_memory = None

        if self._memory_store is None and conn is not None:
            try:
                from src.memory.store import EKMMemoryStore

                self._memory_store = EKMMemoryStore(conn)
            except Exception:
                pass

        if self._lesson_manager is None and conn is not None:
            try:
                from src.memory.lessons import LessonManager

                self._lesson_manager = LessonManager(conn, memory_store=self._memory_store)
            except Exception:
                pass

        if experience_service is not None:
            self._experience_service = experience_service
        elif conn is not None:
            try:
                from src.skills.experience import ExperienceFeedbackService

                self._experience_service = ExperienceFeedbackService(
                    conn=conn,
                    memory_store=self._memory_store,
                    unified_memory=self._unified_memory,
                )
            except Exception:
                self._experience_service = None
        else:
            self._experience_service = None

        if self._retriever is None and self._memory_store is not None:
            try:
                from src.memory.retriever import EKMRetriever

                self._retriever = EKMRetriever(
                    self._memory_store, lesson_manager=self._lesson_manager
                )
            except Exception:
                pass

        if context_assembler is not None:
            self._context_assembler = context_assembler
        else:
            self._context_assembler = ContextAssembler(
                conn=conn,
                memory_store=self._memory_store,
                retriever=self._retriever,
                lesson_manager=self._lesson_manager,
                unified_memory=self._unified_memory,
            )
        self._skill_registry = skill_registry
        self._router = ExecutionRouter(
            skill_registry=self._skill_registry,
            reasoning_providers=self._reasoning_providers,
            default_provider_id=(
                getattr(reasoning_provider, "provider_id", "default")
                if reasoning_provider
                else None
            ),
        )
        self._clarification_gate = clarification_gate
        self._admission_controller = admission_controller
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
    def router(self) -> ExecutionRouter:
        """Return the active ExecutionRouter."""
        return self._router


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

    @property
    def experience_service(self) -> Optional[Any]:
        """Return the active ExperienceFeedbackService, if configured."""
        return self._experience_service

    @property
    def retriever(self) -> Optional[Any]:
        """Return the active EKMRetriever, if configured."""
        return self._retriever

    @property
    def synthesizer(self) -> Optional[TaskSynthesizer]:
        """Return the active TaskSynthesizer, if configured."""
        return self._synthesizer

    def _record_goal_experience(
        self,
        task: Optional[TaskRecord],
        goal_text: str,
        success: bool,
        completed_steps: Sequence[Dict[str, Any]],
        error_summary: Optional[str] = None,
        project_id: Optional[str] = None,
        latency_ms: float = 0.0,
        skill_id: Optional[str] = None,
        synthesis_degraded: bool = False,
        synthesis_error: Optional[str] = None,
    ) -> Optional[Any]:
        """Record an ExperienceEvent upon task completion or terminal failure."""
        from src.skills.experience import ExperienceEvent, ExperienceOutcomeType

        task_id = task.task_id if task else str(uuid4())
        tools_used = tuple(str(s.get("tool", "")) for s in completed_steps if s.get("tool"))
        path = tuple(
            f"{s.get('tool', '')}:{s.get('description', '')}"
            for s in completed_steps
            if s.get("tool")
        )

        outcome = (
            ExperienceOutcomeType.VERIFIED_SUCCESS
            if success
            else ExperienceOutcomeType.EXECUTION_FAILURE
        )

        err_info = error_summary if not success else None
        if synthesis_degraded:
            degraded_tag = f"[DEGRADED_SYNTHESIS] {synthesis_error or 'Deterministic fallback substituted'}"
            err_info = f"{err_info}; {degraded_tag}" if err_info else degraded_tag

        if self._experience_service is None:
            if self._unified_memory is not None:
                try:
                    self._unified_memory.record_task_outcome(
                        task_id=task_id,
                        intent=goal_text,
                        success=success,
                        details=f"Tools: {list(tools_used)}; Outcome: {outcome.value}; Latency: {latency_ms:.1f}ms",
                        error=err_info if not success else None,
                        project_id=project_id,
                        actor="supervisor",
                    )
                except Exception as exc:
                    logger.debug("Direct unified memory recording failed: %s", exc)
            return None

        conf = 1.0 if success else 0.5
        if synthesis_degraded and success:
            conf = 0.8  # Reflect slight degradation in user-facing synthesis fidelity

        event = ExperienceEvent(
            task_id=task_id,
            invocation_id=str(uuid4()),
            skill_id=skill_id or "supervisor.goal_execution",
            intent=goal_text,
            selected_tools=tools_used,
            execution_path=path,
            verification_result="VERIFIED" if success else "FAILED",
            outcome_type=outcome,
            latency_ms=max(0.0, latency_ms),
            error_or_recovery_info=err_info,
            project_id=project_id,
            provenance="sage.manager.supervisor",
            confidence=conf,
        )
        try:
            self._experience_service.record_event(event)
            return event
        except Exception as exc:
            logger.warning("Recording goal experience failed non-blockingly: %s", exc)
            return None

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
        routing_intent: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
    ) -> TaskRecord:
        """Submit a user goal into the system, creating a durable task in SUBMITTED state.

        Advisory intent classification is optionally queried via FastDecisionService,
        preserving the advisory-only invariant.
        """
        if routing_intent is None and self._decision is not None:
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
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
        )
        logger.info(
            "Goal submitted: task_id=%r, routing_intent=%r, mode=%s, parent_task_id=%r",
            task.task_id,
            routing_intent,
            task.execution_mode.value,
            task.parent_task_id,
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
        parent_task_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
    ) -> TaskRecord:
        """Alias for submit_goal to create a durable task."""
        return self.submit_goal(
            goal_text=goal_text,
            session_id=session_id,
            project_id=project_id,
            priority=priority,
            metadata=metadata,
            execution_mode=execution_mode,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
        )

    def create_subtask(
        self,
        parent_task_id: str,
        title: str,
        priority: int = 100,
        routing_intent: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        execution_mode: Optional[Any] = None,
    ) -> TaskRecord:
        """Create a child subtask explicitly linked to a parent task and its root lineage."""
        return self._store.create_task(
            title=title,
            parent_task_id=parent_task_id,
            priority=priority,
            routing_intent=routing_intent,
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
        max_replan_attempts: Optional[int] = None,
        max_total_steps: Optional[int] = None,
        mode: Optional[str] = None,
        task_overrides: Optional[Dict[str, Any]] = None,
    ) -> "GoalExecutionResult":
        """Execute a natural-language goal end-to-end through the bounded agent loop.

        USER GOAL → CONTEXT (ContextIR) → LOCAL REASONING → VALIDATED PLAN
            → SUPERVISOR → TRUST CORE → TOOL BROKER → OBSERVATION
            → VERIFICATION → BOUNDED REPLAN / RETRY → SYNTHESIS → USER RESULT

        Args:
            goal_text:    Natural language user goal.
            actor:        Responsible identity for capability issuance.
            project_id:   Optional project scope.
            session_id:   Optional session ID.
            additional_constraints: Optional extra safety constraints injected into ContextIR.
            max_replan_attempts: Maximum allowed observation-driven replans (defaults to MAX_REPLAN_ATTEMPTS).
            max_total_steps: Maximum total tool execution steps allowed across all attempts.

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
                    routing_decision=None,
                )

        t_start = time.perf_counter()

        # Resolve effective configuration & apply profile bounds
        effective_cfg = None
        if self._config_manager is not None:
            try:
                effective_cfg = self._config_manager.resolve_config(
                    task_overrides=task_overrides,
                    active_mode=mode,
                )
                if max_replan_attempts is None and effective_cfg:
                    max_replan_attempts = effective_cfg.execution_profile.max_replan_attempts
                if max_total_steps is None and effective_cfg:
                    max_total_steps = effective_cfg.execution_profile.max_total_steps
            except Exception as exc:
                logger.debug("Supervisor config resolution fallback: %s", exc)

        # Preflight lesson retrieval for advisory routing awareness
        preflight_lessons = []
        if hasattr(self._context_assembler, "retrieve_relevant_lessons"):
            try:
                preflight_lessons = self._context_assembler.retrieve_relevant_lessons(
                    goal=goal_text,
                    project_id=project_id,
                    limit=5,
                )
            except Exception as exc:
                logger.debug("Preflight lesson retrieval error: %s", exc)

        lesson_triggers = (
            [les.trigger_condition for les in preflight_lessons]
            if preflight_lessons
            else None
        )

        # 1. FAST DECISION & RUNTIME ROUTING RESOLUTION
        eval_result: Optional[FastDecisionResult] = None
        if self._decision is not None:
            try:
                decision_req = FastDecisionRequest(
                    user_input=goal_text,
                    context=RequestContext(
                        project_scope=project_id,
                        active_lessons=lesson_triggers,
                    ),
                )
                eval_result = self._decision.evaluate(decision_req)
            except Exception as exc:
                logger.warning("FastDecision evaluation failed; falling back: %s", exc)
                eval_result = None

        routing_decision, selected_provider, blocking_error = self._router.route(
            goal_text=goal_text,
            decision=eval_result,
            default_provider=self._reasoning_provider,
            lessons=preflight_lessons,
        )

        if blocking_error is not None:
            logger.error("Execution blocked by routing layer: %s", blocking_error)
            # Preserve original RuntimeError contract: when absolutely no provider/planner
            # is configured (not a temporary unavailability), raise RuntimeError so callers
            # get an actionable configuration error rather than a silent failure result.
            _no_config = (
                self._reasoning_provider is None
                and not self._router._providers
                and self._planner is None
                and "No reasoning providers configured" in blocking_error
            )
            if _no_config:
                raise RuntimeError(
                    "No reasoning provider/planner configured. "
                    "Provide a LocalReasoningProvider when constructing Supervisor."
                )
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            self._record_goal_experience(
                task=None,
                goal_text=goal_text,
                success=False,
                completed_steps=[],
                error_summary=blocking_error,
                project_id=project_id,
                latency_ms=elapsed_ms,
            )
            return GoalExecutionResult(
                goal_text=goal_text,
                success=False,
                synthesis=f"Execution blocked: {blocking_error}",
                task_record=None,
                observations=[],
                routing_decision=routing_decision,
            )

        strategy_str = (
            routing_decision.strategy.value
            if hasattr(routing_decision.strategy, "value")
            else str(routing_decision.strategy)
        )
        route_str = (
            routing_decision.route.value
            if hasattr(routing_decision.route, "value")
            else str(routing_decision.route)
        )

        # 2. STRATEGY DISPATCH: DIRECT_SKILL (Fast-path deterministic execution)
        if (
            strategy_str == ExecutionStrategy.DIRECT_SKILL.value
            and routing_decision.selected_skill_id is not None
        ):
            logger.info(
                "Executing goal via direct skill '%s' based on fast decision '%s' (conf=%.2f)",
                routing_decision.selected_skill_id,
                route_str,
                routing_decision.confidence,
            )
            meta = {
                "routing_decision": routing_decision.model_dump(),
                "execution_strategy": strategy_str,
            }
            task = self.submit_goal(
                goal_text=goal_text,
                session_id=session_id,
                project_id=project_id,
                metadata=meta,
                routing_intent=route_str,
            )
            self._store.transition_task_state(task.task_id, TaskState.PLANNING)
            self._store.transition_task_state(task.task_id, TaskState.EXECUTING)

            from src.skills.models import SkillExecutionStatus, SkillInvocation
            skill_input: Dict[str, Any] = {"goal": goal_text, "text": goal_text, "query": goal_text}
            invocation = SkillInvocation(
                skill_id=routing_decision.selected_skill_id,
                input=skill_input,
                task_id=task.task_id,
            )
            try:
                skill_result = self.execute_skill(
                    invocation=invocation,
                    actor=actor,
                    project_id=project_id,
                    parent_task_id=task.task_id,
                    root_task_id=task.root_task_id or task.task_id,
                )
            except Exception as exc:
                logger.error("Direct skill execution failed for task %s: %s", task.task_id, exc)
                final_task = self._store.transition_task_state(
                    task.task_id,
                    TaskState.FAILED,
                    error_summary=str(exc),
                )
                return GoalExecutionResult(
                    goal_text=goal_text,
                    success=False,
                    synthesis=f"Direct skill execution failed: {exc}",
                    task_record=final_task,
                    observations=[],
                    routing_decision=routing_decision,
                )

            if skill_result.status == SkillExecutionStatus.SUCCESS:
                final_task = self._store.transition_task_state(task.task_id, TaskState.COMPLETED)
                obs_out = (
                    str(skill_result.output)
                    if skill_result.output is not None
                    else "Skill completed successfully."
                )
                skill_obs = StepObservation(
                    step_number=1,
                    tool_name=f"skill:{routing_decision.selected_skill_id}",
                    status=ObservationStatus.SUCCESS,
                    output_summary=obs_out,
                    raw_output=obs_out,
                )

                return GoalExecutionResult(
                    goal_text=goal_text,
                    success=True,
                    synthesis=f"Task completed directly via skill '{routing_decision.selected_skill_id}': {obs_out}",
                    task_record=final_task,
                    observations=[skill_obs],
                    routing_decision=routing_decision,
                )
            else:
                final_task = self._store.transition_task_state(
                    task.task_id,
                    TaskState.FAILED,
                    error_summary=skill_result.error or "Skill execution did not succeed",
                )
                return GoalExecutionResult(
                    goal_text=goal_text,
                    success=False,
                    synthesis=f"Direct skill execution failed: {skill_result.error}",
                    task_record=final_task,
                    observations=[],
                    routing_decision=routing_decision,
                )

        # 3. STRATEGY DISPATCH: STRUCTURED_PLAN / RESEARCH_RETRIEVAL
        active_provider = selected_provider or self._reasoning_provider
        if active_provider is not None:
            if self._planner is not None and self._planner.provider == active_provider:
                planner = self._planner
            else:
                planner = StructuredPlanner(active_provider)
        else:
            planner = self._planner

        if planner is None:
            raise RuntimeError(
                "No reasoning provider/planner configured. "
                "Provide a LocalReasoningProvider when constructing Supervisor."
            )

        constraints = list(additional_constraints) if additional_constraints else []
        constraints.append(
            f"Routing Strategy: {strategy_str} | Intent: {route_str}"
        )

        # CONTEXT ASSEMBLY
        context_ir = self._context_assembler.assemble(
            goal=goal_text,
            project_id=project_id,
            additional_constraints=constraints if constraints else None,
        )

        # PLANNING (with one internal repair attempt)
        try:
            plan = planner.generate_plan(context_ir, self._tools)
        except PlanValidationError as exc:
            logger.error("Planning failed for goal %r: %s", goal_text, exc)
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            self._record_goal_experience(
                task=None,
                goal_text=goal_text,
                success=False,
                completed_steps=[],
                error_summary=f"Planning failed: {exc}",
                project_id=project_id,
                latency_ms=elapsed_ms,
            )
            return GoalExecutionResult(
                goal_text=goal_text,
                success=False,
                synthesis=f"Planning failed: {exc}",
                task_record=None,
                observations=[],
                routing_decision=routing_decision,
            )

        # SUBMIT + PERSIST TASK
        task_meta = {
            "routing_decision": routing_decision.model_dump(),
            "execution_strategy": strategy_str,
        }
        task = self.submit_goal(
            goal_text=goal_text,
            session_id=session_id,
            project_id=project_id,
            metadata=task_meta,
            routing_intent=route_str,
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

        # 4. BOUNDED OBSERVATION-DRIVEN EXECUTION LOOP
        max_replan = (
            max_replan_attempts
            if max_replan_attempts is not None
            else self.MAX_REPLAN_ATTEMPTS
        )
        max_steps = (
            max_total_steps
            if max_total_steps is not None
            else self.MAX_TOTAL_EXECUTED_STEPS
        )

        all_observations: list[StepObservation] = []
        all_verifications = []
        completed_steps: list[dict[str, Any]] = []
        uncertain_operations: list[dict[str, Any]] = []
        replan_count = 0
        total_steps_executed = 0

        steps = self._store.list_task_steps(task.task_id)
        execution_queue: list[tuple[StepRecord, Any]] = list(
            zip(steps, plan.steps, strict=False)
        )

        while execution_queue:
            step_record, planned_step = execution_queue.pop(0)

            # Step budget limit check
            if total_steps_executed >= max_steps:
                self._store.transition_step_state(
                    step_record.step_id,
                    StepState.FAILED,
                    result_summary=f"Total step execution budget ({max_steps}) exhausted.",
                )
                final_task = self._store.transition_task_state(
                    task.task_id,
                    TaskState.FAILED,
                    error_summary=f"Execution budget exhausted: reached maximum total steps ({max_steps}).",
                )
                return GoalExecutionResult(
                    goal_text=goal_text,
                    success=False,
                    synthesis=f"Execution halted: Reached execution budget limit ({max_steps} total steps).",
                    task_record=final_task,
                    observations=all_observations,
                )

            # Duplicate-action prevention: never blindly repeat completed non-idempotent operations
            is_duplicate_completed = False
            for comp in completed_steps:
                if (
                    comp.get("tool") == planned_step.tool
                    and comp.get("arguments") == dict(planned_step.arguments)
                    and not planned_step.is_idempotent
                ):
                    is_duplicate_completed = True
                    logger.warning(
                        "Preventing duplicate non-idempotent action '%s': reusing previous verified result.",
                        planned_step.tool,
                    )
                    self._store.transition_step_state(
                        step_record.step_id,
                        StepState.COMPLETED,
                        result_summary=f"Reused verified result: {comp.get('result_summary')}",
                    )
                    break
            if is_duplicate_completed:
                continue

            # Uncertain side-effect check: do not repeat ambiguous non-idempotent side effects
            is_uncertain_repeat = False
            for unc in uncertain_operations:
                if (
                    unc.get("tool") == planned_step.tool
                    and unc.get("arguments") == dict(planned_step.arguments)
                    and not planned_step.is_idempotent
                ):
                    is_uncertain_repeat = True
                    break
            if is_uncertain_repeat:
                self._store.transition_step_state(
                    step_record.step_id,
                    StepState.FAILED,
                    result_summary="Cannot repeat non-idempotent action with uncertain outcome without external state verification.",
                )
                final_task = self._store.transition_task_state(
                    task.task_id,
                    TaskState.FAILED,
                    error_summary="Cannot repeat non-idempotent action with uncertain outcome without external state verification.",
                )
                return GoalExecutionResult(
                    goal_text=goal_text,
                    success=False,
                    synthesis="Execution halted: Attempted to repeat a non-idempotent side-effecting action with an uncertain outcome without external verification.",
                    task_record=final_task,
                    observations=all_observations,
                )

            # Step execution with bounded idempotent retry
            retry_count = 0
            while True:
                total_steps_executed += 1
                self._store.transition_step_state(step_record.step_id, StepState.RUNNING)

                # Pre-execution durable checkpoint
                existing_cps = self._store.list_checkpoints(task.task_id)
                self._store.create_checkpoint(
                    task_id=task.task_id,
                    step_id=step_record.step_id,
                    checkpoint_seq=len(existing_cps) + 1,
                    snapshot={
                        "event": "PRE_EXECUTION",
                        "step_index": step_record.step_index,
                        "tool": planned_step.tool,
                    },
                )

                # Central Policy Evaluation check before tool execution
                if self._config_manager is not None:
                    from src.config.policy_models import (
                        PolicyEvaluationDecisionType,
                        PolicyEvaluationRequest,
                    )

                    tool_defn = (
                        self._tools.get_definition(planned_step.tool)
                        if hasattr(self._tools, "get_definition")
                        else None
                    )
                    action_name = (
                        tool_defn.action_name
                        if tool_defn
                        else f"tool.{planned_step.tool}"
                    )
                    target_res = planned_step.arguments.get(
                        "path"
                    ) or planned_step.arguments.get("target")

                    eval_req = PolicyEvaluationRequest(
                        action_name=action_name,
                        tool_name=planned_step.tool,
                        actor=actor,
                        project_id=project_id,
                        target_resource=str(target_res) if target_res else None,
                        arguments=dict(planned_step.arguments),
                        active_mode=mode,
                    )
                    policy_dec = self._config_manager.evaluate_policy(eval_req)
                    if policy_dec.decision == PolicyEvaluationDecisionType.DENY:
                        logger.warning(
                            "Step %s denied by policy evaluation: %s",
                            planned_step.tool,
                            policy_dec.reason,
                        )
                        obs = StepObservation(
                            step_number=planned_step.step_number,
                            tool_name=planned_step.tool,
                            status=ObservationStatus.DENIED,
                            exit_code=1,
                            output_summary=f"PolicyDenied: {policy_dec.reason}"[:200],
                            raw_output=f"PolicyDenied: {policy_dec.reason}",
                            duration_ms=0.0,
                            metadata={"policy_decision": policy_dec.decision.value, "rule_references": policy_dec.rule_references},
                        )
                        all_observations.append(obs)
                        self._store.transition_step_state(
                            step_record.step_id,
                            StepState.FAILED,
                            result_summary=f"PolicyDenied: {policy_dec.reason}",
                        )
                        break

                # Invoke through ToolBroker (Trust Core validates capability & policies inside)
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

                # Post-execution durable checkpoint
                existing_cps = self._store.list_checkpoints(task.task_id)
                self._store.create_checkpoint(
                    task_id=task.task_id,
                    step_id=step_record.step_id,
                    checkpoint_seq=len(existing_cps) + 1,
                    snapshot={
                        "event": "POST_EXECUTION",
                        "step_index": step_record.step_index,
                        "status": obs.status.value,
                        "exit_code": obs.exit_code,
                    },
                )

                # 5. VERIFICATION
                ver = self._verifier.verify(planned_step, obs)
                all_verifications.append(ver)

                if ver.outcome == VerificationOutcome.VERIFIED_SUCCESS:
                    self._store.transition_step_state(
                        step_record.step_id,
                        StepState.COMPLETED,
                        result_summary=obs.output_summary,
                    )
                    completed_steps.append({
                        "tool": planned_step.tool,
                        "description": planned_step.description,
                        "arguments": dict(planned_step.arguments),
                        "result_summary": obs.output_summary,
                        "step_id": step_record.step_id,
                    })
                    break  # Step succeeded; advance in execution_queue

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

                # Record uncertain operations for non-idempotent actions
                if not planned_step.is_idempotent and (
                    obs.status == ObservationStatus.TIMEOUT
                    or ver.outcome == VerificationOutcome.UNKNOWN
                ):
                    uncertain_operations.append({
                        "tool": planned_step.tool,
                        "arguments": dict(planned_step.arguments),
                        "description": planned_step.description,
                        "rationale": ver.rationale,
                    })

                # Human approval pathway
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

                # Observation-driven replanning branch
                if ver.requires_replan:
                    if replan_count >= max_replan:
                        logger.warning(
                            "Replan limit reached (%d/%d) for task %s",
                            replan_count,
                            max_replan,
                            task.task_id,
                        )
                        self._store.transition_step_state(
                            step_record.step_id,
                            StepState.FAILED,
                            result_summary=f"Replan limit reached: {ver.rationale}",
                        )
                        final_task = self._store.transition_task_state(
                            task.task_id,
                            TaskState.FAILED,
                            error_summary=f"Replan limit reached ({max_replan} attempts): {ver.rationale}",
                        )
                        synthesis = (
                            f"Execution halted: Replan limit reached ({max_replan} attempts). "
                            f"Last error: {ver.rationale}"
                        )
                        return GoalExecutionResult(
                            goal_text=goal_text,
                            success=False,
                            synthesis=synthesis,
                            task_record=final_task,
                            observations=all_observations,
                        )

                    replan_count += 1
                    logger.info(
                        "Triggering observation-driven replanning (attempt %d/%d) after step failure: %s",
                        replan_count,
                        max_replan,
                        ver.rationale,
                    )
                    self._store.transition_step_state(
                        step_record.step_id,
                        StepState.FAILED,
                        result_summary=f"Failed (observation: {obs.output_summary}). Replanning triggered.",
                    )

                    # Mark remaining unexecuted steps in current queue as SKIPPED
                    for rem_rec, _ in execution_queue:
                        if rem_rec.state == StepState.PENDING:
                            self._store.transition_step_state(
                                rem_rec.step_id,
                                StepState.SKIPPED,
                                result_summary="Superseded by replanning.",
                            )
                    execution_queue.clear()

                    # Assemble updated ContextIR containing observation history
                    from src.manager.context import truncate_tool_output

                    new_obs_history = list(context_ir.observation_history)
                    sanitized_obs = truncate_tool_output(obs.output_summary, max_chars=400)
                    new_obs_history.append(
                        f"Step '{planned_step.description}' [{planned_step.tool}] failed: {sanitized_obs}"
                    )
                    new_recent_events = list(context_ir.recent_events)
                    new_recent_events.append(
                        f"Replan #{replan_count}: completed {len(completed_steps)} steps; step '{planned_step.description}' failed."
                    )
                    uncompacted_replan = ContextIR(
                        active_goal=context_ir.active_goal,
                        project_id=context_ir.project_id,
                        task_id=task.task_id,
                        relevant_preferences=context_ir.relevant_preferences,
                        relevant_facts=context_ir.relevant_facts,
                        relevant_lessons=context_ir.relevant_lessons,
                        recent_events=new_recent_events,
                        constraints=context_ir.constraints,
                        observation_history=new_obs_history,
                        available_token_budget=context_ir.available_token_budget,
                    )
                    replan_context = self._context_assembler.compact_context(uncompacted_replan)
                    context_ir = replan_context

                    # Checkpoint replan event
                    existing_cps = self._store.list_checkpoints(task.task_id)
                    self._store.create_checkpoint(
                        task_id=task.task_id,
                        step_id=step_record.step_id,
                        checkpoint_seq=len(existing_cps) + 1,
                        snapshot={
                            "event": "REPLAN_TRIGGERED",
                            "replan_count": replan_count,
                            "failed_step": planned_step.description,
                            "observation": obs.output_summary,
                            "completed_steps": len(completed_steps),
                        },
                    )

                    # Request revised fast decision if decision engine is configured
                    if self._decision is not None:
                        try:
                            replan_prompt = (
                                f"{goal_text} | Failed step: {planned_step.description} | "
                                f"Observed error: {obs.output_summary}"
                            )
                            replan_eval = self._decision.evaluate(
                                FastDecisionRequest(
                                    user_input=replan_prompt,
                                    context=RequestContext(project_scope=project_id),
                                )
                            )
                            replan_routing, replan_provider, _ = self._router.route(
                                goal_text=replan_prompt,
                                decision=replan_eval,
                                default_provider=active_provider,
                            )
                            if replan_provider is not None:
                                if planner is None or planner.provider != replan_provider:
                                    planner = StructuredPlanner(replan_provider)
                                active_provider = replan_provider
                            routing_decision = replan_routing
                        except Exception as exc:
                            logger.warning("Replan decision evaluation failed: %s", exc)

                    # Invoke planner for revised plan based on observation
                    try:
                        new_plan = planner.generate_replan(
                            context_ir=replan_context,
                            broker=self._tools,
                            failed_step_description=planned_step.description,
                            observation_summary=obs.output_summary,
                            completed_step_summaries=completed_steps,
                        )
                    except Exception as exc:
                        logger.error(
                            "Replanning failed closed for task %s: %s", task.task_id, exc
                        )
                        final_task = self._store.transition_task_state(
                            task.task_id,
                            TaskState.FAILED,
                            error_summary=f"Replanning failed: {exc}",
                        )
                        synthesis = f"Replanning failed: {exc}"
                        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
                        self._record_goal_experience(
                            task=final_task,
                            goal_text=goal_text,
                            success=False,
                            completed_steps=completed_steps,
                            error_summary=synthesis,
                            project_id=project_id,
                            latency_ms=elapsed_ms,
                        )
                        return GoalExecutionResult(
                            goal_text=goal_text,
                            success=False,
                            synthesis=synthesis,
                            task_record=final_task,
                            observations=all_observations,
                            routing_decision=routing_decision,
                        )

                    # Persist newly planned steps in StateStore
                    all_task_steps = self._store.list_task_steps(task.task_id)
                    max_idx = max((s.step_index for s in all_task_steps), default=-1)
                    new_queue: list[tuple[StepRecord, Any]] = []
                    for offset, n_step in enumerate(new_plan.steps, start=1):
                        n_rec = self._store.create_step(
                            task_id=task.task_id,
                            step_index=max_idx + offset,
                            action_name=n_step.tool,
                            tool_name=n_step.tool,
                            is_idempotent=n_step.is_idempotent,
                            arguments=dict(n_step.arguments),
                        )
                        new_queue.append((n_rec, n_step))

                    # Checkpoint committed replan
                    existing_cps = self._store.list_checkpoints(task.task_id)
                    self._store.create_checkpoint(
                        task_id=task.task_id,
                        checkpoint_seq=len(existing_cps) + 1,
                        snapshot={
                            "event": "REPLAN_COMMITTED",
                            "replan_count": replan_count,
                            "new_steps": [s.model_dump() for s in new_plan.steps],
                        },
                    )

                    # Enqueue new steps and continue execution
                    execution_queue.extend(new_queue)
                    plan = new_plan
                    break  # Break retry loop to resume execution_queue

                # Fail closed for any unhandled failure outcome
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
                elapsed_ms = (time.perf_counter() - t_start) * 1000.0
                self._record_goal_experience(
                    task=final_task,
                    goal_text=goal_text,
                    success=False,
                    completed_steps=completed_steps,
                    error_summary=synthesis,
                    project_id=project_id,
                    latency_ms=elapsed_ms,
                )
                return GoalExecutionResult(
                    goal_text=goal_text,
                    success=False,
                    synthesis=synthesis,
                    task_record=final_task,
                    observations=all_observations,
                    routing_decision=routing_decision,
                )

        # 6. COMPLETION VERIFICATION & SYNTHESIS
        current_task_rec = self._store.get_task(task.task_id)
        if current_task_rec and current_task_rec.state != TaskState.EXECUTING:
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            self._record_goal_experience(
                task=current_task_rec,
                goal_text=goal_text,
                success=False,
                completed_steps=completed_steps,
                error_summary=f"Task ended in non-executing state: {current_task_rec.state.value}",
                project_id=project_id,
                latency_ms=elapsed_ms,
            )
            return GoalExecutionResult(
                goal_text=goal_text,
                success=False,
                synthesis=f"Task ended in non-executing state: {current_task_rec.state.value}",
                task_record=current_task_rec,
                observations=all_observations,
                routing_decision=routing_decision,
            )

        if not completed_steps:
            final_task = self._store.transition_task_state(
                task.task_id,
                TaskState.FAILED,
                error_summary="Execution finished without any completed steps.",
            )
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            self._record_goal_experience(
                task=final_task,
                goal_text=goal_text,
                success=False,
                completed_steps=[],
                error_summary="Execution finished without any completed steps.",
                project_id=project_id,
                latency_ms=elapsed_ms,
            )
            return GoalExecutionResult(
                goal_text=goal_text,
                success=False,
                synthesis="Execution halted: No steps completed successfully.",
                task_record=final_task,
                observations=all_observations,
                routing_decision=routing_decision,
            )

        completed_task = self._store.transition_task_state(
            task.task_id, TaskState.COMPLETED
        )
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        synthesis_res: Optional[SynthesisResult] = None
        synthesis_text: str
        synthesis_failed_under_strict = False
        synthesis_error_msg: Optional[str] = None

        if self._synthesizer is not None:
            try:
                synthesis_res = self._synthesizer.synthesize(
                    goal=goal_text,
                    plan=plan,
                    observations=all_observations,
                    verifications=all_verifications,
                )
                synthesis_text = synthesis_res.text
            except SynthesisError as exc:
                logger.error("Synthesis failed under STRICT policy: %s", exc)
                synthesis_failed_under_strict = True
                synthesis_error_msg = str(exc)
                synthesis_text = f"Execution halted: Synthesis failed under STRICT policy: {exc}"
                synthesis_res = SynthesisResult(
                    text=synthesis_text,
                    is_degraded=True,
                    fallback_used=False,
                    provider_error=getattr(exc, "diagnostics", None),
                    policy=SynthesisDegradationPolicy.STRICT,
                )
                if getattr(self, "_strict_synthesis", False):
                    self._record_goal_experience(
                        task=completed_task,
                        goal_text=goal_text,
                        success=False,
                        completed_steps=completed_steps,
                        project_id=project_id,
                        latency_ms=elapsed_ms,
                        error_summary=f"Strict synthesis failure: {exc}",
                        synthesis_degraded=True,
                        synthesis_error=str(exc),
                    )
                    raise
        else:
            synthesis_text = (
                f"Goal '{goal_text}' completed successfully across {len(all_observations)} step(s)."
            )

        is_degraded = bool(synthesis_res and synthesis_res.is_degraded)
        final_success = not synthesis_failed_under_strict

        self._record_goal_experience(
            task=completed_task,
            goal_text=goal_text,
            success=final_success,
            completed_steps=completed_steps,
            project_id=project_id,
            latency_ms=elapsed_ms,
            error_summary=synthesis_error_msg if synthesis_failed_under_strict else None,
            synthesis_degraded=is_degraded,
            synthesis_error=(
                synthesis_res.provider_error.error_message
                if (synthesis_res and synthesis_res.provider_error)
                else synthesis_error_msg
            ),
        )

        return GoalExecutionResult(
            goal_text=goal_text,
            success=final_success,
            synthesis=synthesis_text,
            task_record=completed_task,
            observations=all_observations,
            routing_decision=routing_decision,
            synthesis_result=synthesis_res,
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
        parent_task_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        parent_execution_id: Optional[str] = None,
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

        target_task_id = getattr(invocation, "task_id", None) or parent_task_id
        eff_parent_id = parent_task_id or getattr(invocation, "parent_task_id", None)
        eff_root_id = root_task_id or getattr(invocation, "root_task_id", None)
        exec_id = execution_id or getattr(invocation, "invocation_id", None) or str(uuid4())

        try:
            self._skill_registry.transition_lifecycle(
                manifest.skill_id, SkillLifecycleState.ADMITTED
            )
            self._skill_registry.transition_lifecycle(
                manifest.skill_id, SkillLifecycleState.EXECUTING
            )

            # Record durable pre-execution checkpoint for skill
            if self._store is not None and target_task_id:
                try:
                    existing_cps = self._store.list_checkpoints(target_task_id)
                    self._store.create_checkpoint(
                        task_id=target_task_id,
                        checkpoint_seq=len(existing_cps) + 1,
                        snapshot={
                            "event": "SKILL_PRE_EXECUTION",
                            "skill_id": manifest.skill_id,
                            "invocation_id": exec_id,
                        },
                        parent_task_id=eff_parent_id,
                        root_task_id=eff_root_id,
                        execution_id=exec_id,
                        parent_execution_id=parent_execution_id,
                    )
                except Exception as cp_err:
                    logger.warning("Failed to record skill pre-execution checkpoint: %s", cp_err)

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

            # Record durable post-execution checkpoint for skill
            if self._store is not None and target_task_id:
                try:
                    existing_cps = self._store.list_checkpoints(target_task_id)
                    self._store.create_checkpoint(
                        task_id=target_task_id,
                        checkpoint_seq=len(existing_cps) + 1,
                        snapshot={
                            "event": "SKILL_POST_EXECUTION",
                            "skill_id": manifest.skill_id,
                            "invocation_id": exec_id,
                            "status": result.status.value,
                            "verification_status": result.verification_status.value,
                        },
                        parent_task_id=eff_parent_id,
                        root_task_id=eff_root_id,
                        execution_id=exec_id,
                        parent_execution_id=parent_execution_id,
                    )
                except Exception as cp_err:
                    logger.warning("Failed to record skill post-execution checkpoint: %s", cp_err)

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

    __slots__ = (
        "goal_text",
        "success",
        "synthesis",
        "task_record",
        "observations",
        "routing_decision",
        "synthesis_result",
    )

    def __init__(
        self,
        goal_text: str,
        success: bool,
        synthesis: Any,
        task_record: Optional[TaskRecord],
        observations: list,
        routing_decision: Optional[Any] = None,
        synthesis_result: Optional[Any] = None,
    ) -> None:
        self.goal_text = goal_text
        self.success = success
        if hasattr(synthesis, "text"):
            self.synthesis = synthesis.text
            self.synthesis_result = synthesis if synthesis_result is None else synthesis_result
        else:
            self.synthesis = str(synthesis)
            self.synthesis_result = synthesis_result
        self.task_record = task_record
        self.observations = observations
        self.routing_decision = routing_decision

    def __repr__(self) -> str:
        return (
            f"GoalExecutionResult(success={self.success!r}, "
            f"synthesis={self.synthesis!r}, "
            f"steps={len(self.observations)}, "
            f"strategy={getattr(self.routing_decision, 'strategy', None)!r}, "
            f"degraded={getattr(self.synthesis_result, 'is_degraded', False)!r})"
        )

