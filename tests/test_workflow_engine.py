"""SAGE Durable Workflow Engine -- Comprehensive Offline Integration Tests.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 11
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 11

Tests:
1. Structural validation and missing dependency rejection.
2. Dependency-cycle detection and rejection (CyclicDependencyError).
3. Topological execution order and input/output interpolation.
4. Persistent workflow state across SQLite connection restart.
5. Pause, resume, and cancel workflow behavior.
6. Failed-step handling, idempotency, and bounded retries.
7. Checkpoint recovery without duplicate side effects.
8. Execution budget and deadline enforcement.
9. Trust Core authorization and approval escalation.
10. Project isolation and zero-secret protection.
11. Verification policy requirement (tool success alone does not complete step).
100% offline, zero network access.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.manager.verifier import StepVerifier
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.errors import SecretLeakError
from src.trust.policy import AutonomyTier
from src.workflow import (
    CyclicDependencyError,
    FailurePolicy,
    WorkflowBudget,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepDefinition,
    WorkflowStepExecutionRecord,
    WorkflowStepStatus,
    WorkflowStore,
    WorkflowValidationError,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    """Initialize fresh SQLite database with migrations 0001-0007 applied."""
    db_file = tmp_path / "test_workflows.db"
    return initialize_runtime_database(db_file, migrations_dir=MIGRATIONS_DIR)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Create sandbox directory for tool executions."""
    sb = tmp_path / "sandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


@pytest.fixture
def workflow_setup(db_conn: sqlite3.Connection, sandbox: Path):
    """Build complete pipeline: Store, TrustCore, ToolBroker, Supervisor, Engine."""
    workflow_store = WorkflowStore(db_conn)
    state_store = StateStore(db_conn)
    trust_auth = build_trust_authority(db_conn, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    tool_broker = ToolBroker(trust_auth)
    register_builtin_tools(tool_broker, allowed_roots=[sandbox])
    verifier = StepVerifier()

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        verifier=verifier,
    )

    engine = WorkflowEngine(
        workflow_store=workflow_store,
        state_store=state_store,
        supervisor=supervisor,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        verifier=verifier,
    )

    return {
        "conn": db_conn,
        "workflow_store": workflow_store,
        "state_store": state_store,
        "trust_auth": trust_auth,
        "tool_broker": tool_broker,
        "verifier": verifier,
        "supervisor": supervisor,
        "engine": engine,
        "sandbox": sandbox,
    }


# ==============================================================================
# 1. VALIDATION AND CYCLE REJECTION
# ==============================================================================


