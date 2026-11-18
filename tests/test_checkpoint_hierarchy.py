"""Tests for ARCH-060-07: Subtask Checkpoint Hierarchy & Parent Task Linking.

Validates:
1. Root task creation (is_root=True, parent_task_id=None, root_task_id=task_id).
2. Child task creation (is_subtask=True, parent_task_id=parent, root_task_id=root).
3. Multi-tier lineage propagation (root -> child -> grandchild).
4. Circular reference detection (self-parent, cycle across ancestors).
5. Max hierarchy depth enforcement.
6. Checkpoint hierarchy metadata persistence and extraction.
7. Lineage and hierarchy traversal queries (get_child_tasks, get_task_lineage, get_task_hierarchy, etc.).
8. Interrupted subtask crash recovery via ReconciliationEngine.
9. Parent terminal failure cascade (no orphan child tasks).
10. Orchestrating parent reconciliation with subtasks.
11. Sibling task isolation.
12. Backward compatibility with legacy unlinked checkpoints and task records.
13. Zero-secret invariants across hierarchy metadata.
14. Supervisor skill execution linking with durable checkpoints.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest

from src.manager.errors import TaskHierarchyError, TaskNotFoundError
from src.manager.models import (
    CheckpointSnapshot,
    ExecutionMode,
    RecoveryDisposition,
    StepState,
    TaskRecord,
    TaskState,
)
from src.manager.reconciliation import ReconciliationEngine
from src.manager.state_store import MAX_HIERARCHY_DEPTH, StateStore
from src.manager.supervisor import Supervisor
from src.skills.models import SkillExecutionStatus, SkillInvocation, SkillResult, VerificationStatus
from src.storage.database import create_connection
from src.storage.migrations import (
    apply_migrations,
    bootstrap_schema_migrations,
    load_migrations_from_directory,
)
from src.trust.errors import SecretLeakError

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
def db_conn() -> Generator[sqlite3.Connection, None, None]:
    """Provide a fresh SQLite connection with migrations applied."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = create_connection(db_path)
    try:
        migs = load_migrations_from_directory(MIGRATIONS_DIR)
        bootstrap_schema_migrations(conn)
        apply_migrations(conn, migs)
        yield conn
    finally:
        conn.close()
        for ext in ("", "-wal", "-shm"):
            p = Path(db_path + ext)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


@pytest.fixture
def store(db_conn: sqlite3.Connection) -> StateStore:
    return StateStore(db_conn)


# ==============================================================================
# 1. TASK HIERARCHY MODEL & CREATION TESTS
# ==============================================================================


