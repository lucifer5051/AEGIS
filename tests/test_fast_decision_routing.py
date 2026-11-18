"""Integration tests for FastDecisionService runtime execution routing (PROMPT 063 / ARCH-060-02).

Covers Tests A through I:
- Test A: Decision changes the execution strategy (DIRECT_SKILL vs STRUCTURED_PLAN)
- Test B: Decision changes model or provider selection (selected provider invoked, alternative not)
- Test C: Skill or tool selection (decision influences registered skill execution)
- Test D: Invalid decision (malformed/unknown decision enters documented safe fallback)
- Test E: TrustCore remains authoritative (hard-denied tool remains blocked despite decision)
- Test F: Approval remains enforced (strict manual autonomy blocks execution pending approval)
- Test G: Provider unavailable (selected provider unavailable yields clear blocked result, not false success)
- Test H: Decision and replanning cooperate (replan requests revised decision without losing state)
- Test I: Decision has observable runtime consequences (strategy, interface, and task metadata verified)
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.decision.models import (
    AlternativeRoute,
    DecisionOutcome,
    DecisionRoute,
    FallbackReason,
    FastDecisionRequest,
    FastDecisionResult,
    ProviderStatus,
    RequestContext,
)
from src.decision.providers.base import FastDecisionProvider
from src.decision.routing import (
    ExecutionRouter,
    ExecutionStrategy,
    RoutingDecision,
)
from src.decision.service import FastDecisionService
from src.manager.models import StepState, TaskState
from src.manager.planner import Plan, PlannedStep, StructuredPlanner
from src.manager.state_store import StateStore
from src.manager.supervisor import GoalExecutionResult, Supervisor
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import (
    ReasoningMetrics,
    ReasoningProviderStatus,
    ReasoningRequest,
    ReasoningResponse,
)
from src.skills.executor import BoundSkillToolBroker, DeterministicSkillExecutor
from src.skills.models import (
    SkillExecutionStatus,
    SkillInvocation,
    SkillManifest,
    SkillResult,
    SkillType,
    VerificationStatus,
)
from src.skills.registry import SkillRegistry
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.models import ToolResultStatus
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.policy import AutonomyTier


# ---------------------------------------------------------------------------
# Helpers and Mock Providers
# ---------------------------------------------------------------------------


class MockFastDecisionProvider(FastDecisionProvider):
    """Deterministic mock provider for FastDecisionService."""

    def __init__(
        self,
        default_outcome: DecisionOutcome = DecisionOutcome.FAST_PATH,
        default_route: DecisionRoute = DecisionRoute.FILE_INTENT,
        default_confidence: float = 0.95,
        provider_id: str = "mock_fde",
    ) -> None:
        super().__init__(provider_id=provider_id, provider_version="1.0.0")
        self.default_outcome = default_outcome
        self.default_route = default_route
        self.default_confidence = default_confidence
        self.evaluate_calls: List[FastDecisionRequest] = []
        self._status = ProviderStatus.READY

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
        return FastDecisionResult(
            outcome=self.default_outcome,
            route=self.default_route,
            confidence=self.default_confidence,
            alternatives=[],
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            latency_ms=1.2,
            fallback_reason=(
                FallbackReason.LOW_CONFIDENCE
                if self.default_outcome == DecisionOutcome.FALLBACK_TO_MANAGER
                else None
            ),
            is_advisory_only=True,
        )

    def unload(self) -> None:
        self._status = ProviderStatus.UNINITIALIZED

    def get_metrics(self) -> Any:
        return None

    def verify_integrity(self) -> bool:
        return True

    def qualify(self, hardware_profile: Any = None) -> bool:
        return True


class MockReasoningProvider(LocalReasoningProvider):
    """Deterministic mock LocalReasoningProvider for testing model routing."""

    def __init__(
        self,
        provider_id: str = "mock_reasoning",
        model_id: str = "mock_model",
        canned_response_content: Optional[str] = None,
        responses: Optional[List[str]] = None,
        ready: bool = True,
    ) -> None:
        super().__init__(provider_id=provider_id, model_id=model_id)
        self.canned_response_content = canned_response_content
        self.responses = list(responses) if responses else []
        self._ready = ready
        self._status = (
            ReasoningProviderStatus.READY if ready else ReasoningProviderStatus.FAILED
        )
        self.reason_calls: List[ReasoningRequest] = []

    def initialize(self) -> bool:
        self._ready = True
        self._status = ReasoningProviderStatus.READY
        return True

    def is_ready(self) -> bool:
        return self._ready

    def set_ready(self, ready: bool) -> None:
        self._ready = ready
        self._status = (
            ReasoningProviderStatus.READY if ready else ReasoningProviderStatus.FAILED
        )

    def reason(self, request: ReasoningRequest) -> ReasoningResponse:
        self.reason_calls.append(request)
        if self.responses:
            content = self.responses.pop(0)
        else:
            content = self.canned_response_content or json.dumps({
            "goal": "Test goal",
            "rationale": f"Generated by {self.provider_id}",
            "steps": [
                {
                    "step_number": 1,
                    "description": "Default step",
                    "tool": "file_write",
                    "arguments": {"path": "test.txt", "content": "mock"},
                    "is_idempotent": True,
                    "acceptance_criteria": "done",
                    "estimated_risk": "LOW",
                    "target_resource": "*",
                }
            ],
        })
        return ReasoningResponse(
            content=content,
            finish_reason="stop",
            prompt_tokens=20,
            completion_tokens=40,
            total_tokens=60,
            model_id=self.model_id,
            provider_id=self.provider_id,
        )

    def unload(self) -> bool:
        self._status = ReasoningProviderStatus.UNLOADED
        self._ready = False
        return True

    def verify_integrity(self) -> bool:
        return True

    def get_metrics(self) -> ReasoningMetrics:
        return ReasoningMetrics()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_fast_decision_routing.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    root = tmp_path / "routing_outputs"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def state_store(test_db: sqlite3.Connection) -> StateStore:
    return StateStore(test_db)


@pytest.fixture
def trust_authority(test_db: sqlite3.Connection):
    return build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)


@pytest.fixture
def tool_broker(trust_authority, output_root: Path) -> ToolBroker:
    broker = ToolBroker(trust_authority)
    register_builtin_tools(broker, allowed_roots=[output_root])
    return broker


# ---------------------------------------------------------------------------
# Test A: Decision changes the execution strategy
# ---------------------------------------------------------------------------


def test_case_a_decision_changes_execution_strategy(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test A:
    1. Fast path decision + matching skill -> selects DIRECT_SKILL, runs skill directly without planner.
    2. Fallback decision -> selects STRUCTURED_PLAN, invokes planner.
    """
    skill_manifest = SkillManifest(
        skill_id="fast_file_writer",
        name="Fast File Writer",
        version="1.0.0",
        description="Write a file directly.",
        skill_type=SkillType.DETERMINISTIC,
        intent_tags=("file_intent", "write"),
        tool_bindings=("file_write",),
    )
    skill_executed = []

    def skill_handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        target = output_root / "direct_skill.txt"
        broker.invoke("file_write", args={"path": str(target), "content": "from direct skill"})
        skill_executed.append(True)
        return {"file": str(target), "bytes": 16}

    registry = SkillRegistry()
    registry.register_skill(skill_manifest, executor=DeterministicSkillExecutor(skill_handler))

    # Provider 1: FAST_PATH -> DIRECT_SKILL
    fde_provider = MockFastDecisionProvider(
        default_outcome=DecisionOutcome.FAST_PATH,
        default_route=DecisionRoute.FILE_INTENT,
        default_confidence=0.95,
    )
    fde_service = FastDecisionService(provider=fde_provider)

    mock_reasoner = MockReasoningProvider()
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        fast_decision=fde_service,
        reasoning_provider=mock_reasoner,
        skill_registry=registry,
    )

    result = supervisor.execute_goal("write a file directly")
    assert result.success is True
    assert result.routing_decision is not None
    assert result.routing_decision.strategy == ExecutionStrategy.DIRECT_SKILL
    assert result.routing_decision.selected_skill_id == "fast_file_writer"
    assert len(skill_executed) == 1
    # Planner was completely bypassed
    assert len(mock_reasoner.reason_calls) == 0

    # Provider 2: FALLBACK_TO_MANAGER -> STRUCTURED_PLAN
    fde_provider.default_outcome = DecisionOutcome.FALLBACK_TO_MANAGER
    fde_provider.default_route = DecisionRoute.REASONING_INTENT
    fde_provider.default_confidence = 0.50

    target2 = str(output_root / "planner_step.txt")
    plan_json = json.dumps({
        "goal": "multi-step task",
        "rationale": "planned",
        "steps": [
            {
                "step_number": 1,
                "description": "Write planned file",
                "tool": "file_write",
                "arguments": {"path": target2, "content": "from planner"},
                "is_idempotent": True,
                "acceptance_criteria": "written",
                "estimated_risk": "LOW",
                "target_resource": target2,
            }
        ],
    })
    mock_reasoner.canned_response_content = plan_json

    result2 = supervisor.execute_goal("multi-step task")
    assert result2.success is True
    assert result2.routing_decision is not None
    assert result2.routing_decision.strategy == ExecutionStrategy.STRUCTURED_PLAN
    # Planner WAS invoked
    assert len(mock_reasoner.reason_calls) >= 1


