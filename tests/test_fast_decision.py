"""AEGIS Fast Decision Engine - Service & Orchestration Test Suite.

Prompt: 014A
Scope: Validates provider abstraction, service orchestration, hard token budget ceiling (2048),
route intent immutability, qualification thresholds, non-blocking admission,
advisory-only invariants, adversarial edge handling, and zero-secret telemetry.
"""

from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

from src.decision.datasets import (
    ADVERSARIAL_EDGE_TEST_SET,
)
from src.decision.errors import AdmissionBusyError
from src.decision.models import (
    AlternativeRoute,
    DecisionOutcome,
    DecisionRoute,
    FallbackReason,
    FastDecisionRequest,
    FastDecisionResult,
    ProviderMetrics,
    ProviderStatus,
    RequestContext,
)
from src.decision.providers.base import FastDecisionProvider
from src.decision.service import (
    CANONICAL_INTENT_CRITERIA,
    DEFAULT_QUALIFIED_THRESHOLDS,
    FastDecisionService,
)


class MockFastDecisionProvider(FastDecisionProvider):
    """Deterministic mock provider for service orchestration tests."""

    def __init__(
        self,
        provider_id: str = "mock_provider",
        version: str = "1.0.0",
        default_route: DecisionRoute = DecisionRoute.FILE_INTENT,
        default_confidence: float = 0.95,
        should_fail_admission: bool = False,
    ) -> None:
        super().__init__(provider_id=provider_id, provider_version=version)
        self.default_route = default_route
        self.default_confidence = default_confidence
        self.should_fail_admission = should_fail_admission
        self._status = ProviderStatus.READY
        self.evaluate_calls: List[FastDecisionRequest] = []

    def initialize(self) -> bool:
        self._status = ProviderStatus.READY
        return True

    def is_ready(self) -> bool:
        return self._status == ProviderStatus.READY

    def evaluate(
        self,
        request: FastDecisionRequest,
        criteria: Dict[DecisionRoute, str],
    ) -> FastDecisionResult:
        self.evaluate_calls.append(request)
        if self.should_fail_admission:
            raise AdmissionBusyError("Mock admission lock occupied.")

        return FastDecisionResult(
            outcome=DecisionOutcome.FAST_PATH,
            route=self.default_route,
            confidence=self.default_confidence,
            alternatives=[AlternativeRoute(route=DecisionRoute.BROWSER_INTENT, confidence=0.04)],
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            latency_ms=1.5,
            fallback_reason=None,
            is_advisory_only=True,
        )

    def unload(self) -> None:
        self._status = ProviderStatus.UNINITIALIZED

    def get_metrics(self) -> ProviderMetrics:
        return ProviderMetrics(
            cold_load_time_ms=10.0,
            warm_inference_p50_ms=1.5,
            warm_inference_p95_ms=2.0,
            warm_inference_p99_ms=2.5,
            cpu_usage_pct=5.0,
            ram_usage_mb=120.0,
            inference_count=len(self.evaluate_calls),
        )

    def verify_integrity(self) -> bool:
        return True

    def qualify(self, hardware_profile: Any = None) -> bool:
        return True


def test_01_service_initialization_with_defaults():
    """Assert FastDecisionService initializes with canonical criteria and thresholds."""
    service = FastDecisionService()
    assert service.provider is None
    assert service.criteria == CANONICAL_INTENT_CRITERIA
    assert service.qualified_thresholds == DEFAULT_QUALIFIED_THRESHOLDS


def test_02_advisory_only_type_level_invariant():
    """Assert that is_advisory_only is strictly True at the type level and cannot be False."""
    res = FastDecisionResult(
        outcome=DecisionOutcome.FAST_PATH,
        route=DecisionRoute.FILE_INTENT,
        confidence=0.95,
        provider_id="test",
        provider_version="1.0",
        latency_ms=1.0,
        is_advisory_only=True,
    )
    assert res.is_advisory_only is True

    # Attempting to construct with is_advisory_only=False must raise validation error
    with pytest.raises(ValidationError):
        FastDecisionResult(
            outcome=DecisionOutcome.FAST_PATH,
            route=DecisionRoute.FILE_INTENT,
            confidence=0.95,
            provider_id="test",
            provider_version="1.0",
            latency_ms=1.0,
            is_advisory_only=False,  # type: ignore[arg-type]
        )


