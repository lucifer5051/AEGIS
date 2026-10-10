"""SAGE EKM -- Evidence-Gated Lesson Lifecycle Subsystem.

Canonical Reference:
- docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md
- docs/specifications/AEGIS_DOMAIN_SCHEMA_FOUNDATION.md § 4

Invariants:
- "Experience is evidence, not authority."
- Lessons may inform context, never grant capabilities or override Trust Core.
- Lifecycle: ExperienceEvent -> Candidate Lesson -> Corroboration -> Approved Lesson.
- Raw observation is strictly distinguished from inferred explanation.
- Single successful execution CANNOT permanently change behavior for MEDIUM/HIGH risk.
- Contradictory evidence halts or rejects promotion.
- Outdated or refuted lessons can be invalidated or superseded.
- Zero raw secrets permitted.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.memory.models import FactCreate
from src.memory.store import EKMMemoryStore
from src.skills.experience import ExperienceEvent, ExperienceOutcomeType, UserFeedbackType
from src.storage.trust_levels import MemoryTrustLevel
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


@unique
class LessonState(str, Enum):
    """Lifecycle states of an operational execution lesson."""

    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


@unique
class LessonRiskLevel(str, Enum):
    """Sensitivity tier determining evidence thresholds required for promotion."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ExecutionLesson(BaseModel):
    """Typed, evidence-linked execution lesson."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lesson_id: str = Field(description="Unique lesson UUID.")
    title: str = Field(min_length=1, max_length=256, description="Short descriptive title.")
    trigger_condition: str = Field(
        min_length=1, description="Condition or task pattern where lesson applies."
    )
    recommended_action: str = Field(
        min_length=1, description="Recommended procedure or avoidance rule."
    )
    observation: str = Field(
        min_length=1, description="Directly observed outcome or telemetry fact."
    )
    inferred_explanation: str = Field(
        min_length=1, description="Inferred hypothesis explaining why the observation occurred."
    )
    skill_id: Optional[str] = Field(default=None, description="Associated skill ID, if applicable.")
    project_id: Optional[str] = Field(default=None, description="Project scope ID, if scoped.")
    state: LessonState = Field(
        default=LessonState.CANDIDATE, description="Current lifecycle state."
    )
    risk_level: LessonRiskLevel = Field(
        default=LessonRiskLevel.MEDIUM, description="Sensitivity classification tier."
    )
    supporting_event_ids: Tuple[str, ...] = Field(
        default_factory=tuple, description="IDs of corroborating ExperienceEvents."
    )
    contradicting_event_ids: Tuple[str, ...] = Field(
        default_factory=tuple, description="IDs of contradicting ExperienceEvents."
    )
    supporting_evidence_ids: Tuple[str, ...] = Field(
        default_factory=tuple, description="IDs of linked EKM memory_evidence records."
    )
    confirmation_count: int = Field(
        default=1, ge=0, description="Number of successful corroborating executions."
    )
    contradiction_count: int = Field(
        default=0, ge=0, description="Number of contradicting failure executions."
    )
    trust_level: MemoryTrustLevel = Field(
        default=MemoryTrustLevel.M1, description="Current provenance trust level."
    )
    fact_id: Optional[str] = Field(
        default=None, description="Linked canonical memory_facts ID when approved."
    )
    is_superseded: bool = Field(default=False, description="Whether replaced by newer lesson.")
    superseded_by: Optional[str] = Field(default=None, description="Successor lesson ID.")
    superseded_reason: Optional[str] = Field(default=None, description="Supersession reason.")
    created_at: str = Field(description="ISO 8601 UTC creation timestamp.")
    updated_at: str = Field(description="ISO 8601 UTC update timestamp.")


class LessonProposal(BaseModel):
    """Payload to propose a new candidate lesson from an execution experience."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    trigger_condition: str = Field(min_length=1)
    recommended_action: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    inferred_explanation: str = Field(min_length=1)
    skill_id: Optional[str] = Field(default=None)
    project_id: Optional[str] = Field(default=None)
    risk_level: LessonRiskLevel = Field(default=LessonRiskLevel.MEDIUM)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LessonManager:
    """Manages the evidence-gated lifecycle and EKM promotion of operational lessons."""

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        memory_store: Optional[EKMMemoryStore] = None,
    ) -> None:
        self._conn = conn
        self._memory_store = memory_store
        self._lessons: Dict[str, ExecutionLesson] = {}
        if self._conn is not None:
            self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Create auxiliary SQLite table for execution lessons."""
        if self._conn is None:
            return
        sql = """
        CREATE TABLE IF NOT EXISTS execution_lessons (
            lesson_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            trigger_condition TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            observation TEXT NOT NULL,
            inferred_explanation TEXT NOT NULL,
            skill_id TEXT NULL,
            project_id TEXT NULL,
            state TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            supporting_event_ids_json TEXT NOT NULL DEFAULT '[]',
            contradicting_event_ids_json TEXT NOT NULL DEFAULT '[]',
            supporting_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            confirmation_count INTEGER NOT NULL DEFAULT 1,
            contradiction_count INTEGER NOT NULL DEFAULT 0,
            trust_level TEXT NOT NULL DEFAULT 'M1',
            fact_id TEXT NULL,
            is_superseded INTEGER NOT NULL DEFAULT 0,
            superseded_by TEXT NULL,
            superseded_reason TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lessons_skill ON execution_lessons(skill_id);
        CREATE INDEX IF NOT EXISTS idx_lessons_project ON execution_lessons(project_id);
        CREATE INDEX IF NOT EXISTS idx_lessons_state ON execution_lessons(state);
        """
        try:
            self._conn.executescript(sql)
        except sqlite3.OperationalError as exc:
            logger.warning("Lessons table creation warning: %s", exc)

    def propose_candidate(
        self,
        proposal: LessonProposal,
        event: ExperienceEvent,
    ) -> ExecutionLesson:
        """Propose a candidate lesson from an execution experience.

        Strictly enforces zero secret leakage and starts in CANDIDATE state.
        A single execution NEVER directly creates an APPROVED lesson.
        """
        _check_no_secrets(proposal.title)
        _check_no_secrets(proposal.trigger_condition)
        _check_no_secrets(proposal.recommended_action)
        _check_no_secrets(proposal.observation)
        _check_no_secrets(proposal.inferred_explanation)

        lesson_id = str(uuid4())
        now = _utc_now_iso()

        supporting_events = (event.event_id,)
        supporting_evidence = (event.evidence_id,) if event.evidence_id else ()

        lesson = ExecutionLesson(
            lesson_id=lesson_id,
            title=proposal.title,
            trigger_condition=proposal.trigger_condition,
            recommended_action=proposal.recommended_action,
            observation=proposal.observation,
            inferred_explanation=proposal.inferred_explanation,
            skill_id=proposal.skill_id or event.skill_id,
            project_id=proposal.project_id or event.project_id,
            state=LessonState.CANDIDATE,
            risk_level=proposal.risk_level,
            supporting_event_ids=supporting_events,
            contradicting_event_ids=(),
            supporting_evidence_ids=supporting_evidence,
            confirmation_count=1,
            contradiction_count=0,
            trust_level=MemoryTrustLevel.M1,
            fact_id=None,
            is_superseded=False,
            superseded_by=None,
            superseded_reason=None,
            created_at=now,
            updated_at=now,
        )

        self._save_lesson(lesson)
        logger.info(
            "Proposed candidate lesson '%s' (id=%s, risk=%s)",
            lesson.title,
            lesson.lesson_id,
            lesson.risk_level.value,
        )
        return lesson

    def corroborate_lesson(
        self,
        lesson_id: str,
        event: ExperienceEvent,
    ) -> ExecutionLesson:
        """Update a lesson with corroborating or contradicting execution telemetry.

        Enforces risk-based evidence thresholds for promotion to APPROVED.
        """
        lesson = self.get_lesson(lesson_id)
        if not lesson:
            raise KeyError(f"Lesson not found: {lesson_id}")

        if lesson.state in (LessonState.REJECTED, LessonState.INVALIDATED, LessonState.SUPERSEDED):
            logger.warning(
                "Cannot corroborate inactive lesson '%s' in state %s",
                lesson_id,
                lesson.state.value,
            )
            return lesson

        now = _utc_now_iso()

        # Handle positive corroboration
        is_success = event.outcome_type in (
            ExperienceOutcomeType.VERIFIED_SUCCESS,
            ExperienceOutcomeType.USER_ACCEPTED,
        )
        is_user_confirmed = event.user_feedback_type in (
            UserFeedbackType.ACCEPTED,
            UserFeedbackType.EXPLICIT_POSITIVE,
        )

        if is_success:
            new_confirmations = lesson.confirmation_count + 1
            supporting_events = tuple(dict.fromkeys(lesson.supporting_event_ids + (event.event_id,)))
            supporting_evidence = lesson.supporting_evidence_ids
            if event.evidence_id and event.evidence_id not in supporting_evidence:
                supporting_evidence = supporting_evidence + (event.evidence_id,)

            # Check promotion threshold
            # LOW risk: 2 corroborations needed (propose = 1, corroboration = 2)
            # MEDIUM risk: 3 corroborations needed
            # HIGH risk: 4 corroborations OR explicit user confirmation
            promoted = False
            if lesson.risk_level == LessonRiskLevel.LOW and new_confirmations >= 2:
                promoted = True
            elif lesson.risk_level == LessonRiskLevel.MEDIUM and new_confirmations >= 3:
                promoted = True
            elif lesson.risk_level == LessonRiskLevel.HIGH:
                if is_user_confirmed or new_confirmations >= 4:
                    promoted = True

            new_state = LessonState.APPROVED if promoted else LessonState.VERIFIED
            new_trust = MemoryTrustLevel.M4 if is_user_confirmed else MemoryTrustLevel.M3

            fact_id = lesson.fact_id
            if promoted and not fact_id and self._memory_store is not None:
                # Promote to canonical EKM facts
                try:
                    fact_id = self._memory_store.add_fact(
                        FactCreate(
                            subject=lesson.skill_id or lesson.title,
                            predicate="operational_lesson",
                            object=f"{lesson.trigger_condition} -> {lesson.recommended_action}",
                            category="lesson",
                            project_id=lesson.project_id,
                            trust_level=new_trust,
                            confidence=0.9,
                            importance=0.8 if lesson.risk_level == LessonRiskLevel.HIGH else 0.6,
                            provenance_source="system",
                            evidence_id=supporting_evidence[0] if supporting_evidence else None,
                            tags=("lesson", lesson.skill_id or "general"),
                        )
                    )
                except Exception as exc:
                    logger.warning("Non-blocking canonical fact promotion failed: %s", exc)

            updated = lesson.model_copy(
                update={
                    "confirmation_count": new_confirmations,
                    "supporting_event_ids": supporting_events,
                    "supporting_evidence_ids": supporting_evidence,
                    "state": new_state,
                    "trust_level": new_trust,
                    "fact_id": fact_id,
                    "updated_at": now,
                }
            )
            self._save_lesson(updated)
            return updated

        # Handle contradiction / execution failure
        is_contradiction = event.outcome_type in (
            ExperienceOutcomeType.EXECUTION_FAILURE,
            ExperienceOutcomeType.USER_REJECTED,
        ) or event.user_feedback_type in (
            UserFeedbackType.REJECTED,
            UserFeedbackType.EXPLICIT_NEGATIVE,
        )

        if is_contradiction:
            new_contradictions = lesson.contradiction_count + 1
            contradicting_events = tuple(
                dict.fromkeys(lesson.contradicting_event_ids + (event.event_id,))
            )

            # If contradictions meet or exceed threshold (>= 2), reject or demote
            rejected = new_contradictions >= 2
            new_state = LessonState.REJECTED if rejected else lesson.state

            # If rejected and previously promoted to fact, supersede/invalidate in EKM
            if rejected and lesson.fact_id and self._memory_store is not None:
                try:
                    self._memory_store.supersede_fact(
                        lesson.fact_id,
                        successor_fact_id="",
                        reason="Rejected due to contradicting execution failures",
                    )
                except Exception as exc:
                    logger.warning("Non-blocking fact invalidation failed: %s", exc)

            updated = lesson.model_copy(
                update={
                    "contradiction_count": new_contradictions,
                    "contradicting_event_ids": contradicting_events,
                    "state": new_state,
                    "updated_at": now,
                }
            )
            self._save_lesson(updated)
            return updated

        return lesson

    def invalidate_lesson(self, lesson_id: str, reason: str) -> ExecutionLesson:
        """Explicitly invalidate a lesson (e.g., due to system upgrade or policy change)."""
        lesson = self.get_lesson(lesson_id)
        if not lesson:
            raise KeyError(f"Lesson not found: {lesson_id}")

        now = _utc_now_iso()
        if lesson.fact_id and self._memory_store is not None:
            try:
                self._memory_store.supersede_fact(
                    lesson.fact_id,
                    successor_fact_id="",
                    reason=f"Invalidated: {reason}",
                )
            except Exception as exc:
                logger.warning("Fact invalidation failed: %s", exc)

        updated = lesson.model_copy(
            update={
                "state": LessonState.INVALIDATED,
                "is_superseded": True,
                "superseded_reason": reason,
                "updated_at": now,
            }
        )
        self._save_lesson(updated)
        logger.info("Invalidated lesson '%s' (reason=%s)", lesson_id, reason)
        return updated

    def supersede_lesson(
        self,
        lesson_id: str,
        successor_lesson_id: str,
        reason: str,
    ) -> ExecutionLesson:
        """Mark a lesson as superseded by a successor lesson."""
        lesson = self.get_lesson(lesson_id)
        if not lesson:
            raise KeyError(f"Lesson not found: {lesson_id}")

        now = _utc_now_iso()
        successor = self.get_lesson(successor_lesson_id)
        successor_fact_id = successor.fact_id if successor else ""

        if lesson.fact_id and self._memory_store is not None:
            try:
                self._memory_store.supersede_fact(
                    lesson.fact_id,
                    successor_fact_id=successor_fact_id or "",
                    reason=reason,
                )
            except Exception as exc:
                logger.warning("Fact supersession failed: %s", exc)

        updated = lesson.model_copy(
            update={
                "state": LessonState.SUPERSEDED,
                "is_superseded": True,
                "superseded_by": successor_lesson_id,
                "superseded_reason": reason,
                "updated_at": now,
            }
        )
        self._save_lesson(updated)
        logger.info(
            "Superseded lesson '%s' by '%s' (reason=%s)",
            lesson_id,
            successor_lesson_id,
            reason,
        )
        return updated

    def get_lesson(self, lesson_id: str) -> Optional[ExecutionLesson]:
        """Fetch lesson by ID from cache or SQLite."""
        if self._conn is not None:
            try:
                cur = self._conn.execute(
                    "SELECT lesson_id, title, trigger_condition, recommended_action, "
                    "observation, inferred_explanation, skill_id, project_id, state, "
                    "risk_level, supporting_event_ids_json, contradicting_event_ids_json, "
                    "supporting_evidence_ids_json, confirmation_count, contradiction_count, "
                    "trust_level, fact_id, is_superseded, superseded_by, superseded_reason, "
                    "created_at, updated_at FROM execution_lessons WHERE lesson_id = ?;",
                    (lesson_id,),
                )
                row = cur.fetchone()
                if row:
                    return self._row_to_lesson(row)
            except sqlite3.OperationalError:
                pass

        return self._lessons.get(lesson_id)

    def list_lessons(
        self,
        project_id: Optional[str] = None,
        skill_id: Optional[str] = None,
        states: Optional[List[LessonState]] = None,
        include_global: bool = True,
        limit: int = 50,
    ) -> List[ExecutionLesson]:
        """List lessons with optional scope and state filters."""
        if self._conn is not None:
            try:
                clauses: List[str] = []
                params: List[Any] = []

                if project_id is not None:
                    if include_global:
                        clauses.append("(project_id = ? OR project_id IS NULL)")
                        params.append(project_id)
                    else:
                        clauses.append("project_id = ?")
                        params.append(project_id)
                elif not include_global:
                    clauses.append("project_id IS NOT NULL")

                if skill_id is not None:
                    clauses.append("skill_id = ?")
                    params.append(skill_id)

                if states is not None:
                    placeholders = ",".join("?" for _ in states)
                    clauses.append(f"state IN ({placeholders})")
                    params.extend([s.value for s in states])

                where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                sql = f"""
                SELECT lesson_id, title, trigger_condition, recommended_action,
                       observation, inferred_explanation, skill_id, project_id, state,
                       risk_level, supporting_event_ids_json, contradicting_event_ids_json,
                       supporting_evidence_ids_json, confirmation_count, contradiction_count,
                       trust_level, fact_id, is_superseded, superseded_by, superseded_reason,
                       created_at, updated_at
                FROM execution_lessons
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?;
                """
                params.append(limit)
                cur = self._conn.execute(sql, params)
                return [self._row_to_lesson(r) for r in cur.fetchall()]
            except sqlite3.OperationalError as exc:
                logger.warning("Lesson listing error: %s", exc)

        results = list(self._lessons.values())
        if project_id is not None:
            if include_global:
                results = [r for r in results if r.project_id in (project_id, None)]
            else:
                results = [r for r in results if r.project_id == project_id]
        if skill_id is not None:
            results = [r for r in results if r.skill_id == skill_id]
        if states is not None:
            results = [r for r in results if r.state in states]
        return results[:limit]

    def _save_lesson(self, lesson: ExecutionLesson) -> None:
        """Save lesson to in-memory dict and SQLite."""
        self._lessons[lesson.lesson_id] = lesson
        if self._conn is not None:
            try:
                sql = """
                INSERT OR REPLACE INTO execution_lessons (
                    lesson_id, title, trigger_condition, recommended_action,
                    observation, inferred_explanation, skill_id, project_id, state,
                    risk_level, supporting_event_ids_json, contradicting_event_ids_json,
                    supporting_evidence_ids_json, confirmation_count, contradiction_count,
                    trust_level, fact_id, is_superseded, superseded_by, superseded_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """
                self._conn.execute(
                    sql,
                    (
                        lesson.lesson_id,
                        lesson.title,
                        lesson.trigger_condition,
                        lesson.recommended_action,
                        lesson.observation,
                        lesson.inferred_explanation,
                        lesson.skill_id,
                        lesson.project_id,
                        lesson.state.value,
                        lesson.risk_level.value,
                        json.dumps(list(lesson.supporting_event_ids)),
                        json.dumps(list(lesson.contradicting_event_ids)),
                        json.dumps(list(lesson.supporting_evidence_ids)),
                        lesson.confirmation_count,
                        lesson.contradiction_count,
                        lesson.trust_level.value,
                        lesson.fact_id,
                        1 if lesson.is_superseded else 0,
                        lesson.superseded_by,
                        lesson.superseded_reason,
                        lesson.created_at,
                        lesson.updated_at,
                    ),
                )
            except sqlite3.OperationalError as exc:
                logger.warning("Durable lesson persistence error: %s", exc)

    def _row_to_lesson(self, row: Tuple[Any, ...]) -> ExecutionLesson:
        """Convert SQLite row tuple to ExecutionLesson model."""
        return ExecutionLesson(
            lesson_id=row[0],
            title=row[1],
            trigger_condition=row[2],
            recommended_action=row[3],
            observation=row[4],
            inferred_explanation=row[5],
            skill_id=row[6],
            project_id=row[7],
            state=LessonState(row[8]),
            risk_level=LessonRiskLevel(row[9]),
            supporting_event_ids=tuple(json.loads(row[10])),
            contradicting_event_ids=tuple(json.loads(row[11])),
            supporting_evidence_ids=tuple(json.loads(row[12])),
            confirmation_count=int(row[13]),
            contradiction_count=int(row[14]),
            trust_level=MemoryTrustLevel(row[15]),
            fact_id=row[16],
            is_superseded=bool(row[17]),
            superseded_by=row[18],
            superseded_reason=row[19],
            created_at=row[20],
            updated_at=row[21],
        )
