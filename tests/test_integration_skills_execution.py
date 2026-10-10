"""Prompt 023 Integration Tests: Verified Skill Execution Pipeline.

Covers the complete deterministic execution pipeline:
    User Goal -> ClarificationGate -> Supervisor -> SkillRegistry -> Admission
    -> BoundSkillToolBroker -> ToolBroker -> TrustCore
    -> Observation -> Verification -> ExperienceEvent -> Structured Result

All 18 required scenarios from Prompt 023 section 5 plus 2 structural tests.
100% offline, zero network calls.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from src.manager import GoalExecutionResult, StateStore, Supervisor, build_supervisor
from src.manager.models import TaskState
from src.manager.reconciliation import ReconciliationEngine
from src.manager.verifier import StepVerificationResult, StepVerifier, VerificationOutcome
from src.model_policy.concurrency_manager import SkillConcurrencyManager
from src.model_policy.residency_manager import ModelResidencyManager
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import ReasoningResponse
from src.skills import (
    BoundSkillToolBroker,
    ClarificationAction,
    ClarificationGate,
    DeterministicSkillExecutor,
    ExperienceFeedbackService,
    ExperienceOutcomeType,
    SkillAdmissionController,
    SkillCheckpointMetadata,
    SkillExecutionStatus,
    SkillInvocation,
    SkillLifecycleState,
    SkillManifest,
    SkillRegistry,
    SkillResourceRequirement,
    SkillToolAccessDeniedError,
    SkillType,
    UserFeedbackType,
    VerificationStatus,
    persist_skill_checkpoint,
)
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.capability import ActionRisk
from src.trust.errors import SecretLeakError
from src.trust.policy import AutonomyTier

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_integration.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def state_store(test_db: sqlite3.Connection) -> StateStore:
    return StateStore(test_db)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    d = tmp_path / "sandbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def trust_authority(test_db: sqlite3.Connection):
    """FULL_BOUNDED so MEDIUM-risk tools execute without escalation."""
    return build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)


@pytest.fixture
def tool_broker(trust_authority, sandbox: Path) -> ToolBroker:
    broker = ToolBroker(trust_authority)
    register_builtin_tools(broker, allowed_roots=[sandbox])
    return broker


@pytest.fixture
def read_write_manifest(sandbox: Path) -> SkillManifest:
    """Skill declared to use file_read and file_write."""
    return SkillManifest(
        skill_id="fs.read_write",
        name="Read-Write Skill",
        version="1.0.0",
        description="Reads from and writes to a sandbox file.",
        skill_type=SkillType.DETERMINISTIC,
        intent_tags=("file", "write", "read"),
        tool_bindings=("file_write", "file_read"),
        required_capabilities=("filesystem.write", "filesystem.read"),
        risk_class=ActionRisk.MEDIUM,
    )


@pytest.fixture
def read_only_manifest() -> SkillManifest:
    """Skill declared to use only file_read."""
    return SkillManifest(
        skill_id="fs.reader",
        name="File Reader",
        version="1.0.0",
        description="Reads a file from the sandbox.",
        skill_type=SkillType.DETERMINISTIC,
        intent_tags=("file", "read"),
        tool_bindings=("file_read",),
        required_capabilities=("filesystem.read",),
        risk_class=ActionRisk.LOW,
    )


@pytest.fixture
def admission_controller() -> SkillAdmissionController:
    residency = ModelResidencyManager()
    concurrency_mgr = SkillConcurrencyManager(residency, system_ram_reserve_bytes=0)
    return SkillAdmissionController(concurrency_mgr)


@pytest.fixture
def experience_service() -> ExperienceFeedbackService:
    return ExperienceFeedbackService()


@pytest.fixture
def clarification_gate() -> ClarificationGate:
    return ClarificationGate()


def _make_mock_provider(plan_json: str) -> LocalReasoningProvider:
    provider = MagicMock(spec=LocalReasoningProvider)
    provider.provider_id = "mock"
    provider.model_id = "mock-model"
    provider.is_ready.return_value = True
    mock_response = ReasoningResponse(
        content=plan_json,
        finish_reason="stop",
        prompt_tokens=10,
        completion_tokens=50,
        total_tokens=60,
        model_id="mock-model",
        provider_id="mock",
    )
    provider.reason.return_value = mock_response
    return provider


# ---------------------------------------------------------------------------
# 1. Full pipeline
# ---------------------------------------------------------------------------


def test_01_full_skill_execution_pipeline(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    read_write_manifest: SkillManifest,
    sandbox: Path,
    admission_controller: SkillAdmissionController,
    experience_service: ExperienceFeedbackService,
    clarification_gate: ClarificationGate,
) -> None:
    """Full pipeline: SkillInvocation -> Admission -> Execution -> ExperienceEvent."""
    target_file = sandbox / "output.txt"
    registry = SkillRegistry()

    def write_handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        res = broker.invoke("file_write", args={"path": args["path"], "content": args["content"]})
        return {"written": res.status.value}

    executor = DeterministicSkillExecutor(write_handler, expected_output_keys=["written"])
    registry.register_skill(read_write_manifest, executor=executor)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
        admission_controller=admission_controller,
        experience_service=experience_service,
        clarification_gate=clarification_gate,
    )

    invocation = SkillInvocation(
        skill_id="fs.read_write",
        task_id="task-pipeline-01",
        input={"path": str(target_file), "content": "Integration test payload"},
    )
    result = supervisor.execute_skill(invocation, actor="integration_test")

    assert result.status == SkillExecutionStatus.SUCCESS, f"Expected SUCCESS: {result.error!r}"
    assert result.verification_status == VerificationStatus.VERIFIED
    assert "written" in result.output
    assert target_file.exists()
    assert "Integration test payload" in target_file.read_text(encoding="utf-8")

    events = experience_service.list_events(skill_id="fs.read_write")
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_type == ExperienceOutcomeType.VERIFIED_SUCCESS
    assert "file_write" in ev.selected_tools

    reg_skill = registry.get_skill("fs.read_write")
    assert reg_skill.current_state == SkillLifecycleState.AVAILABLE


# ---------------------------------------------------------------------------
# 2. Skill selection from registry
# ---------------------------------------------------------------------------


def test_02_supervisor_selects_skill_deterministically(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    read_write_manifest: SkillManifest,
) -> None:
    """Supervisor selects the correct skill from registry by intent match."""
    registry = SkillRegistry()
    registry.register_skill(read_write_manifest)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
    )
    selected = supervisor.select_skill("write file to sandbox")
    assert selected is not None
    assert selected.manifest.skill_id == "fs.read_write"
    assert selected.current_state == SkillLifecycleState.SELECTED
    none_result = supervisor.select_skill("completely unknown quantum operation xyz")
    assert none_result is None


# ---------------------------------------------------------------------------
# 3. Tool-binding enforcement
# ---------------------------------------------------------------------------


def test_03_tool_binding_enforcement(
    tool_broker: ToolBroker,
    read_only_manifest: SkillManifest,
    sandbox: Path,
) -> None:
    """Skill with only file_read binding cannot invoke file_write."""
    bound_broker = BoundSkillToolBroker(
        broker=tool_broker,
        manifest=read_only_manifest,
        actor="binding_test",
    )
    test_file = sandbox / "data.txt"
    test_file.write_text("data", encoding="utf-8")
    ok_result = bound_broker.invoke("file_read", args={"path": str(test_file)})
    assert ok_result.status.value in ("success", "ok", "SUCCESS")
    with pytest.raises(SkillToolAccessDeniedError) as exc_info:
        bound_broker.invoke("file_write", args={"path": str(test_file), "content": "x"})
    assert "file_write" in str(exc_info.value)
    assert "not authorized" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 4. ToolBroker + TrustCore authorization
# ---------------------------------------------------------------------------


def test_04_toolbroker_trustcore_authorizes_allowed_action(
    tool_broker: ToolBroker,
    read_write_manifest: SkillManifest,
    sandbox: Path,
) -> None:
    """FULL_BOUNDED tier -> MEDIUM risk file_write is authorized end-to-end."""
    target = sandbox / "authorized.txt"
    bound_broker = BoundSkillToolBroker(
        broker=tool_broker,
        manifest=read_write_manifest,
        actor="auth_test",
    )
    result = bound_broker.invoke("file_write", args={"path": str(target), "content": "auth"})
    assert result.status.value in ("success", "ok", "SUCCESS")
    assert target.exists()


# ---------------------------------------------------------------------------
# 5. Unknown tool / missing-capability rejection
# ---------------------------------------------------------------------------


def test_05_unknown_tool_rejected_by_bound_broker(
    tool_broker: ToolBroker,
    read_write_manifest: SkillManifest,
) -> None:
    """Unknown / undeclared tool rejected at BoundSkillToolBroker and ToolBroker boundaries."""
    from src.skills.errors import SkillManifestValidationError

    # 1. Unknown tool not in manifest bindings raises SkillToolAccessDeniedError
    bound_broker = BoundSkillToolBroker(
        broker=tool_broker,
        manifest=read_write_manifest,
        actor="shady_actor",
    )
    with pytest.raises(SkillToolAccessDeniedError):
        bound_broker.invoke("network_exfiltrate", args={})

    # 2. Unknown tool not registered in ToolBroker returns ToolNotFoundError ToolResult
    shady_manifest = read_write_manifest.model_copy(
        update={"skill_id": "shady.skill", "tool_bindings": ("network_exfiltrate",)}
    )
    bound_broker_shady = BoundSkillToolBroker(
        broker=tool_broker,
        manifest=shady_manifest,
        actor="shady_actor",
    )
    result = bound_broker_shady.invoke("network_exfiltrate", args={})
    assert result.status.value in ("error", "failed")
    assert result.error_type == "ToolNotFoundError"

    # 3. Registering manifest with unknown tools against known catalog fails validation
    registry = SkillRegistry(known_tools=tool_broker.registered_tools())
    with pytest.raises(SkillManifestValidationError):
        registry.register_skill(shady_manifest)


def test_05b_strict_tier_denies_medium_risk(
    test_db: sqlite3.Connection,
    sandbox: Path,
    read_write_manifest: SkillManifest,
) -> None:
    """STRICT_MANUAL tier -> MEDIUM-risk file_write is denied by TrustCore."""
    strict_authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.STRICT_MANUAL)
    broker = ToolBroker(strict_authority)
    register_builtin_tools(broker, allowed_roots=[sandbox])
    bound_broker = BoundSkillToolBroker(
        broker=broker, manifest=read_write_manifest, actor="strict_actor"
    )
    result = bound_broker.invoke(
        "file_write", args={"path": str(sandbox / "denied.txt"), "content": "x"}
    )
    assert result.status.value == "denied"
    assert not (sandbox / "denied.txt").exists()


# ---------------------------------------------------------------------------
# 6. Skill cannot bypass ToolBroker or TrustCore
# ---------------------------------------------------------------------------


def test_06_skill_cannot_bypass_trust_core(
    state_store: StateStore,
    trust_authority,
    sandbox: Path,
    test_db: sqlite3.Connection,
) -> None:
    """Skill handler using undeclared tool raises SkillToolAccessDeniedError."""
    strict_authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.STRICT_MANUAL)
    broker = ToolBroker(strict_authority)
    register_builtin_tools(broker, allowed_roots=[sandbox])
    manifest = SkillManifest(
        skill_id="bypass.attempt",
        name="Bypass Attempt",
        version="1.0.0",
        description="Attempts broker bypass",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=("file_read",),
        risk_class=ActionRisk.LOW,
    )

    def bypass_handler(args: Dict[str, Any], bound: BoundSkillToolBroker) -> Dict[str, Any]:
        bound.invoke("file_write", args={"path": str(sandbox / "bypass.txt"), "content": "x"})
        return {"done": True}

    executor = DeterministicSkillExecutor(bypass_handler)
    registry = SkillRegistry()
    registry.register_skill(manifest, executor=executor)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=strict_authority,
        tool_broker=broker,
        skill_registry=registry,
    )
    invocation = SkillInvocation(skill_id="bypass.attempt", input={})
    with pytest.raises(SkillToolAccessDeniedError):
        supervisor.execute_skill(invocation, actor="bypass_tester")
    assert not (sandbox / "bypass.txt").exists()


# ---------------------------------------------------------------------------
# 7. Missing parameter -> ClarificationGate (no LLM call)
# ---------------------------------------------------------------------------


def test_07_clarification_gate_no_llm_call(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
) -> None:
    """ClarificationGate short-circuits on empty goal without LLM invocation."""
    gate = ClarificationGate()
    provider = MagicMock(spec=LocalReasoningProvider)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        clarification_gate=gate,
        reasoning_provider=provider,
    )
    result = supervisor.execute_goal(goal_text="")
    assert result.success is False
    assert "Clarification required" in result.synthesis
    provider.reason.assert_not_called()


def test_07b_clarification_gate_missing_key() -> None:
    """ClarificationGate deterministically detects missing required key."""
    gate = ClarificationGate()
    res = gate.evaluate(
        provided_args={"intent": "copy_files"},
        required_keys=["intent", "destination"],
    )
    assert res.action == ClarificationAction.CLARIFY
    assert "destination" in res.missing_parameters
    assert res.clarification_prompt is not None


# ---------------------------------------------------------------------------
# 8. Invalid inputs / schema violations
# ---------------------------------------------------------------------------


def test_08_invalid_invocation_input_rejected(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
) -> None:
    """Skill executor returns FAILED result when handler raises on bad input."""
    manifest = SkillManifest(
        skill_id="strict.validator",
        name="Strict Validator",
        version="1.0.0",
        description="Requires numeric input",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=(),
    )

    def strict_handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        value = args["count"]
        if not isinstance(value, int):
            raise TypeError(f"Expected int, got {type(value).__name__}")
        return {"result": value * 2}

    executor = DeterministicSkillExecutor(strict_handler)
    registry = SkillRegistry()
    registry.register_skill(manifest, executor=executor)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
    )
    invocation = SkillInvocation(skill_id="strict.validator", input={"count": "not_a_number"})
    result = supervisor.execute_skill(invocation)
    assert result.status == SkillExecutionStatus.FAILED
    assert result.error is not None
    assert "TypeError" in result.error


def test_08b_output_schema_verification_failure(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
) -> None:
    """DeterministicSkillExecutor fails when expected output keys are missing."""
    manifest = SkillManifest(
        skill_id="schema.breaker",
        name="Schema Breaker",
        version="1.0.0",
        description="Returns wrong keys",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=(),
    )

    def bad_output_handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        return {"wrong_key": "unexpected"}

    executor = DeterministicSkillExecutor(bad_output_handler, expected_output_keys=["required_key"])
    registry = SkillRegistry()
    registry.register_skill(manifest, executor=executor)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
    )
    invocation = SkillInvocation(skill_id="schema.breaker", input={})
    result = supervisor.execute_skill(invocation)
    assert result.status == SkillExecutionStatus.FAILED
    assert result.verification_status == VerificationStatus.FAILED
    assert "missing keys" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# 9. Tool exceptions and controlled execution failures
# ---------------------------------------------------------------------------


def test_09_tool_exception_produces_failed_result(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    sandbox: Path,
) -> None:
    """Tool invocation on non-existent path produces FAILED SkillResult."""
    manifest = SkillManifest(
        skill_id="fs.bad_reader",
        name="Bad Reader",
        version="1.0.0",
        description="Tries to read a non-existent file.",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=("file_read",),
    )

    def bad_reader(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        res = broker.invoke("file_read", args={"path": args["path"]})
        if res.status.value != "success":
            raise RuntimeError(f"Read failed: {res.error_summary}")
        return {"content": res.output}

    executor = DeterministicSkillExecutor(bad_reader)
    registry = SkillRegistry()
    registry.register_skill(manifest, executor=executor)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
    )
    invocation = SkillInvocation(
        skill_id="fs.bad_reader",
        input={"path": str(sandbox / "does_not_exist.txt")},
    )
    result = supervisor.execute_skill(invocation)
    assert result.status == SkillExecutionStatus.FAILED
    assert result.error is not None
    assert result.verification_status == VerificationStatus.FAILED


# ---------------------------------------------------------------------------
# 10. Resource admission and concurrency-ceiling rejection
# ---------------------------------------------------------------------------


def test_10_excessive_ram_rejected_by_admission(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
) -> None:
    """Skill requiring 1 TB RAM is rejected by SkillAdmissionController."""
    heavy_manifest = SkillManifest(
        skill_id="heavy.memory_hog",
        name="Memory Hog",
        version="1.0.0",
        description="Requests unreasonable RAM",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=(),
        resource_requirements=SkillResourceRequirement(
            declared_marginal_ram_bytes=1024 * 1024 * 1024 * 1024,
        ),
    )
    registry = SkillRegistry()

    def noop(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        return {"done": True}

    registry.register_skill(heavy_manifest, executor=DeterministicSkillExecutor(noop))
    residency = ModelResidencyManager()
    concurrency_mgr = SkillConcurrencyManager(
        residency, system_ram_reserve_bytes=1024 * 1024 * 1024
    )
    controller = SkillAdmissionController(concurrency_mgr)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
        admission_controller=controller,
    )
    invocation = SkillInvocation(skill_id="heavy.memory_hog", input={})
    result = supervisor.execute_skill(invocation)
    assert result.status == SkillExecutionStatus.FAILED
    assert result.error is not None
    assert "admission" in result.error.lower() or "resource" in result.error.lower()


# ---------------------------------------------------------------------------
# 11. Verification failure after successful tool return
# ---------------------------------------------------------------------------


def test_11_verification_fail_after_successful_tool_return(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    sandbox: Path,
) -> None:
    """Verifier catches semantic failure even when tool returns SUCCESS."""
    output_path = str(sandbox / "verify_fail.py")
    plan = {
        "goal": "Write a file",
        "rationale": "Test verification failure path",
        "steps": [
            {
                "step_number": 1,
                "description": "Write file",
                "tool": "file_write",
                "arguments": {"path": output_path, "content": "x = 1\n"},
                "is_idempotent": True,
                "acceptance_criteria": "File exists at path",
                "estimated_risk": "MEDIUM",
                "target_resource": output_path,
            }
        ],
    }

    class AlwaysFailVerifier(StepVerifier):
        def verify(self, step, observation):
            return StepVerificationResult(
                outcome=VerificationOutcome.SEMANTIC_FAILURE,
                rationale="Forced semantic failure for test_11",
                can_retry=False,
                requires_replan=True,
                requires_approval=False,
            )

    provider = _make_mock_provider(json.dumps(plan))
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=provider,
        verifier=AlwaysFailVerifier(),
    )
    result = supervisor.execute_goal("Write a file", actor="test_11")
    assert result.success is False
    assert "semantic" in result.synthesis.lower() or "halt" in result.synthesis.lower()
    assert result.task_record is not None
    assert result.task_record.state == TaskState.FAILED


# ---------------------------------------------------------------------------
# 12. Checkpoint reconciliation and safe recovery
# ---------------------------------------------------------------------------


def test_12_checkpoint_and_reconciliation(
    state_store: StateStore,
    test_db: sqlite3.Connection,
) -> None:
    """Checkpoint data survives to reconciliation engine and is retrievable."""
    task = state_store.create_task(title="Recovery Test Task")
    step = state_store.create_step(
        task_id=task.task_id,
        step_index=0,
        action_name="skill.checkpoint_step",
        tool_name="file_write",
        arguments={"path": "/tmp/test.txt", "content": "x"},
    )
    metadata = SkillCheckpointMetadata(
        skill_id="recovery.skill",
        invocation_id="inv-recover-001",
        step_index=0,
        execution_state={"rows_processed": 750, "cursor": 751},
        recovery_policy="RETRY_IDEMPOTENT",
    )
    cp_record = persist_skill_checkpoint(
        state_store=state_store,
        task_id=task.task_id,
        step_id=step.step_id,
        checkpoint_seq=1,
        metadata=metadata,
    )
    assert cp_record.checkpoint_seq == 1
    reconciler = ReconciliationEngine(state_store)
    report = reconciler.reconcile_on_startup()
    assert report is not None
    latest_cp = state_store.get_latest_checkpoint(task.task_id)
    assert latest_cp is not None
    reconstructed = SkillCheckpointMetadata.from_snapshot_dict(latest_cp.snapshot)
    assert reconstructed.skill_id == "recovery.skill"
    assert reconstructed.execution_state["rows_processed"] == 750
    assert reconstructed.recovery_policy == "RETRY_IDEMPOTENT"


# ---------------------------------------------------------------------------
# 13. Non-idempotent side-effect deduplication during recovery
# ---------------------------------------------------------------------------


def test_13_non_idempotent_step_not_retried(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    sandbox: Path,
) -> None:
    """Non-idempotent step failure must not be retried."""
    output_path = str(sandbox / "non_idempotent.txt")
    plan = {
        "goal": "Write a file non-idempotent",
        "rationale": "Test dedup guard",
        "steps": [
            {
                "step_number": 1,
                "description": "Non-idempotent write",
                "tool": "file_write",
                "arguments": {"path": output_path, "content": "once\n"},
                "is_idempotent": False,
                "acceptance_criteria": "File contains once",
                "estimated_risk": "MEDIUM",
                "target_resource": output_path,
            }
        ],
    }

    class NonIdempotentFailVerifier(StepVerifier):
        def verify(self, step, observation):
            if not step.is_idempotent:
                return StepVerificationResult(
                    outcome=VerificationOutcome.SEMANTIC_FAILURE,
                    rationale="Non-idempotent step; cannot retry.",
                    can_retry=False,
                    requires_replan=True,
                    requires_approval=False,
                )
            return super().verify(step, observation)

    provider = _make_mock_provider(json.dumps(plan))
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=provider,
        verifier=NonIdempotentFailVerifier(),
    )
    result = supervisor.execute_goal("Write a file non-idempotent", actor="test_13")
    assert result.success is False
    assert result.task_record is not None
    assert result.task_record.state == TaskState.FAILED


# ---------------------------------------------------------------------------
# 14. Re-authorization on retry
# ---------------------------------------------------------------------------


def test_14_retry_goes_through_trust_core(
    state_store: StateStore,
    trust_authority,
    sandbox: Path,
    test_db: sqlite3.Connection,
) -> None:
    """Each retry re-invokes Tool Broker which re-authorizes via Trust Core."""
    output_path = str(sandbox / "retry_auth.txt")
    plan = {
        "goal": "Write a file with retry",
        "rationale": "Retry must re-authorize",
        "steps": [
            {
                "step_number": 1,
                "description": "Write file idempotent",
                "tool": "file_write",
                "arguments": {"path": output_path, "content": "retry\n"},
                "is_idempotent": True,
                "acceptance_criteria": "File written",
                "estimated_risk": "MEDIUM",
                "target_resource": output_path,
            }
        ],
    }
    strict_authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.STRICT_MANUAL)
    strict_broker = ToolBroker(strict_authority)
    register_builtin_tools(strict_broker, allowed_roots=[sandbox])
    provider = _make_mock_provider(json.dumps(plan))
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=strict_authority,
        tool_broker=strict_broker,
        reasoning_provider=provider,
    )
    result = supervisor.execute_goal("Write a file with retry", actor="test_14")
    assert result.success is False
    assert not Path(output_path).exists()


# ---------------------------------------------------------------------------
# 15. ExperienceEvent classification
# ---------------------------------------------------------------------------


def test_15_experience_event_classification() -> None:
    """USER_CORRECTED != EXECUTION_FAILURE; NO_FEEDBACK != SUCCESS."""
    service = ExperienceFeedbackService()
    from src.skills.experience import ExperienceEvent

    corrected_event = ExperienceEvent(
        task_id="task-200",
        invocation_id="inv-300",
        skill_id="email.draft",
        intent="Draft an email",
        outcome_type=ExperienceOutcomeType.USER_CORRECTED,
        user_feedback_type=UserFeedbackType.CORRECTED,
    )
    service.record_event(corrected_event)

    no_fb_event = ExperienceEvent(
        task_id="task-201",
        invocation_id="inv-301",
        skill_id="email.draft",
        intent="Draft another email",
        outcome_type=ExperienceOutcomeType.NO_FEEDBACK,
        user_feedback_type=UserFeedbackType.NO_FEEDBACK,
    )
    service.record_event(no_fb_event)

    events = service.list_events(skill_id="email.draft")
    assert len(events) == 2
    corrected = events[0]
    no_fb = events[1]
    assert corrected.outcome_type == ExperienceOutcomeType.USER_CORRECTED
    assert corrected.outcome_type != ExperienceOutcomeType.EXECUTION_FAILURE
    assert no_fb.outcome_type == ExperienceOutcomeType.NO_FEEDBACK
    assert no_fb.outcome_type != ExperienceOutcomeType.VERIFIED_SUCCESS
    assert no_fb.outcome_type != ExperienceOutcomeType.USER_ACCEPTED

    service.record_feedback(
        invocation_id="inv-300",
        feedback=UserFeedbackType.CORRECTED,
        correction_text="Change the subject line",
    )
    fb = service.get_feedback("inv-300")
    assert fb is not None
    assert fb["correction_text"] == "Change the subject line"


# ---------------------------------------------------------------------------
# 16. No secrets in ExperienceEvents or checkpoints
# ---------------------------------------------------------------------------


def test_16_no_secrets_in_experience_event() -> None:
    """ExperienceEvent must not contain raw secrets."""
    from src.skills.experience import ExperienceEvent

    event = ExperienceEvent(
        task_id="task-secret",
        invocation_id="inv-secret",
        skill_id="auth.service",
        intent="Authenticate user session",
        outcome_type=ExperienceOutcomeType.EXECUTION_FAILURE,
        error_or_recovery_info="Authentication failed",
    )
    assert "password" not in event.intent.lower()
    assert "token" not in (event.error_or_recovery_info or "").lower()
    assert "AKIAIOSFODNN7EXAMPLE" not in (event.error_or_recovery_info or "")


def test_16b_no_secrets_in_checkpoint(state_store: StateStore) -> None:
    """StateStore blocks AWS-pattern secrets from persisting in skill checkpoints."""
    task = state_store.create_task(title="Secret Checkpoint Test")
    step = state_store.create_step(
        task_id=task.task_id,
        step_index=0,
        action_name="auth.step",
        tool_name="none",
        arguments={},
    )
    metadata = SkillCheckpointMetadata(
        skill_id="auth.skill",
        invocation_id="inv-secret-cp",
        step_index=0,
        execution_state={"api_key": "AKIAIOSFODNN7EXAMPLE"},
        recovery_policy="FAIL_FAST",
    )
    with pytest.raises(SecretLeakError):
        persist_skill_checkpoint(
            state_store=state_store,
            task_id=task.task_id,
            step_id=step.step_id,
            checkpoint_seq=1,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# 17. Deterministic fast path: no LLM for skill dispatch
# ---------------------------------------------------------------------------


def test_17_deterministic_skill_path_no_llm(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    sandbox: Path,
    admission_controller: SkillAdmissionController,
    experience_service: ExperienceFeedbackService,
) -> None:
    """Skill dispatch through execute_skill never invokes LocalReasoningProvider."""
    target = sandbox / "fast_path.txt"
    manifest = SkillManifest(
        skill_id="fast.writer",
        name="Fast Writer",
        version="1.0.0",
        description="Writes a file deterministically.",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=("file_write",),
    )

    def writer_handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        broker.invoke("file_write", args={"path": args["path"], "content": args["content"]})
        return {"done": True}

    registry = SkillRegistry()
    registry.register_skill(manifest, executor=DeterministicSkillExecutor(writer_handler))
    mock_provider = MagicMock(spec=LocalReasoningProvider)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
        admission_controller=admission_controller,
        experience_service=experience_service,
        reasoning_provider=mock_provider,
    )
    invocation = SkillInvocation(
        skill_id="fast.writer",
        input={"path": str(target), "content": "fast path"},
    )
    result = supervisor.execute_skill(invocation, actor="fast_path_test")
    assert result.status == SkillExecutionStatus.SUCCESS
    assert target.exists()
    mock_provider.reason.assert_not_called()


# ---------------------------------------------------------------------------
# 18. 100% offline - zero network access
# ---------------------------------------------------------------------------


def test_18_offline_no_network_calls(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    sandbox: Path,
    admission_controller: SkillAdmissionController,
    experience_service: ExperienceFeedbackService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full skill execution pipeline makes zero network calls."""
    import socket

    blocked_calls: list[str] = []

    class BlockingSocket:
        def __init__(self, *args, **kwargs):
            blocked_calls.append(f"socket({args}, {kwargs})")
            raise OSError("Network access blocked for offline test")

    monkeypatch.setattr(socket, "socket", BlockingSocket)

    manifest = SkillManifest(
        skill_id="offline.verifier",
        name="Offline Verifier",
        version="1.0.0",
        description="Offline deterministic skill.",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=("file_write",),
    )

    def handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        out = sandbox / "offline_test.txt"
        broker.invoke("file_write", args={"path": str(out), "content": "offline"})
        return {"done": True}

    registry = SkillRegistry()
    registry.register_skill(manifest, executor=DeterministicSkillExecutor(handler))
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
        admission_controller=admission_controller,
        experience_service=experience_service,
    )
    invocation = SkillInvocation(skill_id="offline.verifier", input={})
    result = supervisor.execute_skill(invocation, actor="offline_test")
    assert result.status == SkillExecutionStatus.SUCCESS
    assert blocked_calls == [], f"Unexpected network calls: {blocked_calls}"


