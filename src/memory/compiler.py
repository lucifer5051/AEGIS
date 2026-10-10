"""SAGE Bounded Context IR Compiler.

Canonical Reference:
- docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md

Invariants:
- Deterministic token estimation with explicit uncertainty bounds (±15%).
- Strict token budget enforcement: lower-ranked candidates are omitted once budget is reached.
- Omitted candidates are tracked deterministically in compilation metadata.
- Integrates cleanly with SAGE ContextIR without violating extra='forbid'.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from src.manager.context import ContextIR
from src.memory.models import MemoryCategory, MemoryRecord, RetrievalResult

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> Tuple[int, int, int]:
    """Deterministically estimate token count with uncertainty bounds.

    Returns:
        (estimated_tokens, min_tokens, max_tokens)
    """
    if not text:
        return 0, 0, 0
    # Heuristic: 1 token ~ 4 characters, adjusted by whitespace word density
    char_count = len(text)
    word_count = len(text.split())
    # Blend 60% char-based and 40% word-based estimation
    char_est = char_count / 3.8
    word_est = word_count * 1.3
    blended = max(1, int(math.ceil(0.6 * char_est + 0.4 * word_est)))

    # Uncertainty window: ±15%
    min_est = max(1, int(math.floor(blended * 0.85)))
    max_est = max(min_est, int(math.ceil(blended * 1.15)))
    return blended, min_est, max_est


class CompiledContext(BaseModel):
    """Detailed compilation artifact containing ContextIR and token budgeting metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_ir: ContextIR
    total_estimated_tokens: int
    token_uncertainty_range: Tuple[int, int]
    budget_allocated: int
    included_record_ids: List[str] = Field(default_factory=list)
    omitted_record_ids: List[str] = Field(default_factory=list)
    unresolved_conflicts: List[Dict[str, Any]] = Field(default_factory=list)


class ContextCompiler:
    """Compiles selective retrieval results into bounded ContextIR representations."""

    DEFAULT_BUDGET = 2048

    def __init__(self, default_token_budget: int = DEFAULT_BUDGET) -> None:
        self._default_budget = default_token_budget

    def compile(
        self,
        goal: str,
        retrieval: RetrievalResult,
        token_budget: Optional[int] = None,
        task_id: Optional[str] = None,
        additional_constraints: Optional[List[str]] = None,
    ) -> CompiledContext:
        """Compile a bounded ContextIR with token estimation and omission tracking."""
        budget = token_budget or retrieval.query.context_token_budget or self._default_budget

        # Base invariant constraints
        constraints: List[str] = [
            "Operate 100% offline; zero network calls allowed.",
            "All filesystem operations must remain within allowed local project directories.",
            "Intelligence proposes; deterministic authority decides.",
        ]
        if additional_constraints:
            constraints.extend(additional_constraints)

        # Baseline tokens for goal and constraints
        base_text = goal + " " + " ".join(constraints)
        base_tokens, _, _ = estimate_tokens(base_text)
        available_budget = max(64, budget - base_tokens)

        included_records: List[MemoryRecord] = []
        omitted_ids: List[str] = []
        current_consumed_tokens = 0

        # Candidates are already sorted by relevance/trust from EKMRetriever
        for record in retrieval.records:
            entry_text = f"{record.title}: {record.content}"
            est, _, _ = estimate_tokens(entry_text)
            if current_consumed_tokens + est <= available_budget:
                included_records.append(record)
                current_consumed_tokens += est
            else:
                omitted_ids.append(record.memory_id)

        # Extract preferences, facts, and entity descriptions
        relevant_preferences: List[str] = []
        relevant_facts: List[str] = []

        for rec in included_records:
            if rec.category == MemoryCategory.PREFERENCE:
                relevant_preferences.append(rec.content)
            elif rec.category == MemoryCategory.FACT:
                relevant_facts.append(rec.content)
            elif rec.category == MemoryCategory.ENTITY:
                relevant_facts.append(f"Entity: {rec.content}")
            else:
                relevant_facts.append(rec.content)

        context_ir = ContextIR(
            active_goal=goal,
            project_id=retrieval.query.project_id,
            task_id=task_id or retrieval.query.task_id,
            relevant_preferences=relevant_preferences,
            relevant_facts=relevant_facts,
            recent_events=[],
            constraints=constraints,
            available_token_budget=budget,
        )

        full_prompt_text = context_ir.to_prompt_context()
        total_tokens, min_tok, max_tok = estimate_tokens(full_prompt_text)

        return CompiledContext(
            context_ir=context_ir,
            total_estimated_tokens=total_tokens,
            token_uncertainty_range=(min_tok, max_tok),
            budget_allocated=budget,
            included_record_ids=[r.memory_id for r in included_records],
            omitted_record_ids=omitted_ids,
            unresolved_conflicts=retrieval.unresolved_conflicts,
        )