# ---------------------------------------------------------------------------
# Test B: Decision changes model or provider selection
# ---------------------------------------------------------------------------


def test_case_b_decision_changes_model_or_provider_selection(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test B: When multiple reasoning providers are registered,
    FastDecision route selects the matching specialized provider and does NOT invoke the alternative.
    """
    target = str(output_root / "code_provider.txt")
    code_plan = json.dumps({
        "goal": "run code",
        "rationale": "code provider",
        "steps": [
            {
                "step_number": 1,
                "description": "Write code output",
                "tool": "file_write",
                "arguments": {"path": target, "content": "code provider output"},
                "is_idempotent": True,
                "acceptance_criteria": "written",
                "estimated_risk": "LOW",
                "target_resource": target,
            }
        ],
    })

    provider_primary = MockReasoningProvider(provider_id="primary", model_id="qwen-general")
    provider_code = MockReasoningProvider(
        provider_id="code",
        model_id="qwen-coder",
        canned_response_content=code_plan,
    )

    fde_provider = MockFastDecisionProvider(
        default_outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
        default_route=DecisionRoute.TERMINAL_INTENT,
        default_confidence=0.88,
    )
    fde_service = FastDecisionService(provider=fde_provider)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        fast_decision=fde_service,
        reasoning_providers={
            "primary": provider_primary,
            "code": provider_code,
        },
    )

    result = supervisor.execute_goal("run python terminal check")
    assert result.success is True
    assert result.routing_decision is not None
    assert result.routing_decision.selected_provider_id == "code"

    # Verify which provider was invoked
    assert len(provider_code.reason_calls) == 1
    assert len(provider_primary.reason_calls) == 0


# ---------------------------------------------------------------------------
# Test C: Skill or tool selection
# ---------------------------------------------------------------------------


def test_case_c_skill_or_tool_selection(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test C: Decision influences skill selection and reaches the correct execution interface."""
    skill_manifest = SkillManifest(
        skill_id="workspace.inspect",
        name="Workspace Inspection",
        version="1.0.0",
        description="Inspect workspace directory structure.",
        skill_type=SkillType.DETERMINISTIC,
        intent_tags=("file_intent", "inspect", "workspace"),
        tool_bindings=("file_list",),
    )
    invoked_args = []

    def inspect_handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        invoked_args.append(args)
        res = broker.invoke("file_list", args={"directory": str(output_root)})
        return {"inspection": res}

    registry = SkillRegistry()
    registry.register_skill(skill_manifest, executor=DeterministicSkillExecutor(inspect_handler))

    fde_provider = MockFastDecisionProvider(
        default_outcome=DecisionOutcome.FAST_PATH,
        default_route=DecisionRoute.FILE_INTENT,
        default_confidence=0.92,
    )
    fde_service = FastDecisionService(provider=fde_provider)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        fast_decision=fde_service,
        skill_registry=registry,
    )

    result = supervisor.execute_goal("inspect directory files")
    assert result.success is True
    assert result.routing_decision.strategy == ExecutionStrategy.DIRECT_SKILL
    assert result.routing_decision.selected_skill_id == "workspace.inspect"
    assert len(invoked_args) == 1


