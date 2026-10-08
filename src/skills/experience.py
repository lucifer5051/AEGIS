"""SAGE Skills Subsystem -- Adaptive Execution & Experience Foundation.

Establishes the deterministic telemetry contract for the future Experience &
Epistemic Knowledge Model (EKM) system.

Architectural Principles:
- "Experience is evidence, not authority."
- USER_CORRECTED does NOT automatically mean task failure.
- Experience events never grant capabilities or alter Trust Core policies.
- Feedback collection is strictly decoupled from the critical execution path.
- No automatic self-modification, no weight updates, no permanent behavior creation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

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
    """Decoupled, non-blocking experience recording interface.

    Returns immediately; designed for background or asynchronous consumption.
    """

    def __init__(self) -> None:
        self._events: List[ExperienceEvent] = []
        self._feedback_records: Dict[str, Dict[str, Any]] = {}

    def record_event(self, event: ExperienceEvent) -> None:
        """Record an experience event asynchronously."""
        self._events.append(event)
        logger.debug(
            "Recorded ExperienceEvent for skill '%s' (invocation=%s, outcome=%s)",
            event.skill_id,
            event.invocation_id,
            event.outcome_type.value,
        )

    def record_feedback(
        self,
        invocation_id: str,
        feedback: UserFeedbackType,
        correction_text: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Record explicit or implicit user feedback for an invocation."""
        record = {
            "invocation_id": invocation_id,
            "feedback": feedback,
            "correction_text": correction_text,
            "notes": notes,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        self._feedback_records[invocation_id] = record
        logger.info(
            "Recorded feedback '%s' for invocation '%s' (notes=%s)",
            feedback.value,
            invocation_id,
            notes,
        )

    def list_events(
        self,
        skill_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[ExperienceEvent]:
        """Query recorded experience events."""
        events = self._events
        if skill_id is not None:
            events = [e for e in events if e.skill_id == skill_id]
        return events[-limit:]

    def get_feedback(self, invocation_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve feedback associated with a specific invocation ID."""
        return self._feedback_records.get(invocation_id)
