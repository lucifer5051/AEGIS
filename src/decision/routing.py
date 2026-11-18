"""SAGE Fast Decision Engine -- Runtime Execution Routing Contract.

Document Reference: docs/specifications/AEGIS_FAST_DECISION_ENGINE.md
Canonical Reference: ARCH-060-02 Remediation

Connects advisory FastDecisionResult to actual execution orchestration:
1. Strategy Selection: Determines the execution approach (e.g. DIRECT_SKILL vs STRUCTURED_PLAN).
2. Model Routing: Resolves an eligible reasoning provider based on intent and availability.
3. Skill/Tool Scoping: Discovers matching registered skills and tool capabilities.
4. Authorization Invariant: Routing decisions NEVER authorize actions. All tool invocations
   must traverse ToolBroker and TrustCore.
"""

from __future__ import annotations

import logging
import re
from enum import Enum, unique
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.decision.models import (
    DecisionOutcome,
    DecisionRoute,
    FallbackReason,
    FastDecisionResult,
)
from src.reasoning.base import LocalReasoningProvider

logger = logging.getLogger(__name__)


# ==============================================================================
# ENUMERATIONS & MODELS
# ==============================================================================


@unique
class ExecutionStrategy(str, Enum):
    """Execution approach resolved from advisory fast decision and runtime state."""

    DIRECT_SKILL = "DIRECT_SKILL"
    STRUCTURED_PLAN = "STRUCTURED_PLAN"
    RESEARCH_RETRIEVAL = "RESEARCH_RETRIEVAL"