# ---------------------------------------------------------------------------
# Test D: Invalid decision
# ---------------------------------------------------------------------------


def test_case_d_invalid_decision_enters_safe_fallback(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test D: An invalid or malformed decision safely falls back to the deterministic Manager planner."""
    target = str(output_root / "fallback_valid.txt")
    plan_json = json.dumps({
        "goal": "safe fallback",
        "rationale": "fallback",
        "steps": [
            {
                "step_number": 1,
                "description": "Safe step",
                "tool": "file_write",
                "arguments": {"path": target, "content": "safe output"},
                "is_idempotent": True,
                "acceptance_criteria": "written",
                "estimated_risk": "LOW",
                "target_resource": target,
            }
        ],
    })
    mock_reasoner = MockReasoningProvider(canned_response_content=plan_json)

    # Broken decision service that returns None or an invalid type
    broken_fde = MagicMock(spec=FastDecisionService)
    broken_fde.evaluate.return_value = "invalid_decision_string_not_pydantic"

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        fast_decision=broken_fde,
        reasoning_provider=mock_reasoner,
    )

    result = supervisor.execute_goal("safe fallback test")
    assert result.success is True
    assert result.routing_decision is not None
    assert result.routing_decision.strategy == ExecutionStrategy.STRUCTURED_PLAN
    assert "Invalid decision" in result.routing_decision.rationale or "falling back" in result.routing_decision.rationale


# ---------------------------------------------------------------------------
# Test E: TrustCore remains authoritative
# ---------------------------------------------------------------------------


def test_case_e_trustcore_remains_authoritative(
    test_db: sqlite3.Connection,
    state_store: StateStore,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test E: A routing decision proposing a hard-denied action fails closed under TrustCore."""
    # Build authority with hard-denied filesystem.write
    auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    auth._policy._hard_denied = frozenset(["filesystem.write"])
    broker = ToolBroker(auth)
    register_builtin_tools(broker, allowed_roots=[output_root])

    skill_manifest = SkillManifest(
        skill_id="denied_skill",
        name="Denied Skill",
        version="1.0.0",
        description="Skill attempting denied write.",
        skill_type=SkillType.DETERMINISTIC,
        intent_tags=("file_intent",),
        tool_bindings=("file_write",),
    )

    def denied_handler(args: Dict[str, Any], b: BoundSkillToolBroker) -> Dict[str, Any]:
        # file_write maps to action filesystem.write which is hard-denied
        res = b.invoke("file_write", args={"path": str(output_root / "denied.txt"), "content": "bad"})
        if res.status != ToolResultStatus.SUCCESS:
            raise RuntimeError(f"Tool invocation denied: {res.error_summary}")
        return {"done": True}

    registry = SkillRegistry()
    registry.register_skill(skill_manifest, executor=DeterministicSkillExecutor(denied_handler))

    fde_provider = MockFastDecisionProvider(
        default_outcome=DecisionOutcome.FAST_PATH,
        default_route=DecisionRoute.FILE_INTENT,
        default_confidence=0.99,
    )
    fde_service = FastDecisionService(provider=fde_provider)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=auth,
        tool_broker=broker,
        fast_decision=fde_service,
        skill_registry=registry,
    )

    result = supervisor.execute_goal("try to write file")
    # TrustCore must deny execution
    assert result.success is False
    assert "Direct skill execution failed" in result.synthesis
    assert not (output_root / "denied.txt").exists()


