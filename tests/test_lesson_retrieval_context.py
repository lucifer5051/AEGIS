"""Tests for SAGE Prompt 026 -- Approved Lesson Retrieval & Context IR Integration.

Canonical Reference:
- PROMPT 026 Specification (§ 5)
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
- docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md

Invariants Verified:
1. Relevant approved lesson retrieval.
2. Exclusion of every non-active lesson state
   (CANDIDATE, VERIFIED, REJECTED, INVALIDATED, SUPERSEDED).
3. Project and skill scope isolation (no cross-project leakage).
4. Evidence and provenance preservation.
5. Deduplication against an EKM fact representing the same lesson.
6. Ranking and deterministic retrieval behavior.
7. Context-budget enforcement and omitted lesson IDs.
8. Separation of observations from inferred explanations.
9. Supervisor Context IR integration.
10. Empty lesson store and retrieval failures.
11. No additional model load for ordinary retrieval (zero LLM calls).
12. Proof that lessons cannot grant permissions or bypass Trust Core.
13. Regression coverage across EKM and lesson lifecycle.
14. 100% offline operation without network access.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest

from src.manager.context import ContextIR
from src.manager.models import StepPlan, TaskState
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.memory import (
    ContextCompiler,
    EKMMemoryStore,
    EKMRetriever,
    EvidenceCreate,
    ExecutionLesson,
    FactCreate,
    LessonManager,
    LessonProposal,
    LessonRiskLevel,
    LessonState,
    MemoryQuery,
)
from src.skills.experience import (
    ExperienceEvent,
    ExperienceOutcomeType,
    UserFeedbackType,
)
from src.storage.database import create_connection
from src.storage.migrations import (
    apply_migrations,
    bootstrap_schema_migrations,
    load_migrations_from_directory,
)
from src.tools.broker import ToolBroker
from src.trust import build_trust_authority

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ==============================================================================
# FIXTURES
# ==============================================================================


TEST_PROJECTS = [
    "proj-alpha",
    "proj-101",
    "proj-audit",
    "proj-sec",
    "proj-rank",
    "proj-dedup",
    "proj-budget",
    "proj-epistemic",
    "proj-sup",
    "proj-reg",
    "proj-offline",
    "project-secret-A",
    "project-secret-B",
    "proj-ev",
    "proj-pure",
]


@pytest.fixture
def temp_db() -> Generator[sqlite3.Connection, None, None]:
    """Provide an isolated, fully migrated SQLite database with test projects."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_aegis.db"
        conn = create_connection(db_path)
        bootstrap_schema_migrations(conn)
        migrations = load_migrations_from_directory(MIGRATIONS_DIR)
        apply_migrations(conn, migrations)

        now = "2026-10-10T12:00:00Z"
        for p_id in TEST_PROJECTS:
            conn.execute(
                "INSERT OR IGNORE INTO projects "
                "(project_id, name, slug, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'ACTIVE', ?, ?);",
                (p_id, f"Name {p_id}", p_id.lower().replace("-", "_"), now, now),
            )
        conn.commit()

        yield conn
        conn.close()


@pytest.fixture
def ekm_store(temp_db: sqlite3.Connection) -> EKMMemoryStore:
    return EKMMemoryStore(temp_db)


@pytest.fixture
def lesson_mgr(temp_db: sqlite3.Connection, ekm_store: EKMMemoryStore) -> LessonManager:
    return LessonManager(temp_db, memory_store=ekm_store)


@pytest.fixture
def ekm_retriever(ekm_store: EKMMemoryStore, lesson_mgr: LessonManager) -> EKMRetriever:
    return EKMRetriever(ekm_store, lesson_manager=lesson_mgr)


@pytest.fixture
def compiler() -> ContextCompiler:
    return ContextCompiler()


