"""End-to-End Integration Test: SAGE Core Agent Loop.

Tests the complete agent loop with mocked local reasoning:
    Goal → ContextIR → Plan (mock LLM) → Supervisor → Trust Core
         → ToolBroker (file_write) → Observation → Verifier
         → Synthesis → GoalExecutionResult(success=True)

Architecture invariants proven:
1. Intelligence proposes (planner) → deterministic authority decides (TrustCore).
2. Tool invocation only through ToolBroker.
3. Observation and verification happen before synthesis.
4. Fail-closed: denied actions produce success=False, not exceptions.
5. File written to allowed temp path outside OneDrive/repo/AEGIS.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.manager.observation import ObservationStatus
from src.manager.state_store import StateStore
from src.manager.supervisor import GoalExecutionResult, Supervisor
from src.manager.verifier import StepVerifier, VerificationOutcome
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import ReasoningResponse
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
    db_file = tmp_path / "test_sage_e2e.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    """A safe writable directory for file_write tool tests."""
    root = tmp_path / "sage_outputs"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def trust_authority(test_db: sqlite3.Connection):
    # FULL_BOUNDED so file_write (MEDIUM) is allowed without escalation
    return build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)


@pytest.fixture
def tool_broker(trust_authority, output_root: Path) -> ToolBroker:
    broker = ToolBroker(trust_authority)
    # Register builtins scoped to output_root
    register_builtin_tools(broker, allowed_roots=[output_root])
    return broker


@pytest.fixture
def state_store(test_db: sqlite3.Connection) -> StateStore:
    return StateStore(test_db)


def _make_mock_reasoning_provider(plan_json: str) -> LocalReasoningProvider:
    """Create a mock LocalReasoningProvider that returns a fixed plan JSON."""
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
# Helpers
# ---------------------------------------------------------------------------


def make_single_step_plan(output_path: str) -> str:
    """Return a JSON plan that writes one Python hello world file."""
    plan = {
        "goal": "Create a Python file that prints hello world",
        "rationale": "Write a single Python file to the specified path.",
        "steps": [
            {
                "step_number": 1,
                "description": "Write hello_world.py with print statement",
                "tool": "file_write",
                "arguments": {
                    "path": output_path,
                    "content": "print('hello world')\n",
                },
                "is_idempotent": True,
                "acceptance_criteria": (
                    "File exists at the target path and contains print statement"
                ),
                "estimated_risk": "MEDIUM",
                "target_resource": output_path,
            }
        ],
    }
    return json.dumps(plan)


# ---------------------------------------------------------------------------
# E2E Tests
# ---------------------------------------------------------------------------


def test_e2e_single_step_file_write_succeeds(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    test_db: sqlite3.Connection,
    output_root: Path,
) -> None:
    """Core E2E: goal → plan → file_write → verified success."""
    output_path = str(output_root / "hello_world.py")
    plan_json = make_single_step_plan(output_path)
    mock_provider = _make_mock_reasoning_provider(plan_json)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Create a Python file that prints hello world",
        actor="test_e2e",
    )

    # 1. Loop completed successfully
    assert isinstance(result, GoalExecutionResult)
    assert result.success is True, f"Expected success=True, got synthesis: {result.synthesis!r}"

    # 2. Exactly one step was observed
    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.status == ObservationStatus.SUCCESS
    assert obs.tool_name == "file_write"

    # 3. File actually exists on disk
    written_file = Path(output_path)
    assert written_file.exists(), f"Expected file at {output_path}"
    content = written_file.read_text(encoding="utf-8")
    assert "print" in content, "Expected print statement in written file"

    # 4. Task record is COMPLETED
    assert result.task_record is not None
    from src.manager.models import TaskState

    assert result.task_record.state == TaskState.COMPLETED

    # 5. Synthesis is non-empty and not an error message
    assert result.synthesis
    assert "fail" not in result.synthesis.lower() or "success" in result.synthesis.lower()


def test_e2e_denied_tool_produces_failure_not_exception(
    state_store: StateStore,
    test_db: sqlite3.Connection,
    output_root: Path,
) -> None:
    """Security: denied capability must produce success=False cleanly, not raise."""
    output_path = str(output_root / "secret.py")
    plan_json = make_single_step_plan(output_path)
    mock_provider = _make_mock_reasoning_provider(plan_json)

    # STRICT_MANUAL tier → file_write (MEDIUM) escalates/denied
    strict_authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.STRICT_MANUAL)
    broker = ToolBroker(strict_authority)
    register_builtin_tools(broker, allowed_roots=[output_root])

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=strict_authority,
        tool_broker=broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Create a Python file that prints hello world",
        actor="test_denied",
    )

    # Should fail cleanly — not raise
    assert result.success is False
    assert result.synthesis  # Some message present
    # File must NOT have been written
    assert not Path(output_path).exists()


def test_e2e_planner_validation_fail_closed(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
) -> None:
    """Planner: completely invalid JSON fails cleanly with success=False."""
    mock_provider = _make_mock_reasoning_provider("this is not json at all {{{")

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Do something invalid",
        actor="test_invalid_plan",
    )

    assert result.success is False
    assert "plan" in result.synthesis.lower() or "fail" in result.synthesis.lower()


def test_e2e_unknown_tool_in_plan_fails_closed(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
) -> None:
    """Planner validation: plan referencing unknown tool is rejected."""
    bad_plan = json.dumps(
        {
            "goal": "Run a secret script",
            "rationale": "Test unknown tool rejection",
            "steps": [
                {
                    "step_number": 1,
                    "description": "Execute malicious tool",
                    "tool": "network_exfiltrate",  # Not registered
                    "arguments": {},
                    "is_idempotent": False,
                    "acceptance_criteria": "Exfiltrated data",
                    "estimated_risk": "CRITICAL",
                    "target_resource": "*",
                }
            ],
        }
    )
    mock_provider = _make_mock_reasoning_provider(bad_plan)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
    )

    result = supervisor.execute_goal(
        goal_text="Run a secret script",
        actor="test_bad_tool",
    )

    assert result.success is False


def test_e2e_verifier_catches_semantic_failure(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
    output_root: Path,
) -> None:
    """Verifier: step with acceptance criteria requiring file existence
    fails semantically if file not written.
    """
    # Plan asks to write a file, criteria checks file exists — tool will succeed but
    # we override the verifier to return SEMANTIC_FAILURE to test the branch.
    output_path = str(output_root / "check_file.py")
    plan_json = make_single_step_plan(output_path)
    mock_provider = _make_mock_reasoning_provider(plan_json)

    from src.manager.verifier import StepVerificationResult

    class AlwaysFailVerifier(StepVerifier):
        def verify(self, step, observation):
            return StepVerificationResult(
                outcome=VerificationOutcome.SEMANTIC_FAILURE,
                rationale="Forced semantic failure for test",
                can_retry=False,
                requires_replan=True,
                requires_approval=False,
            )

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=mock_provider,
        verifier=AlwaysFailVerifier(),
    )

    result = supervisor.execute_goal(
        goal_text="Create a Python file",
        actor="test_semantic_fail",
    )

    assert result.success is False
    assert "semantic" in result.synthesis.lower() or "halt" in result.synthesis.lower()


def test_e2e_no_reasoning_provider_raises(
    state_store: StateStore,
    trust_authority,
    tool_broker: ToolBroker,
) -> None:
    """Supervisor must raise RuntimeError when no planner/provider configured."""
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_authority,
        tool_broker=tool_broker,
        reasoning_provider=None,  # No provider
    )

    with pytest.raises(RuntimeError, match="reasoning provider"):
        supervisor.execute_goal("Do something")