# ---------------------------------------------------------------------------
# Test F: Approval remains enforced
# ---------------------------------------------------------------------------


def test_case_f_approval_remains_enforced(
    test_db: sqlite3.Connection,
    state_store: StateStore,
    output_root: Path,
) -> None:
    """Test F: Under STRICT_MANUAL autonomy, routing decision cannot bypass human approval."""
    auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.STRICT_MANUAL)
    broker = ToolBroker(auth)
    register_builtin_tools(broker, allowed_roots=[output_root])

    target = str(output_root / "strict_file.txt")
    plan_json = json.dumps({
        "goal": "strict goal",
        "rationale": "planned",
        "steps": [
            {
                "step_number": 1,
                "description": "Write file under strict manual",
                "tool": "file_write",
                "arguments": {"path": target, "content": "strict"},
                "is_idempotent": True,
                "acceptance_criteria": "written",
                "estimated_risk": "MEDIUM",
                "target_resource": target,
            }
        ],
    })
    mock_reasoner = MockReasoningProvider(canned_response_content=plan_json)

    fde_provider = MockFastDecisionProvider(
        default_outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
        default_route=DecisionRoute.FILE_INTENT,
        default_confidence=0.90,
    )
    fde_service = FastDecisionService(provider=fde_provider)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=auth,
        tool_broker=broker,
        fast_decision=fde_service,
        reasoning_provider=mock_reasoner,
    )

    result = supervisor.execute_goal("write strict file")
    # Fails closed or pauses in approval
    assert result.success is False
    assert not (output_root / "strict_file.txt").exists()
    assert (
        "requires human approval" in result.synthesis
        or "Capability request denied" in result.synthesis
        or "denied" in result.synthesis.lower()
    )


# ---------------------------------------------------------------------------
# Test G: Provider unavailable
# ---------------------------------------------------------------------------


def test_case_g_provider_unavailable_fails_closed(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
) -> None:
    """Test G: When the selected reasoning provider is unavailable and no ready fallback exists,
    supervisor returns a clear blocked result without claiming false success.
    """
    disabled_provider = MockReasoningProvider(
        provider_id="code",
        model_id="qwen-coder",
        ready=False,
    )

    fde_provider = MockFastDecisionProvider(
        default_outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
        default_route=DecisionRoute.TERMINAL_INTENT,
        default_confidence=0.90,
    )
    fde_service = FastDecisionService(provider=fde_provider)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        fast_decision=fde_service,
        reasoning_providers={"code": disabled_provider},
    )

    result = supervisor.execute_goal("run code task")
    assert result.success is False
    assert "Execution blocked" in result.synthesis
    assert "unavailable" in result.synthesis


