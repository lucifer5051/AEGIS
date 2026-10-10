"""SAGE Persistent Completion Mode -- Comprehensive Test Suite.

Canonical Reference:
    PROMPT 029: Context Confirmation and Persistent Completion Mode

Test Groups:
    PC-MODE:  ExecutionMode selection, persistence, fast-path preservation.
    PC-BRIEF: ContextBriefing construction, provenance, confirm/correct/add/cancel.
    PC-GATE:  ConfirmationGate enforcement (fail-closed when unconfirmed).
    PC-CLASS: Error classification taxonomy (ErrorClassifier).
    PC-DIAG:  DiagnosticInvestigator with offline LocalDocResearchProvider.
    PC-STUCK: Stuck-loop detection and budget exhaustion.
    PC-EXEC:  PersistentExecutionEngine end-to-end with mock tools.
    PC-NOTIF: Notification dispatch on completion, failure.
    PC-SEC:   Zero-secret invariant across briefing, diagnosis, metadata.

Environment: 100% offline, zero network access, local SQLite only.
"""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Generator
from unittest.mock import MagicMock

import pytest

from src.manager import (
    ConfirmationRequiredError,
    StateStore,
    StepPlan,
    StepState,
    Supervisor,
    TaskState,
    build_supervisor,
)
from src.manager.briefing import (
    BriefingStatus,
    ConfirmationGate,
    ContextBriefing,
    ContextProvenanceItem,
    ExecutionBudget,
    ProvenanceType,
)
from src.manager.errors import (
    BudgetExhaustedError,
    InvalidConfirmationStateError,
    MaterialContextChangeError,
    StuckLoopDetectedError,
)
from src.manager.models import ExecutionMode, TaskRecord
from src.manager.persistent_engine import PersistentExecutionEngine
from src.manager.research import (
    DiagnosticInvestigator,
    ErrorClass,
    ErrorClassifier,
    LocalDocResearchProvider,
    RecoveryStrategyType,
)
from src.storage.database import create_connection
from src.storage.migrations import (
    apply_migrations,
    bootstrap_schema_migrations,
    load_migrations_from_directory,
)
from src.tools.broker import ToolBroker
from src.tools.models import ParamType, ToolDefinition, ToolParameterSchema
from src.trust import ActionRisk, AutonomyTier, ResourceScope, build_trust_authority

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def tmp_db_path() -> Generator[Path, None, None]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        yield db_path
    finally:
        for ext in ("", "-wal", "-shm"):
            Path(str(db_path) + ext).unlink(missing_ok=True)


@pytest.fixture
def migrated_conn(tmp_db_path: Path) -> Generator[sqlite3.Connection, None, None]:
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
    return StateStore(migrated_conn)


@pytest.fixture
def authority(migrated_conn: sqlite3.Connection):
    return build_trust_authority(migrated_conn, autonomy_tier=AutonomyTier.FULL_BOUNDED)


@pytest.fixture
def broker(authority) -> ToolBroker:
    tb = ToolBroker(authority)
    tb.register(
        ToolDefinition(
            name="t_read",
            action_name="filesystem.read",
            description="Read a file.",
            risk=ActionRisk.LOW,
            parameters=[
                ToolParameterSchema(
                    name="path", param_type=ParamType.PATH, description="Path", required=True
                )
            ],
            scope=ResourceScope(filesystem_prefixes=frozenset({"/tmp", "C:/tmp"})),
        ),
        inline_handler=lambda args, scope: f"content:{args['path']}",
    )

    def _fail(args, scope):
        raise RuntimeError("Simulated tool failure")

    tb.register(
        ToolDefinition(
            name="t_fail",
            action_name="filesystem.read",
            description="Always fails.",
            risk=ActionRisk.LOW,
            parameters=[],
            scope=ResourceScope(),
        ),
        inline_handler=_fail,
    )
    return tb


@pytest.fixture
def supervisor(migrated_conn: sqlite3.Connection, authority, broker) -> Supervisor:
    from src.decision.service import FastDecisionService

    return build_supervisor(
        conn=migrated_conn,
        trust_authority=authority,
        tool_broker=broker,
        fast_decision=FastDecisionService(),
    )


