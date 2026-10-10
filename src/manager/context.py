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
from typing import Any, List, Optional

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

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        memory_store: Optional[Any] = None,
        retriever: Optional[Any] = None,
        compiler: Optional[Any] = None,
    ) -> None:
        self._conn = conn
        self._store = memory_store
        self._retriever = retriever
        if self._retriever is None and self._store is not None:
            from src.memory.retriever import EKMRetriever

            self._retriever = EKMRetriever(self._store)
        elif self._retriever is None and self._conn is not None:
            try:
                from src.memory.retriever import EKMRetriever
                from src.memory.store import EKMMemoryStore

                self._store = EKMMemoryStore(self._conn)
                self._retriever = EKMRetriever(self._store)
            except Exception:
                pass

        self._compiler = compiler
        if self._compiler is None:
            try:
                from src.memory.compiler import ContextCompiler

                self._compiler = ContextCompiler()
            except Exception:
                pass

    def assemble(
        self,
        goal: str,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        additional_constraints: Optional[List[str]] = None,
    ) -> ContextIR:
        """Assemble a bounded ContextIR object without dumping arbitrary database contents."""
        # 1. Prefer full EKM selective retrieval and compilation if retriever is wired
        if self._retriever is not None and self._compiler is not None:
            try:
                from src.memory.models import MemoryQuery

                query = MemoryQuery(
                    query_text=goal,
                    project_id=project_id,
                    task_id=task_id,
                    limit=10,
                )
                retrieval = self._retriever.retrieve(query)
                compiled = self._compiler.compile(
                    goal=goal,
                    retrieval=retrieval,
                    task_id=task_id,
                    additional_constraints=additional_constraints,
                )
                if isinstance(compiled.context_ir, ContextIR):
                    return compiled.context_ir
            except Exception:
                pass

        # 2. Fallback to basic connection-level queries or safe defaults
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

        if self._conn is not None:
            try:
                cursor = self._conn.execute(
                    "SELECT preference_key, preference_value FROM user_preferences "
                    "WHERE is_superseded = 0 LIMIT 5;"
                )
                for row in cursor.fetchall():
                    preferences.append(f"{row[0]}: {row[1]}")
            except Exception:
                pass

            try:
                cursor = self._conn.execute(
                    "SELECT subject, predicate, object FROM memory_facts "
                    "WHERE is_superseded = 0 LIMIT 5;"
                )
                for row in cursor.fetchall():
                    facts.append(f"{row[0]} {row[1]} {row[2]}")
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
