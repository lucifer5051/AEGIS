"""Integration tests for observation-driven replanning in AEGIS (PROMPT 061 / ARCH-060-01).

Covers Tests A through I:
- Test A: Replan from an observed failure
- Test B: Successful step preservation
- Test C: Replanning reaches the original goal
- Test D: Replan limit reached (terminates safely, incomplete status)
- Test E: Invalid revised plan (rejected, fails closed)
- Test F: Authorization is re-evaluated (new tool passes through Trust Core)
- Test G: Uncertain side effect (non-idempotent timeout / ambiguous outcome stops safely)
- Test H: Planner failure (planner raises exception, previous completed steps preserved)
- Test I: State continuity (checkpoints, step records persisted across replan)
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from src.manager.context import ContextIR
from src.manager.models import StepState, TaskState
from src.manager.observation import ObservationStatus, StepObservation
from src.manager.planner import PlannedStep
from src.manager.state_store import StateStore
from src.manager.supervisor import GoalExecutionResult, Supervisor
from src.manager.verifier import StepVerificationResult, StepVerifier, VerificationOutcome
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import ReasoningRequest, ReasoningResponse
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.policy import AutonomyTier


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_replanning.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    root = tmp_path / "replanning_outputs"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def trust_authority(test_db: sqlite3.Connection):
    return build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)


@pytest.fixture
def tool_broker(trust_authority, output_root: Path) -> ToolBroker:
    broker = ToolBroker(trust_authority)
    register_builtin_tools(broker, allowed_roots=[output_root])
    return broker


@pytest.fixture
def state_store(test_db: sqlite3.Connection) -> StateStore:
    return StateStore(test_db)


def _make_reasoning_response(content: str) -> ReasoningResponse:
    return ReasoningResponse(
        content=content,
        finish_reason="stop",
        prompt_tokens=20,
        completion_tokens=60,
        total_tokens=80,
        model_id="mock-qwen",
        provider_id="mock",
    )


def _build_plan_json(steps: List[Dict[str, Any]], goal: str = "Test goal") -> str:
    return json.dumps({
        "goal": goal,
        "rationale": "Execution plan for test.",
        "steps": steps,
    })


# ---------------------------------------------------------------------------
# Test A: Replan from an observed failure
# ---------------------------------------------------------------------------


def test_replan_from_observed_failure(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test A: First step produces an observation requiring replan.
    Planner receives the observation and returns a revised plan which completes.
    """
    good_file = str(output_root / "final_success.txt")

    # Initial plan: step fails verifier criteria because file is not written
    # We use file_read on non-existent file or a verifier that flags semantic failure
    step1 = {
        "step_number": 1,
        "description": "Attempt to read missing initial config",
        "tool": "file_read",
        "arguments": {"path": str(output_root / "nonexistent.txt")},
        "is_idempotent": False,  # Non-idempotent so tool error triggers SEMANTIC_FAILURE -> replan
        "acceptance_criteria": "Config file read successfully",
        "estimated_risk": "LOW",
        "target_resource": str(output_root / "nonexistent.txt"),
    }
    plan1_json = _build_plan_json([step1], goal="Read config or create fallback")

    # Revised plan: write the fallback file
    revised_step = {
        "step_number": 1,
        "description": "Create fallback config file",
        "tool": "file_write",
        "arguments": {"path": good_file, "content": "fallback=true\n"},
        "is_idempotent": True,
        "acceptance_criteria": "file exists",
        "estimated_risk": "MEDIUM",
        "target_resource": good_file,
    }
    plan2_json = _build_plan_json([revised_step], goal="Read config or create fallback")

    # Provider returns plan1 on first call, plan2 on replan call
    mock_provider = MagicMock(spec=LocalReasoningProvider)
    mock_provider.provider_id = "mock"
    mock_provider.model_id = "mock-qwen"
    mock_provider.is_ready.return_value = True
    mock_provider.reason.side_effect = [
        _make_reasoning_response(plan1_json),
        _make_reasoning_response(plan2_json),
    ]

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Read config or create fallback",
        actor="test_user",
    )

    assert result.success is True
    assert Path(good_file).exists()
    assert Path(good_file).read_text(encoding="utf-8") == "fallback=true\n"
    # Ensure reason was called for initial planning and for replanning
    assert mock_provider.reason.call_count >= 2


