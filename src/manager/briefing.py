"""SAGE Context Understanding & Confirmation Gate.

Canonical Reference:
    PROMPT 029: Context Confirmation & Persistent Completion Mode
    Core Principle: Understand -> Explain -> Confirm -> Execute ->
                    Observe -> Diagnose -> Recover -> Verify -> Complete.

Invariants:
- All domain records are frozen, typed Pydantic models (extra="forbid").
- Zero-Secret Invariant: Briefing contents must never leak raw credentials.
- Selective Memory: Only task-relevant memories and approved lessons are queried;
  never dump the entire user profile.
- Explicit Provenance: Clearly distinguish user-provided inputs, stored memories,
  tool-verified facts, and model inferences.
- Fail-Closed: Ambiguous, conflicting, or missing required parameters trigger
  REQUIRES_CLARIFICATION rather than guessing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum, unique
from typing import List, Optional, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.manager.context import ContextAssembler, ContextIR
from src.manager.errors import (
    InvalidConfirmationStateError,
)
from src.manager.models import StepPlan, TaskRecord
from src.manager.planner import StructuredPlanner
from src.skills.clarification import ClarificationAction, ClarificationGate
from src.tools.broker import ToolBroker
from src.trust.audit import _check_no_secrets
from src.trust.authority import TrustAuthority
from src.trust.capability import ActionRisk

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# ==============================================================================
# ENUMERATIONS & MODELS
# ==============================================================================


@unique
class ProvenanceType(str, Enum):
    """Origin of a piece of context or information in the briefing."""

    USER_PROVIDED = "USER_PROVIDED"
    STORED_MEMORY = "STORED_MEMORY"
    TOOL_VERIFIED = "TOOL_VERIFIED"
    MODEL_INFERENCE = "MODEL_INFERENCE"


@unique
class BriefingStatus(str, Enum):
    """Lifecycle status of a Context Understanding Briefing."""

    DRAFT = "DRAFT"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    CORRECTED = "CORRECTED"
    CANCELLED = "CANCELLED"
    REQUIRES_CLARIFICATION = "REQUIRES_CLARIFICATION"


class ContextProvenanceItem(BaseModel):
    """Individual context assertion with verifiable source provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(description="Context assertion statement.")
    provenance: ProvenanceType = Field(description="Origin category of this assertion.")
    source_reference: Optional[str] = Field(
        default=None, description="Explicit source identifier or evidence link."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Epistemic confidence.")


class ExecutionBudget(BaseModel):
    """Operational limits bounding persistent execution cycles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(default=10, ge=1, le=100)
    max_recovery_cycles: int = Field(default=5, ge=1, le=20)
    max_tool_calls: int = Field(default=25, ge=1, le=200)
    max_research_calls: int = Field(default=5, ge=0, le=30)
    max_time_seconds: float = Field(default=300.0, ge=10.0, le=3600.0)


class ContextBriefing(BaseModel):
    """Comprehensive Context Understanding Briefing presented before execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    briefing_id: str = Field(description="Unique briefing UUID.")
    task_id: str = Field(description="Associated task UUID.")
    goal: str = Field(description="User goal and requested outcome.")
    deliverables: List[str] = Field(
        default_factory=list, description="Expected concrete deliverables."
    )
    requirements: List[str] = Field(
        default_factory=list, description="Scope and specific requirements."
    )
    constraints: List[str] = Field(
        default_factory=list, description="Operational boundaries and limits."
    )
    deadline_utc: Optional[str] = Field(default=None, description="Optional deadline timestamp.")
    provenance_items: List[ContextProvenanceItem] = Field(
        default_factory=list, description="Context items tagged with provenance."
    )
    relevant_memories: List[str] = Field(
        default_factory=list, description="Relevant epistemic facts and user preferences."
    )
    relevant_lessons: List[str] = Field(
        default_factory=list, description="Applicable approved operational lessons."
    )
    available_capabilities: List[str] = Field(
        default_factory=list, description="Tools and capabilities eligible for this task."
    )
    assumptions: List[str] = Field(
        default_factory=list, description="Explicit assumptions made by planner."
    )
    uncertainties: List[str] = Field(
        default_factory=list, description="Unresolved uncertainties or missing data."
    )
    proposed_plan: List[StepPlan] = Field(
        default_factory=list, description="Ordered step execution plan."
    )
    acceptance_criteria: List[str] = Field(
        default_factory=list, description="Observable conditions proving task completion."
    )
    risks: List[str] = Field(
        default_factory=list, description="Potential side effects and operational risks."
    )
    required_approvals: List[str] = Field(
        default_factory=list, description="Explicit Trust Core approval gates required."
    )
    budgets: ExecutionBudget = Field(
        default_factory=ExecutionBudget, description="Execution budgets."
    )
    status: BriefingStatus = Field(
        default=BriefingStatus.DRAFT, description="Current confirmation state."
    )
    user_feedback: Optional[str] = Field(
        default=None, description="User confirmation note, correction, or cancel reason."
    )
    clarification_questions: List[str] = Field(
        default_factory=list, description="Questions emitted if status is REQUIRES_CLARIFICATION."
    )
    version: int = Field(default=1, ge=1, description="Briefing revision version.")
    created_at: str = Field(description="ISO-8601 UTC creation timestamp.")
    updated_at: str = Field(description="ISO-8601 UTC update timestamp.")

    def to_readable_summary(self) -> str:
        """Render a readable human-facing summary of what SAGE understands."""
        lines = [
            f"# SAGE Task Understanding & Confirmation Briefing (v{self.version})",
            f"**Task ID:** {self.task_id} | **Status:** {self.status.value}",
            f"**Goal:** {self.goal}",
        ]
        if self.deliverables:
            lines.append("\n### Deliverables")
            for d in self.deliverables:
                lines.append(f"- {d}")

        if self.requirements:
            lines.append("\n### Requirements & Scope")
            for r in self.requirements:
                lines.append(f"- {r}")

        if self.constraints:
            lines.append("\n### Constraints")
            for c in self.constraints:
                lines.append(f"- {c}")

        if self.provenance_items:
            lines.append("\n### Relevant Context & Provenance")
            for p in self.provenance_items:
                ref = f" (ref: {p.source_reference})" if p.source_reference else ""
                lines.append(f"- `[{p.provenance.value}]` {p.content}{ref}")

        if self.relevant_lessons:
            lines.append("\n### Operational Lessons (Advisory)")
            for lesson in self.relevant_lessons:
                lines.append(f"- {lesson}")

        if self.assumptions:
            lines.append("\n### Assumptions")
            for a in self.assumptions:
                lines.append(f"- {a}")

        if self.uncertainties:
            lines.append("\n### Uncertainties & Ambiguities")
            for u in self.uncertainties:
                lines.append(f"- [!] {u}")

        if self.clarification_questions:
            lines.append("\n### Clarification Needed Before Execution")
            for q in self.clarification_questions:
                lines.append(f"- ? {q}")

        if self.proposed_plan:
            lines.append("\n### Proposed Execution Plan")
            for i, step in enumerate(self.proposed_plan, 1):
                tool = f" (tool: {step.tool_name})" if step.tool_name else ""
                idem = " [idempotent]" if step.is_idempotent else ""
                lines.append(f"{i}. **{step.action_name}**{tool}{idem}")

        if self.acceptance_criteria:
            lines.append("\n### Acceptance Criteria")
            for ac in self.acceptance_criteria:
                lines.append(f"- [ ] {ac}")

        if self.risks or self.required_approvals:
            lines.append("\n### Risks & Permissions")
            for r in self.risks:
                lines.append(f"- Risk: {r}")
            for app in self.required_approvals:
                lines.append(f"- Approval Required: {app}")

        lines.append(
            f"\n### Budgets\n"
            f"- Max Iterations: {self.budgets.max_iterations} | "
            f"Max Recovery Cycles: {self.budgets.max_recovery_cycles} | "
            f"Max Time: {self.budgets.max_time_seconds}s"
        )
        return "\n".join(lines)


# ==============================================================================
# CONFIRMATION GATE ENGINE
# ==============================================================================


class ConfirmationGate:
    """Manages the creation, verification, correction, and confirmation of briefings."""

    def __init__(
        self,
        context_assembler: ContextAssembler,
        planner: Optional[StructuredPlanner] = None,
        tool_broker: Optional[ToolBroker] = None,
        trust_authority: Optional[TrustAuthority] = None,
        clarification_gate: Optional[ClarificationGate] = None,
    ) -> None:
        self._context_assembler = context_assembler
        self._planner = planner
        self._tools = tool_broker
        self._authority = trust_authority
        self._clarification_gate = clarification_gate or ClarificationGate()

    def build_briefing(
        self,
        task: TaskRecord,
        user_goal: str,
        project_id: Optional[str] = None,
        initial_requirements: Optional[Sequence[str]] = None,
        initial_constraints: Optional[Sequence[str]] = None,
        deliverables: Optional[Sequence[str]] = None,
        proposed_steps: Optional[Sequence[StepPlan]] = None,
        budgets: Optional[ExecutionBudget] = None,
    ) -> ContextBriefing:
        """Construct a structured Context Understanding Briefing for a task.

        Validates the goal and parameters against the clarification gate and
        assembles selectively relevant context without dumping the entire store.
        """
        _check_no_secrets(user_goal)
        now = _now_utc()
        task_id = task.task_id

        # 1. Assemble selective Context IR
        context_ir: ContextIR = self._context_assembler.assemble(
            goal=user_goal,
            project_id=project_id,
            task_id=task_id,
            additional_constraints=list(initial_constraints or []),
        )

        # 2. Build explicit provenance items
        provenance_items: List[ContextProvenanceItem] = [
            ContextProvenanceItem(
                content=f"Primary user goal: '{user_goal}'",
                provenance=ProvenanceType.USER_PROVIDED,
                source_reference="user_prompt",
                confidence=1.0,
            )
        ]

        # User preferences from EKM (STORED_MEMORY)
        for pref in context_ir.relevant_preferences:
            _check_no_secrets(pref)
            provenance_items.append(
                ContextProvenanceItem(
                    content=f"User preference: {pref}",
                    provenance=ProvenanceType.STORED_MEMORY,
                    source_reference="ekm:user_preferences",
                    confidence=0.95,
                )
            )

        # Known epistemic facts (STORED_MEMORY)
        for fact in context_ir.relevant_facts:
            _check_no_secrets(fact)
            provenance_items.append(
                ContextProvenanceItem(
                    content=f"Memory fact: {fact}",
                    provenance=ProvenanceType.STORED_MEMORY,
                    source_reference="ekm:memory_facts",
                    confidence=0.90,
                )
            )

        # Operational lessons (STORED_MEMORY / TOOL_VERIFIED)
        lesson_summaries: List[str] = []
        for les in context_ir.relevant_lessons:
            summary = (
                f"[{les.lesson_id[:8]}] {les.title}: When {les.trigger_condition}, "
                f"{les.recommended_action} (observed: {les.observation})"
            )
            _check_no_secrets(summary)
            lesson_summaries.append(summary)
            provenance_items.append(
                ContextProvenanceItem(
                    content=summary,
                    provenance=ProvenanceType.TOOL_VERIFIED,
                    source_reference=f"ekm:lesson:{les.lesson_id}",
                    confidence=les.confidence,
                )
            )

        # 3. Detect capabilities and permissions
        capabilities: List[str] = []
        risks: List[str] = []
        approvals: List[str] = []
        if self._tools is not None:
            for tool_name, tool_def in self._tools.list_tools().items():
                capabilities.append(f"{tool_name}: {tool_def.description}")

        # 4. Formulate proposed plan if not explicitly supplied
        plan_steps: List[StepPlan] = list(proposed_steps or [])
        assumptions: List[str] = []
        if not plan_steps and self._planner is not None and self._tools is not None:
            try:
                gen_plan = self._planner.generate_plan(context_ir, self._tools)
                for ps in gen_plan.steps:
                    plan_steps.append(
                        StepPlan(
                            action_name=ps.tool,
                            tool_name=ps.tool,
                            is_idempotent=ps.is_idempotent,
                            arguments={},
                        )
                    )
                assumptions.append(
                    "Plan formulated via structured planner based on available context."
                )
            except Exception as exc:
                logger.warning("Planner formulation yielded exception: %s", exc)

        # Infer risks and required approvals for proposed steps
        for step in plan_steps:
            if self._authority is not None:
                policy = getattr(self._authority, "_policy", None)
                if policy is not None and hasattr(policy, "evaluate"):
                    pol_res = policy.evaluate(step.action_name)
                    if pol_res.risk in (ActionRisk.HIGH, ActionRisk.CRITICAL):
                        approvals.append(
                            f"Action '{step.action_name}' has risk tier {pol_res.risk.value}; "
                            "may require explicit capability approval."
                        )
            if not step.is_idempotent:
                risks.append(
                    f"Step '{step.action_name}' is non-idempotent; side effects must be monitored."
                )

        # 5. Clarification and uncertainty analysis
        clarification_questions: List[str] = []
        uncertainties: List[str] = []

        # Evaluate deterministic ambiguity via ClarificationGate
        clar_eval = self._clarification_gate.evaluate(
            provided_args={"goal": user_goal},
            required_keys=("goal",),
        )
        if clar_eval.action == ClarificationAction.CLARIFY:
            if clar_eval.clarification_prompt:
                clarification_questions.append(clar_eval.clarification_prompt)
            if clar_eval.reason:
                uncertainties.append(f"Ambiguity detected: {clar_eval.reason.value}")

        # Missing plan check
        if not plan_steps:
            uncertainties.append("No concrete steps could be planned for the specified goal.")
            clarification_questions.append(
                "Can you provide more details or concrete deliverables for this task?"
            )

        # 6. Formulate acceptance criteria
        ac: List[str] = []
        if deliverables:
            for d in deliverables:
                ac.append(f"Deliverable '{d}' successfully created and verified.")
        else:
            ac.append(f"Execution of task '{user_goal}' completed without unhandled errors.")
        ac.append("All step outputs verified against StepVerifier assertions.")

        status = (
            BriefingStatus.REQUIRES_CLARIFICATION
            if clarification_questions
            else BriefingStatus.AWAITING_CONFIRMATION
        )

        return ContextBriefing(
            briefing_id=str(uuid4()),
            task_id=task_id,
            goal=user_goal,
            deliverables=list(deliverables or []),
            requirements=list(initial_requirements or []),
            constraints=list(initial_constraints or context_ir.constraints),
            deadline_utc=None,
            provenance_items=provenance_items,
            relevant_memories=list(context_ir.relevant_preferences)
            + list(context_ir.relevant_facts),
            relevant_lessons=lesson_summaries,
            available_capabilities=capabilities,
            assumptions=assumptions,
            uncertainties=uncertainties,
            proposed_plan=plan_steps,
            acceptance_criteria=ac,
            risks=risks,
            required_approvals=approvals,
            budgets=budgets or ExecutionBudget(),
            status=status,
            clarification_questions=clarification_questions,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def confirm(
        self,
        briefing: ContextBriefing,
        confirmation_note: Optional[str] = None,
    ) -> ContextBriefing:
        """Mark a briefing as confirmed by the user, unlocking execution."""
        if briefing.status in (BriefingStatus.CANCELLED, BriefingStatus.REQUIRES_CLARIFICATION):
            raise InvalidConfirmationStateError(
                f"Cannot confirm briefing in state '{briefing.status.value}'. "
                "Resolve uncertainties or open clarification questions first."
            )

        if confirmation_note:
            _check_no_secrets(confirmation_note)

        now = _now_utc()
        return briefing.model_copy(
            update={
                "status": BriefingStatus.CONFIRMED,
                "user_feedback": confirmation_note or "Confirmed by user.",
                "updated_at": now,
            }
        )

    def correct(
        self,
        briefing: ContextBriefing,
        correction_text: str,
        updated_steps: Optional[Sequence[StepPlan]] = None,
        updated_deliverables: Optional[Sequence[str]] = None,
    ) -> ContextBriefing:
        """Apply user correction to briefing, resetting to AWAITING_CONFIRMATION.

        Increments version and preserves user correction in provenance.
        """
        _check_no_secrets(correction_text)
        now = _now_utc()

        new_provenance = list(briefing.provenance_items)
        new_provenance.append(
            ContextProvenanceItem(
                content=f"User correction (v{briefing.version + 1}): {correction_text}",
                provenance=ProvenanceType.USER_PROVIDED,
                source_reference="user_correction",
                confidence=1.0,
            )
        )

        new_requirements = list(briefing.requirements)
        new_requirements.append(f"Correction: {correction_text}")

        new_plan = (
            list(updated_steps) if updated_steps is not None else list(briefing.proposed_plan)
        )
        new_deliv = (
            list(updated_deliverables)
            if updated_deliverables is not None
            else list(briefing.deliverables)
        )

        return briefing.model_copy(
            update={
                "version": briefing.version + 1,
                "status": BriefingStatus.AWAITING_CONFIRMATION,
                "requirements": new_requirements,
                "deliverables": new_deliv,
                "proposed_plan": new_plan,
                "provenance_items": new_provenance,
                "uncertainties": [
                    u for u in briefing.uncertainties if "Ambiguity detected" not in u
                ],
                "clarification_questions": [],
                "user_feedback": f"Corrected: {correction_text}",
                "updated_at": now,
            }
        )

    def add_context(
        self,
        briefing: ContextBriefing,
        new_context_items: Sequence[str],
        provenance: ProvenanceType = ProvenanceType.USER_PROVIDED,
    ) -> ContextBriefing:
        """Add context items to the briefing and reset to AWAITING_CONFIRMATION."""
        now = _now_utc()
        new_items = list(briefing.provenance_items)
        for item in new_context_items:
            _check_no_secrets(item)
            new_items.append(
                ContextProvenanceItem(
                    content=item,
                    provenance=provenance,
                    source_reference="supplemental_context",
                    confidence=1.0,
                )
            )

        return briefing.model_copy(
            update={
                "version": briefing.version + 1,
                "status": BriefingStatus.AWAITING_CONFIRMATION,
                "provenance_items": new_items,
                "uncertainties": [],
                "clarification_questions": [],
                "updated_at": now,
            }
        )

    def cancel(
        self,
        briefing: ContextBriefing,
        reason: str,
    ) -> ContextBriefing:
        """Cancel the briefing before execution begins."""
        _check_no_secrets(reason)
        now = _now_utc()
        return briefing.model_copy(
            update={
                "status": BriefingStatus.CANCELLED,
                "user_feedback": reason,
                "updated_at": now,
            }
        )