class TestTaskHierarchyCreation:
    """Validate root and child task creation and lineage assignment."""

    def test_root_task_creation(self, store: StateStore):
        """Root tasks must have parent_task_id=None, root_task_id=task_id, is_root=True."""
        task = store.create_task(title="Root goal")
        assert task.parent_task_id is None
        assert task.root_task_id == task.task_id
        assert task.is_root is True
        assert task.is_subtask is False

        fetched = store.get_task(task.task_id)
        assert fetched is not None
        assert fetched.parent_task_id is None
        assert fetched.root_task_id == task.task_id
        assert fetched.is_root is True

    def test_child_subtask_creation(self, store: StateStore):
        """Child tasks must link to their immediate parent and inherit the root task ID."""
        root = store.create_task(title="Root project")
        child = store.create_task(title="Subtask 1", parent_task_id=root.task_id)

        assert child.parent_task_id == root.task_id
        assert child.root_task_id == root.task_id
        assert child.is_root is False
        assert child.is_subtask is True

        fetched = store.get_task(child.task_id)
        assert fetched is not None
        assert fetched.parent_task_id == root.task_id
        assert fetched.root_task_id == root.task_id
        assert fetched.is_subtask is True

    def test_multi_tier_lineage_propagation(self, store: StateStore):
        """Grandchild and deeper subtasks inherit the correct root_task_id across tiers."""
        root = store.create_task(title="Root tier")
        child = store.create_task(title="Tier 1 child", parent_task_id=root.task_id)
        grandchild = store.create_task(title="Tier 2 grandchild", parent_task_id=child.task_id)
        great_grandchild = store.create_task(
            title="Tier 3 great-grandchild", parent_task_id=grandchild.task_id
        )

        assert grandchild.parent_task_id == child.task_id
        assert grandchild.root_task_id == root.task_id

        assert great_grandchild.parent_task_id == grandchild.task_id
        assert great_grandchild.root_task_id == root.task_id

    def test_missing_parent_task_rejected(self, store: StateStore):
        """Specifying a non-existent parent_task_id raises TaskNotFoundError."""
        with pytest.raises(TaskNotFoundError, match="Parent task 'non-existent' not found"):
            store.create_task(title="Orphan attempt", parent_task_id="non-existent")

    def test_self_parent_rejected(self, store: StateStore):
        """A task cannot specify itself as its parent."""
        tid = str(uuid4())
        with pytest.raises(TaskHierarchyError, match="cannot be its own parent"):
            store.create_task(title="Self-parent attempt", task_id=tid, parent_task_id=tid)

    def test_circular_parent_reference_rejected(self, store: StateStore):
        """Circular parent reference across multiple tasks is rejected."""
        t1 = store.create_task(title="Task 1")
        t2 = store.create_task(title="Task 2", parent_task_id=t1.task_id)
        t3 = store.create_task(title="Task 3", parent_task_id=t2.task_id)

        # Attempt to create a task with parent t3, whose id is t1 -> cycle
        with pytest.raises(TaskHierarchyError, match="Circular parent reference detected"):
            store.create_task(title="Cycle Task", task_id=t1.task_id, parent_task_id=t3.task_id)

    def test_max_hierarchy_depth_enforced(self, store: StateStore):
        """Chaining subtasks beyond MAX_HIERARCHY_DEPTH raises TaskHierarchyError."""
        curr = store.create_task(title="Tier 0")
        for i in range(1, MAX_HIERARCHY_DEPTH):
            curr = store.create_task(title=f"Tier {i}", parent_task_id=curr.task_id)

        # Next one exceeds MAX_HIERARCHY_DEPTH
        with pytest.raises(TaskHierarchyError, match="hierarchy depth exceeds maximum"):
            store.create_task(
                title=f"Tier {MAX_HIERARCHY_DEPTH}",
                parent_task_id=curr.task_id,
            )


# ==============================================================================
# 2. CHECKPOINT HIERARCHY PERSISTENCE TESTS
# ==============================================================================


class TestCheckpointHierarchy:
    """Validate checkpoint hierarchy metadata persistence and isolation."""

    def test_root_checkpoint_metadata(self, store: StateStore):
        """Checkpoints on a root task automatically default hierarchy to root."""
        root = store.create_task(title="Root execution")
        cp = store.create_checkpoint(
            task_id=root.task_id,
            checkpoint_seq=1,
            snapshot={"status": "init"},
        )

        assert cp.parent_task_id is None
        assert cp.root_task_id == root.task_id
        assert cp.is_root is True
        assert cp.snapshot == {"status": "init"}

        latest = store.get_latest_checkpoint(root.task_id)
        assert latest is not None
        assert latest.parent_task_id is None
        assert latest.root_task_id == root.task_id
        assert latest.snapshot == {"status": "init"}

    def test_child_checkpoint_inherits_lineage(self, store: StateStore):
        """Checkpoints on a child task automatically capture parent and root task IDs."""
        root = store.create_task(title="Root goal")
        child = store.create_task(title="Subtask", parent_task_id=root.task_id)

        cp = store.create_checkpoint(
            task_id=child.task_id,
            checkpoint_seq=1,
            snapshot={"step": 1},
            execution_id="exec-123",
            parent_execution_id="parent-exec-001",
        )

        assert cp.parent_task_id == root.task_id
        assert cp.root_task_id == root.task_id
        assert cp.execution_id == "exec-123"
        assert cp.parent_execution_id == "parent-exec-001"
        assert cp.snapshot == {"step": 1}

        # Retrieval preserves fields
        listed = store.list_checkpoints(child.task_id)
        assert len(listed) == 1
        assert listed[0].parent_task_id == root.task_id
        assert listed[0].root_task_id == root.task_id
        assert listed[0].execution_id == "exec-123"
        assert listed[0].snapshot == {"step": 1}


# ==============================================================================
# 3. LINEAGE & HIERARCHY QUERY TESTS
# ==============================================================================