# ---------------------------------------------------------------------------
# Test B: Successful step preservation
# ---------------------------------------------------------------------------


def test_successful_step_preservation(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test B: Completed step 1 is NOT repeated after replanning.
    Its verified result remains preserved in task state.
    """
    file1 = str(output_root / "step1.txt")
    file2 = str(output_root / "step2_fixed.txt")

    step1 = {
        "step_number": 1,
        "description": "Write step1 marker",
        "tool": "file_write",
        "arguments": {"path": file1, "content": "step1_done\n"},
        "is_idempotent": False,  # Non-idempotent side effect!
        "acceptance_criteria": "file exists",
        "estimated_risk": "MEDIUM",
        "target_resource": file1,
    }
    step2_fail = {
        "step_number": 2,
        "description": "Failing step",
        "tool": "file_read",
        "arguments": {"path": str(output_root / "nonexistent2.txt")},
        "is_idempotent": False,
        "acceptance_criteria": "file read ok",
        "estimated_risk": "LOW",
        "target_resource": str(output_root / "nonexistent2.txt"),
    }
    plan1_json = _build_plan_json([step1, step2_fail], goal="Two-step pipeline")

    revised_step = {
        "step_number": 1,
        "description": "Write step2 marker",
        "tool": "file_write",
        "arguments": {"path": file2, "content": "step2_done\n"},
        "is_idempotent": True,
        "acceptance_criteria": "file exists",
        "estimated_risk": "MEDIUM",
        "target_resource": file2,
    }
    plan2_json = _build_plan_json([revised_step], goal="Two-step pipeline")

    mock_provider = MagicMock(spec=LocalReasoningProvider)
    mock_provider.provider_id = "mock"
    mock_provider.model_id = "mock-qwen"
    mock_provider.is_ready.return_value = True
    mock_provider.reason.side_effect = [
        _make_reasoning_response(plan1_json),
        _make_reasoning_response(plan2_json),
    ]

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Two-step pipeline",
        actor="test_user",
    )

    assert result.success is True
    assert Path(file1).exists()
    assert Path(file2).exists()

    # Verify step records in store: step1 was completed and never re-run
    task_steps = state_store.list_task_steps(result.task_record.task_id)
    step1_records = [s for s in task_steps if s.arguments.get("path") == file1]
    assert len(step1_records) == 1
    assert step1_records[0].state == StepState.COMPLETED


# ---------------------------------------------------------------------------
# Test C: Replanning reaches the original goal
# ---------------------------------------------------------------------------


def test_replanning_reaches_original_goal(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test C: Multi-step fixture requires replanning and eventually completes the goal."""
    target_out = str(output_root / "goal_output.txt")

    step_broken = {
        "step_number": 1,
        "description": "Try broken operation",
        "tool": "file_read",
        "arguments": {"path": str(output_root / "absent.txt")},
        "is_idempotent": False,
        "acceptance_criteria": "read absent file",
        "estimated_risk": "LOW",
        "target_resource": str(output_root / "absent.txt"),
    }
    plan1_json = _build_plan_json([step_broken], goal="Ensure goal output is created")

    step_fixed = {
        "step_number": 1,
        "description": "Write target output directly",
        "tool": "file_write",
        "arguments": {"path": target_out, "content": "goal achieved\n"},
        "is_idempotent": True,
        "acceptance_criteria": "file exists",
        "estimated_risk": "MEDIUM",
        "target_resource": target_out,
    }
    plan2_json = _build_plan_json([step_fixed], goal="Ensure goal output is created")

    mock_provider = MagicMock(spec=LocalReasoningProvider)
    mock_provider.provider_id = "mock"
    mock_provider.model_id = "mock-qwen"
    mock_provider.is_ready.return_value = True
    mock_provider.reason.side_effect = [
        _make_reasoning_response(plan1_json),
        _make_reasoning_response(plan2_json),
    ]

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Ensure goal output is created",
        actor="test_user",
    )

    assert result.success is True
    assert result.task_record.state == TaskState.COMPLETED
    assert Path(target_out).exists()


# ---------------------------------------------------------------------------
# Test D: Replan limit reached terminates safely
# ---------------------------------------------------------------------------