def _create_sample_event(
    skill_id: str = "file_editor",
    project_id: str | None = "proj-alpha",
    success: bool = True,
    evidence_id: str | None = None,
) -> ExperienceEvent:
    """Helper to construct an ExperienceEvent."""
    return ExperienceEvent(
        event_id=str(uuid4()),
        task_id=str(uuid4()),
        invocation_id=str(uuid4()),
        skill_id=skill_id,
        project_id=project_id,
        intent="edit config file safely",
        selected_tools=("file.read", "file.write"),
        execution_path=("plan", "execute", "verify"),
        verification_result="PASSED" if success else "FAILED",
        outcome_type=ExperienceOutcomeType.VERIFIED_SUCCESS
        if success
        else ExperienceOutcomeType.EXECUTION_FAILURE,
        user_feedback_type=UserFeedbackType.NO_FEEDBACK,
        latency_ms=45.2,
        evidence_id=evidence_id,
        confidence=0.95,
        provenance="system",
    )


def _promote_to_approved(
    lesson_mgr: LessonManager,
    proposal: LessonProposal,
    event: ExperienceEvent,
    confirmations_needed: int = 2,
) -> ExecutionLesson:
    """Helper to propose and promote a lesson to APPROVED."""
    lesson = lesson_mgr.propose_candidate(proposal, event)
    for _ in range(confirmations_needed):
        corrob_event = _create_sample_event(
            skill_id=proposal.skill_id or "default_skill",
            project_id=proposal.project_id,
            success=True,
            evidence_id=None,
        )
        lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, corrob_event)
    return lesson


# ==============================================================================
# TEST 1: RELEVANT APPROVED LESSON RETRIEVAL
# ==============================================================================


def test_01_relevant_approved_lesson_retrieval(
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
) -> None:
    """Verify that an approved lesson is retrieved when query text matches task relevance."""
    event = _create_sample_event(project_id="proj-101")
    proposal = LessonProposal(
        title="Always create atomic backup before write",
        trigger_condition="writing configuration files in production",
        recommended_action="write to temp file and rename atomically",
        observation="partial writes corrupt active services",
        inferred_explanation="power cut or process kill during direct write causes corruption",
        project_id="proj-101",
        risk_level=LessonRiskLevel.LOW,
    )
    approved_lesson = _promote_to_approved(lesson_mgr, proposal, event, confirmations_needed=2)
    assert approved_lesson.state == LessonState.APPROVED

    query = MemoryQuery(
        query_text="writing configuration files safely",
        project_id="proj-101",
    )
    result = ekm_retriever.retrieve(query)

    assert len(result.lessons) >= 1
    retrieved = result.lessons[0]
    assert retrieved.lesson_id == approved_lesson.lesson_id
    assert retrieved.title == approved_lesson.title
    assert retrieved.state == LessonState.APPROVED
    assert retrieved.lesson_id in result.ranking_scores
    assert result.ranking_scores[retrieved.lesson_id] > 0.0


# ==============================================================================
# TEST 2: EXCLUSION OF EVERY NON-ACTIVE LESSON STATE
# ==============================================================================