class TestHierarchyQueries:
    """Validate StateStore query methods for task and checkpoint hierarchy traversal."""

    def test_get_child_tasks(self, store: StateStore):
        """get_child_tasks returns all direct subtasks of a parent."""
        root = store.create_task(title="Root")
        c1 = store.create_task(title="Child 1", parent_task_id=root.task_id)
        c2 = store.create_task(title="Child 2", parent_task_id=root.task_id)
        # Grandchild
        store.create_task(title="Grandchild", parent_task_id=c1.task_id)

        root_children = store.get_child_tasks(root.task_id)
        assert [c.task_id for c in root_children] == [c1.task_id, c2.task_id]

        c1_children = store.get_child_tasks(c1.task_id)
        assert len(c1_children) == 1
        assert c1_children[0].title == "Grandchild"

    def test_get_task_lineage(self, store: StateStore):
        """get_task_lineage returns the chain of ancestor records from root to target."""
        root = store.create_task(title="Root")
        c1 = store.create_task(title="Child", parent_task_id=root.task_id)
        g1 = store.create_task(title="Grandchild", parent_task_id=c1.task_id)

        lineage = store.get_task_lineage(g1.task_id)
        assert len(lineage) == 3
        assert lineage[0].task_id == root.task_id
        assert lineage[1].task_id == c1.task_id
        assert lineage[2].task_id == g1.task_id

    def test_get_task_hierarchy_tree(self, store: StateStore):
        """get_task_hierarchy returns the full recursive tree with checkpoints and counts."""
        root = store.create_task(title="Root Tree")
        store.create_checkpoint(root.task_id, 1, {"p": 1})
        c1 = store.create_task(title="Branch 1", parent_task_id=root.task_id)
        store.create_checkpoint(c1.task_id, 1, {"c": 1})
        store.create_task(title="Branch 2", parent_task_id=root.task_id)

        tree = store.get_task_hierarchy(root.task_id)
        assert tree["task_id"] == root.task_id
        assert tree["checkpoint_count"] == 1
        assert tree["child_count"] == 2
        assert len(tree["children"]) == 2
        assert tree["children"][0]["task_id"] == c1.task_id
        assert tree["children"][0]["checkpoint_count"] == 1

    def test_get_child_and_hierarchy_checkpoints(self, store: StateStore):
        """get_child_checkpoints and get_hierarchy_checkpoints retrieve cross-tree checkpoints."""
        root = store.create_task(title="Root")
        c1 = store.create_task(title="C1", parent_task_id=root.task_id)
        c2 = store.create_task(title="C2", parent_task_id=root.task_id)

        store.create_checkpoint(root.task_id, 1, {"ev": "root_cp"})
        store.create_checkpoint(c1.task_id, 1, {"ev": "c1_cp"})
        store.create_checkpoint(c2.task_id, 1, {"ev": "c2_cp"})

        child_cps = store.get_child_checkpoints(root.task_id)
        assert len(child_cps) == 2
        assert {cp.task_id for cp in child_cps} == {c1.task_id, c2.task_id}

        all_cps = store.get_hierarchy_checkpoints(root.task_id)
        assert len(all_cps) == 3
        assert {cp.task_id for cp in all_cps} == {root.task_id, c1.task_id, c2.task_id}


# ==============================================================================
# 4. CRASH RECOVERY & RECONCILIATION TESTS
# ==============================================================================


