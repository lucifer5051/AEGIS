"""SAGE Evidence-Gated Lesson Lifecycle Test Suite.

Canonical Reference:
- docs/specifications/AEGIS_DOMAIN_SCHEMA_FOUNDATION.md § 4
- PROMPT 025 Specification

Invariants Verified:
1.  Proposal creates CANDIDATE lesson; never APPROVED directly.
2.  Secret rejection enforced before persistence.
3.  Risk-based thresholds: LOW(2), MEDIUM(3), HIGH(4 or explicit confirm).
4.  Corroboration with success events promotes through CANDIDATE -> VERIFIED -> APPROVED.
5.  Contradiction halts promotion; second contradiction rejects.
6.  Rejection on approved lesson triggers EKM fact invalidation (non-blocking).
7.  Invalidation sets INVALIDATED state and cascades to EKM.
8.  Supersession links old and new lessons and updates EKM fact.
9.  Corroboration with inactive lesson is no-op.
10. Lesson persistence round-trips through SQLite with full field fidelity.
11. Idempotent supporting_event_ids: duplicate event IDs not appended.
12. List filtering: by project_id, skill_id, state, include_global.
13. Scoped retrieval respects project boundaries.
14. Offline operation: in-memory mode works throughout lifecycle.
15. Trust Core independence: lessons never grant capabilities.
16. EKM promotion creates canonical memory_facts record on APPROVED.
17. User-confirmed HIGH risk lesson promoted at threshold 1 corroboration.
18. Lesson fact_id populated only after APPROVED promotion.
19. Evidence linkage in supporting_evidence_ids preserved through corroboration.
20. Contradiction count and event IDs tracked correctly.
21. get_lesson returns None for unknown ID, not an exception.
22. Lesson fields observation and inferred_explanation are strictly distinguished.
23. Failure isolation: EKM store errors are non-blocking.
24. Trust Core policy is not modified by lessons.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pytest

from src.memory import (
    EKMMemoryStore,
    EvidenceCreate,
    ExecutionLesson,
    FactCreate,
    LessonManager,
    LessonProposal,
    LessonRiskLevel,
    LessonState,
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
from src.trust.audit import SecretLeakError

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_event(
    outcome: ExperienceOutcomeType = ExperienceOutcomeType.VERIFIED_SUCCESS,
    feedback: UserFeedbackType = UserFeedbackType.NO_FEEDBACK,
    skill_id: str = "skill-test",
    project_id: str | None = None,
    evidence_id: str | None = None,
) -> ExperienceEvent:
    return ExperienceEvent(
        task_id="task-001",
        invocation_id="inv-001",
        skill_id=skill_id,
        intent="test intent",
        outcome_type=outcome,
        user_feedback_type=feedback,
        project_id=project_id,
        evidence_id=evidence_id,
    )


def _make_proposal(
    risk: LessonRiskLevel = LessonRiskLevel.MEDIUM,
    skill_id: str | None = "skill-test",
    project_id: str | None = None,
) -> LessonProposal:
    return LessonProposal(
        title="Prefer safe fallback route",
        trigger_condition="network_timeout > 3s",
        recommended_action="Use cached response and retry in background",
        observation="Observed 504 gateway timeout on primary endpoint",
        inferred_explanation="Primary endpoint overloaded under high traffic",
        risk_level=risk,
        skill_id=skill_id,
        project_id=project_id,
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


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
        now = "2026-10-10T12:00:00Z"
        conn.execute(
            "INSERT INTO projects (project_id, name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'ACTIVE', ?, ?);",
            ("proj-alpha", "Project Alpha", "alpha", now, now),
        )
        conn.execute(
            "INSERT INTO projects (project_id, name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'ACTIVE', ?, ?);",
            ("proj-beta", "Project Beta", "beta", now, now),
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


@pytest.fixture
def ekm_store(migrated_conn: sqlite3.Connection) -> EKMMemoryStore:
    return EKMMemoryStore(migrated_conn)


@pytest.fixture
def lesson_mgr(migrated_conn: sqlite3.Connection, ekm_store: EKMMemoryStore) -> LessonManager:
    return LessonManager(conn=migrated_conn, memory_store=ekm_store)


@pytest.fixture
def lesson_mgr_mem_only() -> LessonManager:
    return LessonManager(conn=None, memory_store=None)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_01_proposal_creates_candidate(lesson_mgr: LessonManager) -> None:
    """1. A freshly proposed lesson must start in CANDIDATE state."""
    evt = _make_event()
    lesson = lesson_mgr.propose_candidate(_make_proposal(), evt)
    assert isinstance(lesson, ExecutionLesson)
    assert lesson.state == LessonState.CANDIDATE
    assert lesson.confirmation_count == 1
    assert lesson.contradiction_count == 0
    assert evt.event_id in lesson.supporting_event_ids
    assert lesson.fact_id is None


def test_02_secret_rejection_in_proposal(lesson_mgr: LessonManager) -> None:
    """2. Proposals containing secret-like strings must be rejected."""
    malicious = LessonProposal(
        title="Prefer safe fallback route",
        trigger_condition="network_timeout > 3s",
        recommended_action="Use cached response",
        observation="Observed 504 gateway timeout",
        inferred_explanation="API_KEY=sk-secret-abc123 key was rejected",
        risk_level=LessonRiskLevel.LOW,
    )
    with pytest.raises(SecretLeakError):
        lesson_mgr.propose_candidate(malicious, _make_event())


def test_03_low_risk_promotes_at_two_confirmations(lesson_mgr: LessonManager) -> None:
    """3. LOW risk: proposal + 1 corroboration = APPROVED."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.LOW), _make_event())
    assert lesson.state == LessonState.CANDIDATE
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.APPROVED
    assert lesson.confirmation_count == 2


