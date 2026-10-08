"""Comprehensive Offline Tests for SAGE Skills Architecture & Tool Integration.

Phase 6 Test Suite verifying:
- Declarative Skill Manifest & deterministic validation.
- Deterministic Skill Registry & lifecycle transitions.
- Duplicate rejection fail-closed.
- Tool binding enforcement (Skill cannot access undeclared tools).
- Trust Core authority invariant (Skill cannot bypass Trust Core).
- Tool Discovery & filtering (ToolScope principle).
- Resource & concurrency admission gate.
- Invocation & result contracts.
- Adaptive execution foundation & ExperienceEvent telemetry.
- Feedback classification (USER_CORRECTED != failure).
- Clarification Gate (missing parameter, ambiguity, continuation).
- Checkpoint metadata compatibility with StateStore.
- Supervisor -> Skill orchestration protocol.
- 100% offline, zero network access.
"""

import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.model_policy.concurrency_manager import SkillConcurrencyManager
from src.model_policy.residency_manager import ModelResidencyManager
from src.skills import (
    BoundSkillToolBroker,
    ClarificationAction,
    ClarificationGate,
    ClarificationReason,
    DeterministicSkillExecutor,
    ExperienceEvent,
    ExperienceFeedbackService,
    ExperienceOutcomeType,
    SkillAdmissionController,
    SkillCheckpointMetadata,
    SkillDuplicateError,
    SkillExecutionStatus,
    SkillInvocation,
    SkillLifecycleError,
    SkillLifecycleState,
    SkillManifest,
    SkillManifestValidationError,
    SkillRegistry,
    SkillResourceRequirement,
    SkillToolAccessDeniedError,
    SkillType,
    ToolDiscoveryService,
    UserFeedbackType,
    VerificationStatus,
    persist_skill_checkpoint,
    validate_skill_manifest,
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
    """Create test SQLite database with all migrations applied."""
    db_file = tmp_path / "test_skills.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def trust_authority(test_db: sqlite3.Connection):
    """Trust authority at FULL_BOUNDED tier."""
    return build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)


@pytest.fixture
def tool_broker(trust_authority, tmp_path: Path) -> ToolBroker:
    """Tool broker with registered safe built-in tools."""
    broker = ToolBroker(trust_authority)
    output_dir = tmp_path / "sandbox"
    output_dir.mkdir(parents=True, exist_ok=True)
    register_builtin_tools(broker, allowed_roots=[output_dir])
    return broker


@pytest.fixture
def state_store(test_db: sqlite3.Connection) -> StateStore:
    return StateStore(test_db)


@pytest.fixture
def valid_manifest() -> SkillManifest:
    """Sample valid deterministic skill manifest."""
    return SkillManifest(
        skill_id="file.read_notes",
        name="Notes Reader",
        version="1.0.0",
        description="Reads note files from the workspace directory.",
        skill_type=SkillType.DETERMINISTIC,
        intent_tags=("notes", "file", "read"),
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"content": {"type": "string"}}},
        tool_bindings=("file_read",),
        required_capabilities=("filesystem.read",),
        risk_class=ActionRisk.LOW,
    )


# ---------------------------------------------------------------------------
# 1. Manifest Validation Tests
# ---------------------------------------------------------------------------


def test_01_valid_skill_registration(valid_manifest: SkillManifest):
    """Valid skill registers successfully and transitions to AVAILABLE."""
    registry = SkillRegistry()
    reg = registry.register_skill(valid_manifest)

    assert reg.manifest.skill_id == "file.read_notes"
    assert reg.current_state == SkillLifecycleState.AVAILABLE
    assert registry.get_skill("file.read_notes") == reg


