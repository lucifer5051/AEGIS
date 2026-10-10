"""SAGE Error Classification & Research Loop Subsystem.

Canonical Reference:
    PROMPT 029: Persistent Completion Mode Error Investigation & Research
    Principle: CLASSIFY -> INVESTIGATE -> RESEARCH IF NEEDED -> FORM HYPOTHESIS
               -> CHOOSE RECOVERY -> REAUTHORIZE -> RETRY/REPLAN -> VERIFY

Invariants:
- All models are frozen and immutable (extra="forbid").
- Zero-Secret Invariant: Error signatures and research records must never store credentials.
- Research outputs are strictly advisory evidence -- never autonomous execution instructions.
- Research cannot grant authorization or override Trust Core boundaries.
- Offline-by-construction: Providers support local offline documentation & deterministic fixtures.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.manager.models import StepPlan
from src.trust.audit import _SECRET_PATTERNS, _check_no_secrets

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# ==============================================================================
# ENUMERATIONS
# ==============================================================================


@unique
class ErrorClass(str, Enum):
    """Classification of tool and execution failures."""

    TRANSIENT = "TRANSIENT"  # Timeouts, busy locks, network blip, temporary retryable
    ENVIRONMENT_RESOURCE = "ENVIRONMENT_RESOURCE"  # Memory, disk, missing file/directory
    CONFIGURATION_PARAM = "CONFIGURATION_PARAM"  # Bad type, missing required key, invalid value
    PERMISSION_AUTH = "PERMISSION_AUTH"  # Capability denied, policy restriction, approval needed
    LOGIC_STRATEGY = (
        "LOGIC_STRATEGY"  # Output verification failed, assertion error, incorrect result
    )
    TOOL_INTERFACE = "TOOL_INTERFACE"  # Tool unregistered, bad signature, contract violation
    UNKNOWN = "UNKNOWN"


@unique
class RecoveryStrategyType(str, Enum):
    """Taxonomy of recovery actions available to the persistent loop."""

    RETRY_WITH_BACKOFF = "RETRY_WITH_BACKOFF"
    PARAM_ADJUSTMENT = "PARAM_ADJUSTMENT"
    ALTERNATIVE_TOOL = "ALTERNATIVE_TOOL"
    ESCALATE_APPROVAL = "ESCALATE_APPROVAL"
    REPLAN = "REPLAN"
    ABORT_BLOCKER = "ABORT_BLOCKER"


# ==============================================================================
# DATA MODELS
# ==============================================================================


class ErrorSignature(BaseModel):
    """Normalized fingerprint of an execution failure for deduplication and stuck-loop detection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_name: str
    tool_name: Optional[str] = None
    error_type: str = "ExecutionError"
    normalized_pattern: str = ""

    def key(self) -> str:
        """Produce a canonical string key for fingerprint comparisons."""
        tool = self.tool_name or "none"
        return f"{tool}:{self.action_name}:{self.error_type}:{self.normalized_pattern}"