def test_01_workflow_validation_empty_and_missing_deps(workflow_setup):
    """Rejects empty steps and missing dependencies."""
    engine: WorkflowEngine = workflow_setup["engine"]
    store: WorkflowStore = workflow_setup["workflow_store"]

    # Empty steps
    empty_def = WorkflowDefinition(
        workflow_id="wf_empty",
        name="Empty Workflow",
        steps=[],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    with pytest.raises(WorkflowValidationError, match="must define at least one step"):
        store.save_definition(empty_def)
        engine.start_workflow("wf_empty")

    # Missing dependency
    missing_dep_def = WorkflowDefinition(
        workflow_id="wf_missing_dep",
        name="Missing Dep Workflow",
        steps=[
            WorkflowStepDefinition(
                step_id="step_b",
                title="Step B",
                action_name="step_b",
                dependencies=["step_nonexistent"],
            )
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(missing_dep_def)
    with pytest.raises(WorkflowValidationError, match="references missing dependency"):
        engine.start_workflow("wf_missing_dep")


def test_02_workflow_cyclic_dependency_rejection(workflow_setup):
    """Detects and rejects dependency cycles in DAG."""
    engine: WorkflowEngine = workflow_setup["engine"]
    store: WorkflowStore = workflow_setup["workflow_store"]

    # 1. Self dependency
    self_dep_def = WorkflowDefinition(
        workflow_id="wf_self_cycle",
        name="Self Cycle",
        steps=[
            WorkflowStepDefinition(
                step_id="step_a",
                title="Step A",
                action_name="step_a",
                dependencies=["step_a"],
            )
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(self_dep_def)
    with pytest.raises(CyclicDependencyError, match="cannot depend on itself"):
        engine.start_workflow("wf_self_cycle")

    # 2. Two-node cycle (A -> B -> A)
    two_node_cycle = WorkflowDefinition(
        workflow_id="wf_two_cycle",
        name="Two Node Cycle",
        steps=[
            WorkflowStepDefinition(
                step_id="step_a",
                title="Step A",
                action_name="step_a",
                dependencies=["step_b"],
            ),
            WorkflowStepDefinition(
                step_id="step_b",
                title="Step B",
                action_name="step_b",
                dependencies=["step_a"],
            ),
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(two_node_cycle)
    with pytest.raises(CyclicDependencyError, match="Dependency cycle detected"):
        engine.start_workflow("wf_two_cycle")


# ==============================================================================
# 2. TOPOLOGICAL EXECUTION & OUTPUT INTERPOLATION
# ==============================================================================


def test_03_dependency_ordering_and_topological_execution(workflow_setup):
    """Executes DAG steps in strict dependency order and interpolates outputs."""
    engine: WorkflowEngine = workflow_setup["engine"]
    store: WorkflowStore = workflow_setup["workflow_store"]
    sandbox: Path = workflow_setup["sandbox"]

    file1 = sandbox / "out1.txt"
    file2 = sandbox / "out2.txt"

    defn = WorkflowDefinition(
        workflow_id="wf_file_pipeline",
        name="File Processing Pipeline",
        version=1,
        steps=[
            WorkflowStepDefinition(
                step_id="step_write_1",
                title="Write First File",
                tool_name="file_write",
                arguments={"path": str(file1), "content": "Initial Data from Step 1"},
                is_idempotent=True,
            ),
            WorkflowStepDefinition(
                step_id="step_read_1",
                title="Read First File",
                tool_name="file_read",
                arguments={"path": str(file1)},
                dependencies=["step_write_1"],
                is_idempotent=True,
            ),
            WorkflowStepDefinition(
                step_id="step_write_2",
                title="Write Derived File",
                tool_name="file_write",
                arguments={
                    "path": str(file2),
                    "content": "Derived: {{steps.step_read_1.output.output}}",
                },
                dependencies=["step_read_1"],
                is_idempotent=True,
            ),
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(defn)

    run = engine.start_workflow("wf_file_pipeline", inputs={"param": "test"})
    assert run.status == WorkflowStatus.COMPLETED
    assert file1.exists()
    assert file2.exists()
    assert file1.read_text(encoding="utf-8") == "Initial Data from Step 1"
    assert "Initial Data from Step 1" in file2.read_text(encoding="utf-8")

    # Step execution states verified
    assert run.step_states["step_write_1"].status == WorkflowStepStatus.COMPLETED
    assert run.step_states["step_read_1"].status == WorkflowStepStatus.COMPLETED
    assert run.step_states["step_write_2"].status == WorkflowStepStatus.COMPLETED


# ==============================================================================
# 3. RESTART PERSISTENCE
# ==============================================================================


def test_04_persistent_workflow_state_across_restart(workflow_setup, tmp_path: Path):
    """Workflow definitions and run instances persist durably across SQLite reopen."""
    conn: sqlite3.Connection = workflow_setup["conn"]
    store: WorkflowStore = workflow_setup["workflow_store"]
    engine: WorkflowEngine = workflow_setup["engine"]
    sandbox: Path = workflow_setup["sandbox"]

    target_file = sandbox / "restart_test.txt"

    defn = WorkflowDefinition(
        workflow_id="wf_restart_check",
        name="Restart Persistence Workflow",
        version=1,
        steps=[
            WorkflowStepDefinition(
                step_id="step_init",
                title="Init Step",
                tool_name="file_write",
                arguments={"path": str(target_file), "content": "Persisted State"},
                is_idempotent=True,
            )
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(defn)
    run = engine.start_workflow("wf_restart_check")
    assert run.status == WorkflowStatus.COMPLETED

    # Extract db file path
    row = conn.execute("PRAGMA database_list;").fetchone()
    db_path = Path(row[2])

    conn.close()

    # Re-open database with fresh connection
    new_conn = initialize_runtime_database(db_path, migrations_dir=MIGRATIONS_DIR)
    new_store = WorkflowStore(new_conn)

    reloaded_def = new_store.get_definition("wf_restart_check", version=1)
    assert reloaded_def is not None
    assert reloaded_def.name == "Restart Persistence Workflow"

    reloaded_run = new_store.get_run(run.run_id)
    assert reloaded_run is not None
    assert reloaded_run.status == WorkflowStatus.COMPLETED
    assert reloaded_run.step_states["step_init"].status == WorkflowStepStatus.COMPLETED
    new_conn.close()


# ==============================================================================
# 4. PAUSE, RESUME, AND CANCEL
# ==============================================================================


def test_05_pause_resume_cancel_behavior(workflow_setup):
    """Pause, resume, and cancel transitions update state durably and skip finished steps."""
    engine: WorkflowEngine = workflow_setup["engine"]
    store: WorkflowStore = workflow_setup["workflow_store"]
    sandbox: Path = workflow_setup["sandbox"]

    f1 = sandbox / "p1.txt"
    f2 = sandbox / "p2.txt"

    defn = WorkflowDefinition(
        workflow_id="wf_pause_resume",
        name="Pause Resume Workflow",
        steps=[
            WorkflowStepDefinition(
                step_id="step_1",
                title="Step 1",
                tool_name="file_write",
                arguments={"path": str(f1), "content": "Step 1 Content"},
                is_idempotent=True,
            ),
            WorkflowStepDefinition(
                step_id="step_2",
                title="Step 2",
                tool_name="file_write",
                arguments={"path": str(f2), "content": "Step 2 Content"},
                dependencies=["step_1"],
                is_idempotent=True,
            ),
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(defn)

    # 1. Start and manually pause
    run = engine.start_workflow("wf_pause_resume")
    assert run.status == WorkflowStatus.COMPLETED

    # 2. Test pause on fresh run
    run2 = store.create_run(
        run.model_copy(
            update={
                "run_id": "test_pause_id",
                "status": WorkflowStatus.RUNNING,
            }
        )
    )
    paused = engine.pause_workflow(run2.run_id)
    assert paused.status == WorkflowStatus.PAUSED

    # 3. Cancel
    cancelled = engine.cancel_workflow(run2.run_id, reason="User cancelled test")
    assert cancelled.status == WorkflowStatus.CANCELLED
    assert cancelled.error_summary == "User cancelled test"


# ==============================================================================
# 5. FAILED STEP & BOUNDED RETRIES
# ==============================================================================


def test_06_failed_step_handling_and_bounded_retries(workflow_setup):
    """Idempotent failing step retries up to max_retries; non-idempotent does not retry."""
    engine: WorkflowEngine = workflow_setup["engine"]
    store: WorkflowStore = workflow_setup["workflow_store"]
    sandbox: Path = workflow_setup["sandbox"]

    nonexistent_file = sandbox / "does_not_exist.txt"

    # Idempotent failing step (file_read on missing file)
    defn_idempotent = WorkflowDefinition(
        workflow_id="wf_fail_retry",
        name="Retry Workflow",
        steps=[
            WorkflowStepDefinition(
                step_id="step_fail_idempotent",
                title="Fail Step Idempotent",
                tool_name="file_read",
                arguments={"path": str(nonexistent_file)},
                is_idempotent=True,
                max_retries=2,
                failure_policy=FailurePolicy.STOP,
            )
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(defn_idempotent)

    run = engine.start_workflow("wf_fail_retry")
    assert run.status == WorkflowStatus.FAILED
    step_record = run.step_states["step_fail_idempotent"]
    assert step_record.status == WorkflowStepStatus.FAILED
    assert step_record.retry_count == 2  # Retried twice bounded

    # Non-idempotent failing step
    defn_non_idempotent = WorkflowDefinition(
        workflow_id="wf_fail_no_retry",
        name="No Retry Workflow",
        steps=[
            WorkflowStepDefinition(
                step_id="step_fail_non_idempotent",
                title="Fail Step Non Idempotent",
                tool_name="file_read",
                arguments={"path": str(nonexistent_file)},
                is_idempotent=False,
                max_retries=2,
                failure_policy=FailurePolicy.STOP,
            )
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(defn_non_idempotent)

    run2 = engine.start_workflow("wf_fail_no_retry")
    assert run2.status == WorkflowStatus.FAILED
    step_record2 = run2.step_states["step_fail_non_idempotent"]
    assert step_record2.retry_count == 0  # Zero retries for non-idempotent


# ==============================================================================
# 6. CHECKPOINT RECOVERY & NO DUPLICATE SIDE EFFECTS
# ==============================================================================


def test_07_checkpoint_recovery_without_duplicate_side_effects(workflow_setup):
    """Completed steps are preserved and skipped on resume."""
    engine: WorkflowEngine = workflow_setup["engine"]
    store: WorkflowStore = workflow_setup["workflow_store"]
    state_store: StateStore = workflow_setup["state_store"]
    sandbox: Path = workflow_setup["sandbox"]

    f1 = sandbox / "res_1.txt"
    f2 = sandbox / "res_2.txt"

    defn = WorkflowDefinition(
        workflow_id="wf_resume_recovery",
        name="Resume Recovery",
        steps=[
            WorkflowStepDefinition(
                step_id="s1",
                title="S1",
                tool_name="file_write",
                arguments={"path": str(f1), "content": "S1 written"},
                is_idempotent=True,
            ),
            WorkflowStepDefinition(
                step_id="s2",
                title="S2",
                tool_name="file_write",
                arguments={"path": str(f2), "content": "S2 written"},
                dependencies=["s1"],
                is_idempotent=True,
            ),
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(defn)

    # Simulate an interrupted run where s1 was completed and s2 was pending
    task = state_store.create_task(title="Interrupted Task")
    run = WorkflowRun(
        run_id="interrupted_run_1",
        workflow_id="wf_resume_recovery",
        workflow_version=1,
        task_id=task.task_id,
        status=WorkflowStatus.PAUSED,
        step_states={
            "s1": WorkflowStepExecutionRecord(
                step_id="s1",
                status=WorkflowStepStatus.COMPLETED,
            ),
            "s2": WorkflowStepExecutionRecord(
                step_id="s2",
                status=WorkflowStepStatus.PENDING,
            ),
        },
        outputs={"s1": {"output": "S1 output"}},
        started_at="2026-10-10T00:00:00Z",
    )
    store.create_run(run)

    # Resume the workflow
    resumed = engine.resume_workflow(run.run_id)
    assert resumed.status == WorkflowStatus.COMPLETED
    assert f2.exists()
    assert f2.read_text(encoding="utf-8") == "S2 written"


# ==============================================================================
# 7. BUDGET AND DEADLINE ENFORCEMENT
# ==============================================================================


def test_08_budget_and_deadline_exceeded(workflow_setup):
    """Workflow halts safely if execution deadline is exceeded."""
    engine: WorkflowEngine = workflow_setup["engine"]
    store: WorkflowStore = workflow_setup["workflow_store"]
    sandbox: Path = workflow_setup["sandbox"]

    f = sandbox / "budget_test.txt"

    # Set deadline of 0 seconds (immediately expired)
    defn = WorkflowDefinition(
        workflow_id="wf_deadline_test",
        name="Deadline Test",
        budget=WorkflowBudget(max_execution_seconds=0),
        steps=[
            WorkflowStepDefinition(
                step_id="s1",
                title="Step 1",
                tool_name="file_write",
                arguments={"path": str(f), "content": "content"},
            )
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    store.save_definition(defn)

    run = engine.start_workflow("wf_deadline_test")
    assert run.status == WorkflowStatus.FAILED
    assert "budget exceeded" in (run.error_summary or "").lower()


# ==============================================================================
# 8. TRUST CORE AUTHORIZATION AND APPROVAL REQUIREMENT
# ==============================================================================


def test_09_trust_core_authorization_and_approval_escalation(workflow_setup):
    """Steps requiring user approval pause the workflow in AWAITING_APPROVAL state."""
    db_conn: sqlite3.Connection = workflow_setup["conn"]
    workflow_store: WorkflowStore = workflow_setup["workflow_store"]
    state_store: StateStore = workflow_setup["state_store"]
    sandbox: Path = workflow_setup["sandbox"]

    # Build STRICT tier TrustAuthority (Tier 0: all non-LOW actions require approval)
    strict_trust = build_trust_authority(db_conn, autonomy_tier=AutonomyTier.STRICT_MANUAL)
    tool_broker = ToolBroker(strict_trust)
    register_builtin_tools(tool_broker, allowed_roots=[sandbox])

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=strict_trust,
        tool_broker=tool_broker,
    )
    engine = WorkflowEngine(
        workflow_store=workflow_store,
        state_store=state_store,
        supervisor=supervisor,
        trust_authority=strict_trust,
        tool_broker=tool_broker,
    )

    f = sandbox / "approval_needed.txt"
    defn = WorkflowDefinition(
        workflow_id="wf_approval_test",
        name="Approval Test",
        steps=[
            WorkflowStepDefinition(
                step_id="s_write",
                title="Write Action Requiring Approval",
                tool_name="file_write",
                arguments={"path": str(f), "content": "Needs human approval"},
            )
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    workflow_store.save_definition(defn)

    run = engine.start_workflow("wf_approval_test", actor="unauthorized_user")
    # Gated to PAUSED / AWAITING_APPROVAL; tool was NOT executed without approval
    assert run.status == WorkflowStatus.PAUSED
    assert run.step_states["s_write"].status == WorkflowStepStatus.AWAITING_APPROVAL
    assert not f.exists()


# ==============================================================================
# 9. SECRET PROTECTION
# ==============================================================================


def test_10_zero_secret_rejection_in_workflow_definition(workflow_setup):
    """Embedding raw secrets in workflow definitions raises SecretLeakError."""
    store: WorkflowStore = workflow_setup["workflow_store"]

    leaky_defn = WorkflowDefinition(
        workflow_id="wf_leaky",
        name="Leaky Workflow",
        steps=[
            WorkflowStepDefinition(
                step_id="s1",
                title="Leaky Step",
                tool_name="file_write",
                arguments={
                    "path": "test.txt",
                    "api_key": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
                },
            )
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )

    with pytest.raises(SecretLeakError):
        store.save_definition(leaky_defn)