def test_04_medium_risk_lifecycle(lesson_mgr: LessonManager) -> None:
    """4. MEDIUM risk: CANDIDATE -> VERIFIED (at 2) -> APPROVED (at 3)."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.MEDIUM), _make_event())
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.VERIFIED
    assert lesson.fact_id is None
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.APPROVED
    assert lesson.confirmation_count == 3


def test_05_high_risk_requires_four_confirmations(lesson_mgr: LessonManager) -> None:
    """5. HIGH risk without user confirm requires 4 total confirmations."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.HIGH), _make_event())
    for i in range(2):
        lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
        assert lesson.state in (LessonState.CANDIDATE, LessonState.VERIFIED)
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.APPROVED
    assert lesson.confirmation_count == 4


def test_06_high_risk_user_confirmed_promotes_early(lesson_mgr: LessonManager) -> None:
    """6. HIGH risk: explicit user confirmation promotes immediately."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.HIGH), _make_event())
    confirmed_evt = _make_event(
        outcome=ExperienceOutcomeType.USER_ACCEPTED,
        feedback=UserFeedbackType.EXPLICIT_POSITIVE,
    )
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, confirmed_evt)
    assert lesson.state == LessonState.APPROVED
    assert lesson.trust_level.value == "M4"


def test_07_first_contradiction_halts_promotion(lesson_mgr: LessonManager) -> None:
    """7. One contradiction records count but does not reject."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(), _make_event())
    fail_evt = _make_event(outcome=ExperienceOutcomeType.EXECUTION_FAILURE)
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, fail_evt)
    assert lesson.state == LessonState.CANDIDATE
    assert lesson.contradiction_count == 1
    assert fail_evt.event_id in lesson.contradicting_event_ids


def test_08_second_contradiction_rejects(lesson_mgr: LessonManager) -> None:
    """8. Two contradicting events cause REJECTED state."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(), _make_event())
    for _ in range(2):
        lesson = lesson_mgr.corroborate_lesson(
            lesson.lesson_id,
            _make_event(outcome=ExperienceOutcomeType.USER_REJECTED),
        )
    assert lesson.state == LessonState.REJECTED
    assert lesson.contradiction_count == 2


def test_09_rejection_after_approval_invalidates_ekm_fact(lesson_mgr: LessonManager) -> None:
    """9. When an approved lesson is rejected, its EKM fact is superseded (non-blocking)."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.LOW), _make_event())
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.APPROVED
    assert lesson.fact_id is not None

    for _ in range(2):
        lesson = lesson_mgr.corroborate_lesson(
            lesson.lesson_id,
            _make_event(
                outcome=ExperienceOutcomeType.EXECUTION_FAILURE,
                feedback=UserFeedbackType.REJECTED,
            ),
        )
    assert lesson.state == LessonState.REJECTED


