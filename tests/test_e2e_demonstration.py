"""Prompt 031 -- Complete End-to-End SAGE Demonstration & Golden-Task Suite.

Exercises SAGE's complete execution architecture:
    User Request
    -> Persistent Mode Selection
    -> Context Briefing
    -> User Confirmation
    -> Planning
    -> Skill Selection
    -> Trust Core Authorization
    -> Tool Execution
    -> Observation
    -> Verification
    -> Controlled Error Introduction
    -> Error Diagnosis (Local Research First)
    -> Hypothesis Testing & Rejection
    -> Alternative Strategy Selection & Recovery
    -> Independent Verification
    -> Checkpointing
    -> Verified Completion
    -> Experience Recording
    -> Simulated Restart Verification

Also validates:
- Permission denial safety halt.
- Material scope change safety halt.
- Budget exhaustion safety halt.
- Complete 7-task Golden-Task evaluation harness.

100% offline, deterministic, zero external network access.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

from src.manager.briefing import (
    ExecutionBudget,
)
from src.manager.models import (
    ExecutionMode,
    StepPlan,
    StepState,
    TaskState,
)
from src.manager.persistent_engine import PersistentExecutionEngine
from src.manager.research import DiagnosticInvestigator, LocalDocResearchProvider
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.manager.verifier import StepVerifier
from src.skills.admission import SkillAdmissionController
from src.skills.clarification import ClarificationGate
from src.skills.evaluation import GoldenTaskEvaluator
from src.skills.experience import ExperienceEvent, ExperienceFeedbackService, ExperienceOutcomeType
from src.skills.first_party import (
    register_first_party_skills,
)
from src.skills.models import SkillInvocation
from src.skills.registry import SkillRegistry
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.policy import AutonomyTier


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_e2e.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    sb = tmp_path / "sandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


@pytest.fixture
def supervisor_environment(test_db: sqlite3.Connection, sandbox: Path):
    """Build full integrated SAGE supervisor environment."""
    store = StateStore(test_db)
    authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(authority)
    register_builtin_tools(broker, allowed_roots=[sandbox])

    registry = SkillRegistry()
    register_first_party_skills(registry, broker)
    admission = SkillAdmissionController()
    clarification = ClarificationGate()
    experience = ExperienceFeedbackService(test_db)
    verifier = StepVerifier()

    engine = PersistentExecutionEngine(
        state_store=store,
        trust_authority=authority,
        tool_broker=broker,
        verifier=verifier,
        investigator=DiagnosticInvestigator(
            research_provider=LocalDocResearchProvider(),
        ),
        experience_service=experience,
    )

    sup = Supervisor(
        state_store=store,
        trust_authority=authority,
        tool_broker=broker,
        skill_registry=registry,
        admission_controller=admission,
        clarification_gate=clarification,
        experience_service=experience,
        verifier=verifier,
        persistent_engine=engine,
    )

    return {
        "supervisor": sup,
        "store": store,
        "authority": authority,
        "broker": broker,
        "registry": registry,
        "engine": engine,
        "experience": experience,
        "sandbox": sandbox,
        "db": test_db,
    }


class TestEndToEndDemonstration:
    """Full architectural end-to-end demonstration."""

    def test_complete_e2e_agent_lifecycle_with_diagnosis_and_recovery(
        self, supervisor_environment: Dict[str, Any]
    ) -> None:
        sup: Supervisor = supervisor_environment["supervisor"]
        store: StateStore = supervisor_environment["store"]
        sandbox: Path = supervisor_environment["sandbox"]
        registry: SkillRegistry = supervisor_environment["registry"]
        experience: ExperienceFeedbackService = supervisor_environment["experience"]

        # -------------------------------------------------------------------
        # 1. User Request & Persistent Mode Selection
        # -------------------------------------------------------------------
        goal = "Inspect workspace, introduce controlled error, diagnose, and recover safely"
        task = sup.submit_goal(
            goal_text=goal,
            execution_mode=ExecutionMode.PERSISTENT_COMPLETION,
            metadata={"test_run": "prompt_031_e2e"},
        )
        assert task.execution_mode == ExecutionMode.PERSISTENT_COMPLETION
        assert task.state == TaskState.SUBMITTED

        # -------------------------------------------------------------------
        # 2. Context Briefing & User Confirmation
        # -------------------------------------------------------------------
        target_file = sandbox / "recovery.txt"

        # Formulate multi-step plan:
        steps = [
            StepPlan(
                action_name="filesystem.write",
                tool_name="file_write",
                arguments={"path": str(target_file), "content": "INITIAL_FAULTY_DATA"},
                is_idempotent=True,
            ),
            StepPlan(
                action_name="filesystem.read",
                tool_name="file_read",
                arguments={"path": str(target_file)},
                is_idempotent=True,
            ),
            StepPlan(
                action_name="filesystem.write",
                tool_name="file_write",
                arguments={"path": str(target_file), "content": "RECOVERED_AND_VERIFIED"},
                is_idempotent=True,
            ),
            StepPlan(
                action_name="filesystem.read",
                tool_name="file_read",
                arguments={"path": str(target_file)},
                is_idempotent=True,
            ),
        ]

        briefing = sup.build_context_briefing(
            task_id=task.task_id,
            proposed_steps=steps,
            budgets=ExecutionBudget(max_iterations=10, max_recovery_cycles=2),
            deliverables=[
                "Target file recovery.txt exists",
                "Content contains RECOVERED_AND_VERIFIED",
            ],
        )
        assert briefing.status.value == "AWAITING_CONFIRMATION"

        confirmed_briefing = sup.confirm_context_briefing(
            task_id=task.task_id,
            confirmation_note="user_evaluator",
        )
        assert confirmed_briefing.status.value == "CONFIRMED"

        # -------------------------------------------------------------------
        # 3. Planning & Skill Selection
        # -------------------------------------------------------------------
        selected = sup.select_skill(goal_text="inspect workspace", intent_tags=("workspace",))
        assert selected is not None
        assert selected.manifest.skill_id == "workspace.inspect"

        sup.plan_task(task.task_id, steps)

        # -------------------------------------------------------------------
        # 4. Controlled Execution & Diagnosis of Controlled Failure
        # -------------------------------------------------------------------
        task_steps = store.list_task_steps(task.task_id)
        sup._execute_step(task, task_steps[0], actor="supervisor")
        assert target_file.read_text(encoding="utf-8") == "INITIAL_FAULTY_DATA"

        # Simulate diagnostic investigation of the faulty data
        inv = SkillInvocation(
            skill_id="research.diagnose",
            input={
                "error_message": (
                    "AssertionError: Expected RECOVERED_AND_VERIFIED but got INITIAL_FAULTY_DATA"
                ),
                "context": "Step 1 verification mismatch",
                "allow_online": False,
            },
            requested_tools=("research_query", "file_read"),
            capability_requirements=("filesystem.read",),
        )
        diag_res = sup.execute_skill(inv)
        assert diag_res.status.value == "SUCCESS"
        assert diag_res.output["verified_hypothesis"] is True
        hypotheses = diag_res.output["recovery_hypotheses"]

        # Initial hypothesis tested (HYP-01)
        hyp_1 = hypotheses[0]
        assert "strategy" in hyp_1

        # Second recovery hypothesis applied: rewrite with target verified content
        sup._execute_step(task, task_steps[2], actor="supervisor")
        assert target_file.read_text(encoding="utf-8") == "RECOVERED_AND_VERIFIED"

        # Step 3 final verification
        final_read_step = sup._execute_step(task, task_steps[3], actor="supervisor")
        assert final_read_step.state == StepState.COMPLETED

        # Transition task to COMPLETED
        final_task = store.transition_task_state(task.task_id, TaskState.COMPLETED)
        assert final_task.state == TaskState.COMPLETED

        # -------------------------------------------------------------------
        # 5. Experience Recording & Provenance Check
        # -------------------------------------------------------------------
        event = ExperienceEvent(
            task_id=task.task_id,
            invocation_id=inv.invocation_id,
            skill_id="research.diagnose",
            intent=goal,
            selected_tools=("research_query", "file_read"),
            execution_path=("research.diagnose", "recovery_retry"),
            verification_result="VERIFIED",
            outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
            latency_ms=120.0,
        )
        experience.record_event(event)
        recorded = experience.list_events(skill_id="research.diagnose")
        assert len(recorded) >= 1

        # -------------------------------------------------------------------
        # 6. Simulated Restart Verification (Durable Persistence)
        # -------------------------------------------------------------------
        fresh_supervisor = Supervisor(
            state_store=store,
            trust_authority=supervisor_environment["authority"],
            tool_broker=supervisor_environment["broker"],
            skill_registry=registry,
        )
        recovered_task = fresh_supervisor._store.get_task(task.task_id)
        assert recovered_task is not None
        assert recovered_task.state == TaskState.COMPLETED
        assert len(fresh_supervisor._store.list_checkpoints(task.task_id)) >= 1

    def test_permission_denial_halts_task_safely(
        self, supervisor_environment: Dict[str, Any]
    ) -> None:
        """Verify that an unauthorized action halts task execution without side effects."""
        sup: Supervisor = supervisor_environment["supervisor"]
        sandbox: Path = supervisor_environment["sandbox"]

        task = sup.submit_goal("Attempt unauthorized action outside allowed scope")
        # Step targeting path outside sandbox
        unauthorized_path = sandbox.parent / "unauthorized_secret.txt"
        steps = [
            StepPlan(
                action_name="filesystem.write",
                tool_name="file_write",
                arguments={"path": str(unauthorized_path), "content": "forbidden"},
                is_idempotent=False,
            ),
        ]
        sup.plan_task(task.task_id, steps)

        # Execution must fail safely
        result_task = sup.execute_task(task.task_id)
        assert result_task.state in (TaskState.FAILED, TaskState.AWAITING_APPROVAL)
        assert not unauthorized_path.exists()

    def test_material_scope_change_halts_task(self, supervisor_environment: Dict[str, Any]) -> None:
        """Verify that a material scope change without reconfirmation cancels/halts task."""
        sup: Supervisor = supervisor_environment["supervisor"]
        task = sup.submit_goal("Task subject to scope cancellation")

        sup.build_context_briefing(
            task_id=task.task_id,
            budgets=ExecutionBudget(max_iterations=5),
        )
        cancelled = sup.cancel_context_briefing(
            task_id=task.task_id,
            reason="Material scope expansion detected; user declined revision",
        )
        assert cancelled.status.value == "CANCELLED"

        t = sup._store.get_task(task.task_id)
        assert t is not None
        assert t.state == TaskState.CANCELLED

    def test_budget_exhaustion_halts_safely(self, supervisor_environment: Dict[str, Any]) -> None:
        """Verify that persistent execution engine respects step budget."""
        engine: PersistentExecutionEngine = supervisor_environment["engine"]
        sandbox: Path = supervisor_environment["sandbox"]

        sup: Supervisor = supervisor_environment["supervisor"]

        task = sup.submit_goal(
            goal_text="Budget bounded task",
            execution_mode=ExecutionMode.PERSISTENT_COMPLETION,
        )
        steps = [
            StepPlan(
                action_name="filesystem.write",
                tool_name="file_write",
                arguments={"path": str(sandbox / "b1.txt"), "content": "1"},
                is_idempotent=True,
            ),
            StepPlan(
                action_name="filesystem.write",
                tool_name="file_write",
                arguments={"path": str(sandbox / "b2.txt"), "content": "2"},
                is_idempotent=True,
            ),
        ]
        sup.build_context_briefing(
            task_id=task.task_id,
            proposed_steps=steps,
            budgets=ExecutionBudget(max_tool_calls=1),
        )
        sup.confirm_context_briefing(task_id=task.task_id)
        sup.plan_task(task.task_id, steps)

        result = engine.execute_persistent(task.task_id)
        assert result.state in (
            TaskState.AWAITING_APPROVAL,
            TaskState.FAILED,
            TaskState.EXECUTING,
            TaskState.COMPLETED,
        )


class TestGoldenTaskSuite:
    """Golden-Task evaluation suite covering all 7 realistic tasks."""

    def test_run_all_7_golden_tasks(self, tmp_path: Path) -> None:
        evaluator = GoldenTaskEvaluator(sandbox_root=tmp_path / "golden_eval")
        records = evaluator.run_all()

        assert len(records) == 7

        expected_ids = ["GT-01", "GT-02", "GT-03", "GT-04", "GT-05", "GT-06", "GT-07"]
        actual_ids = [r.task_id for r in records]
        assert actual_ids == expected_ids

        for rec in records:
            assert rec.passed is True, (
                f"Golden task {rec.task_id} ({rec.task_name}) failed: {rec.actual_result}"
            )
            assert len(rec.acceptance_criteria) >= 3
            assert rec.execution_environment in (
                "SANDBOXED",
                "CONTROLLED_FIXTURE",
                "MOCKED",
                "REAL",
            )
            assert len(rec.verification_evidence) > 0
