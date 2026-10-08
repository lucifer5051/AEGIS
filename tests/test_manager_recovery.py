"""SAGE Supervisor & State Recovery -- Comprehensive Verification Test Suite.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 27
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md

Test Groups:
- TM-STATE: Valid transitions, invalid transitions, terminal-state protection.
- TM-STORE: Persistence, transactions, zero-secret rejection, output sanitization.
- TM-RECON: Crash detection, idempotent checkpoints, ambiguous non-idempotent escalation,
            safe fail-closed handling, deterministic repeated reconciliation.
- TM-INTG:  Supervisor -> Trust Core -> Tool Broker -> checkpoints, policy denial,
            approval flow, tool failure, crash/restart simulation.

Environment: 100% offline, zero network access, local SQLite.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from src.decision.service import FastDecisionService
from src.manager import (
    ManagerError,
    ReconciliationEngine,
    RecoveryDisposition,
    SessionStatus,
    StateStore,
    StepPlan,
    StepState,
    Supervisor,
    TaskState,
    TaskStateError,
    build_supervisor,
)
from src.storage.database import create_connection
from src.storage.migrations import (
    apply_migrations,
    bootstrap_schema_migrations,
    load_migrations_from_directory,
)
from src.tools.broker import ToolBroker
from src.tools.models import ParamType, ToolDefinition, ToolParameterSchema
from src.trust import (
    ActionRisk,
    AutonomyTier,
    PolicyEngine,
    ResourceScope,
    SecretLeakError,
    build_trust_authority,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ==============================================================================
# FIXTURES
# ==============================================================================


@pytest.fixture
def tmp_db_path() -> Generator[Path, None, None]:
    """Provide a temporary database file path and clean up afterwards."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        yield db_path
    finally:
        for ext in ("", "-wal", "-shm"):
            Path(str(db_path) + ext).unlink(missing_ok=True)