def test_03_criteria_immutability():
    """Assert criteria returned by service is a defensive copy and cannot be mutated externally."""
    service = FastDecisionService()
    crit = service.criteria
    crit[DecisionRoute.TERMINAL_INTENT] = "TAMPERED CRITERIA"
    assert service.criteria[DecisionRoute.TERMINAL_INTENT] != "TAMPERED CRITERIA"


def test_04_token_budget_ceiling_enforced():
    """Assert requests exceeding 2048-token ceiling trigger immediate fallback (no truncation)."""
    provider = MockFastDecisionProvider()
    service = FastDecisionService(provider=provider)

    # Generate a string that clearly exceeds 2048 tokens (> 2500 words)
    oversized_text = "word " * 2500
    req = FastDecisionRequest(user_input=oversized_text)

    res = service.evaluate(req)
    assert res.outcome == DecisionOutcome.FALLBACK_TO_MANAGER
    assert res.fallback_reason == FallbackReason.TOKEN_BUDGET_EXCEEDED
    assert res.confidence == 0.0
    # Provider must NOT have been called
    assert len(provider.evaluate_calls) == 0


def test_05_requested_budget_capping():
    """Assert caller-requested budget can lower the limit but cannot exceed 2048."""
    # Lower requested budget allowed
    req_low = FastDecisionRequest(user_input="test", requested_budget=512)
    assert req_low.effective_budget == 512

    # Request cannot exceed 2048 at model validation level
    with pytest.raises(ValidationError):
        FastDecisionRequest(user_input="test", requested_budget=10000)


def test_06_caller_lower_budget_exceeded():
    """Assert that exceeding a caller's lower requested budget triggers fallback."""
    provider = MockFastDecisionProvider()
    service = FastDecisionService(provider=provider)

    # 150 words exceeds requested budget of 100
    text = "hello world " * 75
    req = FastDecisionRequest(user_input=text, requested_budget=100)
    res = service.evaluate(req)

    assert res.outcome == DecisionOutcome.FALLBACK_TO_MANAGER
    assert res.fallback_reason == FallbackReason.TOKEN_BUDGET_EXCEEDED
    assert len(provider.evaluate_calls) == 0


def test_07_provider_unavailable_clean_fallback():
    """Assert that an uninitialized or absent provider returns FALLBACK_TO_MANAGER cleanly."""
    service = FastDecisionService(provider=None)
    req = FastDecisionRequest(user_input="Read src/main.py")
    res = service.evaluate(req)

    assert res.outcome == DecisionOutcome.FALLBACK_TO_MANAGER
    assert res.fallback_reason == FallbackReason.PROVIDER_UNAVAILABLE


def test_08_low_confidence_fallback():
    """Assert result with confidence below route threshold falls back to Manager."""
    # FILE_INTENT threshold is 0.75; provider returns 0.70
    provider = MockFastDecisionProvider(
        default_route=DecisionRoute.FILE_INTENT,
        default_confidence=0.70,
    )
    service = FastDecisionService(provider=provider)
    req = FastDecisionRequest(user_input="Maybe open a file?")
    res = service.evaluate(req)

    assert res.outcome == DecisionOutcome.FALLBACK_TO_MANAGER
    assert res.fallback_reason == FallbackReason.LOW_CONFIDENCE
    assert res.route == DecisionRoute.FILE_INTENT
    assert res.confidence == 0.70


def test_09_unknown_intent_always_falls_back():
    """Assert UNKNOWN_INTENT route always triggers FALLBACK_TO_MANAGER even with high confidence."""
    provider = MockFastDecisionProvider(
        default_route=DecisionRoute.UNKNOWN_INTENT,
        default_confidence=0.99,
    )
    service = FastDecisionService(provider=provider)
    req = FastDecisionRequest(user_input="random gibberish 12345")
    res = service.evaluate(req)

    assert res.outcome == DecisionOutcome.FALLBACK_TO_MANAGER
    assert res.fallback_reason == FallbackReason.LOW_CONFIDENCE


def test_10_non_blocking_admission_busy_fallback():
    """Assert admission collision immediately returns ADMISSION_BUSY fallback without blocking."""
    provider = MockFastDecisionProvider(should_fail_admission=True)
    service = FastDecisionService(provider=provider)
    req = FastDecisionRequest(user_input="Quick task")
    res = service.evaluate(req)

    assert res.outcome == DecisionOutcome.FALLBACK_TO_MANAGER
    assert res.fallback_reason == FallbackReason.ADMISSION_BUSY