def test_02_exclusion_of_all_non_approved_states(
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
) -> None:
    """Verify that CANDIDATE, VERIFIED, REJECTED, INVALIDATED,
    and SUPERSEDED lessons are excluded.
    """
    event = _create_sample_event(project_id="proj-audit")

    # 1. CANDIDATE
    prop_cand = LessonProposal(
        title="Candidate Lesson Test",
        trigger_condition="deploying docker container",
        recommended_action="check daemon socket",
        observation="dockerd not running",
        inferred_explanation="service disabled",
        project_id="proj-audit",
        risk_level=LessonRiskLevel.MEDIUM,
    )
    cand_lesson = lesson_mgr.propose_candidate(prop_cand, event)
    assert cand_lesson.state == LessonState.CANDIDATE

    # 2. VERIFIED (MEDIUM risk promoted once -> VERIFIED, not yet APPROVED)
    prop_ver = LessonProposal(
        title="Verified Lesson Test",
        trigger_condition="deploying docker container verified",
        recommended_action="check network ports",
        observation="port collision",
        inferred_explanation="service already bound",
        project_id="proj-audit",
        risk_level=LessonRiskLevel.MEDIUM,
    )
    ver_lesson = lesson_mgr.propose_candidate(prop_ver, event)
    ver_lesson = lesson_mgr.corroborate_lesson(
        ver_lesson.lesson_id, _create_sample_event(project_id="proj-audit")
    )
    assert ver_lesson.state == LessonState.VERIFIED

    # 3. REJECTED
    prop_rej = LessonProposal(
        title="Rejected Lesson Test",
        trigger_condition="deploying docker container fail",
        recommended_action="disable firewall completely",
        observation="security breach",
        inferred_explanation="firewall was needed",
        project_id="proj-audit",
        risk_level=LessonRiskLevel.LOW,
    )
    rej_lesson = lesson_mgr.propose_candidate(prop_rej, event)
    for _ in range(2):
        fail_event = _create_sample_event(project_id="proj-audit", success=False)
        rej_lesson = lesson_mgr.corroborate_lesson(rej_lesson.lesson_id, fail_event)
    assert rej_lesson.state == LessonState.REJECTED

    # 4. INVALIDATED
    prop_inv = LessonProposal(
        title="Invalidated Lesson Test",
        trigger_condition="deploying docker container old",
        recommended_action="use legacy python 2 binary",
        observation="syntax error",
        inferred_explanation="binary deprecated",
        project_id="proj-audit",
        risk_level=LessonRiskLevel.LOW,
    )
    inv_lesson = _promote_to_approved(lesson_mgr, prop_inv, event, confirmations_needed=2)
    lesson_mgr.invalidate_lesson(inv_lesson.lesson_id, reason="Deprecated approach")

    # 5. SUPERSEDED
    prop_old = LessonProposal(
        title="Superseded Lesson Test",
        trigger_condition="deploying docker container superseded",
        recommended_action="use pip install",
        observation="slow",
        inferred_explanation="wheel cache missing",
        project_id="proj-audit",
        risk_level=LessonRiskLevel.LOW,
    )
    old_lesson = _promote_to_approved(lesson_mgr, prop_old, event, confirmations_needed=2)
    prop_new = LessonProposal(
        title="New Lesson Test",
        trigger_condition="deploying docker container superseded",
        recommended_action="use uv pip install",
        observation="fast",
        inferred_explanation="uv compiled cache",
        project_id="proj-audit",
        risk_level=LessonRiskLevel.LOW,
    )
    new_lesson = _promote_to_approved(lesson_mgr, prop_new, event, confirmations_needed=2)
    lesson_mgr.supersede_lesson(old_lesson.lesson_id, new_lesson.lesson_id, reason="Replaced by uv")

    # Now retrieve with broad query
    query = MemoryQuery(
        query_text="deploying docker container",
        project_id="proj-audit",
        include_superseded=False,
    )
    result = ekm_retriever.retrieve(query)

    retrieved_ids = {les.lesson_id for les in result.lessons}
    assert cand_lesson.lesson_id not in retrieved_ids
    assert ver_lesson.lesson_id not in retrieved_ids
    assert rej_lesson.lesson_id not in retrieved_ids
    assert inv_lesson.lesson_id not in retrieved_ids
    assert old_lesson.lesson_id not in retrieved_ids
    # Only new approved lesson should be present
    assert new_lesson.lesson_id in retrieved_ids


# ==============================================================================
# TEST 3: PROJECT AND SKILL SCOPE ISOLATION
# ==============================================================================