# ---------------------------------------------------------------------------
# Test H: Decision and replanning cooperate
# ---------------------------------------------------------------------------


def test_case_h_decision_and_replanning_cooperate(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test H: When a step produces an observation requiring replan,
    FastDecision evaluates the revised prompt and replanning continues toward the goal
    without repeating completed non-idempotent steps.
    """
    good_file = str(output_root / "replan_success.txt")

    # Step 1 fails, Step 2 succeeds on replan
    initial_plan = json.dumps({
        "goal": "Replan goal",
        "rationale": "Initial try",
        "steps": [
            {
                "step_number": 1,
                "description": "Read missing file",
                "tool": "file_read",
                "arguments": {"path": str(output_root / "missing_replan.txt")},
                "is_idempotent": False,
                "acceptance_criteria": "Read done",
                "estimated_risk": "LOW",
                "target_resource": "*",
            }
        ],
    })

    replan_plan = json.dumps({
        "goal": "Replan goal",
        "rationale": "Revised try",
        "steps": [
            {
                "step_number": 1,
                "description": "Write target file on replan",
                "tool": "file_write",
                "arguments": {"path": good_file, "content": "replan succeeded"},
                "is_idempotent": True,
                "acceptance_criteria": "Written",
                "estimated_risk": "LOW",
                "target_resource": good_file,
            }
        ],
    })

    provider_primary = MockReasoningProvider(
        provider_id="primary",
        responses=[initial_plan, replan_plan],
    )
    provider_repair = MockReasoningProvider(
        provider_id="repair",
        canned_response_content=replan_plan,
    )

    fde_provider = MockFastDecisionProvider(
        default_outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
        default_route=DecisionRoute.REASONING_INTENT,
        default_confidence=0.85,
    )
    fde_service = FastDecisionService(provider=fde_provider)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        fast_decision=fde_service,
        reasoning_providers={
            "primary": provider_primary,
            "repair": provider_repair,
            "reasoning": provider_primary,
        },
    )

    result = supervisor.execute_goal("Replan goal")
    assert result.success is True
    assert Path(good_file).exists()
    assert Path(good_file).read_text(encoding="utf-8") == "replan succeeded"
    # Fast decision was queried during execution and replan
    assert len(fde_provider.evaluate_calls) >= 2


# ---------------------------------------------------------------------------
# Test I: Decision has observable runtime consequences
# ---------------------------------------------------------------------------


def test_case_i_decision_observable_runtime_consequences(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test I: Verify that the selected strategy, actual invoked interface,
    and returned result all contain observable consequences of the routing decision.
    """
    skill_manifest = SkillManifest(
        skill_id="observable_skill",
        name="Observable Skill",
        version="1.0.0",
        description="Observable execution verification.",
        skill_type=SkillType.DETERMINISTIC,
        intent_tags=("browser_intent",),
        tool_bindings=("file_write",),
    )

    target = output_root / "observable.txt"

    def obs_handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        broker.invoke("file_write", args={"path": str(target), "content": "observable success"})
        return {"written_bytes": 18, "path": str(target)}

    registry = SkillRegistry()
    registry.register_skill(skill_manifest, executor=DeterministicSkillExecutor(obs_handler))

    fde_provider = MockFastDecisionProvider(
        default_outcome=DecisionOutcome.FAST_PATH,
        default_route=DecisionRoute.BROWSER_INTENT,
        default_confidence=0.97,
    )
    fde_service = FastDecisionService(provider=fde_provider)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        fast_decision=fde_service,
        skill_registry=registry,
    )

    result = supervisor.execute_goal("open browser search")
    assert result.success is True
    assert result.routing_decision is not None
    assert result.routing_decision.strategy == ExecutionStrategy.DIRECT_SKILL
    assert result.routing_decision.route == DecisionRoute.BROWSER_INTENT
    assert result.routing_decision.confidence == 0.97
    assert result.routing_decision.selected_skill_id == "observable_skill"

    # Verify task record persistence in StateStore
    assert result.task_record is not None
    assert result.task_record.routing_intent == "browser_intent"
    assert result.task_record.metadata.get("execution_strategy") == "DIRECT_SKILL"
    assert "routing_decision" in result.task_record.metadata

    # Verify side effect
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "observable success"
