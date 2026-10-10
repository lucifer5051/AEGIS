"""SAGE Skills Subsystem -- Adaptive Execution & Experience Foundation.

Establishes the deterministic telemetry contract for the Experience &
Epistemic Knowledge Model (EKM) system.

Architectural Principles:
- "Experience is evidence, not authority."
- USER_CORRECTED does NOT automatically mean task failure.
- Experience events never grant capabilities or alter Trust Core policies.
- Feedback collection is strictly decoupled from the critical execution path.
- No automatic self-modification, no weight updates, no permanent behavior creation.
- Zero raw secrets permitted in events or feedback.
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

from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


@unique
class ExperienceOutcomeType(str, Enum):
    """Categorisation of execution outcome for experience logging."""

    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    USER_ACCEPTED = "USER_ACCEPTED"
    USER_CORRECTED = "USER_CORRECTED"
    USER_REJECTED = "USER_REJECTED"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    NO_FEEDBACK = "NO_FEEDBACK"


@unique
class UserFeedbackType(str, Enum):
    """Explicit or inferred user feedback signal."""

    ACCEPTED = "ACCEPTED"
    CORRECTED = "CORRECTED"
    REJECTED = "REJECTED"
    EXPLICIT_POSITIVE = "EXPLICIT_POSITIVE"
    EXPLICIT_NEGATIVE = "EXPLICIT_NEGATIVE"
    NO_FEEDBACK = "NO_FEEDBACK"


class ExperienceEvent(BaseModel):
    """Structured telemetry record of a skill execution experience.

    Immutable data contract passed asynchronously to future learning engines.
    Never contains raw credentials or unredacted secrets.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique event UUID.",
    )
    task_id: str = Field(description="Associated supervisor task ID.")
    invocation_id: str = Field(description="Corresponding skill invocation ID.")
    skill_id: str = Field(description="Executed skill identifier.")
    intent: str = Field(description="Routing intent or goal description.")
    relevant_context_reference: Optional[str] = Field(
        default=None,
        description="Reference to ContextIR or memory item.",
    )
    selected_tools: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Tools bound and utilized during execution.",
    )
    execution_path: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Sequential list of executed steps or actions.",
    )
    verification_result: str = Field(
        default="UNVERIFIED",
        description="Outcome of verifier evaluation.",
    )
    outcome_type: ExperienceOutcomeType = Field(
        default=ExperienceOutcomeType.NO_FEEDBACK,
        description="High-level execution outcome classification.",
    )
    user_feedback_type: UserFeedbackType = Field(
        default=UserFeedbackType.NO_FEEDBACK,
        description="User feedback signal received.",
    )
    latency_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Total execution latency in milliseconds.",
    )
    error_or_recovery_info: Optional[str] = Field(
        default=None,
        description="Sanitized error or recovery hint if failed.",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="Project scope identifier, if project-scoped.",
    )
    evidence_id: Optional[str] = Field(
        default=None,
        description="Linked memory_evidence ID, if persisted in EKM.",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp.",
    )
    provenance: str = Field(
        default="sage.skills.executor",
        description="Subsystem generating this event.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score of execution or routing.",
    )