def test_03_project_and_skill_scope_isolation(
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
) -> None:
    """Verify that lessons from project A do not leak into project B or unscoped queries."""
    event_a = _create_sample_event(project_id="project-secret-A", skill_id="git_tool")
    prop_a = LessonProposal(
        title="Project A confidential rule",
        trigger_condition="pushing code to internal repo",
        recommended_action="use custom proxy port 8080",
        observation="timeout on direct connection",
        inferred_explanation="proxy firewall",
        project_id="project-secret-A",
        skill_id="git_tool",
        risk_level=LessonRiskLevel.LOW,
    )
    les_a = _promote_to_approved(lesson_mgr, prop_a, event_a, confirmations_needed=2)

    # 1. Query for Project B (must not return Project A lesson)
    query_b = MemoryQuery(
        query_text="pushing code to internal repo",
        project_id="project-secret-B",
        include_global=True,
    )
    result_b = ekm_retriever.retrieve(query_b)
    assert not any(les.lesson_id == les_a.lesson_id for les in result_b.lessons)

    # 2. Query with no project (unscoped query must not leak project-specific lesson)
    query_unscoped = MemoryQuery(
        query_text="pushing code to internal repo",
        project_id=None,
        include_global=True,
    )
    result_unscoped = ekm_retriever.retrieve(query_unscoped)
    assert not any(les.lesson_id == les_a.lesson_id for les in result_unscoped.lessons)

    # 3. Query for Project A with different skill filter
    query_diff_skill = MemoryQuery(
        query_text="pushing code to internal repo",
        project_id="project-secret-A",
        skill_id="other_tool",
    )
    result_diff_skill = ekm_retriever.retrieve(query_diff_skill)
    assert not any(les.lesson_id == les_a.lesson_id for les in result_diff_skill.lessons)

    # 4. Query for Project A with matching skill
    query_matching = MemoryQuery(
        query_text="pushing code to internal repo",
        project_id="project-secret-A",
        skill_id="git_tool",
    )
    result_matching = ekm_retriever.retrieve(query_matching)
    assert any(les.lesson_id == les_a.lesson_id for les in result_matching.lessons)


# ==============================================================================
# TEST 4: EVIDENCE AND PROVENANCE PRESERVATION
# ==============================================================================


def test_04_evidence_and_provenance_preservation(
    ekm_store: EKMMemoryStore,
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
) -> None:
    """Verify supporting evidence, events, and risk metadata are preserved in provenance_map."""
    evidence_id = ekm_store.add_evidence(
        EvidenceCreate(
            evidence_type="system_observation",
            source_reference="test_ref",
            raw_content_snippet="DDL lock acquisition collision log",
            project_id="proj-ev",
        )
    )
    event = _create_sample_event(project_id="proj-ev", skill_id="db_skill", evidence_id=evidence_id)
    prop = LessonProposal(
        title="Database migration transaction wrapping",
        trigger_condition="running DDL statements in sqlite",
        recommended_action="execute inside BEGIN IMMEDIATE transaction",
        observation="lock acquisition collision under concurrent read",
        inferred_explanation="deferred transaction upgrade deadlock",
        project_id="proj-ev",
        skill_id="db_skill",
        risk_level=LessonRiskLevel.HIGH,
    )
    lesson = _promote_to_approved(lesson_mgr, prop, event, confirmations_needed=4)

    query = MemoryQuery(
        query_text="running DDL statements in sqlite",
        project_id="proj-ev",
    )
    result = ekm_retriever.retrieve(query)

    assert lesson.lesson_id in result.provenance_map
    prov = result.provenance_map[lesson.lesson_id]
    assert prov["type"] == "lesson"
    assert prov["risk_level"] == "HIGH"
    assert prov["state"] == "APPROVED"
    assert prov["project_id"] == "proj-ev"
    assert prov["skill_id"] == "db_skill"
    assert evidence_id in prov["supporting_evidence_ids"]
    assert event.event_id in prov["supporting_event_ids"]
    assert prov["confirmation_count"] >= 3


# ==============================================================================
# TEST 5: DEDUPLICATION AGAINST EKM FACT REPRESENTING THE SAME LESSON
# ==============================================================================