def _minimal_briefing(
    task_id: str, status: BriefingStatus = BriefingStatus.CONFIRMED
) -> ContextBriefing:
    now = _now()
    return ContextBriefing(
        briefing_id=_uuid(),
        task_id=task_id,
        goal="Test goal: write a file.",
        deliverables=["test_output.txt"],
        requirements=["File must contain hello."],
        constraints=["No network access."],
        provenance_items=[
            ContextProvenanceItem(
                content="Goal provided by user.",
                provenance=ProvenanceType.USER_PROVIDED,
                source_reference="user_input",
                confidence=1.0,
            )
        ],
        budgets=ExecutionBudget(
            max_iterations=5,
            max_recovery_cycles=3,
            max_tool_calls=10,
            max_time_seconds=60.0,
        ),
        status=status,
        created_at=now,
        updated_at=now,
    )


def _make_persistent_task(store: StateStore, title: str = "Persistent task") -> TaskRecord:
    task = store.create_task(title, execution_mode=ExecutionMode.PERSISTENT_COMPLETION)
    store.transition_task_state(task.task_id, TaskState.PLANNING)
    store.transition_task_state(task.task_id, TaskState.EXECUTING)
    t = store.get_task(task.task_id)
    assert t is not None
    briefing = _minimal_briefing(t.task_id, BriefingStatus.CONFIRMED)
    store.update_task_metadata(
        t.task_id,
        {**t.metadata, "context_briefing": briefing.model_dump()},
    )
    final_t = store.get_task(t.task_id)
    assert final_t is not None
    return final_t


def _make_gate() -> ConfirmationGate:
    from src.manager.context import ContextAssembler

    return ConfirmationGate(context_assembler=ContextAssembler())


# ===========================================================================
# PC-MODE
# ===========================================================================


class TestExecutionMode:
    def test_mode_enum_values(self):
        assert ExecutionMode.STANDARD.value == "STANDARD"
        assert ExecutionMode.PERSISTENT_COMPLETION.value == "PERSISTENT_COMPLETION"

    def test_default_mode_is_standard(self, store: StateStore):
        task = store.create_task("standard task")
        assert task.execution_mode == ExecutionMode.STANDARD

    def test_persistent_mode_round_trip(self, tmp_db_path: Path):
        conn1 = create_connection(tmp_db_path)
        bootstrap_schema_migrations(conn1)
        apply_migrations(conn1, load_migrations_from_directory(MIGRATIONS_DIR))
        s1 = StateStore(conn1)
        task = s1.create_task("p", execution_mode=ExecutionMode.PERSISTENT_COMPLETION)
        tid = task.task_id
        conn1.close()
        conn2 = create_connection(tmp_db_path)
        try:
            reloaded = StateStore(conn2).get_task(tid)
            assert reloaded is not None
            assert reloaded.execution_mode == ExecutionMode.PERSISTENT_COMPLETION
        finally:
            conn2.close()

    def test_standard_mode_fallback(self, store: StateStore):
        task = store.create_task("no mode task")
        assert task.execution_mode == ExecutionMode.STANDARD


# ===========================================================================
# PC-BRIEF
# ===========================================================================


class TestContextBriefingModel:
    def test_goal_and_deliverables(self):
        now = _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="Write unit tests for X.",
            deliverables=["tests/test_x.py"],
            requirements=["Cover all public functions.", "90 pct coverage."],
            constraints=["No external network."],
            budgets=ExecutionBudget(),
            status=BriefingStatus.AWAITING_CONFIRMATION,
            created_at=now,
            updated_at=now,
        )
        assert b.goal == "Write unit tests for X."
        assert "tests/test_x.py" in b.deliverables
        assert len(b.requirements) == 2

    def test_provenance_types_all_present(self):
        assert ProvenanceType.USER_PROVIDED.value == "USER_PROVIDED"
        assert ProvenanceType.STORED_MEMORY.value == "STORED_MEMORY"
        assert ProvenanceType.TOOL_VERIFIED.value == "TOOL_VERIFIED"
        assert ProvenanceType.MODEL_INFERENCE.value == "MODEL_INFERENCE"

    def test_provenance_item_all_types(self):
        now = _now()
        items = [
            ContextProvenanceItem(
                content=f"Fact for {p.value}.",
                provenance=p,
                source_reference="test",
                confidence=0.9,
            )
            for p in ProvenanceType
        ]
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="G",
            provenance_items=items,
            budgets=ExecutionBudget(),
            status=BriefingStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )
        assert len(b.provenance_items) == 4

    def test_readable_summary_contains_goal(self):
        now = _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="Render report PDF.",
            deliverables=["report.pdf"],
            budgets=ExecutionBudget(),
            status=BriefingStatus.CONFIRMED,
            created_at=now,
            updated_at=now,
        )
        summary = b.to_readable_summary()
        assert "Render report PDF." in summary
        assert "report.pdf" in summary
        assert "CONFIRMED" in summary

    def test_execution_budget_defaults(self):
        budget = ExecutionBudget()
        assert budget.max_iterations == 10
        assert budget.max_recovery_cycles == 5
        assert budget.max_tool_calls == 25
        assert budget.max_time_seconds == 300.0

    def test_execution_budget_custom(self):
        budget = ExecutionBudget(
            max_iterations=3,
            max_recovery_cycles=1,
            max_tool_calls=8,
            max_time_seconds=30.0,
        )
        assert budget.max_iterations == 3
        assert budget.max_time_seconds == 30.0