@pytest.fixture
def migrated_conn(tmp_db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Create a SQLite connection with all migrations 0001-0006 applied."""
    conn = create_connection(tmp_db_path)
    try:
        migrations = load_migrations_from_directory(MIGRATIONS_DIR)
        bootstrap_schema_migrations(conn)
        apply_migrations(conn, migrations)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def store(migrated_conn: sqlite3.Connection) -> StateStore:
    """StateStore wired to the migrated test database."""
    return StateStore(migrated_conn)


@pytest.fixture
def authority(migrated_conn: sqlite3.Connection):
    """TrustAuthority instance wired to the migrated test database."""
    return build_trust_authority(migrated_conn)


@pytest.fixture
def broker(authority) -> ToolBroker:
    """ToolBroker registered with the test TrustAuthority."""
    tb = ToolBroker(authority)

    # Register standard test tools
    read_file_def = ToolDefinition(
        name="test_read_file",
        action_name="filesystem.read",
        description="Read file contents for testing.",
        risk=ActionRisk.LOW,
        parameters=[
            ToolParameterSchema(
                name="path",
                param_type=ParamType.PATH,
                description="Path to read",
                required=True,
            )
        ],
        scope=ResourceScope(filesystem_prefixes=frozenset({"/tmp", "C:/tmp"})),
    )
    tb.register(read_file_def, inline_handler=lambda args, scope: f"CONTENTS_OF:{args['path']}")

    write_file_def = ToolDefinition(
        name="test_write_file",
        action_name="filesystem.write",
        description="Write file contents for testing.",
        risk=ActionRisk.MEDIUM,
        parameters=[
            ToolParameterSchema(
                name="path",
                param_type=ParamType.PATH,
                description="Path to write",
                required=True,
            ),
            ToolParameterSchema(
                name="content",
                param_type=ParamType.STRING,
                description="Text to write",
                required=True,
            ),
        ],
        scope=ResourceScope(filesystem_prefixes=frozenset({"/tmp", "C:/tmp"})),
    )
    tb.register(
        write_file_def, inline_handler=lambda args, scope: f"WROTE:{len(args['content'])} bytes"
    )

    failing_tool_def = ToolDefinition(
        name="test_failing_tool",
        action_name="filesystem.read",
        description="Tool that always fails for testing.",
        risk=ActionRisk.LOW,
        parameters=[],
        scope=ResourceScope(),
    )

    def _failing_handler(args, scope):
        raise RuntimeError("Simulated internal tool crash")

    tb.register(failing_tool_def, inline_handler=_failing_handler)

    return tb


@pytest.fixture
def supervisor(migrated_conn: sqlite3.Connection, authority, broker) -> Supervisor:
    """Supervisor instance with advisory fast decision service."""
    fast_decision = FastDecisionService()
    return build_supervisor(
        conn=migrated_conn,
        trust_authority=authority,
        tool_broker=broker,
        fast_decision=fast_decision,
    )


# ==============================================================================
# TM-STATE: State Machine Transitions & Protection
# ==============================================================================


class TestTaskStateMachine:
    """Validate task and step state transitions and terminal immutability."""

    def test_valid_task_lifecycle_transitions(self, store: StateStore):
        """SUBMITTED -> PLANNING -> EXECUTING -> COMPLETED."""
        task = store.create_task("Implement feature X")
        assert task.state == TaskState.SUBMITTED

        t_plan = store.transition_task_state(task.task_id, TaskState.PLANNING)
        assert t_plan.state == TaskState.PLANNING

        t_exec = store.transition_task_state(task.task_id, TaskState.EXECUTING)
        assert t_exec.state == TaskState.EXECUTING

        t_done = store.transition_task_state(task.task_id, TaskState.COMPLETED)
        assert t_done.state == TaskState.COMPLETED
        assert t_done.completed_at is not None

    def test_invalid_task_transition_rejected(self, store: StateStore):
        """Illegal transitions raise TaskStateError."""
        task = store.create_task("Test invalid transition")

        # SUBMITTED cannot transition directly to EXECUTING (must plan first)
        with pytest.raises(TaskStateError, match="Illegal task transition"):
            store.transition_task_state(task.task_id, TaskState.EXECUTING)

        # SUBMITTED cannot transition directly to COMPLETED
        with pytest.raises(TaskStateError, match="Illegal task transition"):
            store.transition_task_state(task.task_id, TaskState.COMPLETED)

    def test_terminal_state_protection(self, store: StateStore):
        """Terminal tasks (COMPLETED, FAILED, CANCELLED) cannot be transitioned."""
        # 1. COMPLETED terminal protection
        task1 = store.create_task("Task 1")
        store.transition_task_state(task1.task_id, TaskState.PLANNING)
        store.transition_task_state(task1.task_id, TaskState.EXECUTING)
        store.transition_task_state(task1.task_id, TaskState.COMPLETED)

        with pytest.raises(TaskStateError, match="terminal state"):
            store.transition_task_state(task1.task_id, TaskState.EXECUTING)

        # 2. FAILED terminal protection
        task2 = store.create_task("Task 2")
        store.transition_task_state(task2.task_id, TaskState.PLANNING)
        store.transition_task_state(task2.task_id, TaskState.FAILED, error_summary="Unrecoverable")

        with pytest.raises(TaskStateError, match="terminal state"):
            store.transition_task_state(task2.task_id, TaskState.PLANNING)

        # 3. CANCELLED terminal protection
        task3 = store.create_task("Task 3")
        store.transition_task_state(task3.task_id, TaskState.CANCELLED)

        with pytest.raises(TaskStateError, match="terminal state"):
            store.transition_task_state(task3.task_id, TaskState.PLANNING)

    def test_step_state_transitions_and_terminal_protection(self, store: StateStore):
        """Step state transitions follow rules and protect terminal states."""
        task = store.create_task("Task with steps")
        step = store.create_step(task.task_id, 0, "read_log")
        assert step.state == StepState.PENDING

        # PENDING -> RUNNING -> COMPLETED
        s_run = store.transition_step_state(step.step_id, StepState.RUNNING)
        assert s_run.state == StepState.RUNNING

        s_done = store.transition_step_state(
            step.step_id, StepState.COMPLETED, result_summary="Done"
        )
        assert s_done.state == StepState.COMPLETED
        assert s_done.completed_at is not None

        # Cannot transition out of COMPLETED
        with pytest.raises(TaskStateError, match="terminal state"):
            store.transition_step_state(step.step_id, StepState.RUNNING)


# ==============================================================================
# TM-STORE: Persistence, Transactions & Zero-Secret Enforcement
# ==============================================================================


class TestStateStorePersistence:
    """Validate SQLite persistence, constraints, transactions, and sanitization."""

    def test_session_lifecycle(self, store: StateStore):
        """Sessions create, fetch, and update cleanly."""
        session = store.create_session(metadata={"client": "desktop"})
        assert session.status == SessionStatus.ACTIVE

        fetched = store.get_session(session.session_id)
        assert fetched is not None
        assert fetched.session_id == session.session_id
        assert fetched.metadata == {"client": "desktop"}

        updated = store.update_session_status(session.session_id, SessionStatus.COMPLETED)
        assert updated.status == SessionStatus.COMPLETED

    def test_task_and_step_persistence(self, store: StateStore):
        """Tasks and ordered steps persist and retrieve faithfully."""
        task = store.create_task("Multi-step task", priority=50)
        store.create_step(task.task_id, 0, "prep", arguments={"flag": True})
        store.create_step(task.task_id, 1, "exec", is_idempotent=True, idempotency_key="key-123")

        steps = store.list_task_steps(task.task_id)
        assert len(steps) == 2
        assert steps[0].step_index == 0
        assert steps[0].action_name == "prep"
        assert steps[0].arguments == {"flag": True}

        assert steps[1].step_index == 1
        assert steps[1].is_idempotent is True
        assert steps[1].idempotency_key == "key-123"

    def test_duplicate_step_index_rejected(self, store: StateStore):
        """Task steps enforce UNIQUE(task_id, step_index)."""
        task = store.create_task("Unique index task")
        store.create_step(task.task_id, 0, "first")
        with pytest.raises(ManagerError, match="Step index constraint"):
            store.create_step(task.task_id, 0, "duplicate")

    def test_checkpoint_persistence_order(self, store: StateStore):
        """Checkpoints persist and retrieve in sequence order."""
        task = store.create_task("Checkpoint task")
        store.create_checkpoint(task.task_id, 1, {"progress": 25})
        store.create_checkpoint(task.task_id, 2, {"progress": 50})

        latest = store.get_latest_checkpoint(task.task_id)
        assert latest is not None
        assert latest.checkpoint_seq == 2
        assert latest.snapshot == {"progress": 50}

        all_cps = store.list_checkpoints(task.task_id)
        assert [c.checkpoint_seq for c in all_cps] == [1, 2]

    def test_zero_secret_rejection_in_task_metadata(self, store: StateStore):
        """Secrets in task title or metadata trigger SecretLeakError."""
        with pytest.raises(SecretLeakError):
            store.create_task("Normal title", metadata={"api_key": "AKIA1234567890123456"})

        with pytest.raises(SecretLeakError):
            store.create_task('Task with password="super_secret_password"')

    def test_zero_secret_rejection_in_step_arguments(self, store: StateStore):
        """Secrets in step arguments trigger SecretLeakError."""
        task = store.create_task("Secret test task")
        with pytest.raises(SecretLeakError):
            store.create_step(
                task.task_id,
                0,
                "login",
                arguments={"auth": "Bearer secret_jwt_token_here_with_long_value"},
            )

    def test_zero_secret_rejection_in_checkpoints(self, store: StateStore):
        """Secrets in checkpoint snapshot trigger SecretLeakError."""
        task = store.create_task("Secret checkpoint task")
        with pytest.raises(SecretLeakError):
            store.create_checkpoint(
                task.task_id,
                1,
                {"credentials": {"api_key": "AKIA1234567890123456"}},
            )


# ==============================================================================
# TM-RECON: Startup Reconciliation & Crash Recovery
# ==============================================================================


class TestReconciliationEngine:
    """Validate crash discovery, idempotent recovery, and escalation."""

    def test_reconcile_discovers_interrupted_executing_task(self, store: StateStore):
        """An in-flight EXECUTING task is discovered and safely reconciled."""
        task = store.create_task("Interrupted task")
        store.transition_task_state(task.task_id, TaskState.PLANNING)
        store.create_step(task.task_id, 0, "filesystem.read", is_idempotent=True)
        store.transition_task_state(task.task_id, TaskState.EXECUTING)

        engine = ReconciliationEngine(store)
        report = engine.reconcile_startup()

        assert report.tasks_discovered == 1
        assert report.tasks_resumed == 1
        assert report.tasks_escalated == 0
        assert report.tasks_failed == 0
        assert report.reconciled_tasks[0].task_id == task.task_id
        assert report.reconciled_tasks[0].disposition == RecoveryDisposition.RESUME

    def test_idempotent_checkpoint_recovered_as_completed(self, store: StateStore):
        """An in-flight step that checkpointed completion recovers as COMPLETED."""
        task = store.create_task("Mid-flight completed task")
        store.transition_task_state(task.task_id, TaskState.PLANNING)
        step = store.create_step(task.task_id, 0, "compute_hash", is_idempotent=True)
        store.transition_task_state(task.task_id, TaskState.EXECUTING)

        # Step was RUNNING when crash occurred, but completion checkpoint exists
        store.transition_step_state(step.step_id, StepState.RUNNING)
        store.create_checkpoint(
            task_id=task.task_id,
            checkpoint_seq=1,
            snapshot={"step_completed": True, "step_index": 0},
            step_id=step.step_id,
        )

        engine = ReconciliationEngine(store)
        report = engine.reconcile_startup()

        # Step is advanced to COMPLETED, and since all steps are done, task is COMPLETED
        updated_step = store.get_step(step.step_id)
        assert updated_step is not None
        assert updated_step.state == StepState.COMPLETED
        updated_task = store.get_task(task.task_id)
        assert updated_task is not None
        assert updated_task.state == TaskState.COMPLETED
        assert report.tasks_resumed == 1

    def test_idempotent_step_without_checkpoint_resets_to_pending(self, store: StateStore):
        """Idempotent in-flight step without completion checkpoint safely resets to PENDING."""
        task = store.create_task("Idempotent unconfirmed task")
        store.transition_task_state(task.task_id, TaskState.PLANNING)
        step = store.create_step(task.task_id, 0, "filesystem.read", is_idempotent=True)
        store.transition_task_state(task.task_id, TaskState.EXECUTING)
        store.transition_step_state(step.step_id, StepState.RUNNING)

        engine = ReconciliationEngine(store)
        report = engine.reconcile_startup()

        updated_step = store.get_step(step.step_id)
        assert updated_step is not None
        assert updated_step.state == StepState.PENDING
        assert report.tasks_resumed == 1
        assert "reset to PENDING" in report.reconciled_tasks[0].action_summary

    def test_ambiguous_non_idempotent_step_escalates_to_awaiting_approval(self, store: StateStore):
        """Non-idempotent in-flight step without checkpoint escalates to AWAITING_APPROVAL."""
        task = store.create_task("Dangerous mutation task")
        store.transition_task_state(task.task_id, TaskState.PLANNING)
        step = store.create_step(task.task_id, 0, "database.drop_table", is_idempotent=False)
        store.transition_task_state(task.task_id, TaskState.EXECUTING)
        store.transition_step_state(step.step_id, StepState.RUNNING)

        engine = ReconciliationEngine(store)
        report = engine.reconcile_startup()

        # Invariant: NEVER blindly replay ambiguous in-flight side effects!
        updated_step = store.get_step(step.step_id)
        assert updated_step is not None
        assert updated_step.state == StepState.AWAITING_APPROVAL
        updated_task = store.get_task(task.task_id)
        assert updated_task is not None
        assert updated_task.state == TaskState.AWAITING_APPROVAL
        assert report.tasks_escalated == 1
        assert report.tasks_resumed == 0
        assert report.reconciled_tasks[0].disposition == RecoveryDisposition.ESCALATE_TO_USER

    def test_unrecoverable_empty_executing_task_fails_safely(self, store: StateStore):
        """An EXECUTING task with zero registered steps fails safely (fail closed)."""
        task = store.create_task("Corrupt empty task")
        store.transition_task_state(task.task_id, TaskState.PLANNING)
        store.transition_task_state(task.task_id, TaskState.EXECUTING)

        engine = ReconciliationEngine(store)
        report = engine.reconcile_startup()

        updated_task = store.get_task(task.task_id)
        assert updated_task is not None
        assert updated_task.state == TaskState.FAILED
        assert report.tasks_failed == 1
        assert report.reconciled_tasks[0].disposition == RecoveryDisposition.FAIL_TERMINAL

    def test_repeated_reconciliation_is_idempotent(self, store: StateStore):
        """Consecutive reconciliation runs on undisturbed database are deterministic."""
        task = store.create_task("Repeat test task")
        store.transition_task_state(task.task_id, TaskState.PLANNING)
        store.create_step(task.task_id, 0, "filesystem.read", is_idempotent=True)
        store.transition_task_state(task.task_id, TaskState.EXECUTING)

        engine = ReconciliationEngine(store)
        report1 = engine.reconcile_startup()
        report2 = engine.reconcile_startup()

        assert report1.tasks_discovered == report2.tasks_discovered
        assert report1.tasks_resumed == report2.tasks_resumed
        assert report1.tasks_escalated == report2.tasks_escalated
        assert report1.tasks_failed == report2.tasks_failed


# ==============================================================================
# TM-INTG: Supervisor Orchestration, Trust Core, Tools & Recovery Flow
# ==============================================================================


class TestSupervisorIntegration:
    """Validate end-to-end task execution, authorization gates, and crash simulation."""

    def test_supervisor_goal_submission_and_planning(
        self, supervisor: Supervisor, store: StateStore
    ):
        """Submit a goal, plan steps, and transition to EXECUTING."""
        task = supervisor.submit_goal("Read configuration file /tmp/config.json")
        assert task.state == TaskState.SUBMITTED
        assert task.routing_intent is not None  # Advisory FDE classified intent

        steps = [
            StepPlan(
                action_name="filesystem.read",
                tool_name="test_read_file",
                arguments={"path": "/tmp/config.json"},
            ),
        ]
        planned_task = supervisor.plan_task(task.task_id, steps)
        assert planned_task.state == TaskState.EXECUTING

        db_steps = store.list_task_steps(task.task_id)
        assert len(db_steps) == 1
        assert db_steps[0].state == StepState.PENDING

    def test_successful_multi_step_execution(self, supervisor: Supervisor, store: StateStore):
        """Execute a multi-step task through Trust Core and Tool Broker to completion."""
        task = supervisor.submit_goal("Execute 2-step workflow")
        steps = [
            StepPlan(
                action_name="filesystem.read",
                tool_name="test_read_file",
                arguments={"path": "/tmp/file1.txt"},
            ),
            StepPlan(
                action_name="filesystem.read",
                tool_name="test_read_file",
                arguments={"path": "/tmp/file2.txt"},
            ),
        ]
        supervisor.plan_task(task.task_id, steps)

        completed_task = supervisor.execute_task(task.task_id)
        assert completed_task.state == TaskState.COMPLETED
        assert completed_task.completed_at is not None

        db_steps = store.list_task_steps(task.task_id)
        assert all(s.state == StepState.COMPLETED for s in db_steps)
        assert db_steps[0].result_summary is not None
        assert "CONTENTS_OF:/tmp/file1.txt" in db_steps[0].result_summary
        assert db_steps[1].result_summary is not None
        assert "CONTENTS_OF:/tmp/file2.txt" in db_steps[1].result_summary

        # Checkpoints were persisted before and after each step
        cps = store.list_checkpoints(task.task_id)
        assert len(cps) >= 4  # plan + 2*dispatch + 2*complete

    def test_supervisor_policy_denial_escalates_to_awaiting_approval(
        self,
        migrated_conn: sqlite3.Connection,
        store: StateStore,
        authority,
        broker: ToolBroker,
    ):
        """Actions requiring approval at current tier escalate to AWAITING_APPROVAL."""
        # Set autonomy tier to STRICT_MANUAL (Tier 0) where MEDIUM risk actions escalate
        policy_engine: PolicyEngine = authority._policy
        policy_engine.set_autonomy_tier(AutonomyTier.STRICT_MANUAL)

        sup = Supervisor(
            state_store=store,
            trust_authority=authority,
            tool_broker=broker,
        )

        task = sup.submit_goal("Write sensitive data")
        steps = [
            # test_write_file has MEDIUM risk, which escalates under STRICT_MANUAL
            StepPlan(
                action_name="filesystem.write",
                tool_name="test_write_file",
                arguments={"path": "/tmp/out.txt", "content": "hello world"},
            ),
        ]
        sup.plan_task(task.task_id, steps)
        result_task = sup.execute_task(task.task_id)

        # Task and step must both be in AWAITING_APPROVAL
        assert result_task.state == TaskState.AWAITING_APPROVAL
        db_steps = store.list_task_steps(task.task_id)
        assert db_steps[0].state == StepState.AWAITING_APPROVAL

    def test_supervisor_approve_and_resume_flow(
        self,
        migrated_conn: sqlite3.Connection,
        store: StateStore,
        authority,
        broker: ToolBroker,
    ):
        """Human approval unblocks an escalated step, allowing execution to resume."""
        policy_engine: PolicyEngine = authority._policy
        policy_engine.set_autonomy_tier(AutonomyTier.STRICT_MANUAL)

        sup = Supervisor(
            state_store=store,
            trust_authority=authority,
            tool_broker=broker,
        )

        task = sup.submit_goal("Approval workflow")
        steps = [
            StepPlan(
                action_name="filesystem.write",
                tool_name="test_write_file",
                arguments={"path": "/tmp/data.txt", "content": "payload"},
            ),
        ]
        sup.plan_task(task.task_id, steps)
        escalated_task = sup.execute_task(task.task_id)
        assert escalated_task.state == TaskState.AWAITING_APPROVAL

        # Now simulate user granting approval: raise tier or resolve approval
        policy_engine.set_autonomy_tier(AutonomyTier.SUPERVISED)  # Tier 2 allows MEDIUM risk

        db_steps = store.list_task_steps(task.task_id)
        sup.approve_step(task.task_id, db_steps[0].step_id, actor="operator_alice")

        # Resume task
        resumed_task = sup.resume_task(task.task_id)
        assert resumed_task.state == TaskState.COMPLETED

    def test_supervisor_tool_failure_fails_task(self, supervisor: Supervisor, store: StateStore):
        """Tool runtime failure halts task and marks step and task FAILED."""
        task = supervisor.submit_goal("Failure test")
        steps = [
            StepPlan(action_name="filesystem.read", tool_name="test_failing_tool"),
        ]
        supervisor.plan_task(task.task_id, steps)
        failed_task = supervisor.execute_task(task.task_id)

        assert failed_task.state == TaskState.FAILED
        assert "RuntimeError" in (failed_task.error_summary or "")
        db_steps = store.list_task_steps(task.task_id)
        assert db_steps[0].state == StepState.FAILED

    def test_supervisor_cancel_task(self, supervisor: Supervisor):
        """Non-terminal task can be cleanly cancelled."""
        task = supervisor.submit_goal("Task to cancel")
        cancelled = supervisor.cancel_task(task.task_id, reason="User requested abort.")
        assert cancelled.state == TaskState.CANCELLED
        assert "User requested abort" in (cancelled.error_summary or "")

    def test_crash_and_restart_simulation(self, tmp_db_path: Path):
        """Simulate process termination during multi-step execution, then restart & resume."""
        # -------------------------------------------------------------
        # Phase 1: Process 1 starts execution
        # -------------------------------------------------------------
        conn1 = create_connection(tmp_db_path)
        migrations = load_migrations_from_directory(MIGRATIONS_DIR)
        bootstrap_schema_migrations(conn1)
        apply_migrations(conn1, migrations)

        auth1 = build_trust_authority(conn1)
        tb1 = ToolBroker(auth1)
        read_def = ToolDefinition(
            name="read_tool",
            action_name="filesystem.read",
            description="Reader",
            risk=ActionRisk.LOW,
            parameters=[
                ToolParameterSchema(
                    name="path", param_type=ParamType.PATH, description="p", required=True
                )
            ],
            scope=ResourceScope(filesystem_prefixes=frozenset({"/tmp", "C:/tmp"})),
        )
        tb1.register(read_def, inline_handler=lambda args, scope: f"READ:{args['path']}")

        sup1 = build_supervisor(conn1, auth1, tb1)
        task = sup1.submit_goal("Crash simulation goal")
        steps = [
            StepPlan(
                action_name="filesystem.read",
                tool_name="read_tool",
                is_idempotent=True,
                arguments={"path": "/tmp/a.txt"},
            ),
            StepPlan(
                action_name="filesystem.read",
                tool_name="read_tool",
                is_idempotent=True,
                arguments={"path": "/tmp/b.txt"},
            ),
        ]
        sup1.plan_task(task.task_id, steps)

        # Manually execute step 1 and mark it completed
        db_steps = sup1._store.list_task_steps(task.task_id)
        sup1._store.transition_step_state(db_steps[0].step_id, StepState.RUNNING)
        sup1._store.transition_step_state(
            db_steps[0].step_id, StepState.COMPLETED, result_summary="READ:/tmp/a.txt"
        )
        sup1._store.create_checkpoint(
            task.task_id, 2, {"step_completed": True, "step_index": 0}, step_id=db_steps[0].step_id
        )

        # Simulate crash while step 2 is RUNNING
        sup1._store.transition_step_state(db_steps[1].step_id, StepState.RUNNING)

        # CRASH: abruptly close database connection without completing step 2 or task
        conn1.close()

        # -------------------------------------------------------------
        # Phase 2: Process 2 boots up after crash
        # -------------------------------------------------------------
        conn2 = create_connection(tmp_db_path)
        try:
            auth2 = build_trust_authority(conn2)
            tb2 = ToolBroker(auth2)
            tb2.register(read_def, inline_handler=lambda args, scope: f"READ:{args['path']}")
            store2 = StateStore(conn2)

            # Startup reconciliation
            reconciliation_engine = ReconciliationEngine(store2)
            report = reconciliation_engine.reconcile_startup()

            assert report.tasks_discovered == 1
            assert report.tasks_resumed == 1

            # Step 2 was idempotent, so it was safely reset to PENDING
            reconciled_steps = store2.list_task_steps(task.task_id)
            assert reconciled_steps[0].state == StepState.COMPLETED
            assert reconciled_steps[1].state == StepState.PENDING

            # Now resume task execution to completion
            sup2 = Supervisor(store2, auth2, tb2)
            final_task = sup2.resume_task(task.task_id)
            assert final_task.state == TaskState.COMPLETED
        finally:
            conn2.close()