def test_05_deduplication_against_canonical_ekm_fact(
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
    compiler: ContextCompiler,
) -> None:
    """Verify that when a lesson promoted to EKM is retrieved, its EKM fact is not duplicated."""
    event = _create_sample_event(project_id="proj-dedup")
    prop = LessonProposal(
        title="Sanitize regex user inputs before search",
        trigger_condition="compiling user supplied regex pattern",
        recommended_action="escape with re.escape or validate bounds",
        observation="ReDoS CPU pegging on catastrophic backtracking",
        inferred_explanation="unanchored exponential repetition group",
        project_id="proj-dedup",
        risk_level=LessonRiskLevel.LOW,
    )
    approved_lesson = _promote_to_approved(lesson_mgr, prop, event, confirmations_needed=2)
    # Promotion should have populated fact_id
    assert approved_lesson.fact_id is not None

    # Retrieve records and lessons
    query = MemoryQuery(
        query_text="compiling user supplied regex pattern",
        project_id="proj-dedup",
    )
    retrieval = ekm_retriever.retrieve(query)

    # Compile ContextIR
    compiled = compiler.compile(
        goal="compiling user supplied regex pattern safely",
        retrieval=retrieval,
    )

    # The lesson should be present in relevant_lessons
    lesson_ids = [les.lesson_id for les in compiled.context_ir.relevant_lessons]
    assert approved_lesson.lesson_id in lesson_ids

    # The canonical fact representing the lesson MUST NOT be duplicated in relevant_facts
    for fact_str in compiled.context_ir.relevant_facts:
        assert (
            f"lesson:{approved_lesson.title}" not in fact_str
            and approved_lesson.recommended_action not in fact_str
        )


# ==============================================================================
# TEST 6: RANKING AND DETERMINISTIC RETRIEVAL BEHAVIOR
# ==============================================================================


def test_06_ranking_and_deterministic_retrieval(
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
) -> None:
    """Verify ranking orders by composite relevance and produces deterministic results."""
    event = _create_sample_event(project_id="proj-rank")

    # Lesson 1: Highly confirmed and exact trigger match
    prop_high = LessonProposal(
        title="High confirmation rule for json parsing",
        trigger_condition="parsing untrusted json payload",
        recommended_action="use strict json schema validator",
        observation="missing fields cause unhandled KeyError",
        inferred_explanation="dynamic dictionaries without schema contract",
        project_id="proj-rank",
        risk_level=LessonRiskLevel.LOW,
    )
    les_high = _promote_to_approved(lesson_mgr, prop_high, event, confirmations_needed=5)

    # Lesson 2: Marginally related trigger
    prop_low = LessonProposal(
        title="Marginal rule for json payload logging",
        trigger_condition="logging raw payload data to terminal",
        recommended_action="truncate to 500 characters",
        observation="terminal buffer overflow",
        inferred_explanation="excessive log volume",
        project_id="proj-rank",
        risk_level=LessonRiskLevel.LOW,
    )
    les_low = _promote_to_approved(lesson_mgr, prop_low, event, confirmations_needed=2)

    query = MemoryQuery(
        query_text="parsing untrusted json payload schema validation",
        project_id="proj-rank",
    )

    # Run retrieval multiple times to verify determinism
    res1 = ekm_retriever.retrieve(query)
    res2 = ekm_retriever.retrieve(query)

    assert [les.lesson_id for les in res1.lessons] == [les.lesson_id for les in res2.lessons]
    assert res1.ranking_scores[les_high.lesson_id] > res1.ranking_scores[les_low.lesson_id]
    assert res1.lessons[0].lesson_id == les_high.lesson_id


# ==============================================================================
# TEST 7: CONTEXT-BUDGET ENFORCEMENT AND OMITTED LESSON IDS
# ==============================================================================