def test_replan_limit_reached_terminates_safely(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test D: When replanning is continuously required, execution halts at configured limit."""
    step_failing = {
        "step_number": 1,
        "description": "Repeated failing action",
        "tool": "file_read",
        "arguments": {"path": str(output_root / "missing.txt")},
        "is_idempotent": False,
        "acceptance_criteria": "missing file exists",
        "estimated_risk": "LOW",
        "target_resource": str(output_root / "missing.txt"),
    }
    plan_json = _build_plan_json([step_failing], goal="Unachievable goal")

    mock_provider = MagicMock(spec=LocalReasoningProvider)
    mock_provider.provider_id = "mock"
    mock_provider.model_id = "mock-qwen"
    mock_provider.is_ready.return_value = True
    # Always returns the same failing plan
    mock_provider.reason.return_value = _make_reasoning_response(plan_json)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    # Use max_replan_attempts=2
    result = supervisor.execute_goal(
        goal_text="Unachievable goal",
        actor="test_user",
        max_replan_attempts=2,
    )

    assert result.success is False
    assert result.task_record.state == TaskState.FAILED
    assert "replan limit" in result.synthesis.lower() or "halted" in result.synthesis.lower()


# ---------------------------------------------------------------------------
# Test E: Invalid revised plan is rejected (fails closed)
# ---------------------------------------------------------------------------


def test_invalid_revised_plan_rejected(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test E: Malformed or boundary-violating revised plan is rejected."""
    step1 = {
        "step_number": 1,
        "description": "Step that fails",
        "tool": "file_read",
        "arguments": {"path": str(output_root / "missing.txt")},
        "is_idempotent": False,
        "acceptance_criteria": "ok",
        "estimated_risk": "LOW",
        "target_resource": str(output_root / "missing.txt"),
    }
    plan1_json = _build_plan_json([step1], goal="Test invalid replan")

    # Revised plan references an invalid/unknown tool
    bad_revised_plan = json.dumps({
        "goal": "Test invalid replan",
        "rationale": "Uses non-existent tool",
        "steps": [
            {
                "step_number": 1,
                "description": "Malicious or unknown tool invocation",
                "tool": "nonexistent_system_destroyer",
                "arguments": {},
                "is_idempotent": False,
                "acceptance_criteria": "done",
                "estimated_risk": "CRITICAL",
                "target_resource": "*",
            }
        ],
    })

    mock_provider = MagicMock(spec=LocalReasoningProvider)
    mock_provider.provider_id = "mock"
    mock_provider.model_id = "mock-qwen"
    mock_provider.is_ready.return_value = True
    # 1: initial plan
    # 2: replan query -> returns bad_revised_plan
    # 3: repair attempt query -> returns bad_revised_plan again
    mock_provider.reason.side_effect = [
        _make_reasoning_response(plan1_json),
        _make_reasoning_response(bad_revised_plan),
        _make_reasoning_response(bad_revised_plan),
    ]

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Test invalid replan",
        actor="test_user",
    )

    assert result.success is False
    assert result.task_record.state == TaskState.FAILED
    assert "replanning failed" in result.synthesis.lower() or "unknown tool" in result.synthesis.lower()


# ---------------------------------------------------------------------------
# Test F: Authorization is re-evaluated on replanned steps
# ---------------------------------------------------------------------------