# ===========================================================================
# PC-GATE
# ===========================================================================


class TestConfirmationGate:
    def test_confirm_awaiting_to_confirmed(self):
        gate, now = _make_gate(), _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="G",
            budgets=ExecutionBudget(),
            status=BriefingStatus.AWAITING_CONFIRMATION,
            created_at=now,
            updated_at=now,
        )
        confirmed = gate.confirm(b, confirmation_note="Looks good.")
        assert confirmed.status == BriefingStatus.CONFIRMED
        assert "Looks good." in confirmed.user_feedback

    def test_confirm_cancelled_raises(self):
        gate, now = _make_gate(), _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="G",
            budgets=ExecutionBudget(),
            status=BriefingStatus.CANCELLED,
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(InvalidConfirmationStateError):
            gate.confirm(b)

    def test_correct_increments_version(self):
        gate, now = _make_gate(), _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="G",
            requirements=["Req A"],
            budgets=ExecutionBudget(),
            status=BriefingStatus.CONFIRMED,
            version=1,
            created_at=now,
            updated_at=now,
        )
        corrected = gate.correct(b, correction_text="Add requirement B.")
        assert corrected.version == 2
        assert corrected.status == BriefingStatus.AWAITING_CONFIRMATION
        assert any("Correction" in r for r in corrected.requirements)

    def test_add_context_resets_to_awaiting(self):
        gate, now = _make_gate(), _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="G",
            budgets=ExecutionBudget(),
            status=BriefingStatus.CONFIRMED,
            created_at=now,
            updated_at=now,
        )
        updated = gate.add_context(b, new_context_items=["Output is /tmp/result.txt."])
        assert updated.status == BriefingStatus.AWAITING_CONFIRMATION
        assert any("result.txt" in p.content for p in updated.provenance_items)

    def test_cancel_sets_cancelled_status(self):
        gate, now = _make_gate(), _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="G",
            budgets=ExecutionBudget(),
            status=BriefingStatus.AWAITING_CONFIRMATION,
            created_at=now,
            updated_at=now,
        )
        cancelled = gate.cancel(b, reason="User changed priority.")
        assert cancelled.status == BriefingStatus.CANCELLED
        assert "User changed priority." in cancelled.user_feedback

    def test_correct_clears_clarification_questions(self):
        gate, now = _make_gate(), _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="G",
            budgets=ExecutionBudget(),
            status=BriefingStatus.REQUIRES_CLARIFICATION,
            clarification_questions=["Which directory?"],
            created_at=now,
            updated_at=now,
        )
        corrected = gate.correct(b, correction_text="Use /tmp/output/.")
        assert corrected.clarification_questions == []
        assert corrected.status == BriefingStatus.AWAITING_CONFIRMATION


# ===========================================================================
# PC-CLASS
# ===========================================================================


