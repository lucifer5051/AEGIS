"""Phase 7: TaskSynthesizer Error Propagation & Explicit Degradation Tests.

Remediates ARCH-060-05:
- Distinguishes model synthesis from fallback output.
- Configurable degradation policies: RESILIENT, STRICT, DISABLED.
- Preserves structured exception diagnostics.
- Sanitizes all diagnostic and user-facing content (Zero-Secret Invariant).
- Integrates with StructuredPlanner, Supervisor, and ExperienceFeedbackService.

100% Offline Guarantee: Zero network calls.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, List, Optional

import pytest

from src.manager.context import ContextAssembler, ContextIR
from src.manager.observation import ObservationStatus, StepObservation
from src.manager.planner import Plan, PlannedStep, StructuredPlanner
from src.manager.state_store import StateStore
from src.manager.supervisor import GoalExecutionResult, Supervisor
from src.manager.synthesis import (
    ProviderErrorDiagnostics,
    SynthesisContextLengthError,
    SynthesisDegradationPolicy,
    SynthesisError,
    SynthesisFailureType,
    SynthesisProviderUnavailableError,
    SynthesisResult,
    SynthesisTimeoutError,
    TaskSynthesizer,
)
from src.manager.verifier import StepVerificationResult, VerificationOutcome
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.errors import ReasoningAdmissionError
from src.reasoning.models import ReasoningMetrics, ReasoningRequest, ReasoningResponse
from src.skills.experience import ExperienceEvent, ExperienceFeedbackService
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.policy import AutonomyTier


class MockReasoningProvider(LocalReasoningProvider):
    """Deterministic mock provider for synthesis testing."""

    def __init__(
        self,
        content: str = "Task executed cleanly.",
        finish_reason: str = "stop",
        exception_to_raise: Optional[Exception] = None,
    ) -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.exception_to_raise = exception_to_raise
        self.call_count = 0
        self.last_request: Optional[ReasoningRequest] = None
        self.provider_id = "mock_provider"
        self.model_id = "mock_model_v1"

    def reason(self, request: ReasoningRequest) -> ReasoningResponse:
        self.call_count += 1
        self.last_request = request
        if self.exception_to_raise is not None:
            raise self.exception_to_raise
        return ReasoningResponse(
            content=self.content,
            finish_reason=self.finish_reason,
            prompt_tokens=42,
            completion_tokens=18,
            total_tokens=60,
            model_id=self.model_id,
            provider_id=self.provider_id,
        )

    def is_ready(self) -> bool:
        return True

    def initialize(self) -> None:
        pass

    def unload(self) -> None:
        pass

    def verify_integrity(self) -> bool:
        return True

    def get_metrics(self) -> ReasoningMetrics:
        return ReasoningMetrics(total_inferences=self.call_count)


def _make_dummy_plan() -> Plan:
    return Plan(
        goal="Test goal",
        steps=[
            PlannedStep(
                step_number=1,
                tool="file_write",
                description="Write dummy file",
                arguments={"path": "test.txt", "content": "hello"},
                acceptance_criteria="file exists",
            )
        ],
        rationale="Initial plan",
    )


def _make_dummy_obs() -> List[StepObservation]:
    return [
        StepObservation(
            step_number=1,
            tool_name="file_write",
            status=ObservationStatus.SUCCESS,
            exit_code=0,
            output_summary="File written successfully",
            raw_output="bytes_written: 5",
            duration_ms=12.5,
        )
    ]


def _make_dummy_ver() -> List[StepVerificationResult]:
    return [
        StepVerificationResult(
            outcome=VerificationOutcome.VERIFIED_SUCCESS,
            rationale="File verified on disk",
            requires_replan=False,
            requires_approval=False,
        )
    ]


def _make_single_step_plan(output_path: str) -> str:
    plan = {
        "goal": "Write test file",
        "rationale": "Write file to target path",
        "steps": [
            {
                "step_number": 1,
                "description": "Write target file",
                "tool": "file_write",
                "arguments": {
                    "path": output_path,
                    "content": "hello synthesis",
                },
                "is_idempotent": True,
                "acceptance_criteria": "file exists",
                "estimated_risk": "LOW",
                "target_resource": output_path,
            }
        ],
    }
    return json.dumps(plan)


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_synthesis.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


# ==============================================================================
# TEST CASES
# ==============================================================================


def test_01_successful_synthesis_returns_non_degraded_result() -> None:
    """Successful synthesis returns a non-degraded SynthesisResult."""
    provider = MockReasoningProvider(content="The file was created successfully.")
    synthesizer = TaskSynthesizer(provider=provider)

    result = synthesizer.synthesize(
        goal="Create test file",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )

    assert isinstance(result, SynthesisResult)
    assert result.is_degraded is False
    assert result.fallback_used is False
    assert result.provider_error is None
    assert result.text == "The file was created successfully."
    assert str(result) == "The file was created successfully."
    assert provider.call_count == 1


def test_02_provider_timeout_produces_explicit_degradation() -> None:
    """Provider timeout produces explicit degradation with TIMEOUT failure type."""
    provider = MockReasoningProvider(exception_to_raise=TimeoutError("Request timed out after 30s"))
    synthesizer = TaskSynthesizer(provider=provider, policy=SynthesisDegradationPolicy.RESILIENT)

    result = synthesizer.synthesize(
        goal="Create test file",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )

    assert result.is_degraded is True
    assert result.fallback_used is True
    assert result.provider_error is not None
    assert result.provider_error.failure_type == SynthesisFailureType.TIMEOUT
    assert "timed out" in result.provider_error.error_message
    assert "Goal completed successfully: 'Create test file'." in result.text


def test_03_provider_unavailable_produces_explicit_degradation() -> None:
    """Missing or unavailable provider produces explicit degradation."""
    # Case A: Provider is None
    synth_none = TaskSynthesizer(provider=None, policy=SynthesisDegradationPolicy.RESILIENT)
    res_none = synth_none.synthesize(
        goal="Create test file",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )
    assert res_none.is_degraded is True
    assert res_none.provider_error is not None
    assert res_none.provider_error.failure_type == SynthesisFailureType.PROVIDER_UNAVAILABLE

    # Case B: Provider raises ReasoningAdmissionError
    provider = MockReasoningProvider(exception_to_raise=ReasoningAdmissionError("Model admission denied"))
    synth_admit = TaskSynthesizer(provider=provider, policy=SynthesisDegradationPolicy.RESILIENT)
    res_admit = synth_admit.synthesize(
        goal="Create test file",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )
    assert res_admit.is_degraded is True
    assert res_admit.provider_error.failure_type == SynthesisFailureType.PROVIDER_UNAVAILABLE


def test_04_context_length_failure_identified_correctly() -> None:
    """Context-length or token budget overflow is classified as CONTEXT_LENGTH."""
    # Case A: Provider raises token limit error
    provider = MockReasoningProvider(exception_to_raise=RuntimeError("Maximum context length exceeded: 4096 tokens"))
    synthesizer = TaskSynthesizer(provider=provider, policy=SynthesisDegradationPolicy.RESILIENT)
    result = synthesizer.synthesize(
        goal="Create test file",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )
    assert result.is_degraded is True
    assert result.provider_error.failure_type == SynthesisFailureType.CONTEXT_LENGTH

    # Case B: Provider completes with finish_reason='length'
    provider_len = MockReasoningProvider(content="Truncated output...", finish_reason="length")
    synth_len = TaskSynthesizer(provider=provider_len, policy=SynthesisDegradationPolicy.RESILIENT)
    res_len = synth_len.synthesize(
        goal="Create test file",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )
    assert res_len.is_degraded is True
    assert res_len.provider_error.failure_type == SynthesisFailureType.CONTEXT_LENGTH
    assert res_len.fallback_used is False


def test_05_unexpected_provider_exception_preserves_diagnostic_information() -> None:
    """Unexpected provider exception preserves error class and message diagnostics."""
    provider = MockReasoningProvider(exception_to_raise=RuntimeError("CUDA out of memory error"))
    synthesizer = TaskSynthesizer(provider=provider, policy=SynthesisDegradationPolicy.RESILIENT)

    result = synthesizer.synthesize(
        goal="Create test file",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )

    assert result.is_degraded is True
    assert result.fallback_used is True
    assert result.provider_error is not None
    assert result.provider_error.failure_type == SynthesisFailureType.EXECUTION_ERROR
    assert result.provider_error.error_class == "RuntimeError"
    assert "CUDA out of memory error" in result.provider_error.error_message


def test_06_resilient_mode_uses_explicitly_marked_fallback() -> None:
    """Resilient mode uses deterministic fallback and marks is_degraded=True."""
    provider = MockReasoningProvider(exception_to_raise=ValueError("Model crashed"))
    synthesizer = TaskSynthesizer(provider=provider, policy=SynthesisDegradationPolicy.RESILIENT)

    result = synthesizer.synthesize(
        goal="Execute data pipeline",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )

    assert result.is_degraded is True
    assert result.fallback_used is True
    assert "Goal completed successfully: 'Execute data pipeline'." in result.text
    assert result.policy == SynthesisDegradationPolicy.RESILIENT


def test_07_strict_mode_raises_expected_typed_exception() -> None:
    """Strict mode raises typed SynthesisError subclasses without falling back."""
    # 1. Timeout -> SynthesisTimeoutError
    synth_timeout = TaskSynthesizer(
        provider=MockReasoningProvider(exception_to_raise=TimeoutError("Socket timeout")),
        policy=SynthesisDegradationPolicy.STRICT,
    )
    with pytest.raises(SynthesisTimeoutError) as exc_info:
        synth_timeout.synthesize("goal", _make_dummy_plan(), _make_dummy_obs(), _make_dummy_ver())
    assert exc_info.value.diagnostics.failure_type == SynthesisFailureType.TIMEOUT

    # 2. Context length -> SynthesisContextLengthError
    synth_ctx = TaskSynthesizer(
        provider=MockReasoningProvider(exception_to_raise=RuntimeError("Max context limit reached")),
        policy=SynthesisDegradationPolicy.STRICT,
    )
    with pytest.raises(SynthesisContextLengthError) as exc_ctx:
        synth_ctx.synthesize("goal", _make_dummy_plan(), _make_dummy_obs(), _make_dummy_ver())
    assert exc_ctx.value.diagnostics.failure_type == SynthesisFailureType.CONTEXT_LENGTH

    # 3. Missing provider -> SynthesisProviderUnavailableError
    synth_unavail = TaskSynthesizer(provider=None, policy=SynthesisDegradationPolicy.STRICT)
    with pytest.raises(SynthesisProviderUnavailableError) as exc_unavail:
        synth_unavail.synthesize("goal", _make_dummy_plan(), _make_dummy_obs(), _make_dummy_ver())
    assert exc_unavail.value.diagnostics.failure_type == SynthesisFailureType.PROVIDER_UNAVAILABLE

    # 4. Generic error -> SynthesisError
    synth_gen = TaskSynthesizer(
        provider=MockReasoningProvider(exception_to_raise=KeyError("Missing tensor key")),
        policy=SynthesisDegradationPolicy.STRICT,
    )
    with pytest.raises(SynthesisError):
        synth_gen.synthesize("goal", _make_dummy_plan(), _make_dummy_obs(), _make_dummy_ver())


def test_08_disabled_mode_does_not_invoke_provider() -> None:
    """Disabled mode produces explicit fallback without invoking the provider."""
    provider = MockReasoningProvider(content="This should never be generated")
    synthesizer = TaskSynthesizer(provider=provider, policy=SynthesisDegradationPolicy.DISABLED)

    result = synthesizer.synthesize(
        goal="Batch data processing",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )

    assert provider.call_count == 0  # Provider was never called
    assert result.is_degraded is False
    assert result.fallback_used is True
    assert result.policy == SynthesisDegradationPolicy.DISABLED
    assert result.provider_error is None
    assert "Goal completed successfully: 'Batch data processing'." in result.text


def test_09_sensitive_information_excluded_from_diagnostics() -> None:
    """Sensitive keys or tokens in exception messages are redacted from diagnostics."""
    leak_msg = "Error connecting to provider with key sk-abcdef12345678901234567890 and ghp_abcdefghijklmnopqrstuvwxyz123456"
    provider = MockReasoningProvider(exception_to_raise=RuntimeError(leak_msg))
    synthesizer = TaskSynthesizer(provider=provider, policy=SynthesisDegradationPolicy.RESILIENT)

    result = synthesizer.synthesize(
        goal="Secure task",
        plan=_make_dummy_plan(),
        observations=_make_dummy_obs(),
        verifications=_make_dummy_ver(),
    )

    assert "sk-abcdef" not in result.provider_error.error_message
    assert "ghp_abcdef" not in result.provider_error.error_message
    assert "[REDACTED_SECRET]" in result.provider_error.error_message
    assert result.provider_error.sanitized is True


def test_10_structured_planner_handles_degraded_synthesis_correctly() -> None:
    """StructuredPlanner validates synthesis context and distinguishes degraded input."""
    planner = StructuredPlanner(provider=MockReasoningProvider())

    valid_synth = SynthesisResult(
        text="All tasks finished smoothly.",
        is_degraded=False,
        fallback_used=False,
    )
    assert planner.validate_synthesis_context(valid_synth) is True
    assert planner.validate_synthesis_context("Plain string summary") is True

    degraded_synth = SynthesisResult(
        text="Fallback summary.",
        is_degraded=True,
        fallback_used=True,
        provider_error=ProviderErrorDiagnostics(
            failure_type=SynthesisFailureType.TIMEOUT,
            error_class="TimeoutError",
            error_message="Timed out",
        ),
    )
    # validate_synthesis_context explicitly identifies degraded context
    assert planner.validate_synthesis_context(degraded_synth) is False


def test_11_supervisor_handles_synthesis_failures_safely(test_db: sqlite3.Connection, tmp_path: Path) -> None:
    """Supervisor handles synthesis failures without crashing or presenting unverified success."""
    state_store = StateStore(test_db)
    trust_auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    tool_broker = ToolBroker(trust_auth)
    register_builtin_tools(tool_broker, allowed_roots=[tmp_path])

    target_file = str(tmp_path / "hello_11.txt")
    plan_json = _make_single_step_plan(target_file)

    # 1. Resilient mode failure: execution succeeds but marks degraded synthesis
    failing_synth = TaskSynthesizer(
        provider=MockReasoningProvider(exception_to_raise=TimeoutError("Inference timeout")),
        policy=SynthesisDegradationPolicy.RESILIENT,
    )
    sup_resilient = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        reasoning_provider=MockReasoningProvider(content=plan_json),
        synthesizer=failing_synth,
    )
    res = sup_resilient.execute_goal("Test goal resilient")
    assert res.success is True
    assert res.synthesis_result is not None
    assert res.synthesis_result.is_degraded is True
    assert res.synthesis_result.provider_error.failure_type == SynthesisFailureType.TIMEOUT

    # 2. Strict mode failure: supervisor marks success=False and records diagnostic failure
    strict_synth = TaskSynthesizer(
        provider=MockReasoningProvider(exception_to_raise=TimeoutError("Inference timeout")),
        policy=SynthesisDegradationPolicy.STRICT,
    )
    target_file_strict = str(tmp_path / "hello_11_strict.txt")
    plan_strict_json = _make_single_step_plan(target_file_strict)

    sup_strict = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        reasoning_provider=MockReasoningProvider(content=plan_strict_json),
        synthesizer=strict_synth,
        strict_synthesis=False,  # Safe handling mode
    )
    res_strict = sup_strict.execute_goal("Test goal strict safe")
    assert res_strict.success is False
    assert "Synthesis failed under STRICT policy" in res_strict.synthesis
    assert res_strict.synthesis_result.is_degraded is True


def test_12_experience_feedback_records_appropriate_degradation_events(
    test_db: sqlite3.Connection, tmp_path: Path
) -> None:
    """Experience feedback service captures [DEGRADED_SYNTHESIS] operational warning."""
    state_store = StateStore(test_db)
    trust_auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    tool_broker = ToolBroker(trust_auth)
    register_builtin_tools(tool_broker, allowed_roots=[tmp_path])

    exp_service = ExperienceFeedbackService(conn=test_db)
    failing_synth = TaskSynthesizer(
        provider=MockReasoningProvider(exception_to_raise=RuntimeError("Provider out of memory")),
        policy=SynthesisDegradationPolicy.RESILIENT,
    )
    target_file = str(tmp_path / "hello_12.txt")
    plan_json = _make_single_step_plan(target_file)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        reasoning_provider=MockReasoningProvider(content=plan_json),
        synthesizer=failing_synth,
        experience_service=exp_service,
    )

    res = supervisor.execute_goal("Test experience degradation recording")
    assert res.success is True
    assert res.synthesis_result.is_degraded is True

    # Inspect recorded experience events
    events = exp_service.list_events()
    assert len(events) >= 1
    last_event = events[-1]
    assert last_event.error_or_recovery_info is not None
    assert "[DEGRADED_SYNTHESIS]" in last_event.error_or_recovery_info
    assert "Provider out of memory" in last_event.error_or_recovery_info
    assert last_event.confidence == 0.8  # Reflects degraded synthesis fidelity


def test_13_existing_task_execution_behavior_remains_compatible(
    test_db: sqlite3.Connection, tmp_path: Path
) -> None:
    """Existing task execution continues to return GoalExecutionResult with string synthesis."""
    state_store = StateStore(test_db)
    trust_auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    tool_broker = ToolBroker(trust_auth)
    register_builtin_tools(tool_broker, allowed_roots=[tmp_path])

    target_file = str(tmp_path / "hello_13.txt")
    plan_json = _make_single_step_plan(target_file)

    provider = MockReasoningProvider(content="Everything completed cleanly.")
    plan_provider = MockReasoningProvider(content=plan_json)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        reasoning_provider=plan_provider,
        synthesizer=TaskSynthesizer(provider=provider),
    )

    res = supervisor.execute_goal("Standard execution goal")
    assert isinstance(res, GoalExecutionResult)
    assert res.success is True
    assert isinstance(res.synthesis, str)
    assert res.synthesis == "Everything completed cleanly."
    assert res.synthesis_result is not None
    assert res.synthesis_result.is_degraded is False