class ExperienceFeedbackService:
    """Decoupled, non-blocking experience recording interface with optional SQLite backing.

    Returns immediately; designed for background or asynchronous consumption.
    Safe against secret leaks and execution path errors.
    """

    DEFAULT_RETENTION_LIMIT = 10000

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        memory_store: Optional[Any] = None,
        max_retention: int = DEFAULT_RETENTION_LIMIT,
    ) -> None:
        self._conn = conn
        self._memory_store = memory_store
        self._max_retention = max_retention
        self._events: List[ExperienceEvent] = []
        self._feedback_records: Dict[str, Dict[str, Any]] = {}
        if self._conn is not None:
            self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Create auxiliary SQLite tables for durable experience and feedback storage."""
        if self._conn is None:
            return
        sql = """
        CREATE TABLE IF NOT EXISTS experience_events (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            project_id TEXT NULL,
            intent TEXT NOT NULL,
            selected_tools_json TEXT NOT NULL DEFAULT '[]',
            execution_path_json TEXT NOT NULL DEFAULT '[]',
            verification_result TEXT NOT NULL,
            outcome_type TEXT NOT NULL,
            user_feedback_type TEXT NOT NULL,
            latency_ms REAL NOT NULL DEFAULT 0.0,
            error_or_recovery_info TEXT NULL,
            evidence_id TEXT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            provenance TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_exp_events_task ON experience_events(task_id);
        CREATE INDEX IF NOT EXISTS idx_exp_events_skill ON experience_events(skill_id);
        CREATE INDEX IF NOT EXISTS idx_exp_events_project ON experience_events(project_id);
        CREATE INDEX IF NOT EXISTS idx_exp_events_created ON experience_events(created_at);

        CREATE TABLE IF NOT EXISTS experience_feedback (
            feedback_id TEXT PRIMARY KEY,
            invocation_id TEXT NOT NULL,
            feedback_type TEXT NOT NULL,
            correction_text TEXT NULL,
            notes TEXT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_exp_feedback_inv ON experience_feedback(invocation_id);
        """
        try:
            self._conn.executescript(sql)
        except sqlite3.OperationalError as exc:
            logger.warning("Experience tables creation warning: %s", exc)

    def record_event(self, event: ExperienceEvent) -> None:
        """Record an experience event asynchronously with secret leak detection."""
        # 1. Enforce zero secret leakage
        _check_no_secrets(event.intent)
        if event.error_or_recovery_info:
            _check_no_secrets(event.error_or_recovery_info)
        if event.relevant_context_reference:
            _check_no_secrets(event.relevant_context_reference)

        persisted_event = event

        # 2. Link into EKM memory_evidence if memory_store is available and not yet linked
        if self._memory_store is not None and not persisted_event.evidence_id:
            try:
                from src.memory.models import EvidenceCreate

                evidence_id = self._memory_store.add_evidence(
                    EvidenceCreate(
                        evidence_type="system_observation",
                        source_reference=(
                            f"skill_execution:{persisted_event.skill_id}:"
                            f"{persisted_event.invocation_id}"
                        ),
                        raw_content_snippet=(
                            f"Skill {persisted_event.skill_id} executed with outcome "
                            f"{persisted_event.outcome_type.value}. Latency: "
                            f"{persisted_event.latency_ms:.1f}ms."
                        ),
                        project_id=persisted_event.project_id,
                        trust_score=persisted_event.confidence,
                    )
                )
                persisted_event = persisted_event.model_copy(update={"evidence_id": evidence_id})
            except Exception as exc:
                logger.warning("Non-blocking evidence creation failed: %s", exc)

        # 3. Persist to SQLite if connection present
        if self._conn is not None:
            try:
                sql = """
                INSERT OR REPLACE INTO experience_events (
                    event_id, task_id, invocation_id, skill_id, project_id, intent,
                    selected_tools_json, execution_path_json, verification_result,
                    outcome_type, user_feedback_type, latency_ms, error_or_recovery_info,
                    evidence_id, confidence, provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """
                self._conn.execute(
                    sql,
                    (
                        persisted_event.event_id,
                        persisted_event.task_id,
                        persisted_event.invocation_id,
                        persisted_event.skill_id,
                        persisted_event.project_id,
                        persisted_event.intent,
                        json.dumps(list(persisted_event.selected_tools)),
                        json.dumps(list(persisted_event.execution_path)),
                        persisted_event.verification_result,
                        persisted_event.outcome_type.value,
                        persisted_event.user_feedback_type.value,
                        persisted_event.latency_ms,
                        persisted_event.error_or_recovery_info,
                        persisted_event.evidence_id,
                        persisted_event.confidence,
                        persisted_event.provenance,
                        persisted_event.timestamp,
                    ),
                )
                self._prune_retention()
            except sqlite3.OperationalError as exc:
                logger.warning("Durable event persistence error: %s", exc)

        # 4. In-memory append
        self._events.append(persisted_event)
        logger.debug(
            "Recorded ExperienceEvent for skill '%s' (invocation=%s, outcome=%s)",
            persisted_event.skill_id,
            persisted_event.invocation_id,
            persisted_event.outcome_type.value,
        )

    def _prune_retention(self) -> None:
        """Enforce bounded retention by removing excess historical events."""
        if self._conn is None:
            return
        try:
            count_cur = self._conn.execute("SELECT COUNT(*) FROM experience_events;")
            total = count_cur.fetchone()[0]
            if total > self._max_retention:
                excess = total - self._max_retention
                self._conn.execute(
                    "DELETE FROM experience_events WHERE event_id IN ("
                    "SELECT event_id FROM experience_events ORDER BY created_at ASC LIMIT ?"
                    ");",
                    (excess,),
                )
        except sqlite3.OperationalError:
            pass

    def record_feedback(
        self,
        invocation_id: str,
        feedback: UserFeedbackType,
        correction_text: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Record explicit or implicit user feedback for an invocation."""
        if correction_text:
            _check_no_secrets(correction_text)
        if notes:
            _check_no_secrets(notes)

        now = datetime.now(timezone.utc).isoformat()
        record = {
            "invocation_id": invocation_id,
            "feedback": feedback,
            "correction_text": correction_text,
            "notes": notes,
            "recorded_at": now,
        }
        self._feedback_records[invocation_id] = record

        if self._conn is not None:
            try:
                feedback_id = str(uuid4())
                sql = """
                INSERT INTO experience_feedback (
                    feedback_id, invocation_id, feedback_type, correction_text, notes, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?);
                """
                self._conn.execute(
                    sql,
                    (
                        feedback_id,
                        invocation_id,
                        feedback.value,
                        correction_text,
                        notes,
                        now,
                    ),
                )
            except sqlite3.OperationalError as exc:
                logger.warning("Durable feedback persistence error: %s", exc)

        logger.info(
            "Recorded feedback '%s' for invocation '%s' (notes=%s)",
            feedback.value,
            invocation_id,
            notes,
        )

    def list_events(
        self,
        skill_id: Optional[str] = None,
        project_id: Optional[str] = None,
        outcome_type: Optional[ExperienceOutcomeType] = None,
        limit: int = 50,
    ) -> List[ExperienceEvent]:
        """Query recorded experience events with scope and outcome filtering."""
        if self._conn is not None:
            try:
                clauses: List[str] = []
                params: List[Any] = []
                if skill_id is not None:
                    clauses.append("skill_id = ?")
                    params.append(skill_id)
                if project_id is not None:
                    clauses.append("project_id = ?")
                    params.append(project_id)
                if outcome_type is not None:
                    clauses.append("outcome_type = ?")
                    params.append(outcome_type.value)

                where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                sql = f"""
                SELECT event_id, task_id, invocation_id, skill_id, project_id, intent,
                       selected_tools_json, execution_path_json, verification_result,
                       outcome_type, user_feedback_type, latency_ms, error_or_recovery_info,
                       evidence_id, confidence, provenance, created_at
                FROM experience_events
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ?;
                """
                params.append(limit)
                cur = self._conn.execute(sql, params)
                rows = cur.fetchall()
                persisted_events: List[ExperienceEvent] = []
                for r in reversed(rows):
                    persisted_events.append(
                        ExperienceEvent(
                            event_id=r[0],
                            task_id=r[1],
                            invocation_id=r[2],
                            skill_id=r[3],
                            project_id=r[4],
                            intent=r[5],
                            selected_tools=tuple(json.loads(r[6])),
                            execution_path=tuple(json.loads(r[7])),
                            verification_result=r[8],
                            outcome_type=ExperienceOutcomeType(r[9]),
                            user_feedback_type=UserFeedbackType(r[10]),
                            latency_ms=float(r[11]),
                            error_or_recovery_info=r[12],
                            evidence_id=r[13],
                            confidence=float(r[14]),
                            provenance=r[15],
                            timestamp=r[16],
                        )
                    )
                return persisted_events
            except sqlite3.OperationalError as exc:
                logger.warning("Durable event retrieval error: %s", exc)

        # Fallback to in-memory list
        events = self._events
        if skill_id is not None:
            events = [e for e in events if e.skill_id == skill_id]
        if project_id is not None:
            events = [e for e in events if e.project_id == project_id]
        if outcome_type is not None:
            events = [e for e in events if e.outcome_type == outcome_type]
        return events[-limit:]

    def get_feedback(self, invocation_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve feedback associated with a specific invocation ID."""
        if self._conn is not None:
            try:
                cur = self._conn.execute(
                    "SELECT invocation_id, feedback_type, correction_text, notes, recorded_at "
                    "FROM experience_feedback WHERE invocation_id = ? "
                    "ORDER BY recorded_at DESC LIMIT 1;",
                    (invocation_id,),
                )
                row = cur.fetchone()
                if row:
                    return {
                        "invocation_id": row[0],
                        "feedback": UserFeedbackType(row[1]),
                        "correction_text": row[2],
                        "notes": row[3],
                        "recorded_at": row[4],
                    }
            except sqlite3.OperationalError as exc:
                logger.warning("Durable feedback query error: %s", exc)

        return self._feedback_records.get(invocation_id)