class RoutingDecision(BaseModel):
    """Validated, typed routing decision linking advisory classification to runtime orchestration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique UUIDv4 identifier for routing correlation.",
    )
    strategy: ExecutionStrategy = Field(
        ...,
        description="Resolved runtime execution strategy.",
    )
    route: DecisionRoute = Field(
        ...,
        description="Identified user intent route.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score associated with the route classification.",
    )
    selected_provider_id: Optional[str] = Field(
        default=None,
        description="Identifier of the reasoning provider selected for downstream execution.",
    )
    selected_skill_id: Optional[str] = Field(
        default=None,
        description="Identifier of the registered skill selected for direct execution, if any.",
    )
    rationale: str = Field(
        ...,
        description="Deterministic justification explaining strategy and provider resolution.",
    )
    is_advisory_only: Literal[True] = Field(
        default=True,
        description="Inviolable invariant: routing decisions are advisory and never authorize execution.",
    )
    fallback_reason: Optional[FallbackReason] = Field(
        default=None,
        description="Structured fallback reason if fallback path was selected.",
    )


# ==============================================================================
# EXECUTION ROUTER
# ==============================================================================


class ExecutionRouter:
    """Translates advisory FastDecisionResult into concrete runtime execution choices.

    Invariants:
    1. Zero Authorization Authority: Routing selects strategies and models, but NEVER grants
       capabilities or bypasses TrustCore.
    2. Fail-Closed / Fail-Safe: Malformed, unknown, or missing decisions fall back safely to
       the deterministic Manager planning loop or return a clear blocked result.
    3. Availability Awareness: Providers are checked for readiness (`is_ready()`) before selection.
       Unavailable models trigger configured fallbacks or safe rejection.
    """

    def __init__(
        self,
        skill_registry: Optional[Any] = None,
        reasoning_providers: Optional[Dict[str, LocalReasoningProvider]] = None,
        default_provider_id: Optional[str] = None,
    ) -> None:
        self._skills = skill_registry
        self._providers = dict(reasoning_providers) if reasoning_providers else {}
        self._default_provider_id = default_provider_id

    def validate_decision(self, decision: Any) -> Optional[FastDecisionResult]:
        """Validate that a decision object conforms strictly to FastDecisionResult."""
        if decision is None:
            return None
        if not isinstance(decision, FastDecisionResult):
            logger.warning("Invalid decision object received of type: %s", type(decision))
            return None
        if not getattr(decision, "is_advisory_only", False):
            logger.error("Security violation: FastDecisionResult violates advisory-only invariant.")
            return None
        return decision

    def resolve_strategy(
        self,
        goal_text: str,
        decision: Optional[FastDecisionResult],
        lessons: Optional[Sequence[Any]] = None,
    ) -> Tuple[ExecutionStrategy, Optional[str], str]:
        """Resolve the operational execution strategy from validated decision and available capabilities.

        Returns:
            Tuple of (strategy, selected_skill_id, rationale).
        """
        valid_decision = self.validate_decision(decision)
        if valid_decision is None:
            return (
                ExecutionStrategy.STRUCTURED_PLAN,
                None,
                "No valid fast decision provided; falling back to structured planner.",
            )

        # 0. Check advisory operational lessons for caution signals
        if lessons:
            goal_lower = goal_text.lower()
            for les in lessons:
                trigger = getattr(les, "trigger_condition", "")
                rec = getattr(les, "recommended_action", "")
                title = getattr(les, "title", "")
                combined = f"{trigger} {rec} {title}".lower()
                cautionary = any(
                    kw in combined
                    for kw in ("plan", "structured", "caution", "avoid direct", "require verification")
                )
                if cautionary:
                    trigger_words = [w for w in re.findall(r"\w+", trigger.lower()) if len(w) > 3]
                    if any(tw in goal_lower for tw in trigger_words):
                        return (
                            ExecutionStrategy.STRUCTURED_PLAN,
                            None,
                            f"Advisory operational lesson '{title}' recommends structured planning for matching trigger '{trigger}'.",
                        )

        # 1. Fast-path intent: Check for a matching registered skill
        if valid_decision.outcome == DecisionOutcome.FAST_PATH:
            route_val = (
                valid_decision.route.value
                if hasattr(valid_decision.route, "value")
                else str(valid_decision.route)
            )

            if self._skills is not None:
                # Search by intent route tag first
                candidates = self._skills.search_skills(tags=[route_val])
                if not candidates:
                    # Search by intent text
                    candidates = self._skills.search_skills(intent=goal_text)

                if candidates:
                    selected_skill = candidates[0]
                    skill_id = selected_skill.manifest.skill_id
                    return (
                        ExecutionStrategy.DIRECT_SKILL,
                        skill_id,
                        f"Fast-path route '{route_val}' (conf={valid_decision.confidence:.2f}) "
                        f"matched registered skill '{skill_id}'.",
                    )


            # If fast-path intent has no registered skill, proceed to planning
            return (
                ExecutionStrategy.STRUCTURED_PLAN,
                None,
                f"Fast-path route '{route_val}' (conf={valid_decision.confidence:.2f}) "
                f"has no registered direct skill; routing to structured planner.",
            )

        # 2. Memory / Knowledge retrieval intent
        if valid_decision.route == DecisionRoute.MEMORY_INTENT:
            return (
                ExecutionStrategy.RESEARCH_RETRIEVAL,
                None,
                f"Decision routed to memory retrieval (conf={valid_decision.confidence:.2f}).",
            )

        # 3. Standard fallback to Manager loop
        reason_str = (
            valid_decision.fallback_reason.value
            if valid_decision.fallback_reason and hasattr(valid_decision.fallback_reason, "value")
            else str(valid_decision.fallback_reason or "unspecified")
        )
        return (
            ExecutionStrategy.STRUCTURED_PLAN,
            None,
            f"Manager reasoning loop selected (fallback_reason={reason_str}).",
        )

    def resolve_reasoning_provider(
        self,
        route: Any,
        fallback_default: Optional[LocalReasoningProvider] = None,
    ) -> Tuple[Optional[LocalReasoningProvider], Optional[str], Optional[str]]:
        """Resolve an eligible, available reasoning provider for the given intent route.

        Returns:
            Tuple of (provider_instance, provider_id, error_message_if_blocked).
        """
        if not self._providers:
            if fallback_default is not None:
                p_id = getattr(fallback_default, "provider_id", "default")
                if fallback_default.is_ready():
                    return (fallback_default, p_id, None)
                return (None, p_id, f"Default reasoning provider '{p_id}' is not ready.")
            return (None, None, "No reasoning providers configured.")

        # Determine target provider key from route mapping or default
        route_str = route.value if hasattr(route, "value") else str(route)
        target_key: Optional[str] = None

        # Check explicit route-to-provider mappings
        if route_str in self._providers:
            target_key = route_str
        elif route_str == DecisionRoute.REASONING_INTENT.value and "reasoning" in self._providers:
            target_key = "reasoning"
        elif route_str == DecisionRoute.TERMINAL_INTENT.value and "code" in self._providers:
            target_key = "code"
        elif route_str == DecisionRoute.FILE_INTENT.value and "fast" in self._providers:
            target_key = "fast"
        elif self._default_provider_id and self._default_provider_id in self._providers:
            target_key = self._default_provider_id
        elif "primary" in self._providers:
            target_key = "primary"
        else:
            # First registered provider
            target_key = next(iter(self._providers))

        provider = self._providers.get(target_key)
        if provider is not None and provider.is_ready():
            return (provider, target_key, None)


        # Provider unavailable: Attempt safe fallback to primary/default
        fallback_candidates = [
            self._default_provider_id,
            "primary",
            "fallback",
        ]
        for fb_key in fallback_candidates:
            if fb_key and fb_key != target_key and fb_key in self._providers:
                fb_provider = self._providers[fb_key]
                if fb_provider.is_ready():
                    logger.warning(
                        "Target provider '%s' unavailable; falling back to '%s'.",
                        target_key,
                        fb_key,
                    )
                    return (fb_provider, fb_key, None)

        if fallback_default is not None and fallback_default.is_ready():
            logger.warning(
                "Target provider '%s' unavailable; falling back to default supervisor provider.",
                target_key,
            )
            return (
                fallback_default,
                getattr(fallback_default, "provider_id", "default"),
                None,
            )

        # No ready provider found: fail-closed
        return (
            None,
            target_key,
            f"Selected reasoning provider '{target_key}' is unavailable and no ready fallback exists.",
        )

    def route(
        self,
        goal_text: str,
        decision: Optional[FastDecisionResult],
        default_provider: Optional[LocalReasoningProvider] = None,
        lessons: Optional[Sequence[Any]] = None,
    ) -> Tuple[RoutingDecision, Optional[LocalReasoningProvider], Optional[str]]:
        """Perform end-to-end routing triage for an incoming goal.

        Returns:
            Tuple of (RoutingDecision, resolved_provider, blocking_error_if_any).
        """
        valid_decision = self.validate_decision(decision)

        # Fallback values if decision was None or invalid
        route_enum = valid_decision.route if valid_decision else DecisionRoute.UNKNOWN_INTENT
        confidence_val = valid_decision.confidence if valid_decision else 0.0
        fallback_rsn = valid_decision.fallback_reason if valid_decision else FallbackReason.LOW_CONFIDENCE

        strategy, skill_id, rationale = self.resolve_strategy(goal_text, valid_decision, lessons=lessons)

        provider = None
        provider_id = None
        blocking_error = None

        if strategy in (ExecutionStrategy.STRUCTURED_PLAN, ExecutionStrategy.RESEARCH_RETRIEVAL):
            provider, provider_id, blocking_error = self.resolve_reasoning_provider(
                route=route_enum,
                fallback_default=default_provider,
            )

        routing_decision = RoutingDecision(
            strategy=strategy,
            route=route_enum,
            confidence=confidence_val,
            selected_provider_id=provider_id,
            selected_skill_id=skill_id,
            rationale=rationale,
            is_advisory_only=True,
            fallback_reason=fallback_rsn if strategy == ExecutionStrategy.STRUCTURED_PLAN else None,
        )

        return (routing_decision, provider, blocking_error)