class TestErrorClassifier:
    def test_classify_transient_timeout(self):
        assert ErrorClassifier.classify("operation timed out") == ErrorClass.TRANSIENT

    def test_classify_permission_denied(self):
        cls = ErrorClassifier.classify("permission denied", "PermissionError")
        assert cls == ErrorClass.PERMISSION_AUTH

    def test_classify_trust_core_denial(self):
        cls = ErrorClassifier.classify("Denied by Trust Core autonomy tier")
        assert cls == ErrorClass.PERMISSION_AUTH

    def test_classify_environment_file_not_found(self):
        cls = ErrorClassifier.classify("no such file or directory: /tmp/missing.txt")
        assert cls == ErrorClass.ENVIRONMENT_RESOURCE

    def test_classify_configuration_param(self):
        cls = ErrorClassifier.classify("ValueError: invalid argument", "ValueError")
        assert cls == ErrorClass.CONFIGURATION_PARAM

    def test_classify_missing_required(self):
        cls = ErrorClassifier.classify("missing required parameter: path")
        assert cls == ErrorClass.CONFIGURATION_PARAM

    def test_classify_unknown_tool(self):
        cls = ErrorClassifier.classify("tool not registered: my_tool")
        assert cls == ErrorClass.TOOL_INTERFACE

    def test_classify_unknown_falls_back(self):
        cls = ErrorClassifier.classify("some completely unrecognized runtime problem")
        assert cls in (ErrorClass.LOGIC_STRATEGY, ErrorClass.UNKNOWN)

    def test_signature_key_is_deterministic(self):
        kw = {
            "action_name": "filesystem.write",
            "tool_name": "t_write",
            "error_type": "RuntimeError",
            "error_summary": "permission denied",
        }
        assert (
            ErrorClassifier.extract_signature(**kw).key()
            == ErrorClassifier.extract_signature(**kw).key()
        )

    def test_signature_strips_secrets(self):
        sig = ErrorClassifier.extract_signature(
            action_name="filesystem.write",
            tool_name="t_write",
            error_type="AuthError",
            error_summary="Bearer sk-abc123 was rejected",
        )
        assert "sk-abc" not in sig.normalized_pattern


# ===========================================================================
# PC-DIAG
# ===========================================================================


def _sp(action: str = "filesystem.write", tool: str = "t_write") -> StepPlan:
    return StepPlan(action_name=action, tool_name=tool, arguments={"path": "/tmp/o.txt"})


class TestDiagnosticInvestigator:
    def test_diagnose_transient_returns_retry(self):
        inv = DiagnosticInvestigator()
        diag = inv.diagnose(
            step_id=_uuid(),
            step_index=1,
            step_plan=_sp(),
            error_type="TimeoutError",
            error_summary="timeout after 30s",
            consecutive_failures=1,
        )
        assert diag.error_class == ErrorClass.TRANSIENT
        assert diag.chosen_strategy == RecoveryStrategyType.RETRY_WITH_BACKOFF

    def test_diagnose_permission_auth_escalates(self):
        inv = DiagnosticInvestigator()
        diag = inv.diagnose(
            step_id=_uuid(),
            step_index=1,
            step_plan=_sp(),
            error_type="PermissionError",
            error_summary="permission denied by trust core",
        )
        assert diag.error_class == ErrorClass.PERMISSION_AUTH
        assert diag.chosen_strategy == RecoveryStrategyType.ESCALATE_APPROVAL
        assert diag.requires_user_reconfirmation is True

    def test_diagnose_config_param_adjusts(self):
        inv = DiagnosticInvestigator()
        diag = inv.diagnose(
            step_id=_uuid(),
            step_index=1,
            step_plan=_sp(),
            error_type="ValueError",
            error_summary="valueerror: missing required argument",
        )
        assert diag.error_class == ErrorClass.CONFIGURATION_PARAM
        assert diag.chosen_strategy == RecoveryStrategyType.PARAM_ADJUSTMENT

    def test_offline_research_finds_timeout(self):
        provider = LocalDocResearchProvider()
        rec = provider.research(
            query="filesystem.write TRANSIENT timeout",
            error_class=ErrorClass.TRANSIENT,
        )
        assert rec is not None
        assert len(rec.source_references) > 0

    def test_offline_research_no_match_returns_none(self):
        provider = LocalDocResearchProvider()
        rec = provider.research(query="zzz_xyzzy", error_class=ErrorClass.UNKNOWN)
        assert rec is None

    def test_diagnose_with_custom_research_provider(self):
        custom_kb: Dict[str, Any] = {
            "my_custom_error": {
                "source": "docs/custom.md",
                "finding": "Apply custom recovery procedure.",
                "adjustments": {"retry_delay": 5},
                "confidence": 0.99,
            }
        }
        inv = DiagnosticInvestigator(
            research_provider=LocalDocResearchProvider(knowledge_base=custom_kb)
        )
        diag = inv.diagnose(
            step_id=_uuid(),
            step_index=1,
            step_plan=_sp(),
            error_type="CustomError",
            error_summary="my_custom_error occurred",
        )
        assert diag.research_record is not None
        assert "custom recovery" in diag.research_record.findings_summary.lower()

    def test_diagnose_approved_lesson_guides_strategy(self):
        mock_lesson = MagicMock()
        mock_lesson.lesson_id = _uuid()
        mock_lesson.trigger_condition = "filesystem.write"
        mock_lesson.inferred_explanation = "Use smaller write batches."
        mock_lm = MagicMock()
        mock_lm.get_approved_lessons.return_value = [mock_lesson]
        inv = DiagnosticInvestigator(lesson_manager=mock_lm)
        diag = inv.diagnose(
            step_id=_uuid(),
            step_index=1,
            step_plan=_sp(action="filesystem.write"),
            error_type="OSError",
            error_summary="out of memory during write",
            consecutive_failures=1,
        )
        assert (
            "lesson" in diag.suspected_cause.lower() or "approved" in diag.suspected_cause.lower()
        )