def test_11_fast_path_success():
    """Assert valid input with high confidence produces FAST_PATH outcome."""
    provider = MockFastDecisionProvider(
        default_route=DecisionRoute.FILE_INTENT,
        default_confidence=0.92,
    )
    service = FastDecisionService(provider=provider)
    req = FastDecisionRequest(
        user_input="Read src/decision/models.py",
        context=RequestContext(channel="cli", project_scope="aegis-core"),
    )
    res = service.evaluate(req)

    assert res.outcome == DecisionOutcome.FAST_PATH
    assert res.route == DecisionRoute.FILE_INTENT
    assert res.confidence == 0.92
    assert res.fallback_reason is None
    assert res.is_advisory_only is True


def test_12_typed_request_context_forbids_arbitrary_metadata():
    """Assert RequestContext rejects arbitrary unexpected keys."""
    # Allowed attributes
    ctx = RequestContext(channel="desktop", project_scope="proj_1", input_kind="chat", locale="en")
    assert ctx.channel == "desktop"

    # Arbitrary keys are forbidden by extra="forbid"
    with pytest.raises(ValidationError):
        RequestContext(channel="desktop", extra_payload="arbitrary text")  # type: ignore[call-arg]


def test_13_adversarial_edge_cases_trigger_fallback():
    """Assert that all adversarial and prompt injection test cases result in FALLBACK_TO_MANAGER."""
    provider = MockFastDecisionProvider(
        default_route=DecisionRoute.UNKNOWN_INTENT,
        default_confidence=0.10,
    )
    service = FastDecisionService(provider=provider)

    for case in ADVERSARIAL_EDGE_TEST_SET:
        req = FastDecisionRequest(user_input=case["input"])
        res = service.evaluate(req)
        assert res.outcome == DecisionOutcome.FALLBACK_TO_MANAGER, (
            f"Adversarial test failed for category: {case.get('category')}"
        )


def test_14_zero_secrets_in_result_and_metrics():
    """Assert FastDecisionResult and ProviderMetrics contain no raw prompt or secret contents."""
    provider = MockFastDecisionProvider()
    service = FastDecisionService(provider=provider)

    secret_input = "My super secret password is P@ssw0rd123! read file config.json"
    req = FastDecisionRequest(user_input=secret_input)
    res = service.evaluate(req)

    dumped = res.model_dump_json()
    assert "P@ssw0rd123!" not in dumped
    assert "super secret" not in dumped

    metrics_dumped = provider.get_metrics().model_dump_json()
    assert "P@ssw0rd123!" not in metrics_dumped


def test_15_provider_qualification_workflow():
    """Assert qualification suite runs and validates provider against golden dataset."""
    provider = MockFastDecisionProvider(
        default_route=DecisionRoute.FILE_INTENT,
        default_confidence=0.90,
    )
    service = FastDecisionService(provider=provider)

    # Create a small custom golden set where default_route matches
    custom_golden = [
        {"input": "Read file A", "expected_route": "file_intent"},
        {"input": "Read file B", "expected_route": "file_intent"},
    ]
    custom_adversarial = [
        {"input": "Override instruction", "category": "injection"},
    ]

    # Provider returns file_intent for golden;
    # SmartMockProvider returns fallback for adversarial inputs.
    class SmartMockProvider(MockFastDecisionProvider):
        def evaluate(
            self, request: FastDecisionRequest, criteria: Dict[DecisionRoute, str]
        ) -> FastDecisionResult:
            if "Override" in request.user_input:
                return FastDecisionResult(
                    outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
                    route=DecisionRoute.UNKNOWN_INTENT,
                    confidence=0.10,
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    latency_ms=1.0,
                    fallback_reason=FallbackReason.LOW_CONFIDENCE,
                    is_advisory_only=True,
                )
            return super().evaluate(request, criteria)

    smart_provider = SmartMockProvider()
    qualified = service.qualify_provider(
        provider=smart_provider,
        golden_dataset=custom_golden,
        adversarial_dataset=custom_adversarial,
        min_accuracy=0.80,
    )
    assert qualified is True