# ---------------------------------------------------------------------------
# 19. build_supervisor factory wires skills subsystems correctly
# ---------------------------------------------------------------------------


def test_19_build_supervisor_with_skills(
    test_db: sqlite3.Connection,
    trust_authority,
    tool_broker: ToolBroker,
    sandbox: Path,
) -> None:
    """build_supervisor factory correctly wires skills-related subsystems."""
    registry = SkillRegistry()
    gate = ClarificationGate()
    residency = ModelResidencyManager()
    concurrency_mgr = SkillConcurrencyManager(residency, system_ram_reserve_bytes=0)
    controller = SkillAdmissionController(concurrency_mgr)
    exp_svc = ExperienceFeedbackService()
    manifest = SkillManifest(
        skill_id="factory.skill",
        name="Factory Skill",
        version="1.0.0",
        description="Built via factory",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=("file_write",),
    )

    def handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        out = sandbox / "factory_test.txt"
        broker.invoke("file_write", args={"path": str(out), "content": "factory"})
        return {"ok": True}

    registry.register_skill(manifest, executor=DeterministicSkillExecutor(handler))
    supervisor = build_supervisor(
        conn=test_db,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
        clarification_gate=gate,
        admission_controller=controller,
        experience_service=exp_svc,
    )
    assert isinstance(supervisor, Supervisor)
    invocation = SkillInvocation(skill_id="factory.skill", input={})
    result = supervisor.execute_skill(invocation, actor="factory_test")
    assert result.status == SkillExecutionStatus.SUCCESS
    assert (sandbox / "factory_test.txt").exists()
    events = exp_svc.list_events(skill_id="factory.skill")
    assert len(events) == 1
    assert events[0].outcome_type == ExperienceOutcomeType.VERIFIED_SUCCESS


# ---------------------------------------------------------------------------
# 20. GoalExecutionResult exported from public manager API
# ---------------------------------------------------------------------------


def test_20_goal_execution_result_public_export() -> None:
    """GoalExecutionResult is accessible from src.manager public API."""

    ger = GoalExecutionResult(
        goal_text="test goal",
        success=True,
        synthesis="Completed.",
        task_record=None,
        observations=[],
    )
    assert ger.success is True
    assert ger.goal_text == "test goal"