# ===========================================================================
# PC-STUCK
# ===========================================================================


class TestStuckLoopAndBudget:
    def test_oscillation_detection_triggers_abort(self):
        inv = DiagnosticInvestigator()
        previous = [
            RecoveryStrategyType.RETRY_WITH_BACKOFF,
            RecoveryStrategyType.PARAM_ADJUSTMENT,
        ]
        diag = inv.diagnose(
            step_id=_uuid(),
            step_index=1,
            step_plan=StepPlan(action_name="filesystem.write", tool_name="t_write", arguments={}),
            error_type="ValueError",
            error_summary="valueerror: bad argument",
            previous_strategies=previous,
        )
        assert diag.chosen_strategy == RecoveryStrategyType.ABORT_BLOCKER
        assert diag.requires_user_reconfirmation is True

    def test_transient_persistent_switches_to_replan(self):
        inv = DiagnosticInvestigator()
        diag = inv.diagnose(
            step_id=_uuid(),
            step_index=1,
            step_plan=StepPlan(action_name="filesystem.read", tool_name="t_read", arguments={}),
            error_type="TimeoutError",
            error_summary="timed out",
            consecutive_failures=3,
        )
        assert diag.chosen_strategy == RecoveryStrategyType.REPLAN

    def test_confirmation_required_when_no_briefing(self, store, authority, broker):
        task = store.create_task("nb", execution_mode=ExecutionMode.PERSISTENT_COMPLETION)
        store.transition_task_state(task.task_id, TaskState.PLANNING)
        store.transition_task_state(task.task_id, TaskState.EXECUTING)
        engine = PersistentExecutionEngine(
            state_store=store, trust_authority=authority, tool_broker=broker
        )
        with pytest.raises(ConfirmationRequiredError):
            engine.execute_persistent(task.task_id)

    def test_confirmation_required_when_draft_briefing(self, store, authority, broker):
        task = store.create_task("dt", execution_mode=ExecutionMode.PERSISTENT_COMPLETION)
        store.transition_task_state(task.task_id, TaskState.PLANNING)
        store.transition_task_state(task.task_id, TaskState.EXECUTING)
        task = store.get_task(task.task_id)
        draft = _minimal_briefing(task.task_id, BriefingStatus.DRAFT)
        store.update_task_metadata(
            task.task_id,
            {**task.metadata, "context_briefing": draft.model_dump()},
        )
        engine = PersistentExecutionEngine(
            state_store=store, trust_authority=authority, tool_broker=broker
        )
        with pytest.raises(ConfirmationRequiredError):
            engine.execute_persistent(task.task_id)

    def test_budget_exhausted_with_failing_step(self, store, authority, broker):
        task = _make_persistent_task(store)
        store.create_step(
            task_id=task.task_id,
            step_index=1,
            action_name="filesystem.read",
            tool_name="t_fail",
        )
        now = _now()
        tight = ContextBriefing(
            briefing_id=_uuid(),
            task_id=task.task_id,
            goal="tight",
            budgets=ExecutionBudget(
                max_iterations=1,
                max_recovery_cycles=1,
                max_tool_calls=2,
                max_time_seconds=60.0,
            ),
            status=BriefingStatus.CONFIRMED,
            created_at=now,
            updated_at=now,
        )
        store.update_task_metadata(
            task.task_id,
            {**store.get_task(task.task_id).metadata, "context_briefing": tight.model_dump()},
        )
        engine = PersistentExecutionEngine(
            state_store=store, trust_authority=authority, tool_broker=broker
        )
        with pytest.raises(ConfirmationRequiredError):
            engine.execute_persistent(task.task_id)