class ResearchRecord(BaseModel):
    """Documented evidence from inspecting local or authoritative reference sources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    research_id: str = Field(description="Unique research query record UUID.")
    query: str = Field(description="Normalized research query text.")
    source_references: List[str] = Field(
        default_factory=list, description="Authoritative reference URLs or local doc paths."
    )
    findings_summary: str = Field(description="Extracted empirical finding or recommendation.")
    suggested_adjustments: Dict[str, Any] = Field(
        default_factory=dict, description="Recommended parameter or strategy adjustments."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Epistemic confidence.")
    created_at: str = Field(description="ISO-8601 UTC timestamp.")


class ErrorDiagnosis(BaseModel):
    """Complete diagnostic hypothesis and recovery prescription for a step failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnosis_id: str = Field(description="Unique diagnosis UUID.")
    step_id: str = Field(description="Failing step UUID.")
    step_index: int = Field(description="Step index in task.")
    error_signature: str = Field(description="Normalized error key.")
    error_class: ErrorClass = Field(description="Classified failure category.")
    suspected_cause: str = Field(description="Hypothesis explaining root cause.")
    research_record: Optional[ResearchRecord] = Field(
        default=None, description="Supporting research evidence, if conducted."
    )
    chosen_strategy: RecoveryStrategyType = Field(description="Prescribed recovery strategy.")
    proposed_step: Optional[StepPlan] = Field(
        default=None, description="Replacement or recovery step plan."
    )
    recovery_arguments: Dict[str, Any] = Field(
        default_factory=dict, description="Adjusted arguments for retry."
    )
    requires_user_reconfirmation: bool = Field(
        default=False, description="True if recovery materially changes task scope."
    )
    reconfirmation_reason: Optional[str] = Field(
        default=None, description="Explanation why user reconfirmation is required."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Diagnostic confidence.")
    created_at: str = Field(description="ISO-8601 UTC timestamp.")


# ==============================================================================
# RESEARCH PROVIDER INTERFACE & OFFLINE IMPLEMENTATION
# ==============================================================================


class ResearchProvider(ABC):
    """Pluggable provider for looking up reference documentation and recovery guidance."""

    @abstractmethod
    def research(
        self,
        query: str,
        error_class: ErrorClass,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ResearchRecord]:
        """Perform lookup against reference documentation and return structured evidence."""


class LocalDocResearchProvider(ResearchProvider):
    """Deterministic, 100% offline research provider backed by an in-memory knowledge table."""

    def __init__(self, knowledge_base: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._kb = knowledge_base or self._build_default_kb()

    def _build_default_kb(self) -> Dict[str, Dict[str, Any]]:
        return {
            "timeout": {
                "source": "docs/specifications/AEGIS_PERFORMANCE_METRICS_SPECIFICATION.md § 4",
                "finding": "Step timed out; recommend RETRY_WITH_BACKOFF with elevated timeout.",
                "adjustments": {"timeout_seconds": 60},
                "confidence": 0.90,
            },
            "file_not_found": {
                "source": "docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12",
                "finding": "Target directory or file missing; check path or create directory.",
                "adjustments": {"create_parents": True},
                "confidence": 0.95,
            },
            "policy_denied": {
                "source": "docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 2",
                "finding": "Action denied by Trust Core autonomy tier; requires user approval.",
                "adjustments": {"requires_approval": True},
                "confidence": 1.0,
            },
            "type_error": {
                "source": "docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 8",
                "finding": "Tool parameter type mismatch; coerce argument to expected type.",
                "adjustments": {},
                "confidence": 0.85,
            },
        }

    def register_entry(
        self,
        keyword: str,
        source: str,
        finding: str,
        adjustments: Optional[Dict[str, Any]] = None,
        confidence: float = 1.0,
    ) -> None:
        """Register a deterministic test or domain reference entry."""
        self._kb[keyword.lower()] = {
            "source": source,
            "finding": finding,
            "adjustments": adjustments or {},
            "confidence": confidence,
        }

    def research(
        self,
        query: str,
        error_class: ErrorClass,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ResearchRecord]:
        """Search local offline reference knowledge base for relevant recovery guidance."""
        _check_no_secrets(query)
        q_lower = query.lower()
        now = _now_utc()

        for key, entry in self._kb.items():
            if key in q_lower:
                return ResearchRecord(
                    research_id=str(uuid4()),
                    query=query,
                    source_references=[entry["source"]],
                    findings_summary=entry["finding"],
                    suggested_adjustments=entry.get("adjustments", {}),
                    confidence=entry.get("confidence", 1.0),
                    created_at=now,
                )

        return None


# ==============================================================================
# ERROR CLASSIFIER & INVESTIGATOR
# ==============================================================================


class ErrorClassifier:
    """Classifies execution errors into standard taxonomy based on deterministic heuristics."""

    _PATTERNS = [
        (
            ErrorClass.PERMISSION_AUTH,
            re.compile(
                r"(permission|denied|unauthorized|forbidden|trust core|"
                r"capability|approval required)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.TRANSIENT,
            re.compile(
                r"(timeout|timed out|busy|lock|temporarily unavailable|connection reset)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.ENVIRONMENT_RESOURCE,
            re.compile(
                r"(no such file|not found|disk full|out of memory|vram|insufficient space)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.CONFIGURATION_PARAM,
            re.compile(
                r"(valueerror|typeerror|invalid argument|missing required|"
                r"coercion|unknown parameter)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.TOOL_INTERFACE,
            re.compile(
                r"(tool not registered|unregistered tool|unknown tool|schema mismatch)",
                re.IGNORECASE,
            ),
        ),
        (
            ErrorClass.LOGIC_STRATEGY,
            re.compile(
                r"(assertion|verification failed|unexpected output|test failed)",
                re.IGNORECASE,
            ),
        ),
    ]

    @classmethod
    def classify(cls, error_text: str, error_type: Optional[str] = None) -> ErrorClass:
        """Classify error message into ErrorClass taxonomy."""
        combined = f"{error_type or ''} {error_text}".strip()
        for error_class, pattern in cls._PATTERNS:
            if pattern.search(combined):
                return error_class
        return ErrorClass.UNKNOWN

    @classmethod
    def extract_signature(
        cls,
        action_name: str,
        tool_name: Optional[str],
        error_type: Optional[str],
        error_summary: str,
    ) -> ErrorSignature:
        """Extract a normalized error signature for deduplication and stuck-loop detection."""
        # Strip secrets first to uphold zero-secret invariant
        normalized = error_summary
        for pat in _SECRET_PATTERNS:
            normalized = pat.sub("<secret>", normalized)
        normalized = re.sub(r"\b(?:sk|pk)[_-][A-Za-z0-9_-]+\b", "<secret>", normalized)
        normalized = re.sub(r"(?i)bearer\s+\S+", "Bearer <secret>", normalized)

        # Strip numbers, paths, and UUIDs to normalize the error pattern
        normalized = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", normalized
        )
        normalized = re.sub(r"[A-Za-z]:\\[\w\\.-]+", "<path>", normalized)
        normalized = re.sub(r"/[\w/.-]+", "<path>", normalized)
        normalized = re.sub(r"\d+", "<n>", normalized)
        norm_clean = normalized[:60].strip()

        return ErrorSignature(
            action_name=action_name,
            tool_name=tool_name,
            error_type=error_type or "ExecutionError",
            normalized_pattern=norm_clean,
        )


class DiagnosticInvestigator:
    """Investigates failing steps, local lessons, and research documentation to diagnose."""

    def __init__(
        self,
        research_provider: Optional[ResearchProvider] = None,
        lesson_manager: Optional[Any] = None,
    ) -> None:
        self._research = research_provider or LocalDocResearchProvider()
        self._lessons = lesson_manager

    def diagnose(
        self,
        step_id: str,
        step_index: int,
        step_plan: StepPlan,
        error_type: Optional[str],
        error_summary: str,
        consecutive_failures: int = 1,
        previous_strategies: Optional[List[RecoveryStrategyType]] = None,
    ) -> ErrorDiagnosis:
        """Analyze a failure and synthesize a structured diagnosis and recovery strategy.

        Consults:
        1. Error classification taxonomy
        2. Relevant approved lessons in LessonManager
        3. LocalDocResearchProvider reference documentation
        4. Stuck-loop and oscillation heuristics
        """
        _check_no_secrets(error_summary)
        now = _now_utc()

        err_class = ErrorClassifier.classify(error_summary, error_type)
        sig = ErrorClassifier.extract_signature(
            action_name=step_plan.action_name,
            tool_name=step_plan.tool_name,
            error_type=error_type,
            error_summary=error_summary,
        )

        # 1. Check applicable approved lessons
        relevant_lesson = None
        if self._lessons is not None:
            try:
                approved_lessons = self._lessons.get_approved_lessons()
                for les in approved_lessons:
                    if (
                        les.trigger_condition.lower() in error_summary.lower()
                        or step_plan.action_name.lower() in les.trigger_condition.lower()
                    ):
                        relevant_lesson = les
                        break
            except Exception as exc:
                logger.warning("Lesson lookup during diagnosis failed: %s", exc)

        # 2. Check research provider if error is unfamiliar or recurring
        research_rec: Optional[ResearchRecord] = None
        if self._research is not None:
            research_query = f"{step_plan.action_name} {err_class.value} {error_summary[:80]}"
            research_rec = self._research.research(
                query=research_query,
                error_class=err_class,
                context={"action_name": step_plan.action_name, "tool_name": step_plan.tool_name},
            )

        # 3. Determine recovery strategy
        suspected_cause = f"Failure categorized as {err_class.value}: {error_summary}"
        chosen_strategy = RecoveryStrategyType.RETRY_WITH_BACKOFF
        recovery_args = dict(step_plan.arguments)
        proposed_step: Optional[StepPlan] = None
        requires_reconfirmation = False
        reconfirmation_reason: Optional[str] = None

        if err_class == ErrorClass.PERMISSION_AUTH:
            chosen_strategy = RecoveryStrategyType.ESCALATE_APPROVAL
            requires_reconfirmation = True
            reconfirmation_reason = (
                f"Action '{step_plan.action_name}' was denied by Trust Core policy; "
                "user authorization approval is required."
            )
        elif err_class == ErrorClass.CONFIGURATION_PARAM:
            chosen_strategy = RecoveryStrategyType.PARAM_ADJUSTMENT
            if research_rec and research_rec.suggested_adjustments:
                recovery_args.update(research_rec.suggested_adjustments)
            suspected_cause = "Tool parameter invalid or missing; adjusted based on schema/docs."
        elif err_class == ErrorClass.TRANSIENT:
            chosen_strategy = RecoveryStrategyType.RETRY_WITH_BACKOFF
            if consecutive_failures > 2:
                # If transient error persists 3 times, switch strategy
                chosen_strategy = RecoveryStrategyType.REPLAN
                suspected_cause = (
                    "Transient failure did not resolve after multiple retries; replanning."
                )
        elif err_class in (ErrorClass.LOGIC_STRATEGY, ErrorClass.ENVIRONMENT_RESOURCE):
            if relevant_lesson is not None:
                suspected_cause = (
                    f"Guided by approved lesson [{relevant_lesson.lesson_id[:8]}]: "
                    f"{relevant_lesson.inferred_explanation}"
                )
                chosen_strategy = RecoveryStrategyType.PARAM_ADJUSTMENT
            elif consecutive_failures > 1:
                chosen_strategy = RecoveryStrategyType.REPLAN
                requires_reconfirmation = True
                reconfirmation_reason = (
                    f"Step '{step_plan.action_name}' failed verification repeatedly; "
                    "replanning with alternative strategy requires confirmation."
                )
            else:
                chosen_strategy = RecoveryStrategyType.PARAM_ADJUSTMENT

        # Oscillation check: if strategies are cycling or repeating after multiple attempts
        prev = previous_strategies or []
        if len(prev) >= 2 and (
            chosen_strategy in prev or (prev[-1] != chosen_strategy and prev[-2] == chosen_strategy)
        ):
            chosen_strategy = RecoveryStrategyType.ABORT_BLOCKER
            suspected_cause = (
                "Oscillating recovery strategies detected with no measurable progress."
            )
            requires_reconfirmation = True
            reconfirmation_reason = "Stuck loop detected: oscillating recovery strategies."

        return ErrorDiagnosis(
            diagnosis_id=str(uuid4()),
            step_id=step_id,
            step_index=step_index,
            error_signature=sig.key(),
            error_class=err_class,
            suspected_cause=suspected_cause,
            research_record=research_rec,
            chosen_strategy=chosen_strategy,
            proposed_step=proposed_step,
            recovery_arguments=recovery_args,
            requires_user_reconfirmation=requires_reconfirmation,
            reconfirmation_reason=reconfirmation_reason,
            confidence=0.90 if research_rec else 0.80,
            created_at=now,
        )
