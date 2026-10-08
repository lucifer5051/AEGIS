"""SAGE ContextIR -- Context Intermediate Representation.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md

Invariants:
1. Strict frozen Pydantic model with extra='forbid'.
2. Minimal, bounded context representation for local reasoning & planning.
3. No arbitrary database dumping: strictly relevant preferences, facts, and events.
4. Deterministic formatting for reasoning provider consumption.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ContextIR(BaseModel):
    """Context Intermediate Representation for planning and reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    active_goal: str = Field(description="Primary user goal to plan and execute.")
    project_id: Optional[str] = Field(
        default=None, description="Active project context scope, if any."
    )
    task_id: Optional[str] = Field(
        default=None, description="Durable task identifier, if already created."
    )
    relevant_preferences: List[str] = Field(
        default_factory=list, description="User preferences relevant to the goal."
    )
    relevant_facts: List[str] = Field(
        default_factory=list, description="Epistemic facts relevant to the goal."
    )
    recent_events: List[str] = Field(
        default_factory=list, description="Recent execution milestones or events."
    )
    constraints: List[str] = Field(
        default_factory=list, description="Hard operational boundaries or safety constraints."
    )
    available_token_budget: int = Field(
        default=4096, ge=512, le=32768, description="Allocated context budget."
    )

    def to_prompt_context(self) -> str:
        """Render bounded, human-readable structured context for LLM reasoning."""
        lines = [f"GOAL: {self.active_goal}"]
        if self.project_id:
            lines.append(f"PROJECT: {self.project_id}")
        if self.relevant_preferences:
            lines.append("USER PREFERENCES:")
            for p in self.relevant_preferences[:5]:
                lines.append(f" - {p}")
        if self.relevant_facts:
            lines.append("KNOWN FACTS:")
            for f in self.relevant_facts[:5]:
                lines.append(f" - {f}")
        if self.recent_events:
            lines.append("RECENT EVENTS:")
            for e in self.recent_events[:5]:
                lines.append(f" - {e}")
        if self.constraints:
            lines.append("OPERATIONAL CONSTRAINTS:")
            for c in self.constraints[:5]:
                lines.append(f" - {c}")
        return "\n".join(lines)


class ContextAssembler:
    """Assembles ContextIR instances from goal text and local storage."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        self._conn = conn

    def assemble(
        self,
        goal: str,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        additional_constraints: Optional[List[str]] = None,
    ) -> ContextIR:
        """Assemble a bounded ContextIR object without dumping arbitrary database contents."""
        preferences: List[str] = []
        facts: List[str] = []
        events: List[str] = []
        constraints = [
            "Operate 100% offline; zero network calls allowed.",
            "All filesystem operations must remain within allowed local project directories.",
            "Intelligence proposes; deterministic authority decides.",
        ]
        if additional_constraints:
            constraints.extend(additional_constraints)

        # Attempt to read bounded preferences if DB connection exists and table present
        if self._conn is not None:
            try:
                cursor = self._conn.execute(
                    "SELECT preference_key, preference_value FROM user_preferences LIMIT 5;"
                )
                for row in cursor.fetchall():
                    preferences.append(f"{row[0]}: {row[1]}")
            except Exception:
                pass  # Fall back gracefully if table not yet populated

            try:
                cursor = self._conn.execute("SELECT fact_text FROM memory_facts LIMIT 5;")
                for row in cursor.fetchall():
                    facts.append(str(row[0]))
            except Exception:
                pass

        return ContextIR(
            active_goal=goal,
            project_id=project_id,
            task_id=task_id,
            relevant_preferences=preferences,
            relevant_facts=facts,
            recent_events=events,
            constraints=constraints,
        )