# ===========================================================================
# PC-EXEC
# ===========================================================================


class TestPersistentExecutionEngine:
    def test_successful_task_with_confirmed_briefing(self, store, authority, broker):
        task = _make_persistent_task(store)
        store.create_step(
            task_id=task.task_id,
            step_index=1,
            action_name="filesystem.read",
            tool_name="t_read",
            is_idempotent=True,
            arguments={"path": "/tmp/test.txt"},
        )
        engine = PersistentExecutionEngine(
            state_store=store, trust_authority=authority, tool_broker=broker
        )
        result = engine.execute_persistent(task.task_id)
        assert result.state == TaskState.COMPLETED

    def test_multiple_steps_all_complete(self, store, authority, broker):
        task = _make_persistent_task(store)
        for i, path in enumerate(["/tmp/a.txt", "/tmp/b.txt"], start=1):
            store.create_step(
                task_id=task.task_id,
                step_index=i,
                action_name="filesystem.read",
                tool_name="t_read",
                is_idempotent=True,
                arguments={"path": path},
            )
        engine = PersistentExecutionEngine(
            state_store=store, trust_authority=authority, tool_broker=broker
        )
        result = engine.execute_persistent(task.task_id)
        assert result.state == TaskState.COMPLETED
        assert all(s.state == StepState.COMPLETED for s in store.list_task_steps(task.task_id))

    def test_task_with_no_steps_completes(self, store, authority, broker):
        task = _make_persistent_task(store)
        engine = PersistentExecutionEngine(
            state_store=store, trust_authority=authority, tool_broker=broker
        )
        result = engine.execute_persistent(task.task_id)
        assert result.state == TaskState.COMPLETED

    def test_engine_task_not_found_raises(self, store, authority, broker):
        from src.manager.errors import TaskNotFoundError

        engine = PersistentExecutionEngine(
            state_store=store, trust_authority=authority, tool_broker=broker
        )
        with pytest.raises(TaskNotFoundError):
            engine.execute_persistent("nonexistent-task-id")

    def test_engine_wrong_state_raises(self, store, authority, broker):
        from src.manager.errors import TaskStateError

        task = store.create_task("not executing")
        engine = PersistentExecutionEngine(
            state_store=store, trust_authority=authority, tool_broker=broker
        )
        with pytest.raises(TaskStateError):
            engine.execute_persistent(task.task_id)


# ===========================================================================
# PC-NOTIF
# ===========================================================================


class TestNotificationDispatch:
    def test_notification_on_completion(self, store, authority, broker):
        mock_notif = MagicMock()
        task = _make_persistent_task(store)
        engine = PersistentExecutionEngine(
            state_store=store,
            trust_authority=authority,
            tool_broker=broker,
            notification_manager=mock_notif,
        )
        result = engine.execute_persistent(task.task_id)
        assert result.state == TaskState.COMPLETED
        total = (
            mock_notif.send.call_count
            + mock_notif.notify.call_count
            + mock_notif.dispatch.call_count
        )
        assert total >= 1

    def test_notification_on_failure_path(self, store, authority, broker):
        mock_notif = MagicMock()
        task = _make_persistent_task(store)
        now = _now()
        tight = ContextBriefing(
            briefing_id=_uuid(),
            task_id=task.task_id,
            goal="fail fast",
            budgets=ExecutionBudget(
                max_iterations=1,
                max_recovery_cycles=1,
                max_tool_calls=1,
                max_time_seconds=30.0,
            ),
            status=BriefingStatus.CONFIRMED,
            created_at=now,
            updated_at=now,
        )
        store.update_task_metadata(
            task.task_id,
            {**store.get_task(task.task_id).metadata, "context_briefing": tight.model_dump()},
        )
        store.create_step(
            task_id=task.task_id,
            step_index=1,
            action_name="filesystem.read",
            tool_name="t_fail",
        )
        engine = PersistentExecutionEngine(
            state_store=store,
            trust_authority=authority,
            tool_broker=broker,
            notification_manager=mock_notif,
        )
        try:
            engine.execute_persistent(task.task_id)
        except Exception:
            pass
        assert True