class TestReconciliationHierarchy:
    """Validate interrupted subtask handling and parent-child recovery."""

    def test_interrupted_subtask_reconciles_safely(self, store: StateStore):
        """An interrupted idempotent step in a child task safely resets to PENDING."""
        root = store.create_task(title="Root Task")
        child = store.create_task(title="Child Task", parent_task_id=root.task_id)

        store.transition_task_state(child.task_id, TaskState.PLANNING)
        step = store.create_step(
            task_id=child.task_id,
            step_index=0,
            action_name="filesystem.read",
            is_idempotent=True,
        )
        store.transition_task_state(child.task_id, TaskState.EXECUTING)
        store.transition_step_state(step.step_id, StepState.RUNNING)

        engine = ReconciliationEngine(store)
        report = engine.reconcile_startup()

        child_result = next((r for r in report.reconciled_tasks if r.task_id == child.task_id), None)
        assert child_result is not None
        assert child_result.disposition == RecoveryDisposition.RESUME
        assert child_result.parent_task_id == root.task_id
        assert child_result.root_task_id == root.task_id

        reloaded_step = store.get_step(step.step_id)
        assert reloaded_step is not None
        assert reloaded_step.state == StepState.PENDING

    def test_parent_terminal_failure_cascades_to_running_child(self, store: StateStore):
        """When parent task is FAILED or CANCELLED, executing subtasks cascade to safe termination."""
        parent = store.create_task(title="Parent")
        store.transition_task_state(parent.task_id, TaskState.PLANNING)
        store.transition_task_state(parent.task_id, TaskState.EXECUTING)
        store.transition_task_state(parent.task_id, TaskState.FAILED, error_summary="Fatal crash")

        child = store.create_task(title="Child", parent_task_id=parent.task_id)
        store.transition_task_state(child.task_id, TaskState.PLANNING)
        store.transition_task_state(child.task_id, TaskState.EXECUTING)

        engine = ReconciliationEngine(store)
        report = engine.reconcile_startup()

        child_result = next((r for r in report.reconciled_tasks if r.task_id == child.task_id), None)
        assert child_result is not None
        assert child_result.disposition == RecoveryDisposition.FAIL_TERMINAL
        assert child_result.new_state == TaskState.FAILED
        assert "safely terminated" in child_result.action_summary

        updated_child = store.get_task(child.task_id)
        assert updated_child is not None
        assert updated_child.state == TaskState.FAILED

    def test_orchestrating_parent_reconciles_subtasks(self, store: StateStore):
        """An executing parent task with completed subtasks reconciles to COMPLETED."""
        parent = store.create_task(title="Orchestrator Parent")
        store.transition_task_state(parent.task_id, TaskState.PLANNING)
        store.transition_task_state(parent.task_id, TaskState.EXECUTING)

        c1 = store.create_task(title="Sub 1", parent_task_id=parent.task_id)
        store.transition_task_state(c1.task_id, TaskState.PLANNING)
        store.transition_task_state(c1.task_id, TaskState.EXECUTING)
        store.transition_task_state(c1.task_id, TaskState.COMPLETED)

        engine = ReconciliationEngine(store)
        report = engine.reconcile_startup()

        parent_res = next((r for r in report.reconciled_tasks if r.task_id == parent.task_id), None)
        assert parent_res is not None
        assert parent_res.disposition == RecoveryDisposition.RESUME
        assert parent_res.new_state == TaskState.COMPLETED

    def test_sibling_task_isolation(self, store: StateStore):
        """Failure in one sibling child task does not corrupt independent sibling child task."""
        root = store.create_task(title="Root")
        c1 = store.create_task(title="Sibling 1", parent_task_id=root.task_id)
        c2 = store.create_task(title="Sibling 2", parent_task_id=root.task_id)

        # C1 fails
        store.transition_task_state(c1.task_id, TaskState.PLANNING)
        store.transition_task_state(c1.task_id, TaskState.EXECUTING)
        store.transition_task_state(c1.task_id, TaskState.FAILED, error_summary="Failure in 1")

        # C2 is SUBMITTED
        engine = ReconciliationEngine(store)
        report = engine.reconcile_startup()

        c2_res = next((r for r in report.reconciled_tasks if r.task_id == c2.task_id), None)
        assert c2_res is not None
        assert c2_res.disposition == RecoveryDisposition.RESUME
        assert c2_res.new_state == TaskState.SUBMITTED


# ==============================================================================
# 5. BACKWARD COMPATIBILITY & ZERO-SECRET TESTS
# ==============================================================================