def test_02_invalid_manifest_rejections():
    """Invalid manifests are rejected fail-closed by validation."""
    # Empty skill_id
    with pytest.raises(SkillManifestValidationError):
        validate_skill_manifest(
            SkillManifest(
                skill_id="",
                name="Bad",
                version="1.0.0",
                description="desc",
                skill_type=SkillType.DETERMINISTIC,
            )
        )

    # Invalid characters in skill_id (uppercase, spaces)
    with pytest.raises(SkillManifestValidationError):
        validate_skill_manifest(
            SkillManifest(
                skill_id="Bad ID With Spaces",
                name="Bad",
                version="1.0.0",
                description="desc",
                skill_type=SkillType.DETERMINISTIC,
            )
        )

    # Invalid SemVer
    with pytest.raises(SkillManifestValidationError):
        validate_skill_manifest(
            SkillManifest(
                skill_id="bad.version",
                name="Bad",
                version="v1_beta",
                description="desc",
                skill_type=SkillType.DETERMINISTIC,
            )
        )

    # Negative RAM
    with pytest.raises(ValueError):  # Pydantic ge=0 constraint
        SkillResourceRequirement(declared_marginal_ram_bytes=-1024)

    # Manifest attempting to inject Trust Core overrides
    with pytest.raises(SkillManifestValidationError):
        validate_skill_manifest(
            SkillManifest(
                skill_id="malicious.skill",
                name="Malicious",
                version="1.0.0",
                description="Attempts to bypass Trust Core",
                skill_type=SkillType.DETERMINISTIC,
                input_schema={"trust_core_override": True},
            )
        )


def test_03_duplicate_skill_rejection(valid_manifest: SkillManifest):
    """Registering a duplicate skill ID fails closed."""
    registry = SkillRegistry()
    registry.register_skill(valid_manifest)

    with pytest.raises(SkillDuplicateError):
        registry.register_skill(valid_manifest)


# ---------------------------------------------------------------------------
# 2. Lifecycle State Machine Tests
# ---------------------------------------------------------------------------


def test_04_lifecycle_transitions(valid_manifest: SkillManifest):
    """Verify state machine transitions along the happy and error paths."""
    registry = SkillRegistry()
    registry.register_skill(valid_manifest)

    # AVAILABLE -> SELECTED -> ADMITTED -> EXECUTING -> VERIFYING -> SUCCEEDED -> AVAILABLE
    s1 = registry.transition_lifecycle(valid_manifest.skill_id, SkillLifecycleState.SELECTED)
    assert s1.current_state == SkillLifecycleState.SELECTED

    s2 = registry.transition_lifecycle(valid_manifest.skill_id, SkillLifecycleState.ADMITTED)
    assert s2.current_state == SkillLifecycleState.ADMITTED

    s3 = registry.transition_lifecycle(valid_manifest.skill_id, SkillLifecycleState.EXECUTING)
    assert s3.current_state == SkillLifecycleState.EXECUTING

    s4 = registry.transition_lifecycle(valid_manifest.skill_id, SkillLifecycleState.VERIFYING)
    assert s4.current_state == SkillLifecycleState.VERIFYING

    s5 = registry.transition_lifecycle(valid_manifest.skill_id, SkillLifecycleState.SUCCEEDED)
    assert s5.current_state == SkillLifecycleState.SUCCEEDED

    s6 = registry.transition_lifecycle(valid_manifest.skill_id, SkillLifecycleState.AVAILABLE)
    assert s6.current_state == SkillLifecycleState.AVAILABLE

    # Illegal transition: AVAILABLE -> EXECUTING directly
    with pytest.raises(SkillLifecycleError):
        registry.transition_lifecycle(valid_manifest.skill_id, SkillLifecycleState.EXECUTING)


# ---------------------------------------------------------------------------
# 3. Tool Binding & Security Invariant Tests
# ---------------------------------------------------------------------------


def test_05_unknown_tool_rejection():
    """Manifest referencing unknown tool rejected when checked against catalog."""
    manifest = SkillManifest(
        skill_id="test.unknown_tool",
        name="Unknown Tool Skill",
        version="1.0.0",
        description="References uncataloged tool",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=("exfiltrate_network",),
    )
    with pytest.raises(SkillManifestValidationError):
        validate_skill_manifest(manifest, known_tools=["file_read", "file_write"])


def test_06_tool_binding_enforcement(tool_broker: ToolBroker, valid_manifest: SkillManifest):
    """Skill can ONLY invoke tools declared in its manifest tool_bindings."""
    bound_broker = BoundSkillToolBroker(
        broker=tool_broker,
        manifest=valid_manifest,  # only has 'file_read'
        actor="test_skill",
    )

    # Attempt to invoke 'file_write' which is NOT in tool_bindings
    with pytest.raises(SkillToolAccessDeniedError) as exc_info:
        bound_broker.invoke("file_write", args={"path": "test.txt", "content": "hi"})

    assert "not authorized to invoke tool 'file_write'" in str(exc_info.value)