def test_authorization_reevaluated_on_replan(
    test_db: sqlite3.Connection,
    state_store: StateStore,
    output_root: Path,
) -> None:
    """Test F: A revised plan proposing a new action must pass through Trust Core.
    In STRICT_MANUAL autonomy tier, a MEDIUM-risk file_write is denied/escalates.
    """
    strict_auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.STRICT_MANUAL)
    broker = ToolBroker(strict_auth)
    register_builtin_tools(broker, allowed_roots=[output_root])

    # Initial plan: low-risk tool that fails
    step1 = {
        "step_number": 1,
        "description": "Low risk read that fails",
        "tool": "file_read",
        "arguments": {"path": str(output_root / "not_there.txt")},
        "is_idempotent": False,
        "acceptance_criteria": "read file",
        "estimated_risk": "LOW",
        "target_resource": str(output_root / "not_there.txt"),
    }
    plan1_json = _build_plan_json([step1], goal="Strict manual authorization test")

    # Revised plan: proposes file_write (MEDIUM risk, requires approval under STRICT_MANUAL)
    file_target = str(output_root / "denied_file.txt")
    step2 = {
        "step_number": 1,
        "description": "Write file under strict policy",
        "tool": "file_write",
        "arguments": {"path": file_target, "content": "secret\n"},
        "is_idempotent": True,
        "acceptance_criteria": "file exists",
        "estimated_risk": "MEDIUM",
        "target_resource": file_target,
    }
    plan2_json = _build_plan_json([step2], goal="Strict manual authorization test")

    mock_provider = MagicMock(spec=LocalReasoningProvider)
    mock_provider.provider_id = "mock"
    mock_provider.model_id = "mock-qwen"
    mock_provider.is_ready.return_value = True
    mock_provider.reason.side_effect = [
        _make_reasoning_response(plan1_json),
        _make_reasoning_response(plan2_json),
    ]

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=strict_auth,
        tool_broker=broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Strict manual authorization test",
        actor="test_user",
    )

    # Must fail or escalate to awaiting approval — Trust Core must NOT be bypassed!
    assert result.success is False
    assert not Path(file_target).exists()
    assert (
        result.task_record.state == TaskState.AWAITING_APPROVAL
        or result.task_record.state == TaskState.FAILED
    )


# ---------------------------------------------------------------------------
# Test G: Uncertain side effect stops safely
# ---------------------------------------------------------------------------


def test_uncertain_side_effect_stops_safely(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test G: An ambiguous or uncertain non-idempotent operation is NOT blindly repeated."""
    file_path = str(output_root / "uncertain_file.txt")

    # Non-idempotent step with timeout simulation
    step1 = {
        "step_number": 1,
        "description": "Non-idempotent write with uncertain outcome",
        "tool": "file_write",
        "arguments": {"path": file_path, "content": "data\n"},
        "is_idempotent": False,
        "acceptance_criteria": "file written",
        "estimated_risk": "MEDIUM",
        "target_resource": file_path,
    }
    plan1_json = _build_plan_json([step1], goal="Uncertain side effect test")

    # Replanner naively tries the exact same non-idempotent action again
    plan2_json = _build_plan_json([step1], goal="Uncertain side effect test")

    mock_provider = MagicMock(spec=LocalReasoningProvider)
    mock_provider.provider_id = "mock"
    mock_provider.model_id = "mock-qwen"
    mock_provider.is_ready.return_value = True
    mock_provider.reason.side_effect = [
        _make_reasoning_response(plan1_json),
        _make_reasoning_response(plan2_json),
    ]

    # Custom verifier that simulates timeout / UNKNOWN on the first step
    class TimeoutVerifier(StepVerifier):
        def verify(self, step: PlannedStep, observation: StepObservation) -> StepVerificationResult:
            return StepVerificationResult(
                outcome=VerificationOutcome.UNKNOWN,
                rationale="Uncertain side effect outcome: timed out before confirmation",
                can_retry=False,
                requires_replan=True,
                requires_approval=False,
            )

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
        verifier=TimeoutVerifier(),
    )

    result = supervisor.execute_goal(
        goal_text="Uncertain side effect test",
        actor="test_user",
    )

    # Must halt safely without blindly repeating the uncertain non-idempotent action
    assert result.success is False
    assert result.task_record.state == TaskState.FAILED
    assert "uncertain" in result.synthesis.lower() or "halted" in result.synthesis.lower()


# ---------------------------------------------------------------------------
# Test H: Planner failure preserves previous state
# ---------------------------------------------------------------------------


def test_planner_failure_preserves_previous_state(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test H: When replanning raises an error, previous completed steps and checkpoints are preserved."""
    file1 = str(output_root / "step1_persists.txt")

    step1 = {
        "step_number": 1,
        "description": "First successful step",
        "tool": "file_write",
        "arguments": {"path": file1, "content": "step1 ok\n"},
        "is_idempotent": True,
        "acceptance_criteria": "file exists",
        "estimated_risk": "MEDIUM",
        "target_resource": file1,
    }
    step2 = {
        "step_number": 2,
        "description": "Step that fails",
        "tool": "file_read",
        "arguments": {"path": str(output_root / "none.txt")},
        "is_idempotent": False,
        "acceptance_criteria": "ok",
        "estimated_risk": "LOW",
        "target_resource": str(output_root / "none.txt"),
    }
    plan1_json = _build_plan_json([step1, step2], goal="Planner failure preservation test")

    mock_provider = MagicMock(spec=LocalReasoningProvider)
    mock_provider.provider_id = "mock"
    mock_provider.model_id = "mock-qwen"
    mock_provider.is_ready.return_value = True

    # First call returns plan1. Second call (replan) raises RuntimeError
    def side_effect(req: ReasoningRequest):
        if "REMAINING" in req.prompt or "ALREADY COMPLETED" in req.prompt:
            raise RuntimeError("LLM reasoning service crashed during replan")
        return _make_reasoning_response(plan1_json)

    mock_provider.reason.side_effect = side_effect

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Planner failure preservation test",
        actor="test_user",
    )

    assert result.success is False
    assert result.task_record.state == TaskState.FAILED
    assert "replanning failed" in result.synthesis.lower()

    # Verify step1 is preserved as COMPLETED in store
    task_steps = state_store.list_task_steps(result.task_record.task_id)
    step1_records = [s for s in task_steps if s.arguments.get("path") == file1]
    assert len(step1_records) == 1
    assert step1_records[0].state == StepState.COMPLETED
    assert Path(file1).exists()


