"""SAGE Fast Decision Engine - Service & Orchestration Layer.

Document Reference: docs/specifications/AEGIS_FAST_DECISION_ENGINE.md
Constraints:
- Fast Decision is STRICTLY ADVISORY. It has ZERO authority to execute,
  approve, or bypass Trust Core.
- Maximum sequence budget is hard-capped at 2048 tokens; input over budget returns fallback.
- Never silently truncate user input to force fast-path execution.
- Decision criteria and route schemas are immutable and internally defined.
- Confidence thresholds are route-specific qualification parameters, not hardcoded assumptions.
- Telemetry streams must never record raw user prompt text or credentials.
- Fallback returns advisory FALLBACK_TO_MANAGER without invoking the unbuilt Manager runtime.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.decision.datasets import (
    ADVERSARIAL_EDGE_TEST_SET,
    GOLDEN_ROUTING_QUALIFICATION_SET,
)
from src.decision.errors import (
    AdmissionBusyError,
)
from src.decision.models import (
    DecisionOutcome,
    DecisionRoute,
    FallbackReason,
    FastDecisionRequest,
    FastDecisionResult,
)
from src.decision.providers.base import FastDecisionProvider

logger = logging.getLogger(__name__)

# ==============================================================================
# CANONICAL ROUTING CRITERIA (IMMUTABLE)
# ==============================================================================

CANONICAL_INTENT_CRITERIA: Dict[DecisionRoute, str] = {
    DecisionRoute.BROWSER_INTENT: (
        "Web search, web page viewing, external documentation lookup, or browser-based navigation."
    ),
    DecisionRoute.FILE_INTENT: (
        "Reading, writing, creating, inspecting, or searching "
        "local workspace files and directories."
    ),
    DecisionRoute.TERMINAL_INTENT: (
        "Running command-line tools, executing shell scripts, "
        "testing build commands, or terminal checks."
    ),
    DecisionRoute.MEMORY_INTENT: (
        "Recalling user preferences, historical project facts, past conversation context, or notes."
    ),
    DecisionRoute.COMMUNICATION_INTENT: (
        "Drafting messages, sending desktop alerts, or communicating "
        "via configured external channels."
    ),
    DecisionRoute.SETTINGS_INTENT: (
        "Modifying SAGE configuration parameters, runtime policies, log levels, or system settings."
    ),
    DecisionRoute.REASONING_INTENT: (
        "Complex problem analysis, architectural design, "
        "multi-step planning, or open-ended synthesis."
    ),
}

# Default qualification acceptance thresholds per intent
DEFAULT_QUALIFIED_THRESHOLDS: Dict[DecisionRoute, float] = {
    DecisionRoute.BROWSER_INTENT: 0.75,
    DecisionRoute.FILE_INTENT: 0.75,
    DecisionRoute.TERMINAL_INTENT: 0.80,
    DecisionRoute.MEMORY_INTENT: 0.75,
    DecisionRoute.COMMUNICATION_INTENT: 0.80,
    DecisionRoute.SETTINGS_INTENT: 0.80,
    DecisionRoute.REASONING_INTENT: 0.85,
    DecisionRoute.UNKNOWN_INTENT: 1.00,  # Unknown always falls back
}


class FastDecisionService:
    """Orchestrates fast-path decision routing across providers with deterministic safety bounds."""

    def __init__(
        self,
        provider: Optional[FastDecisionProvider] = None,
        qualified_thresholds: Optional[Dict[DecisionRoute, float]] = None,
        criteria: Optional[Dict[DecisionRoute, str]] = None,
    ) -> None:
        self.provider = provider
        self.qualified_thresholds = (
            dict(qualified_thresholds)
            if qualified_thresholds
            else dict(DEFAULT_QUALIFIED_THRESHOLDS)
        )
        # Criteria are frozen and immutable
        self._criteria = dict(criteria or CANONICAL_INTENT_CRITERIA)

    @property
    def criteria(self) -> Dict[DecisionRoute, str]:
        """Return a copy of the canonical, immutable routing criteria."""
        return dict(self._criteria)

    def estimate_token_count(self, text: str) -> int:
        """Estimate token count conservatively without external tokenizer dependencies.

        Uses max of whitespace/word tokenization and character-length heuristic (~4 chars/token).
        """
        if not text:
            return 0
        word_count = len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE))
        char_count = len(text)
        char_est = (char_count + 3) // 4
        return max(word_count, char_est)

    def evaluate(self, request: FastDecisionRequest) -> FastDecisionResult:
        """Evaluate a user request through the Fast Decision Engine.

        Execution Pipeline:
        1. Token budget ceiling check (max 2048). Over-budget returns fallback
           (no silent truncation).
        2. Provider availability check. Missing/disabled provider returns fallback.
        3. Provider evaluation over immutable canonical criteria.
        4. Admission control: non-blocking; busy state falls back immediately.
        5. Calibration check: confidence compared against route-specific acceptance threshold.
        6. Return typed advisory result.

        Zero Secret Invariant: Logs never contain raw user prompts or credentials.
        """
        decision_id = str(uuid4())
        t_start = time.perf_counter()

        # Step 1: Sequence token budget enforcement
        effective_budget = request.effective_budget
        est_tokens = self.estimate_token_count(request.user_input)

        if est_tokens > effective_budget:
            latency_ms = (time.perf_counter() - t_start) * 1000.0
            logger.info(
                "Decision %s: token budget exceeded (est=%d, max=%d); returning fallback.",
                decision_id,
                est_tokens,
                effective_budget,
            )
            return FastDecisionResult(
                decision_id=decision_id,
                outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
                route=DecisionRoute.UNKNOWN_INTENT,
                confidence=0.0,
                alternatives=[],
                provider_id=self.provider.provider_id if self.provider else "none",
                provider_version=self.provider.provider_version if self.provider else "0.0.0",
                latency_ms=latency_ms,
                fallback_reason=FallbackReason.TOKEN_BUDGET_EXCEEDED,
                is_advisory_only=True,
            )

        # Step 2: Provider readiness check
        if self.provider is None or not self.provider.is_ready():
            latency_ms = (time.perf_counter() - t_start) * 1000.0
            logger.info(
                "Decision %s: provider unavailable or not ready; returning fallback.",
                decision_id,
            )
            return FastDecisionResult(
                decision_id=decision_id,
                outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
                route=DecisionRoute.UNKNOWN_INTENT,
                confidence=0.0,
                alternatives=[],
                provider_id=self.provider.provider_id if self.provider else "none",
                provider_version=self.provider.provider_version if self.provider else "0.0.0",
                latency_ms=latency_ms,
                fallback_reason=FallbackReason.PROVIDER_UNAVAILABLE,
                is_advisory_only=True,
            )

        # Step 3: Provider evaluation
        try:
            raw_result = self.provider.evaluate(
                request=request,
                criteria=self._criteria,
            )
        except AdmissionBusyError:
            latency_ms = (time.perf_counter() - t_start) * 1000.0
            logger.info("Decision %s: admission busy; immediate fallback.", decision_id)
            return FastDecisionResult(
                decision_id=decision_id,
                outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
                route=DecisionRoute.UNKNOWN_INTENT,
                confidence=0.0,
                alternatives=[],
                provider_id=self.provider.provider_id,
                provider_version=self.provider.provider_version,
                latency_ms=latency_ms,
                fallback_reason=FallbackReason.ADMISSION_BUSY,
                is_advisory_only=True,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - t_start) * 1000.0
            logger.error("Decision %s: provider evaluation error: %s", decision_id, exc)
            return FastDecisionResult(
                decision_id=decision_id,
                outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
                route=DecisionRoute.UNKNOWN_INTENT,
                confidence=0.0,
                alternatives=[],
                provider_id=self.provider.provider_id,
                provider_version=self.provider.provider_version,
                latency_ms=latency_ms,
                fallback_reason=FallbackReason.INFERENCE_EXCEPTION,
                is_advisory_only=True,
            )

        # If provider itself emitted a fallback outcome
        if raw_result.outcome == DecisionOutcome.FALLBACK_TO_MANAGER:
            return raw_result

        # Step 4: Calibrated route threshold evaluation
        route = raw_result.route
        conf = raw_result.confidence
        min_threshold = self.qualified_thresholds.get(route, 0.70)

        if route == DecisionRoute.UNKNOWN_INTENT or conf < min_threshold:
            logger.info(
                "Decision %s: route '%s' confidence (%.2f) below threshold (%.2f); "
                "fallback to Manager.",
                decision_id,
                route.value if hasattr(route, "value") else route,
                conf,
                min_threshold,
            )
            return FastDecisionResult(
                decision_id=decision_id,
                outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
                route=route,
                confidence=conf,
                alternatives=raw_result.alternatives,
                provider_id=raw_result.provider_id,
                provider_version=raw_result.provider_version,
                latency_ms=raw_result.latency_ms,
                fallback_reason=FallbackReason.LOW_CONFIDENCE,
                is_advisory_only=True,
            )

        # Step 5: Fast-path route accepted
        return FastDecisionResult(
            decision_id=decision_id,
            outcome=DecisionOutcome.FAST_PATH,
            route=route,
            confidence=conf,
            alternatives=raw_result.alternatives,
            provider_id=raw_result.provider_id,
            provider_version=raw_result.provider_version,
            latency_ms=raw_result.latency_ms,
            fallback_reason=None,
            is_advisory_only=True,
        )

    def qualify_provider(
        self,
        provider: FastDecisionProvider,
        golden_dataset: Optional[List[Dict[str, str]]] = None,
        adversarial_dataset: Optional[List[Dict[str, str]]] = None,
        hardware_profile: Optional[Any] = None,
        min_accuracy: float = 0.80,
    ) -> bool:
        """Run formal provider qualification using frozen datasets and measured hardware state.

        Requirements for READY state:
        1. Model integrity verification passes (SHA-256 match).
        2. Hardware qualification passes (measured RAM fits safely).
        3. Accuracy on GOLDEN_ROUTING_QUALIFICATION_SET >= min_accuracy.
        4. 100% of ADVERSARIAL_EDGE_TEST_SET correctly fall back to Manager.
        5. Zero authorization or tool bypass capability.

        Returns:
            True if provider qualifies and is marked READY, False otherwise.
        """
        if not provider.initialize():
            logger.warning("Qualification failed: provider initialization failed.")
            return False

        if not provider.qualify(hardware_profile):
            logger.warning("Qualification failed: hardware qualification failed.")
            return False

        golden_set = golden_dataset or GOLDEN_ROUTING_QUALIFICATION_SET
        adversarial_set = adversarial_dataset or ADVERSARIAL_EDGE_TEST_SET

        # 1. Evaluate golden qualification set
        correct_count = 0
        for item in golden_set:
            req = FastDecisionRequest(user_input=item["input"])
            res = provider.evaluate(req, criteria=self._criteria)
            expected_route = item["expected_route"]
            actual_route = res.route.value if hasattr(res.route, "value") else str(res.route)
            if actual_route == expected_route:
                correct_count += 1

        accuracy = correct_count / len(golden_set) if golden_set else 0.0
        logger.info(
            "Golden qualification accuracy: %.2f%% (%d/%d)",
            accuracy * 100.0,
            correct_count,
            len(golden_set),
        )

        if accuracy < min_accuracy:
            logger.warning(
                "Qualification failed: accuracy (%.2f) below minimum threshold (%.2f)",
                accuracy,
                min_accuracy,
            )
            provider.unload()
            return False

        # 2. Evaluate adversarial edge set (must all fall back to Manager)
        eval_service = FastDecisionService(
            provider=provider,
            qualified_thresholds=self.qualified_thresholds,
            criteria=self._criteria,
        )
        for item in adversarial_set:
            req = FastDecisionRequest(user_input=item["input"])
            res = eval_service.evaluate(req)
            # Security invariant: injection/ambiguous input must never execute fast path directly
            if res.outcome != DecisionOutcome.FALLBACK_TO_MANAGER:
                logger.warning(
                    "Qualification failed: adversarial input '%s' "
                    "not routed to FALLBACK_TO_MANAGER",
                    item.get("category", "adversarial"),
                )
                provider.unload()
                return False

        logger.info("Provider '%s' successfully qualified.", provider.provider_id)
        return True