def test_07_skill_cannot_bypass_trust_core(test_db: sqlite3.Connection, tmp_path: Path):
    """Even if declared in tool_bindings, Tool Broker and Trust Core deny unauthorized actions."""
    # STRICT_MANUAL autonomy tier
    strict_authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.STRICT_MANUAL)
    broker = ToolBroker(strict_authority)
    register_builtin_tools(broker, allowed_roots=[tmp_path])

    manifest = SkillManifest(
        skill_id="file.writer",
        name="Writer",
        version="1.0.0",
        description="Writes files",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=("file_write",),
        risk_class=ActionRisk.MEDIUM,
    )

    bound_broker = BoundSkillToolBroker(
        broker=broker,
        manifest=manifest,
        actor="test_actor",
    )

    # Invoking file_write (MEDIUM risk) at STRICT_MANUAL requires approval -> DENIED
    result = bound_broker.invoke(
        "file_write",
        args={"path": str(tmp_path / "out.txt"), "content": "data"},
    )
    assert result.status.value == "denied"
    assert not (tmp_path / "out.txt").exists()


def test_08_skill_tool_broker_integration(tool_broker: ToolBroker, tmp_path: Path):
    """Skill executes successfully via BoundSkillToolBroker when authorized."""
    sandbox = tmp_path / "sandbox"
    test_file = sandbox / "hello.txt"
    test_file.write_text("Hello from file!", encoding="utf-8")

    manifest = SkillManifest(
        skill_id="reader.notes",
        name="Notes Reader",
        version="1.0.0",
        description="Reads notes",
        skill_type=SkillType.DETERMINISTIC,
        tool_bindings=("file_read",),
        risk_class=ActionRisk.LOW,
    )

    def reader_handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        res = broker.invoke("file_read", args={"path": args["path"]})
        return {"content": res.output}

    executor = DeterministicSkillExecutor(reader_handler)
    bound_broker = BoundSkillToolBroker(broker=tool_broker, manifest=manifest, actor="tester")

    invocation = SkillInvocation(
        skill_id="reader.notes",
        input={"path": str(test_file)},
    )

    result = executor.execute(invocation, bound_broker)
    assert result.status == SkillExecutionStatus.SUCCESS
    assert "Hello from file!" in result.output["content"]
    assert result.verification_status == VerificationStatus.VERIFIED


# ---------------------------------------------------------------------------
# 4. Tool Discovery & Filtering (ToolScope Principle)
# ---------------------------------------------------------------------------


def test_09_tool_discovery_and_filtering(tool_broker: ToolBroker):
    """ToolDiscoveryService filters catalog deterministically by intent, risk, and top_k."""
    service = ToolDiscoveryService()
    catalog = tool_broker.list_tools()

    # 1. Filter by intent "read file"
    tools = service.filter_tools(catalog, intent="read file contents", top_k=2)
    assert len(tools) <= 2
    assert tools[0].name == "file_read"

    # 2. Filter by risk limit (LOW only)
    low_tools = service.filter_tools(catalog, max_risk=ActionRisk.LOW)
    for t in low_tools:
        assert t.risk == ActionRisk.LOW

    # 3. Filter by allowed_bindings subset
    bound_tools = service.filter_tools(catalog, allowed_bindings=["file_list"])
    assert len(bound_tools) == 1
    assert bound_tools[0].name == "file_list"


# ---------------------------------------------------------------------------
# 5. Resource & Concurrency Admission Tests
# ---------------------------------------------------------------------------


def test_10_resource_admission_controller(valid_manifest: SkillManifest):
    """SkillAdmissionController evaluates admission against live memory headroom."""
    residency = ModelResidencyManager()
    concurrency_mgr = SkillConcurrencyManager(residency)
    controller = SkillAdmissionController(concurrency_mgr)

    decision = controller.evaluate_admission(valid_manifest)
    assert decision.admitted is True
    assert decision.action == "PROCEED"
    assert decision.lease is not None

    controller.release_lease(decision.lease)