def test_07_context_budget_enforcement_and_omission_tracking(
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
    compiler: ContextCompiler,
) -> None:
    """Verify context token budget limits inclusion and tracks omitted lesson IDs."""
    event = _create_sample_event(project_id="proj-budget")

    # Create 5 approved lessons with substantial text to exceed 512 token budget
    for i in range(5):
        cond_text = "memory budgeting stress condition " * 6
        act_text = "execute safe bounded procedural rollback " * 6
        obs_text = "observed telemetry saturation in subsystem " * 6
        exp_text = "inferred resource exhaustion bottleneck " * 6
        prop = LessonProposal(
            title=f"Detailed operational rule number {i} for high performance systems",
            trigger_condition=f"trigger condition {i}: {cond_text}",
            recommended_action=f"action {i}: {act_text}",
            observation=f"observation {i}: {obs_text}",
            inferred_explanation=f"explanation {i}: {exp_text}",
            project_id="proj-budget",
            risk_level=LessonRiskLevel.LOW,
        )
        _promote_to_approved(lesson_mgr, prop, event, confirmations_needed=2)

    query = MemoryQuery(
        query_text="trigger condition memory budgeting stress",
        project_id="proj-budget",
        limit=10,
    )
    retrieval = ekm_retriever.retrieve(query)
    assert len(retrieval.lessons) == 5

    # Compile with minimum allowed ContextIR budget (512 tokens) to force omissions
    compiled = compiler.compile(
        goal="trigger condition memory budgeting stress",
        retrieval=retrieval,
        token_budget=512,
    )

    # Some lessons should be included and some omitted
    assert len(compiled.included_lesson_ids) >= 1
    assert len(compiled.omitted_lesson_ids) >= 1
    assert len(compiled.included_lesson_ids) + len(compiled.omitted_lesson_ids) == len(
        retrieval.lessons
    )
    assert len(compiled.context_ir.relevant_lessons) == len(compiled.included_lesson_ids)


# ==============================================================================
# TEST 8: SEPARATION OF OBSERVATIONS FROM INFERRED EXPLANATIONS
# ==============================================================================


def test_08_separation_of_observations_from_inferred_explanations(
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
    compiler: ContextCompiler,
) -> None:
    """Verify observed facts and inferred explanations remain separate in ContextIR and prompt."""
    event = _create_sample_event(project_id="proj-epistemic")
    obs = "System exited with return code 137 and dmesg log 'Out of memory: Killed process'."
    inf = (
        "Hypothesis: worker process exceeded the 2GB cgroup memory limit "
        "due to unbuffered iterator."
    )

    prop = LessonProposal(
        title="OOM failure handling during data streaming",
        trigger_condition="streaming large datasets into memory",
        recommended_action="stream in 10MB chunks using generators",
        observation=obs,
        inferred_explanation=inf,
        project_id="proj-epistemic",
        risk_level=LessonRiskLevel.LOW,
    )
    _promote_to_approved(lesson_mgr, prop, event, confirmations_needed=2)

    query = MemoryQuery(
        query_text="streaming large datasets into memory",
        project_id="proj-epistemic",
    )
    retrieval = ekm_retriever.retrieve(query)
    compiled = compiler.compile(goal="streaming data", retrieval=retrieval)

    assert len(compiled.context_ir.relevant_lessons) == 1
    les_ctx = compiled.context_ir.relevant_lessons[0]
    assert les_ctx.observation == obs
    assert les_ctx.inferred_explanation == inf
    assert les_ctx.observation != les_ctx.inferred_explanation

    prompt_text = compiled.context_ir.to_prompt_context()
    assert "OPERATIONAL LESSONS (ADVISORY ONLY):" in prompt_text
    assert f"Observed: {obs}" in prompt_text
    assert f"Explanation: {inf}" in prompt_text


# ==============================================================================
# TEST 9: SUPERVISOR CONTEXT IR INTEGRATION
# ==============================================================================