# ---------------------------------------------------------------------------
# Test I: State continuity and checkpoints survive across replan
# ---------------------------------------------------------------------------


def test_state_continuity_and_checkpoints(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Test I: Full state continuity: step records (COMPLETED, FAILED, new steps)
    and checkpoints (PLAN_CREATED, REPLAN_TRIGGERED, REPLAN_COMMITTED) are persisted durably.
    """
    file_initial = str(output_root / "initial_work.txt")
    file_final = str(output_root / "final_work.txt")

    step1 = {
        "step_number": 1,
        "description": "Initial successful step",
        "tool": "file_write",
        "arguments": {"path": file_initial, "content": "initial\n"},
        "is_idempotent": True,
        "acceptance_criteria": "file exists",
        "estimated_risk": "MEDIUM",
        "target_resource": file_initial,
    }
    step2_fail = {
        "step_number": 2,
        "description": "Failing intermediate step",
        "tool": "file_read",
        "arguments": {"path": str(output_root / "unfound.txt")},
        "is_idempotent": False,
        "acceptance_criteria": "read unfound",
        "estimated_risk": "LOW",
        "target_resource": str(output_root / "unfound.txt"),
    }
    plan1_json = _build_plan_json([step1, step2_fail], goal="Checkpoints test")

    revised_step = {
        "step_number": 1,
        "description": "Final resolving step",
        "tool": "file_write",
        "arguments": {"path": file_final, "content": "completed\n"},
        "is_idempotent": True,
        "acceptance_criteria": "file exists",
        "estimated_risk": "MEDIUM",
        "target_resource": file_final,
    }
    plan2_json = _build_plan_json([revised_step], goal="Checkpoints test")

    mock_provider = MagicMock(spec=LocalReasoningProvider)
    mock_provider.provider_id = "mock"
    mock_provider.model_id = "mock-qwen"
    mock_provider.is_ready.return_value = True
    mock_provider.reason.side_effect = [
        _make_reasoning_response(plan1_json),
        _make_reasoning_response(plan2_json),
    ]

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Checkpoints test",
        actor="test_user",
    )

    assert result.success is True
    task_id = result.task_record.task_id

    # Check step states in SQLite StateStore
    all_steps = state_store.list_task_steps(task_id)
    assert len(all_steps) >= 3

    completed_steps = [s for s in all_steps if s.state == StepState.COMPLETED]
    failed_steps = [s for s in all_steps if s.state == StepState.FAILED]
    assert len(completed_steps) == 2  # step1 and revised_step
    assert len(failed_steps) == 1  # step2_fail

    # Check checkpoints in SQLite StateStore
    checkpoints = state_store.list_checkpoints(task_id)
    events = [cp.snapshot.get("event") for cp in checkpoints if cp.snapshot]

    assert "PLAN_FORMULATED" in events
    assert "REPLAN_TRIGGERED" in events
    assert "REPLAN_COMMITTED" in events
    assert "PRE_EXECUTION" in events
    assert "POST_EXECUTION" in events