def test_11_insufficient_resource_admission_rejection():
    """Skill requesting excessive RAM is rejected deterministically."""
    residency = ModelResidencyManager()
    concurrency_mgr = SkillConcurrencyManager(
        residency,
        system_ram_reserve_bytes=1024 * 1024 * 1024,
    )
    controller = SkillAdmissionController(concurrency_mgr)

    # Manifest declaring 1 Terabyte of RAM
    heavy_manifest = SkillManifest(
        skill_id="heavy.worker",
        name="Heavy Worker",
        version="1.0.0",
        description="Huge memory skill",
        skill_type=SkillType.DETERMINISTIC,
        resource_requirements=SkillResourceRequirement(
            declared_marginal_ram_bytes=1024 * 1024 * 1024 * 1024
        ),
    )

    decision = controller.evaluate_admission(heavy_manifest)
    assert decision.admitted is False
    assert decision.action in ("REJECT", "DEFER", "QUEUE")


# ---------------------------------------------------------------------------
# 6. Adaptive Execution & Experience Foundation Tests
# ---------------------------------------------------------------------------


def test_12_experience_event_and_feedback():
    """Experience events and user feedback are recorded decoupled from execution."""
    feedback_service = ExperienceFeedbackService()

    event = ExperienceEvent(
        task_id="task-100",
        invocation_id="inv-200",
        skill_id="file.read_notes",
        intent="Read project notes",
        selected_tools=("file_read",),
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
        user_feedback_type=UserFeedbackType.ACCEPTED,
        latency_ms=12.5,
    )

    feedback_service.record_event(event)
    events = feedback_service.list_events(skill_id="file.read_notes")
    assert len(events) == 1
    assert events[0].invocation_id == "inv-200"

    # Record explicit user feedback
    feedback_service.record_feedback(
        invocation_id="inv-200",
        feedback=UserFeedbackType.EXPLICIT_POSITIVE,
        notes="Fast and accurate",
    )
    fb = feedback_service.get_feedback("inv-200")
    assert fb is not None
    assert fb["feedback"] == UserFeedbackType.EXPLICIT_POSITIVE


def test_13_user_corrected_not_automatic_failure():
    """Invariant: USER_CORRECTED does NOT automatically mean execution failure."""
    event = ExperienceEvent(
        task_id="task-101",
        invocation_id="inv-201",
        skill_id="email.draft",
        intent="Draft meeting notes email",
        outcome_type=ExperienceOutcomeType.USER_CORRECTED,
        user_feedback_type=UserFeedbackType.CORRECTED,
    )
    # Corrected experience must be valid evidence, not treated as an execution crash
    assert event.outcome_type == ExperienceOutcomeType.USER_CORRECTED
    assert event.outcome_type != ExperienceOutcomeType.EXECUTION_FAILURE


# ---------------------------------------------------------------------------
# 7. Clarification Gate Tests
# ---------------------------------------------------------------------------


def test_14_clarification_missing_required_parameter():
    """ClarificationGate catches missing parameters without LLM calls."""
    gate = ClarificationGate()

    # "Set an alarm" missing target time
    res = gate.evaluate(
        provided_args={"intent": "set_alarm"},
        required_keys=["alarm_time"],
    )
    assert res.action == ClarificationAction.CLARIFY
    assert res.reason == ClarificationReason.MISSING_REQUIRED_PARAMETER
    assert res.clarification_prompt == "What time should I set the alarm?"
    assert "alarm_time" in res.missing_parameters


def test_15_clarification_ambiguous_targets():
    """ClarificationGate detects multiple ambiguous file matches."""
    gate = ClarificationGate()

    res = gate.evaluate(
        provided_args={"action": "open"},
        required_keys=["action"],
        candidate_matches={"file": ["budget_2025.xlsx", "budget_2026.xlsx"]},
    )
    assert res.action == ClarificationAction.CLARIFY
    assert res.reason == ClarificationReason.AMBIGUOUS_FILE
    assert "budget_2025.xlsx" in res.clarification_prompt


def test_16_clarification_deterministic_continuation():
    """ClarificationGate returns CONTINUE when all inputs are unambiguous."""
    gate = ClarificationGate()

    res = gate.evaluate(
        provided_args={"path": "report.pdf", "format": "pdf"},
        required_keys=["path"],
        candidate_matches={"file": ["report.pdf"]},
    )
    assert res.action == ClarificationAction.CONTINUE
    assert res.clarification_prompt is None


# ---------------------------------------------------------------------------
# 8. Checkpoint Integration Tests
# ---------------------------------------------------------------------------