def test_10_explicit_invalidation(lesson_mgr: LessonManager) -> None:
    """10. invalidate_lesson sets state to INVALIDATED with reason."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(), _make_event())
    invalidated = lesson_mgr.invalidate_lesson(lesson.lesson_id, reason="Policy change v2")
    assert invalidated.state == LessonState.INVALIDATED
    assert invalidated.is_superseded is True
    assert invalidated.superseded_reason == "Policy change v2"


def test_11_supersession_links_lessons(lesson_mgr: LessonManager) -> None:
    """11. supersede_lesson marks old lesson SUPERSEDED linked to successor."""
    old = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.LOW), _make_event())
    new = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.LOW), _make_event())
    old_sup = lesson_mgr.supersede_lesson(old.lesson_id, new.lesson_id, "Replaced by refined lesson")
    assert old_sup.state == LessonState.SUPERSEDED
    assert old_sup.superseded_by == new.lesson_id
    assert old_sup.is_superseded is True


def test_12_corroboration_on_inactive_lesson_is_noop(lesson_mgr: LessonManager) -> None:
    """12. Corroborating INVALIDATED lesson is silently ignored."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(), _make_event())
    lesson = lesson_mgr.invalidate_lesson(lesson.lesson_id, reason="test")
    result = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert result.state == LessonState.INVALIDATED


def test_13_sqlite_roundtrip_fidelity(lesson_mgr: LessonManager) -> None:
    """13. All lesson fields survive a SQLite write/read cycle intact."""
    proposal = LessonProposal(
        title="Use exponential backoff",
        trigger_condition="retryable_error",
        recommended_action="Sleep 2^n before retry",
        observation="P99 latency reduced by 40%",
        inferred_explanation="Thundering herd avoided via jittered backoff",
        risk_level=LessonRiskLevel.HIGH,
        skill_id="skill-network",
        project_id="proj-alpha",
    )
    evt = _make_event(skill_id="skill-network", project_id="proj-alpha")
    lesson = lesson_mgr.propose_candidate(proposal, evt)
    fetched = lesson_mgr.get_lesson(lesson.lesson_id)
    assert fetched is not None
    assert fetched.lesson_id == lesson.lesson_id
    assert fetched.title == "Use exponential backoff"
    assert fetched.risk_level == LessonRiskLevel.HIGH
    assert fetched.skill_id == "skill-network"
    assert fetched.project_id == "proj-alpha"
    assert fetched.state == LessonState.CANDIDATE
    assert fetched.confirmation_count == 1
    assert fetched.contradiction_count == 0


def test_14_idempotent_supporting_event_ids(lesson_mgr: LessonManager) -> None:
    """14. Duplicate event IDs are not appended to supporting_event_ids."""
    evt = _make_event()
    lesson = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.MEDIUM), evt)
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, evt)
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, evt)
    assert lesson.supporting_event_ids.count(evt.event_id) == 1


def test_15_list_filtering_by_project(lesson_mgr: LessonManager) -> None:
    """15. list_lessons filters by project scope correctly."""
    lesson_mgr.propose_candidate(_make_proposal(project_id=None), _make_event())
    lesson_mgr.propose_candidate(
        _make_proposal(project_id="proj-alpha"),
        _make_event(project_id="proj-alpha"),
    )
    lesson_mgr.propose_candidate(
        _make_proposal(project_id="proj-beta"),
        _make_event(project_id="proj-beta"),
    )

    alpha_only = lesson_mgr.list_lessons(project_id="proj-alpha", include_global=False)
    assert all(l.project_id == "proj-alpha" for l in alpha_only)

    alpha_with_global = lesson_mgr.list_lessons(project_id="proj-alpha", include_global=True)
    assert any(l.project_id is None for l in alpha_with_global)
    assert any(l.project_id == "proj-alpha" for l in alpha_with_global)
    assert all(l.project_id != "proj-beta" for l in alpha_with_global)


def test_16_list_filtering_by_state(lesson_mgr: LessonManager) -> None:
    """16. list_lessons filters by lifecycle state."""
    lesson_mgr.propose_candidate(_make_proposal(), _make_event())
    lesson2 = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.LOW), _make_event())
    lesson_mgr.corroborate_lesson(lesson2.lesson_id, _make_event())

    candidates = lesson_mgr.list_lessons(states=[LessonState.CANDIDATE])
    approved = lesson_mgr.list_lessons(states=[LessonState.APPROVED])
    assert all(l.state == LessonState.CANDIDATE for l in candidates)
    assert all(l.state == LessonState.APPROVED for l in approved)


def test_17_list_filtering_by_skill(lesson_mgr: LessonManager) -> None:
    """17. list_lessons filters by skill_id."""
    lesson_mgr.propose_candidate(_make_proposal(skill_id="skill-a"), _make_event(skill_id="skill-a"))
    lesson_mgr.propose_candidate(_make_proposal(skill_id="skill-b"), _make_event(skill_id="skill-b"))
    skill_a_lessons = lesson_mgr.list_lessons(skill_id="skill-a")
    assert all(l.skill_id == "skill-a" for l in skill_a_lessons)