class TestBackwardCompatibilityAndSecurity:
    """Validate backward compatibility and zero-secret invariants."""

    def test_legacy_unlinked_checkpoint_deserialization(self):
        """Legacy checkpoints without hierarchy fields deserialize with correct defaults."""
        legacy_data = {
            "checkpoint_id": "legacy-cp-1",
            "task_id": "legacy-task-1",
            "checkpoint_seq": 1,
            "snapshot": {"result": "ok"},
            "created_at": "2026-09-01T00:00:00Z",
        }
        cp = CheckpointSnapshot.model_validate(legacy_data)
        assert cp.parent_task_id is None
        assert cp.root_task_id == "legacy-task-1"
        assert cp.execution_id is None
        assert cp.parent_execution_id is None
        assert cp.is_root is True
        assert cp.snapshot == {"result": "ok"}

    def test_legacy_unlinked_task_record_deserialization(self):
        """Legacy task records without hierarchy fields deserialize with correct defaults."""
        legacy_data = {
            "task_id": "legacy-task-100",
            "title": "Legacy Task",
            "state": "SUBMITTED",
            "priority": 100,
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
            "metadata": {},
        }
        task = TaskRecord.model_validate(legacy_data)
        assert task.parent_task_id is None
        assert task.root_task_id == "legacy-task-100"
        assert task.is_root is True
        assert task.is_subtask is False

    def test_zero_secret_rejection_in_hierarchy_metadata(self, store: StateStore):
        """Secret patterns detected in metadata raise SecretLeakError."""
        with pytest.raises(SecretLeakError):
            store.create_task(
                title="Normal title",
                metadata={"api_token": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890"},
            )

    def test_zero_secret_rejection_in_checkpoint_hierarchy(self, store: StateStore):
        """Secret patterns detected in checkpoint snapshots raise SecretLeakError."""
        task = store.create_task(title="Secure Task")
        with pytest.raises(SecretLeakError):
            store.create_checkpoint(
                task_id=task.task_id,
                checkpoint_seq=1,
                snapshot={"auth_header": "Bearer ghp_abcdefghijklmnopqrstuvwxyz123456"},
            )


from src.trust import build_trust_authority
from src.tools.broker import ToolBroker


@pytest.fixture
def supervisor(db_conn: sqlite3.Connection, store: StateStore) -> Supervisor:
    authority = build_trust_authority(db_conn)
    broker = ToolBroker(authority)
    return Supervisor(state_store=store, trust_authority=authority, tool_broker=broker)


# ==============================================================================
# 6. SUPERVISOR SUBTASK & SKILL EXECUTION TESTS
# ==============================================================================


class TestSupervisorSubtaskIntegration:
    """Validate Supervisor subtask creation and skill execution linking."""

    def test_supervisor_create_subtask(self, supervisor: Supervisor, store: StateStore):
        """Supervisor.create_subtask produces a durable task linked to parent."""
        parent = supervisor.submit_goal("Parent Goal")
        subtask = supervisor.create_subtask(
            parent_task_id=parent.task_id,
            title="Subtask Goal",
        )

        assert subtask.parent_task_id == parent.task_id
        assert subtask.root_task_id == parent.task_id
        assert subtask.is_subtask is True

        children = store.get_child_tasks(parent.task_id)
        assert len(children) == 1
        assert children[0].task_id == subtask.task_id

    def test_supervisor_execute_skill_creates_hierarchical_checkpoints(
        self, db_conn: sqlite3.Connection, store: StateStore
    ):
        """execute_skill creates pre- and post-execution checkpoints linked to task hierarchy."""
        from src.skills.executor import SkillExecutor
        from src.skills.models import SkillManifest, SkillType
        from src.skills.registry import SkillRegistry

        class SimpleExecutor(SkillExecutor):
            def execute(self, invocation, bound_broker):
                return SkillResult(
                    invocation_id=invocation.invocation_id,
                    skill_id=invocation.skill_id,
                    status=SkillExecutionStatus.SUCCESS,
                    output={"result": "ok"},
                    verification_status=VerificationStatus.VERIFIED,
                )

        registry = SkillRegistry()
        manifest = SkillManifest(
            skill_id="test.simple",
            version="1.0.0",
            name="Test Simple",
            description="Test skill",
            skill_type=SkillType.DETERMINISTIC,
        )
        registry.register_skill(manifest, executor=SimpleExecutor())

        authority = build_trust_authority(db_conn)
        broker = ToolBroker(authority)
        supervisor = Supervisor(
            state_store=store,
            trust_authority=authority,
            tool_broker=broker,
            skill_registry=registry,
        )

        parent = supervisor.submit_goal("Parent Goal")
        subtask = supervisor.create_subtask(parent_task_id=parent.task_id, title="Subtask Skill")

        invocation = SkillInvocation(
            skill_id="test.simple",
            task_id=subtask.task_id,
            input={"foo": "bar"},
        )

        res = supervisor.execute_skill(
            invocation=invocation,
            parent_task_id=parent.task_id,
            root_task_id=parent.root_task_id,
        )
        assert res.status == SkillExecutionStatus.SUCCESS

        checkpoints = store.list_checkpoints(subtask.task_id)
        assert len(checkpoints) == 2
        # Pre-execution checkpoint
        assert checkpoints[0].snapshot["event"] == "SKILL_PRE_EXECUTION"
        assert checkpoints[0].parent_task_id == parent.task_id
        assert checkpoints[0].root_task_id == parent.task_id
        assert checkpoints[0].execution_id == invocation.invocation_id

        # Post-execution checkpoint
        assert checkpoints[1].snapshot["event"] == "SKILL_POST_EXECUTION"
        assert checkpoints[1].parent_task_id == parent.task_id
        assert checkpoints[1].root_task_id == parent.task_id
        assert checkpoints[1].execution_id == invocation.invocation_id