def test_17_checkpoint_metadata_compatibility(state_store: StateStore):
    """Skill execution checkpoint integrates with durable StateStore."""
    task = state_store.create_task(title="Skill Execution Task")
    step = state_store.create_step(
        task_id=task.task_id,
        step_index=1,
        action_name="skill.step",
        tool_name="file_read",
        arguments={},
    )

    metadata = SkillCheckpointMetadata(
        skill_id="data.processor",
        invocation_id="inv-999",
        step_index=1,
        execution_state={"processed_rows": 1500, "cursor": 1501},
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
    loaded = state_store.get_latest_checkpoint(task.task_id)
    assert loaded is not None

    reconstructed = SkillCheckpointMetadata.from_snapshot_dict(loaded.snapshot)
    assert reconstructed.skill_id == "data.processor"
    assert reconstructed.execution_state["processed_rows"] == 1500


def test_18_no_secret_leak_in_checkpoint(state_store: StateStore):
    """StateStore secret scanner blocks accidental credential persistence in skill checkpoints."""
    task = state_store.create_task(title="Security Check")
    step = state_store.create_step(
        task_id=task.task_id,
        step_index=1,
        action_name="skill.step",
        tool_name="none",
        arguments={},
    )

    metadata = SkillCheckpointMetadata(
        skill_id="auth.skill",
        invocation_id="inv-123",
        step_index=1,
        execution_state={"api_key": "AKIAIOSFODNN7EXAMPLE"},  # AWS access key pattern
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
# 9. Supervisor -> Skill Protocol End-to-End Test
# ---------------------------------------------------------------------------


def test_19_supervisor_skill_protocol_flow(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    valid_manifest: SkillManifest,
    tmp_path: Path,
):
    """End-to-end test of Supervisor -> Skill Registry -> Admission -> Execution -> Verification."""
    sandbox = tmp_path / "sandbox"
    note_path = sandbox / "meeting.txt"
    note_path.write_text("Discuss Phase 6 Skills", encoding="utf-8")

    # 1. Setup Skill Registry & Executor
    registry = SkillRegistry()

    def notes_handler(args: Dict[str, Any], broker: BoundSkillToolBroker) -> Dict[str, Any]:
        res = broker.invoke("file_read", args={"path": args["path"]})
        return {"content": res.output}

    executor = DeterministicSkillExecutor(notes_handler)
    registry.register_skill(valid_manifest, executor=executor)

    # 2. Setup Admission & Feedback
    residency = ModelResidencyManager()
    concurrency_mgr = SkillConcurrencyManager(residency)
    admission_controller = SkillAdmissionController(concurrency_mgr)
    feedback_service = ExperienceFeedbackService()
    clarification_gate = ClarificationGate()

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        skill_registry=registry,
        clarification_gate=clarification_gate,
        admission_controller=admission_controller,
        experience_service=feedback_service,
    )

    # 3. Supervisor selects skill
    selected = supervisor.select_skill("read notes from workspace")
    assert selected is not None
    assert selected.manifest.skill_id == "file.read_notes"
    assert selected.current_state == SkillLifecycleState.SELECTED

    # 4. Supervisor executes skill
    invocation = SkillInvocation(
        skill_id="file.read_notes",
        input={"path": str(note_path)},
    )
    result = supervisor.execute_skill(invocation)

    assert result.status == SkillExecutionStatus.SUCCESS
    assert "Discuss Phase 6 Skills" in result.output["content"]
    assert result.verification_status == VerificationStatus.VERIFIED

    # 5. Experience telemetry verified
    events = feedback_service.list_events(skill_id="file.read_notes")
    assert len(events) == 1
    assert events[0].outcome_type == ExperienceOutcomeType.VERIFIED_SUCCESS
    assert events[0].selected_tools == ("file_read",)

    # 6. Skill lifecycle restored to AVAILABLE for next use
    refreshed_skill = registry.get_skill("file.read_notes")
    assert refreshed_skill.current_state == SkillLifecycleState.AVAILABLE


def test_20_supervisor_clarification_gate_short_circuit(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
):
    """Supervisor execute_goal returns clarification immediately if parameter missing."""
    gate = ClarificationGate()
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        clarification_gate=gate,
    )

    # Calling with empty goal text
    result = supervisor.execute_goal(goal_text="")
    assert result.success is False
    assert "Clarification required" in result.synthesis