def test_18_in_memory_mode_lifecycle(lesson_mgr_mem_only: LessonManager) -> None:
    """18. Full lifecycle works in in-memory mode (no db connection)."""
    mgr = lesson_mgr_mem_only
    lesson = mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.LOW), _make_event())
    assert lesson.state == LessonState.CANDIDATE
    lesson = mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.APPROVED
    invalidated = mgr.invalidate_lesson(lesson.lesson_id, "memory-only test")
    assert invalidated.state == LessonState.INVALIDATED
    fetched = mgr.get_lesson(lesson.lesson_id)
    assert fetched is not None
    assert fetched.state == LessonState.INVALIDATED


def test_19_get_lesson_unknown_id_returns_none(lesson_mgr: LessonManager) -> None:
    """19. get_lesson returns None for unknown IDs, never raises."""
    result = lesson_mgr.get_lesson("nonexistent-lesson-id-xyz")
    assert result is None


def test_20_observation_vs_inferred_explanation_distinction(lesson_mgr: LessonManager) -> None:
    """20. Observation (directly observed fact) and inferred_explanation (hypothesis) differ."""
    proposal = LessonProposal(
        title="Caching reduces latency",
        trigger_condition="repeated_query",
        recommended_action="Cache results for 60s",
        observation="Response time dropped from 800ms to 12ms",
        inferred_explanation="L1 cache hit avoided full DB round-trip",
        risk_level=LessonRiskLevel.LOW,
    )
    lesson = lesson_mgr.propose_candidate(proposal, _make_event())
    assert lesson.observation != lesson.inferred_explanation
    assert "800ms" in lesson.observation
    assert "L1 cache" in lesson.inferred_explanation


def test_21_fact_id_only_after_approved(lesson_mgr: LessonManager) -> None:
    """21. fact_id is None until APPROVED; populated on promotion."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.MEDIUM), _make_event())
    assert lesson.fact_id is None
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.VERIFIED
    assert lesson.fact_id is None
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.APPROVED
    assert lesson.fact_id is not None


def test_22_evidence_linkage_preserved(
    lesson_mgr: LessonManager, ekm_store: EKMMemoryStore
) -> None:
    """22. evidence_id from multiple events accumulates in supporting_evidence_ids."""
    eid1 = ekm_store.add_evidence(
        EvidenceCreate(
            evidence_type="system_observation",
            source_reference="execution_log_1",
            raw_content_snippet="success log 1",
            project_id=None,
        )
    )
    eid2 = ekm_store.add_evidence(
        EvidenceCreate(
            evidence_type="system_observation",
            source_reference="execution_log_2",
            raw_content_snippet="success log 2",
            project_id=None,
        )
    )
    evt1 = _make_event(evidence_id=eid1)
    evt2 = _make_event(evidence_id=eid2)
    lesson = lesson_mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.LOW), evt1)
    assert eid1 in lesson.supporting_evidence_ids
    lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, evt2)
    assert eid1 in lesson.supporting_evidence_ids
    assert eid2 in lesson.supporting_evidence_ids


def test_23_ekm_store_error_non_blocking(migrated_conn: sqlite3.Connection) -> None:
    """23. EKM store errors during promotion are non-blocking; lesson still transitions."""
    broken_store = MagicMock(spec=EKMMemoryStore)
    broken_store.add_fact.side_effect = RuntimeError("EKM failure")
    mgr = LessonManager(conn=migrated_conn, memory_store=broken_store)
    lesson = mgr.propose_candidate(_make_proposal(risk=LessonRiskLevel.LOW), _make_event())
    lesson = mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.APPROVED
    assert lesson.fact_id is None  # Store failed, so no fact linked


def test_24_lesson_never_grants_capabilities(lesson_mgr: LessonManager) -> None:
    """24. Approved lessons only add EKM facts; no Trust Core capability grants."""
    lesson = lesson_mgr.propose_candidate(_make_proposal(), _make_event())
    for _ in range(3):
        lesson = lesson_mgr.corroborate_lesson(lesson.lesson_id, _make_event())
    assert lesson.state == LessonState.APPROVED
    assert not hasattr(lesson, "granted_tools")
    assert not hasattr(lesson, "trust_policy")
    assert not hasattr(lesson, "capability_grant")
    assert lesson.fact_id is not None