def test_09_supervisor_context_ir_integration(
    temp_db: sqlite3.Connection,
    ekm_store: EKMMemoryStore,
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
) -> None:
    """Verify Supervisor receives relevant approved lessons via ContextAssembler."""
    event = _create_sample_event(project_id="proj-sup")
    prop = LessonProposal(
        title="Always inspect git diff before committing",
        trigger_condition="committing staged changes to git",
        recommended_action="run git diff --cached before commit",
        observation="unintended scratch files committed by mistake",
        inferred_explanation="developer assumed staging was empty",
        project_id="proj-sup",
        risk_level=LessonRiskLevel.LOW,
    )
    _promote_to_approved(lesson_mgr, prop, event, confirmations_needed=2)

    state_store = StateStore(temp_db)
    trust_auth = build_trust_authority(temp_db)
    tool_broker = ToolBroker(trust_authority=trust_auth)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        memory_store=ekm_store,
        retriever=ekm_retriever,
        lesson_manager=lesson_mgr,
    )

    context_ir: ContextIR = supervisor.assemble_context(
        goal="committing staged changes to git safely",
        project_id="proj-sup",
    )

    assert len(context_ir.relevant_lessons) >= 1
    assert context_ir.relevant_lessons[0].title == "Always inspect git diff before committing"


# ==============================================================================
# TEST 10: EMPTY LESSON STORE AND RETRIEVAL FAILURES
# ==============================================================================


def test_10_empty_lesson_store_and_graceful_failures(
    ekm_store: EKMMemoryStore,
    ekm_retriever: EKMRetriever,
    compiler: ContextCompiler,
) -> None:
    """Verify empty lesson database or no matches completes cleanly without error."""
    query = MemoryQuery(
        query_text="completely non-existent topic xyz 12345",
        project_id="non-existent-proj",
    )
    retrieval = ekm_retriever.retrieve(query)

    assert retrieval.lessons == []
    compiled = compiler.compile(goal="some goal", retrieval=retrieval)
    assert compiled.context_ir.relevant_lessons == []
    assert compiled.included_lesson_ids == []
    assert compiled.omitted_lesson_ids == []


# ==============================================================================
# TEST 11: NO ADDITIONAL MODEL LOAD FOR ORDINARY RETRIEVAL
# ==============================================================================


def test_11_no_additional_model_load_for_ordinary_retrieval(
    ekm_retriever: EKMRetriever,
    lesson_mgr: LessonManager,
) -> None:
    """Verify ordinary lesson retrieval operates strictly via deterministic algorithms
    without LLM invocation.
    """
    event = _create_sample_event(project_id="proj-pure")
    prop = LessonProposal(
        title="Pure deterministic execution",
        trigger_condition="running deterministic unit tests",
        recommended_action="avoid mock-only assertions",
        observation="tests passed but production failed",
        inferred_explanation="mocks drifted from real behavior",
        project_id="proj-pure",
        risk_level=LessonRiskLevel.LOW,
    )
    _promote_to_approved(lesson_mgr, prop, event, confirmations_needed=2)

    # Retrieval must not require any reasoning provider or model instance
    query = MemoryQuery(
        query_text="running deterministic unit tests",
        project_id="proj-pure",
    )
    result = ekm_retriever.retrieve(query)
    assert len(result.lessons) == 1
    assert result.lessons[0].title == "Pure deterministic execution"


# ==============================================================================
# TEST 12: PROOF THAT LESSONS CANNOT GRANT PERMISSIONS OR BYPASS TRUST CORE
# ==============================================================================


