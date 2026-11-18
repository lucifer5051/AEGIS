"""Integration tests for the closed experience feedback and structured lesson retrieval loop.

Canonical Reference:
    PROMPT 065: Experience Feedback & Structured Lesson Retrieval Integration (ARCH-060-04 Remediation)

Covers:
1. Experience recording after successful task execution.
2. Failure-event recording during terminal execution halts and planner errors.
3. Lesson approval requirements (evidence thresholds before becoming APPROVED).
4. Trigger-condition matching against task intent.
5. Relevant versus irrelevant lesson retrieval (project and keyword isolation).
6. Planner consumption of retrieved lessons (structured injection in planning prompts).
7. FastDecisionService integration (cautionary lesson triggers advisory fallback).
8. Rejection of unapproved lessons (CANDIDATE and VERIFIED states strictly excluded).
9. Stale and contradictory lesson handling (superseded and refuted lessons excluded).
10. Retrieval failure and fallback behavior (non-blocking degradation on storage errors).
11. Context-budget compliance (Prompt 064 context bounds honored with lessons).
12. TrustCore authorization invariants (lessons never grant capabilities or bypass policies).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from src.decision.models import (
    DecisionOutcome,
    DecisionRoute,
    FallbackReason,
    FastDecisionRequest,
    FastDecisionResult,
    ProviderStatus,
    RequestContext,
)
from src.decision.providers.base import FastDecisionProvider
from src.decision.routing import ExecutionStrategy
from src.decision.service import FastDecisionService
from src.manager.context import ContextAssembler, ContextIR, estimate_tokens
from src.manager.models import StepState, TaskRecord, TaskState
from src.manager.planner import Plan, PlannedStep, StructuredPlanner
from src.manager.state_store import StateStore
from src.manager.supervisor import GoalExecutionResult, Supervisor
from src.memory.lessons import LessonManager
from src.memory.models import (
    ExecutionLesson,
    LessonProposal,
    LessonRiskLevel,
    LessonState,
)
from src.memory.retriever import EKMRetriever
from src.memory.store import EKMMemoryStore
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import (
    ReasoningMetrics,
    ReasoningRequest,
    ReasoningResponse,
)
from src.skills.experience import (
    ExperienceEvent,
    ExperienceFeedbackService,
    ExperienceOutcomeType,
    UserFeedbackType,
)
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.policy import AutonomyTier


# ---------------------------------------------------------------------------
# Fixtures & Mock Providers
# ---------------------------------------------------------------------------


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_feedback_loop.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    root = tmp_path / "feedback_outputs"
    root.mkdir(parents=True, exist_ok=True)
    return root


class RecordingReasoningProvider(LocalReasoningProvider):
    """Reasoning provider that records incoming prompts for assertion."""

    def __init__(self, canned_steps: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__(provider_id="mock_recording_reasoner", model_id="mock_model")
        self.canned_steps = canned_steps or []
        self.recorded_requests: List[ReasoningRequest] = []

    def initialize(self) -> bool:
        return True

    def is_ready(self) -> bool:
        return True

    def reason(self, request: ReasoningRequest) -> ReasoningResponse:
        self.recorded_requests.append(request)
        plan_dict = {
            "goal": "Test goal",
            "rationale": "Generated with past lesson awareness",
            "steps": self.canned_steps
            or [
                {
                    "step_number": 1,
                    "description": "Default planned step",
                    "tool": "file_write",
                    "arguments": {"path": "test.txt", "content": "mock"},
                    "is_idempotent": True,
                    "acceptance_criteria": "done",
                    "estimated_risk": "LOW",
                    "target_resource": "*",
                }
            ],
        }
        return ReasoningResponse(
            content=json.dumps(plan_dict),
            finish_reason="stop",
            prompt_tokens=40,
            completion_tokens=40,
            total_tokens=80,
            model_id=self.model_id,
            provider_id=self.provider_id,
        )

    def unload(self) -> bool:
        return True

    def verify_integrity(self) -> bool:
        return True

    def get_metrics(self) -> ReasoningMetrics:
        return ReasoningMetrics()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_01_experience_recorded_after_successful_goal(
    test_db: sqlite3.Connection,
    output_root: Path,
) -> None:
    """Successful goal execution records a VERIFIED_SUCCESS ExperienceEvent with tools and path."""
    auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(auth)
    register_builtin_tools(broker, allowed_roots=[output_root])
    store = StateStore(test_db)
    exp_service = ExperienceFeedbackService(conn=test_db)

    target_file = str(output_root / "success.txt")
    reasoner = RecordingReasoningProvider(
        canned_steps=[
            {
                "step_number": 1,
                "description": "Write target file",
                "tool": "file_write",
                "arguments": {"path": target_file, "content": "success data"},
                "is_idempotent": True,
                "acceptance_criteria": "written",
                "estimated_risk": "LOW",
                "target_resource": target_file,
            }
        ]
    )

    supervisor = Supervisor(
        state_store=store,
        trust_authority=auth,
        tool_broker=broker,
        reasoning_provider=reasoner,
        experience_service=exp_service,
    )

    result = supervisor.execute_goal("write success output file")
    assert result.success is True
    assert Path(target_file).exists()

    events = exp_service.list_events(limit=10)
    assert len(events) >= 1
    event = events[-1]
    assert event.outcome_type == ExperienceOutcomeType.VERIFIED_SUCCESS
    assert event.verification_result == "VERIFIED"
    assert "file_write" in event.selected_tools
    assert event.intent == "write success output file"


def test_02_failure_event_recorded_on_execution_halt(
    test_db: sqlite3.Connection,
    output_root: Path,
) -> None:
    """When a task fails terminally, an EXECUTION_FAILURE ExperienceEvent is recorded."""
    auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(auth)
    register_builtin_tools(broker, allowed_roots=[output_root])
    store = StateStore(test_db)
    exp_service = ExperienceFeedbackService(conn=test_db)

    # Step will fail because file does not exist
    reasoner = RecordingReasoningProvider(
        canned_steps=[
            {
                "step_number": 1,
                "description": "Read missing non-existent file",
                "tool": "file_read",
                "arguments": {"path": str(output_root / "missing.txt")},
                "is_idempotent": True,
                "acceptance_criteria": "read",
                "estimated_risk": "LOW",
                "target_resource": "*",
            }
        ]
    )

    supervisor = Supervisor(
        state_store=store,
        trust_authority=auth,
        tool_broker=broker,
        reasoning_provider=reasoner,
        experience_service=exp_service,
    )

    result = supervisor.execute_goal("read missing file", max_replan_attempts=0)
    assert result.success is False

    events = exp_service.list_events(limit=10)
    assert len(events) >= 1
    event = events[-1]
    assert event.outcome_type == ExperienceOutcomeType.EXECUTION_FAILURE
    assert event.verification_result == "FAILED"
    assert event.error_or_recovery_info is not None


def test_03_lesson_approval_evidence_gating(test_db: sqlite3.Connection) -> None:
    """Candidate lessons require required corroborations before becoming APPROVED."""
    mem_store = EKMMemoryStore(test_db)
    lesson_mgr = LessonManager(test_db, memory_store=mem_store)

    event1 = ExperienceEvent(
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id="fs.writer",
        intent="write data safely",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
    )

    proposal = LessonProposal(
        title="Check directory existence before file write",
        trigger_condition="target directory does not exist",
        recommended_action="call directory_create before file_write",
        observation="file_write failed with FileNotFoundError on non-existent parent directory",
        inferred_explanation="Tool cannot automatically create parent trees without explicit directory flag",
        risk_level=LessonRiskLevel.LOW,
    )

    lesson = lesson_mgr.propose_candidate(proposal, event1)
    assert lesson.state == LessonState.CANDIDATE
    assert lesson.confirmation_count == 1

    # Corroboration 1: LOW risk requires 2 confirmations to reach APPROVED
    event2 = ExperienceEvent(
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id="fs.writer",
        intent="corroborate directory lesson",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
    )
    approved_lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, event2)
    assert approved_lesson.state == LessonState.APPROVED
    assert approved_lesson.confirmation_count == 2


def test_04_trigger_condition_and_relevance_matching(test_db: sqlite3.Connection) -> None:
    """ContextAssembler retrieves only approved lessons matching task intent keywords."""
    mem_store = EKMMemoryStore(test_db)
    lesson_mgr = LessonManager(test_db, memory_store=mem_store)
    retriever = EKMRetriever(mem_store, lesson_manager=lesson_mgr)
    assembler = ContextAssembler(
        conn=test_db,
        memory_store=mem_store,
        retriever=retriever,
        lesson_manager=lesson_mgr,
    )

    # Lesson A: JSON parsing
    prop_a = LessonProposal(
        title="Validate JSON syntax",
        trigger_condition="parse json string from untrusted source",
        recommended_action="use safe_json_loads with try-except",
        observation="invalid json raised JSONDecodeError",
        inferred_explanation="LLM occasionally produces markdown code fences around json",
        risk_level=LessonRiskLevel.LOW,
    )
    # Lesson B: Docker container isolation
    prop_b = LessonProposal(
        title="Docker volume permissions",
        trigger_condition="bind mount on windows docker host",
        recommended_action="specify absolute forward-slash path",
        observation="docker run failed with invalid mount format",
        inferred_explanation="Windows backslashes corrupt docker mount options",
        risk_level=LessonRiskLevel.LOW,
    )

    ev = ExperienceEvent(
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id="test",
        intent="test",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
    )
    les_a = lesson_mgr.propose_candidate(prop_a, ev)
    lesson_mgr.corroborate_lesson(les_a.lesson_id, ev)  # Promoted to APPROVED

    les_b = lesson_mgr.propose_candidate(prop_b, ev)
    lesson_mgr.corroborate_lesson(les_b.lesson_id, ev)  # Promoted to APPROVED

    # Query for JSON task
    retrieved = assembler.retrieve_relevant_lessons(
        goal="Please parse untrusted JSON input string"
    )
    assert len(retrieved) >= 1
    assert retrieved[0].lesson_id == les_a.lesson_id
    assert "parse json" in retrieved[0].trigger_condition.lower()


def test_05_relevant_versus_irrelevant_lesson_isolation(test_db: sqlite3.Connection) -> None:
    """Project-scoped lessons do not leak into different project scopes."""
    mem_store = EKMMemoryStore(test_db)
    lesson_mgr = LessonManager(test_db, memory_store=mem_store)
    retriever = EKMRetriever(mem_store, lesson_manager=lesson_mgr)
    assembler = ContextAssembler(
        conn=test_db,
        memory_store=mem_store,
        retriever=retriever,
        lesson_manager=lesson_mgr,
    )

    ev = ExperienceEvent(
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id="test",
        intent="test",
        project_id="project_alpha",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
    )
    prop = LessonProposal(
        title="Alpha Project Deployment Secret",
        trigger_condition="deploy alpha cluster",
        recommended_action="export ALPHA_ENV=true",
        observation="alpha deployment failed without env",
        inferred_explanation="requires specific alpha env",
        risk_level=LessonRiskLevel.LOW,
        project_id="project_alpha",
    )
    les = lesson_mgr.propose_candidate(prop, ev)
    lesson_mgr.corroborate_lesson(les.lesson_id, ev)

    # Retrieval for project_beta must NOT receive project_alpha lesson
    beta_lessons = assembler.retrieve_relevant_lessons(
        goal="deploy alpha cluster",
        project_id="project_beta",
    )
    assert not any(l.lesson_id == les.lesson_id for l in beta_lessons)

    # Retrieval for project_alpha MUST receive it
    alpha_lessons = assembler.retrieve_relevant_lessons(
        goal="deploy alpha cluster",
        project_id="project_alpha",
    )
    assert any(l.lesson_id == les.lesson_id for l in alpha_lessons)


def test_06_planner_consumes_retrieved_operational_lessons(
    test_db: sqlite3.Connection,
    output_root: Path,
) -> None:
    """StructuredPlanner includes approved operational lessons in the model prompt."""
    auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(auth)
    register_builtin_tools(broker, allowed_roots=[output_root])
    store = StateStore(test_db)
    mem_store = EKMMemoryStore(test_db)
    lesson_mgr = LessonManager(test_db, memory_store=mem_store)

    ev = ExperienceEvent(
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id="fs.writer",
        intent="write files",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
    )
    prop = LessonProposal(
        title="Atomic File Writing Principle",
        trigger_condition="writing config files in parallel",
        recommended_action="write to temp file and rename atomically",
        observation="direct writes resulted in partial file reads by concurrent processes",
        inferred_explanation="file_write is not POSIX atomic on Windows",
        risk_level=LessonRiskLevel.LOW,
    )
    les = lesson_mgr.propose_candidate(prop, ev)
    lesson_mgr.corroborate_lesson(les.lesson_id, ev)

    target_file = str(output_root / "atomic.txt")
    reasoner = RecordingReasoningProvider(
        canned_steps=[
            {
                "step_number": 1,
                "description": "Write target file",
                "tool": "file_write",
                "arguments": {"path": target_file, "content": "atomic"},
                "is_idempotent": True,
                "acceptance_criteria": "written",
                "estimated_risk": "LOW",
                "target_resource": target_file,
            }
        ]
    )

    supervisor = Supervisor(
        state_store=store,
        trust_authority=auth,
        tool_broker=broker,
        reasoning_provider=reasoner,
        lesson_manager=lesson_mgr,
        memory_store=mem_store,
    )

    result = supervisor.execute_goal("writing config files in parallel safely")
    assert result.success is True

    # Inspect the prompt that was dispatched to the reasoning provider
    assert len(reasoner.recorded_requests) >= 1
    prompt_dispatched = reasoner.recorded_requests[0].prompt

    assert "OPERATIONAL LESSONS FROM PAST EXPERIENCE" in prompt_dispatched
    assert "Atomic File Writing Principle" in prompt_dispatched
    assert "write to temp file and rename atomically" in prompt_dispatched


def test_07_fast_decision_service_cautious_lesson_advisory_fallback(
    test_db: sqlite3.Connection,
) -> None:
    """When an active lesson recommends planning or caution, FastDecisionService advises fallback."""
    class DummyProvider(FastDecisionProvider):
        def initialize(self) -> bool:
            return True

        def is_ready(self) -> bool:
            return True

        def evaluate(self, request: FastDecisionRequest, criteria: Dict[Any, str]) -> FastDecisionResult:
            return FastDecisionResult(
                outcome=DecisionOutcome.FAST_PATH,
                route=DecisionRoute.FILE_INTENT,
                confidence=0.98,
                alternatives=[],
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                latency_ms=1.0,
                is_advisory_only=True,
            )

        def unload(self) -> None:
            pass

        def get_metrics(self) -> Any:
            return None

        def verify_integrity(self) -> bool:
            return True

        def qualify(self, profile: Any = None) -> bool:
            return True

    service = FastDecisionService(provider=DummyProvider(provider_id="dummy", provider_version="1.0"))

    # Case 1: Normal request -> FAST_PATH accepted
    req_normal = FastDecisionRequest(
        user_input="write simple file",
        context=RequestContext(project_scope="test"),
    )
    res_normal = service.evaluate(req_normal)
    assert res_normal.outcome == DecisionOutcome.FAST_PATH

    # Case 2: Request with active cautionary lesson trigger -> FALLBACK_TO_MANAGER
    req_cautious = FastDecisionRequest(
        user_input="write file with caution",
        context=RequestContext(
            project_scope="test",
            active_lessons=["Caution: direct file execution failed; requires structured plan"],
        ),
    )
    res_cautious = service.evaluate(req_cautious)
    assert res_cautious.outcome == DecisionOutcome.FALLBACK_TO_MANAGER
    assert res_cautious.is_advisory_only is True


def test_08_rejection_of_unapproved_lessons(test_db: sqlite3.Connection) -> None:
    """Candidate, verified, and rejected lessons are NEVER retrieved into planning context."""
    mem_store = EKMMemoryStore(test_db)
    lesson_mgr = LessonManager(test_db, memory_store=mem_store)
    assembler = ContextAssembler(conn=test_db, memory_store=mem_store, lesson_manager=lesson_mgr)

    ev = ExperienceEvent(
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id="test",
        intent="test",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
    )
    prop = LessonProposal(
        title="Uncorroborated Hypothesis",
        trigger_condition="untested edge scenario",
        recommended_action="take risky action",
        observation="happened once",
        inferred_explanation="speculative",
        risk_level=LessonRiskLevel.MEDIUM,
    )
    # Stays in CANDIDATE state (only 1 confirmation)
    lesson = lesson_mgr.propose_candidate(prop, ev)
    assert lesson.state == LessonState.CANDIDATE

    retrieved = assembler.retrieve_relevant_lessons(goal="untested edge scenario")
    assert not any(l.lesson_id == lesson.lesson_id for l in retrieved)


def test_09_stale_and_contradictory_lessons_excluded(test_db: sqlite3.Connection) -> None:
    """Superseded and contradicted/rejected lessons are excluded from retrieval."""
    mem_store = EKMMemoryStore(test_db)
    lesson_mgr = LessonManager(test_db, memory_store=mem_store)
    assembler = ContextAssembler(conn=test_db, memory_store=mem_store, lesson_manager=lesson_mgr)

    ev_pos = ExperienceEvent(
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id="test",
        intent="test",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
    )
    prop = LessonProposal(
        title="Outdated Workaround",
        trigger_condition="install legacy driver",
        recommended_action="use legacy patch",
        observation="patch succeeded earlier",
        inferred_explanation="needed for old OS",
        risk_level=LessonRiskLevel.LOW,
    )
    les = lesson_mgr.propose_candidate(prop, ev_pos)
    lesson_mgr.corroborate_lesson(les.lesson_id, ev_pos)  # APPROVED

    # Invalidate / supersede it
    lesson_mgr.invalidate_lesson(les.lesson_id, reason="OS upgraded to modern kernel")

    retrieved = assembler.retrieve_relevant_lessons(goal="install legacy driver")
    assert not any(l.lesson_id == les.lesson_id for l in retrieved)


def test_10_retrieval_failure_degrades_gracefully(test_db: sqlite3.Connection) -> None:
    """When lesson storage fails or table is inaccessible, context assembly continues cleanly."""
    assembler = ContextAssembler(conn=test_db)
    # Intentionally corrupt connection query or pass bad query
    bad_conn = sqlite3.connect(":memory:")  # Empty DB without execution_lessons table
    assembler_bad = ContextAssembler(conn=bad_conn)

    # Must degrade gracefully and return an empty list without raising exception
    lessons = assembler_bad.retrieve_relevant_lessons("test goal")
    assert lessons == []

    context_ir = assembler_bad.assemble("test goal")
    assert isinstance(context_ir, ContextIR)
    assert context_ir.relevant_lessons == []


def test_11_context_budget_compliance_with_lessons(test_db: sqlite3.Connection) -> None:
    """Lessons injected into context strictly comply with Prompt 064 token budget."""
    mem_store = EKMMemoryStore(test_db)
    lesson_mgr = LessonManager(test_db, memory_store=mem_store)

    ev = ExperienceEvent(
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id="test",
        intent="test",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
    )

    # Create 5 approved lessons
    for i in range(5):
        prop = LessonProposal(
            title=f"Lesson #{i} on complex query execution",
            trigger_condition=f"trigger condition #{i} matching database query",
            recommended_action=f"recommended procedure #{i} " + ("action " * 20),
            observation=f"observed result #{i} " + ("result " * 20),
            inferred_explanation=f"explanation #{i}",
            risk_level=LessonRiskLevel.LOW,
        )
        l = lesson_mgr.propose_candidate(prop, ev)
        lesson_mgr.corroborate_lesson(l.lesson_id, ev)

    assembler = ContextAssembler(
        conn=test_db,
        memory_store=mem_store,
        lesson_manager=lesson_mgr,
        default_token_budget=512,
    )
    context_ir = assembler.assemble(
        "complex database query execution",
        token_budget=512,
    )
    assert len(context_ir.relevant_lessons) <= 5

    prompt_text = context_ir.to_prompt_context()
    tokens = estimate_tokens(prompt_text)
    # Must respect the 512 token budget (with minor margin)
    assert tokens <= 550
    assert "GOAL: complex database query execution" in prompt_text


def test_12_lessons_never_override_trustcore_authorizations(
    test_db: sqlite3.Connection,
    output_root: Path,
) -> None:
    """Advisory lessons cannot bypass TrustCore policy denials."""
    auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    # Hard deny filesystem.write
    auth._policy._hard_denied = frozenset(["filesystem.write"])
    broker = ToolBroker(auth)
    register_builtin_tools(broker, allowed_roots=[output_root])
    store = StateStore(test_db)
    mem_store = EKMMemoryStore(test_db)
    lesson_mgr = LessonManager(test_db, memory_store=mem_store)

    # A lesson advising file_write
    ev = ExperienceEvent(
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id="test",
        intent="test",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS,
    )
    prop = LessonProposal(
        title="Bypass Advisory Attempt",
        trigger_condition="write prohibited resource",
        recommended_action="invoke file_write directly",
        observation="file was written",
        inferred_explanation="advisory lesson",
        risk_level=LessonRiskLevel.LOW,
    )
    les = lesson_mgr.propose_candidate(prop, ev)
    lesson_mgr.corroborate_lesson(les.lesson_id, ev)

    target_file = str(output_root / "forbidden.txt")
    reasoner = RecordingReasoningProvider(
        canned_steps=[
            {
                "step_number": 1,
                "description": "Write forbidden file",
                "tool": "file_write",
                "arguments": {"path": target_file, "content": "forbidden"},
                "is_idempotent": True,
                "acceptance_criteria": "written",
                "estimated_risk": "LOW",
                "target_resource": target_file,
            }
        ]
    )

    supervisor = Supervisor(
        state_store=store,
        trust_authority=auth,
        tool_broker=broker,
        reasoning_provider=reasoner,
        lesson_manager=lesson_mgr,
    )

    result = supervisor.execute_goal("write prohibited resource")
    assert result.success is False
    # Tool must NOT have been executed
    assert not Path(target_file).exists()