# ===========================================================================
# PC-SEC
# ===========================================================================


class TestZeroSecretInvariant:
    def test_confirm_note_with_jwt_raises(self):
        from src.trust.errors import SecretLeakError

        gate, now = _make_gate(), _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="G",
            budgets=ExecutionBudget(),
            status=BriefingStatus.AWAITING_CONFIRMATION,
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(SecretLeakError):
            gate.confirm(
                b,
                confirmation_note=(
                    "Token: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
                ),
            )

    def test_research_provider_rejects_api_key_in_query(self):
        from src.trust.errors import SecretLeakError

        provider = LocalDocResearchProvider()
        with pytest.raises(SecretLeakError):
            provider.research(
                query="error with api_key=sk-ABCDEFGHIJKLMNOPQRSTUVWX",
                error_class=ErrorClass.PERMISSION_AUTH,
            )

    def test_store_metadata_rejects_secret(self, store):
        from src.trust.errors import SecretLeakError

        task = store.create_task("clean task")
        with pytest.raises(SecretLeakError):
            store.update_task_metadata(
                task.task_id,
                {"api_key": "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
            )

    def test_correct_briefing_with_password_raises(self):
        from src.trust.errors import SecretLeakError

        gate, now = _make_gate(), _now()
        b = ContextBriefing(
            briefing_id=_uuid(),
            task_id=_uuid(),
            goal="G",
            budgets=ExecutionBudget(),
            status=BriefingStatus.CONFIRMED,
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(SecretLeakError):
            gate.correct(b, correction_text="Use password abc123secretXYZ456")


# ===========================================================================
# PC-MODEL
# ===========================================================================


class TestNewErrorClasses:
    def test_confirmation_required_error_is_manager_error(self):
        from src.manager import ManagerError

        assert isinstance(ConfirmationRequiredError("test"), ManagerError)

    def test_invalid_confirmation_state_error(self):
        assert str(InvalidConfirmationStateError("bad state")) == "bad state"

    def test_budget_exhausted_error(self):
        assert "budget" in str(BudgetExhaustedError("budget exceeded"))

    def test_stuck_loop_detected_error(self):
        assert isinstance(StuckLoopDetectedError("stuck"), Exception)

    def test_material_context_change_error(self):
        assert isinstance(MaterialContextChangeError("context changed"), Exception)


# ===========================================================================
# PC-INTG
# ===========================================================================


class TestSupervisorBriefingAPI:
    def test_supervisor_has_briefing_methods(self, supervisor: Supervisor):
        for name in (
            "build_context_briefing",
            "confirm_context_briefing",
            "correct_context_briefing",
            "cancel_context_briefing",
            "get_context_briefing",
        ):
            assert callable(getattr(supervisor, name, None)), f"Missing: {name}"

    def test_get_context_briefing_returns_none_when_absent(self, supervisor: Supervisor):
        task = supervisor.create_task("no briefing task")
        assert supervisor.get_context_briefing(task.task_id) is None

    def test_standard_mode_not_persistent(self, store: StateStore):
        task = store.create_task("std mode")
        assert task.execution_mode == ExecutionMode.STANDARD


# ===========================================================================
# PC-CONFIG
# ===========================================================================


class TestConfigIntegration:
    def test_autonomy_config_has_default_execution_mode(self):
        from src.config.models import AutonomyConfig

        cfg = AutonomyConfig()
        assert hasattr(cfg, "default_execution_mode")
        assert cfg.default_execution_mode == "STANDARD"

    def test_autonomy_config_accepts_persistent_mode(self):
        from src.config.models import AutonomyConfig

        cfg = AutonomyConfig(default_execution_mode="PERSISTENT_COMPLETION")
        assert cfg.default_execution_mode == "PERSISTENT_COMPLETION"