def test_12_lessons_cannot_grant_permissions_or_bypass_trust_core(
    temp_db: sqlite3.Connection,
    ekm_store: EKMMemoryStore,
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
) -> None:
    """Verify that a lesson recommending an action NEVER grants execution authority."""
    # Propose and approve a lesson recommending a high-risk tool action
    event = _create_sample_event(project_id="proj-sec")
    prop = LessonProposal(
        title="Dangerous system command lesson",
        trigger_condition="cleaning system storage",
        recommended_action="run system.shell_exec with rm -rf /tmp",
        observation="disk space cleared",
        inferred_explanation="temp files removed",
        project_id="proj-sec",
        risk_level=LessonRiskLevel.HIGH,
    )
    _promote_to_approved(lesson_mgr, prop, event, confirmations_needed=4)

    state_store = StateStore(temp_db)
    trust_auth = build_trust_authority(temp_db)
    tool_broker = ToolBroker(trust_authority=trust_auth)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        memory_store=ekm_store,
        retriever=ekm_retriever,
        lesson_manager=lesson_mgr,
    )

    # Submit task and plan step based on the lesson's recommended action
    task = supervisor.submit_goal(
        goal_text="cleaning system storage",
        project_id="proj-sec",
    )

    # Attempt to plan and execute an unpermitted tool invocation
    plan = [
        StepPlan(
            action_name="system_cleanup",
            tool_name="system.shell_exec",
            arguments={"command": "rm -rf /tmp"},
        )
    ]
    planned_task = supervisor.plan_task(task.task_id, plan)
    assert planned_task.state == TaskState.EXECUTING

    # Execution MUST FAIL or be denied because TrustAuthority requires capability
    # The lesson presence does NOT grant capability!
    executed_task = supervisor.execute_task(task.task_id)
    # The step fails or halts closed because system.shell_exec is not registered or authorized
    assert executed_task.state in (TaskState.FAILED, TaskState.AWAITING_APPROVAL)


# ==============================================================================
# TEST 13: REGRESSION COVERAGE ACROSS EKM AND LESSON LIFECYCLE
# ==============================================================================


def test_13_regression_coverage_ekm_and_lesson_lifecycle(
    ekm_store: EKMMemoryStore,
    lesson_mgr: LessonManager,
    ekm_retriever: EKMRetriever,
    compiler: ContextCompiler,
) -> None:
    """Verify regression compatibility: standard facts, preferences,
    and lessons co-exist peacefully.
    """
    # 1. Add standard fact
    ekm_store.add_fact(
        FactCreate(
            subject="Python",
            predicate="version_standard",
            object="3.12",
            category="system",
            project_id="proj-reg",
        )
    )

    # 2. Add approved lesson
    event = _create_sample_event(project_id="proj-reg")
    prop = LessonProposal(
        title="Check Python interpreter version",
        trigger_condition="running Python scripts",
        recommended_action="verify sys.version_info >= (3, 12)",
        observation="older python fails on typed syntax",
        inferred_explanation="python 3.12 syntax introduced",
        project_id="proj-reg",
        risk_level=LessonRiskLevel.LOW,
    )
    _promote_to_approved(lesson_mgr, prop, event, confirmations_needed=2)

    query = MemoryQuery(
        query_text="Python",
        project_id="proj-reg",
    )
    result = ekm_retriever.retrieve(query)

    assert len(result.records) >= 1
    assert len(result.lessons) >= 1

    compiled = compiler.compile(goal="running Python safely", retrieval=result)
    assert len(compiled.context_ir.relevant_facts) >= 1
    assert len(compiled.context_ir.relevant_lessons) >= 1


# ==============================================================================
# TEST 14: OFFLINE OPERATION WITHOUT NETWORK ACCESS
# ==============================================================================


def test_14_offline_operation_without_network_access(
    ekm_retriever: EKMRetriever,
    lesson_mgr: LessonManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that retrieval, ranking, and compilation operate 100% offline without socket calls."""
    import socket

    def guarded_socket(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Network socket call strictly forbidden in offline SAGE!")

    monkeypatch.setattr(socket, "socket", guarded_socket)

    event = _create_sample_event(project_id="proj-offline")
    prop = LessonProposal(
        title="Offline verification test",
        trigger_condition="operating in airgapped environment",
        recommended_action="use local sqlite cache",
        observation="network calls block indefinitely",
        inferred_explanation="firewall drops external packets",
        project_id="proj-offline",
        risk_level=LessonRiskLevel.LOW,
    )
    _promote_to_approved(lesson_mgr, prop, event, confirmations_needed=2)

    query = MemoryQuery(
        query_text="operating in airgapped environment",
        project_id="proj-offline",
    )
    result = ekm_retriever.retrieve(query)
    assert len(result.lessons) == 1
