"""SAGE ContextIR -- Context Intermediate Representation.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md
    PROMPT 064: Bounded Context Window & Event History Compaction (ARCH-060-03)

Invariants:
1. Strict frozen Pydantic model with extra='forbid'.
2. Minimal, bounded context representation for local reasoning & planning.
3. No arbitrary database dumping: strictly relevant preferences, facts, and events.
4. Deterministic formatting for reasoning provider consumption.
5. Inviolable preservation of active goal, system instructions, and security constraints.
6. Guaranteed bounded prompt rendering respecting configured token budget.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ==============================================================================
# TOKEN ESTIMATION & TOOL OUTPUT TRUNCATION UTILITIES
# ==============================================================================


def estimate_tokens(text: str, tokenizer: Optional[Any] = None) -> int:
    """Deterministically estimate token count with conservative fallback.

    If an external tokenizer instance (e.g. HuggingFace tokenizers, tiktoken, or callable)
    is provided and succeeds, it is used. Otherwise, uses a conservative blended heuristic
    combining unicode word segmentation and character length (1 token ~ 3.8 chars).
    """
    if not text:
        return 0

    if tokenizer is not None:
        try:
            if callable(tokenizer):
                res = tokenizer(text)
                if isinstance(res, int):
                    return res
                if hasattr(res, "__len__"):
                    return len(res)
            elif hasattr(tokenizer, "encode"):
                encoded = tokenizer.encode(text)
                if hasattr(encoded, "ids"):
                    return len(encoded.ids)
                return len(encoded)
        except Exception as exc:
            logger.debug("Tokenizer failed; falling back to heuristic: %s", exc)

    # Conservative heuristic: 60% char-based (~3.8 chars/tok) + 40% word-based (~1.25 tok/word)
    char_count = len(text)
    words = re.findall(r"\w+|[^\w\s]", text, re.UNICODE)
    word_count = len(words)
    char_est = char_count / 3.8
    word_est = word_count * 1.25
    blended = max(1, int(math.ceil(0.6 * char_est + 0.4 * word_est)))
    return blended


def truncate_tool_output(
    output: str,
    max_chars: int = 1500,
    max_lines: int = 40,
    preserve_head_chars: int = 800,
    preserve_tail_chars: int = 600,
) -> str:
    """Truncate oversized tool outputs while preserving critical headers and failure summaries.

    - Preserves command headers and initial output structure (head).
    - Preserves exit errors, stack traces, and final summary (tail).
    - Checks omitted text for critical failure signals (Error, Exception, Traceback, Denied).
    - Inserts a clear, informative truncation boundary marker.
    """
    if not output:
        return ""

    lines = output.splitlines()
    if len(output) <= max_chars and len(lines) <= max_lines:
        return output

    head = output[:preserve_head_chars]
    tail = output[-preserve_tail_chars:] if len(output) > preserve_tail_chars else ""
    omitted_chars = max(0, len(output) - len(head) - len(tail))
    omitted_lines = max(0, len(lines) - (len(head.splitlines()) + len(tail.splitlines())))

    # Inspect omitted middle text for critical failure alerts
    middle = output[preserve_head_chars:-preserve_tail_chars] if omitted_chars > 0 else ""
    alerts = []
    for pattern in ("Error:", "Exception:", "Traceback", "FAILED", "DENIED", "CRITICAL"):
        if pattern.lower() in middle.lower():
            alerts.append(pattern)

    alert_note = f" [Alert: omitted middle contained: {', '.join(set(alerts))}]" if alerts else ""
    marker = (
        f"\n... [TRUNCATED {omitted_chars} chars / {omitted_lines} lines"
        f"{alert_note}; key error/status preserved below] ...\n"
    )

    return head + marker + tail


# ==============================================================================
# HISTORY COMPACTION
# ==============================================================================


class HistoryCompactor:
    """Manages bounded history by preserving recent events and compacting older steps."""

    DEFAULT_PRESERVE_RECENT: int = 3
    MAX_ENTRY_CHARS: int = 1000

    def __init__(
        self,
        preserve_recent_count: int = DEFAULT_PRESERVE_RECENT,
        max_entry_chars: int = MAX_ENTRY_CHARS,
        summarizer: Optional[Callable[[Sequence[str]], str]] = None,
        tokenizer: Optional[Any] = None,
    ) -> None:
        self.preserve_recent_count = preserve_recent_count
        self.max_entry_chars = max_entry_chars
        self.summarizer = summarizer
        self.tokenizer = tokenizer

    def compact_observations(
        self,
        observations: Sequence[str],
        token_budget: int = 1024,
    ) -> List[str]:
        """Compact a list of step observations, preserving recent ones and summarizing older ones.

        Guarantees:
        1. Recent observations (up to preserve_recent_count) are preserved with high fidelity.
        2. Older observations are summarized into a concise chronological digest.
        3. Already-compacted summaries are preserved without redundant recursive re-summarization.
        4. Summarization failures fall back to deterministic rule-based compaction.
        """
        if not observations:
            return []

        # Sanitize/truncate individual oversized entries
        sanitized = [
            truncate_tool_output(obs, max_chars=self.max_entry_chars) for obs in observations
        ]

        total_tok = sum(estimate_tokens(obs, self.tokenizer) for obs in sanitized)
        if total_tok <= token_budget and len(sanitized) <= self.preserve_recent_count:
            return sanitized

        if len(sanitized) <= self.preserve_recent_count:
            # All are recent; scale down individual max sizes to fit budget
            target_per_item = max(100, (token_budget * 4) // len(sanitized))
            return [
                truncate_tool_output(obs, max_chars=target_per_item) for obs in sanitized
            ]

        split_idx = max(0, len(sanitized) - self.preserve_recent_count)
        older = sanitized[:split_idx]
        recent = sanitized[split_idx:]

        summary_text = self._summarize_items(older, prefix="COMPACTED HISTORY")
        result = [summary_text] + recent

        # If still over budget, fold earlier items from recent into summary
        while (
            sum(estimate_tokens(x, self.tokenizer) for x in result) > token_budget
            and len(recent) > 1
        ):
            older.append(recent.pop(0))
            summary_text = self._summarize_items(older, prefix="COMPACTED HISTORY")
            result = [summary_text] + recent

        return result

    def compact_events(
        self,
        events: Sequence[str],
        token_budget: int = 512,
    ) -> List[str]:
        """Compact recent execution events to remain strictly within allocated token budget."""
        if not events:
            return []

        sanitized = [
            truncate_tool_output(e, max_chars=self.max_entry_chars) for e in events
        ]
        total_tok = sum(estimate_tokens(e, self.tokenizer) for e in sanitized)
        if total_tok <= token_budget and len(sanitized) <= self.preserve_recent_count:
            return sanitized

        if len(sanitized) <= self.preserve_recent_count:
            target_per_item = max(80, (token_budget * 4) // len(sanitized))
            return [
                truncate_tool_output(e, max_chars=target_per_item) for e in sanitized
            ]

        split_idx = max(0, len(sanitized) - self.preserve_recent_count)
        older = sanitized[:split_idx]
        recent = sanitized[split_idx:]

        summary_text = self._summarize_items(older, prefix="COMPACTED EVENTS")
        return [summary_text] + recent

    def _summarize_items(self, items: Sequence[str], prefix: str = "COMPACTED HISTORY") -> str:
        """Deterministically summarize older history entries with causality preservation."""
        if not items:
            return ""

        # Try custom summarizer if provided
        if self.summarizer is not None:
            try:
                custom_summary = self.summarizer(items)
                if custom_summary and isinstance(custom_summary, str):
                    return f"[{prefix} ({len(items)} items)]: {custom_summary.strip()}"
            except Exception as exc:
                logger.warning("Custom summarizer failed; falling back to deterministic: %s", exc)

        # Deterministic rule-based compaction
        successes = 0
        failures: List[str] = []
        existing_summaries: List[str] = []
        milestones: List[str] = []

        for item in items:
            # Check if this item is already an existing compacted summary
            if item.startswith("[COMPACTED HISTORY") or item.startswith("[COMPACTED EVENTS"):
                # Avoid redundant recursive nesting; extract inner content
                inner = item.split("]:", 1)[-1].strip() if "]:" in item else item.strip("[]")
                existing_summaries.append(inner)
                continue

            item_lower = item.lower()
            if "failed" in item_lower or "error" in item_lower or "denied" in item_lower:
                clean = item.strip().splitlines()[0]
                if len(clean) > 120:
                    clean = clean[:117] + "..."
                failures.append(clean)
            elif "success" in item_lower or "completed" in item_lower:
                successes += 1
            else:
                clean = item.strip().splitlines()[0]
                if len(clean) > 80:
                    clean = clean[:77] + "..."
                milestones.append(clean)

        parts: List[str] = []
        if existing_summaries:
            for es in existing_summaries:
                parts.append(es)
        if successes:
            parts.append(f"{successes} earlier steps succeeded")
        if failures:
            parts.append(f"failures noted: {'; '.join(failures[:3])}")
        if milestones:
            parts.append(f"events: {', '.join(milestones[:2])}")

        summary_body = "; ".join(parts) if parts else f"{len(items)} prior steps executed."
        return f"[{prefix} ({len(items)} items)]: {summary_body}"


# ==============================================================================
# CONTEXT MODELS
# ==============================================================================


class ExecutionLessonContext(BaseModel):
    """Structured representation of an approved execution lesson in Context IR."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lesson_id: str = Field(description="Unique lesson UUID.")
    title: str = Field(description="Lesson title.")
    trigger_condition: str = Field(description="Condition under which lesson applies.")
    recommended_action: str = Field(description="Recommended procedure or avoidance rule.")
    observation: str = Field(description="Directly observed outcome.")
    inferred_explanation: str = Field(
        description="Inferred hypothesis explaining why outcome occurred."
    )
    risk_level: str = Field(description="Risk classification tier (LOW, MEDIUM, HIGH).")
    lifecycle_state: str = Field(description="Approved lifecycle state.")
    supporting_evidence_ids: List[str] = Field(default_factory=list)
    supporting_event_ids: List[str] = Field(default_factory=list)
    project_id: Optional[str] = Field(default=None)
    skill_id: Optional[str] = Field(default=None)
    trust_level: str = Field(default="M1")
    confidence: float = Field(default=1.0)
    confirmation_count: int = Field(default=1)


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
    relevant_lessons: List[ExecutionLessonContext] = Field(
        default_factory=list,
        description="Approved operational execution lessons informing reasoning.",
    )
    recent_events: List[str] = Field(
        default_factory=list, description="Recent execution milestones or events."
    )
    constraints: List[str] = Field(
        default_factory=list, description="Hard operational boundaries or safety constraints."
    )
    observation_history: List[str] = Field(
        default_factory=list,
        description="Structured observations from previous execution attempts informing replanning.",
    )
    available_token_budget: int = Field(
        default=4096, ge=512, le=32768, description="Allocated context budget."
    )

    def to_prompt_context(self, max_tokens: Optional[int] = None) -> str:
        """Render bounded, human-readable structured context for LLM reasoning.

        Enforces available_token_budget strictly:
        - Inviolable Invariant: Goal and Operational Constraints are ALWAYS fully preserved.
        - High Priority: Most recent observations and events are preserved with high fidelity.
        - Dynamic Compaction: If the total prompt exceeds the token budget, lower-priority
          sections (facts, preferences, older history) are compacted or trimmed.
        """
        budget = max_tokens or self.available_token_budget

        # 1. Base mandatory sections (Goal + Constraints)
        base_lines = [f"GOAL: {self.active_goal}"]
        if self.project_id:
            base_lines.append(f"PROJECT: {self.project_id}")

        constraint_lines: List[str] = []
        if self.constraints:
            constraint_lines.append("OPERATIONAL CONSTRAINTS:")
            for c in self.constraints:
                constraint_lines.append(f" - {c}")

        # Budget reserve for invariant baseline
        mandatory_text = "\n".join(base_lines + constraint_lines)
        mandatory_tokens = estimate_tokens(mandatory_text)
        remaining_budget = max(64, budget - mandatory_tokens)

        # 2. Build high-priority observations and events (taking most recent)
        obs_lines: List[str] = []
        if self.observation_history:
            obs_lines.append("PREVIOUS ATTEMPT OBSERVATIONS:")
            # If observation_history has a compacted summary first, preserve it + recent
            if any(obs.startswith("[COMPACTED HISTORY") for obs in self.observation_history):
                selected_obs = self.observation_history
            else:
                # Take the most recent observations
                selected_obs = self.observation_history[-5:]

            for obs in selected_obs:
                truncated_obs = truncate_tool_output(obs, max_chars=800)
                obs_lines.append(f" - {truncated_obs}")

        event_lines: List[str] = []
        if self.recent_events:
            event_lines.append("RECENT EVENTS:")
            if any(e.startswith("[COMPACTED EVENTS") for e in self.recent_events):
                selected_events = self.recent_events
            else:
                selected_events = self.recent_events[-5:]

            for e in selected_events:
                truncated_e = truncate_tool_output(e, max_chars=400)
                event_lines.append(f" - {truncated_e}")

        # 3. Build lessons, preferences, facts
        lesson_lines: List[str] = []
        if self.relevant_lessons:
            lesson_lines.append("OPERATIONAL LESSONS (ADVISORY ONLY):")
            for les in self.relevant_lessons[:5]:
                lesson_lines.append(f" - [{les.lesson_id[:8]}] {les.title} (Risk: {les.risk_level})")
                lesson_lines.append(f"   Trigger: {les.trigger_condition}")
                lesson_lines.append(f"   Action: {les.recommended_action}")
                lesson_lines.append(f"   Observed: {les.observation}")
                lesson_lines.append(f"   Explanation: {les.inferred_explanation}")

        pref_lines: List[str] = []
        if self.relevant_preferences:
            pref_lines.append("USER PREFERENCES:")
            for p in self.relevant_preferences[:5]:
                pref_lines.append(f" - {p}")

        fact_lines: List[str] = []
        if self.relevant_facts:
            fact_lines.append("KNOWN FACTS:")
            for f in self.relevant_facts[:5]:
                fact_lines.append(f" - {f}")

        # Assemble full standard document
        full_sections = [
            base_lines,
            pref_lines,
            fact_lines,
            lesson_lines,
            event_lines,
            obs_lines,
            constraint_lines,
        ]
        candidate_lines: List[str] = []
        for sec in full_sections:
            if sec:
                candidate_lines.extend(sec)

        candidate_text = "\n".join(candidate_lines)
        if estimate_tokens(candidate_text) <= budget:
            return candidate_text

        # If candidate exceeds budget, apply tiered pruning/compaction:
        # Tier 3 (Lowest priority): trim facts and preferences first
        pruned_sections = [base_lines]
        # Keep lessons if budget allows
        if lesson_lines and estimate_tokens("\n".join(lesson_lines)) < remaining_budget // 2:
            pruned_sections.append(lesson_lines)
        # Keep observations and events (compacted)
        if event_lines:
            pruned_sections.append(event_lines)
        if obs_lines:
            pruned_sections.append(obs_lines)
        # Always append invariant constraints
        pruned_sections.append(constraint_lines)

        result_lines: List[str] = []
        for sec in pruned_sections:
            if sec:
                result_lines.extend(sec)

        result_text = "\n".join(result_lines)
        # Final emergency truncation guard (preserving invariant constraints at bottom)
        if estimate_tokens(result_text) > budget:
            # Cut down non-mandatory lines
            non_mandatory = result_lines[len(base_lines) : -len(constraint_lines)]
            while non_mandatory and estimate_tokens("\n".join(base_lines + non_mandatory + constraint_lines)) > budget:
                non_mandatory.pop(0)
            return "\n".join(base_lines + non_mandatory + constraint_lines)

        return result_text


# ==============================================================================
# CONTEXT ASSEMBLER
# ==============================================================================


class ContextAssembler:
    """Assembles and compacts ContextIR instances from goal text, runtime state, and memory."""

    DEFAULT_BUDGET: int = 4096

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        memory_store: Optional[Any] = None,
        retriever: Optional[Any] = None,
        compiler: Optional[Any] = None,
        lesson_manager: Optional[Any] = None,
        default_token_budget: int = DEFAULT_BUDGET,
        tokenizer: Optional[Any] = None,
        compactor: Optional[HistoryCompactor] = None,
        unified_memory: Optional[Any] = None,
    ) -> None:
        self._conn = conn
        self._store = memory_store
        self._retriever = retriever
        self._lesson_manager = lesson_manager
        self._default_token_budget = default_token_budget
        self._tokenizer = tokenizer
        self._compactor = compactor or HistoryCompactor(tokenizer=self._tokenizer)
        self._unified_memory = unified_memory

        if self._retriever is None and self._store is not None:
            from src.memory.retriever import EKMRetriever

            self._retriever = EKMRetriever(self._store, lesson_manager=self._lesson_manager)
        elif self._retriever is None and self._conn is not None:
            try:
                from src.memory.retriever import EKMRetriever
                from src.memory.store import EKMMemoryStore

                self._store = EKMMemoryStore(self._conn)
                self._retriever = EKMRetriever(self._store, lesson_manager=self._lesson_manager)
            except Exception:
                pass

        self._compiler = compiler
        if self._compiler is None:
            try:
                from src.memory.compiler import ContextCompiler

                self._compiler = ContextCompiler()
            except Exception:
                pass

    @property
    def compactor(self) -> HistoryCompactor:
        """Return the active HistoryCompactor."""
        return self._compactor

    def compact_context(
        self,
        context: ContextIR,
        target_budget: Optional[int] = None,
    ) -> ContextIR:
        """Compact an existing ContextIR object to strictly honor the target token budget."""
        budget = target_budget or context.available_token_budget

        # Sub-budgets
        obs_budget = max(256, int(budget * 0.35))
        event_budget = max(128, int(budget * 0.15))

        compacted_obs = self._compactor.compact_observations(
            context.observation_history,
            token_budget=obs_budget,
        )
        compacted_events = self._compactor.compact_events(
            context.recent_events,
            token_budget=event_budget,
        )

        return ContextIR(
            active_goal=context.active_goal,
            project_id=context.project_id,
            task_id=context.task_id,
            relevant_preferences=context.relevant_preferences,
            relevant_facts=context.relevant_facts,
            relevant_lessons=context.relevant_lessons,
            recent_events=compacted_events,
            constraints=context.constraints,
            observation_history=compacted_obs,
            available_token_budget=budget,
        )

    def retrieve_relevant_lessons(
        self,
        goal: str,
        project_id: Optional[str] = None,
        limit: int = 5,
    ) -> List[ExecutionLessonContext]:
        """Retrieve strictly approved, non-superseded, relevant operational lessons.

        Guarantees:
        - Only lessons in APPROVED state are returned.
        - Superseded, rejected, or uncorroborated candidate lessons are excluded.
        - Matches trigger conditions, titles, and recommended actions against goal intent.
        - Preserves provenance, risk levels, and confidence.
        """
        # 1. Prefer EKMRetriever if wired
        if self._retriever is not None:
            try:
                from src.memory.models import MemoryCategory, MemoryQuery

                query = MemoryQuery(
                    query_text=goal,
                    project_id=project_id,
                    categories=[MemoryCategory.LESSON],
                    limit=limit,
                )
                retrieval = self._retriever.retrieve(query)
                results: List[ExecutionLessonContext] = []
                for les in getattr(retrieval, "lessons", []):
                    state_val = (
                        les.state.value if hasattr(les.state, "value") else str(les.state)
                    )
                    if state_val == "APPROVED" and not les.is_superseded:
                        risk_str = (
                            les.risk_level.value
                            if hasattr(les.risk_level, "value")
                            else str(les.risk_level)
                        )
                        trust_str = (
                            les.trust_level.value
                            if hasattr(les.trust_level, "value")
                            else str(les.trust_level)
                        )
                        results.append(
                            ExecutionLessonContext(
                                lesson_id=les.lesson_id,
                                title=les.title,
                                trigger_condition=les.trigger_condition,
                                recommended_action=les.recommended_action,
                                observation=les.observation,
                                inferred_explanation=les.inferred_explanation,
                                risk_level=risk_str,
                                lifecycle_state="APPROVED",
                                supporting_evidence_ids=list(les.supporting_evidence_ids),
                                supporting_event_ids=list(les.supporting_event_ids),
                                project_id=les.project_id,
                                skill_id=les.skill_id,
                                trust_level=trust_str,
                                confidence=1.0,
                                confirmation_count=les.confirmation_count,
                            )
                        )
                if results:
                    return results[:limit]
            except Exception as exc:
                logger.debug("EKMRetriever lesson retrieval fallback: %s", exc)

        # 2. Use LessonManager if wired
        if self._lesson_manager is not None:
            try:
                from src.memory.models import LessonState

                all_approved = self._lesson_manager.list_lessons(
                    project_id=project_id,
                    states=[LessonState.APPROVED],
                    limit=limit * 2,
                )
                goal_words = set(w.lower() for w in re.findall(r"\w+", goal) if len(w) > 2)
                scored = []
                for les in all_approved:
                    if les.is_superseded:
                        continue
                    text = f"{les.title} {les.trigger_condition} {les.recommended_action}".lower()
                    overlap = sum(1 for w in goal_words if w in text)
                    scored.append((overlap, les))
                scored.sort(key=lambda x: (x[0], x[1].confirmation_count), reverse=True)
                return [
                    ExecutionLessonContext(
                        lesson_id=les.lesson_id,
                        title=les.title,
                        trigger_condition=les.trigger_condition,
                        recommended_action=les.recommended_action,
                        observation=les.observation,
                        inferred_explanation=les.inferred_explanation,
                        risk_level=(
                            les.risk_level.value
                            if hasattr(les.risk_level, "value")
                            else str(les.risk_level)
                        ),
                        lifecycle_state="APPROVED",
                        supporting_evidence_ids=list(les.supporting_evidence_ids),
                        supporting_event_ids=list(les.supporting_event_ids),
                        project_id=les.project_id,
                        skill_id=les.skill_id,
                        trust_level=(
                            les.trust_level.value
                            if hasattr(les.trust_level, "value")
                            else str(les.trust_level)
                        ),
                        confidence=1.0,
                        confirmation_count=les.confirmation_count,
                    )
                    for _, les in scored[:limit]
                ]
            except Exception as exc:
                logger.debug("LessonManager list_lessons fallback: %s", exc)

        # 3. Direct SQLite connection query fallback
        if self._conn is not None:
            try:
                cursor = self._conn.execute(
                    "SELECT lesson_id, title, trigger_condition, recommended_action, "
                    "observation, inferred_explanation, risk_level, project_id, skill_id, "
                    "confirmation_count FROM execution_lessons WHERE state = 'APPROVED' "
                    "AND is_superseded = 0 AND (project_id = ? OR project_id IS NULL) "
                    "LIMIT 20;",
                    (project_id,),
                )
                rows = cursor.fetchall()
                goal_words = set(w.lower() for w in re.findall(r"\w+", goal) if len(w) > 2)
                scored_rows = []
                for r in rows:
                    text = f"{r[1]} {r[2]} {r[3]}".lower()
                    overlap = sum(1 for w in goal_words if w in text)
                    scored_rows.append((overlap, r))
                scored_rows.sort(key=lambda x: (x[0], x[1][9] if len(x[1]) > 9 else 0), reverse=True)
                return [
                    ExecutionLessonContext(
                        lesson_id=r[0],
                        title=r[1],
                        trigger_condition=r[2],
                        recommended_action=r[3],
                        observation=r[4],
                        inferred_explanation=r[5],
                        risk_level=r[6],
                        lifecycle_state="APPROVED",
                        project_id=r[7],
                        skill_id=r[8],
                        confirmation_count=r[9] if len(r) > 9 else 1,
                    )
                    for _, r in scored_rows[:limit]
                ]
            except Exception as exc:
                logger.debug("Direct connection lesson retrieval error: %s", exc)

        return []

    def assemble(
        self,
        goal: str,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        additional_constraints: Optional[List[str]] = None,
        recent_events: Optional[List[str]] = None,
        observation_history: Optional[List[str]] = None,
        token_budget: Optional[int] = None,
    ) -> ContextIR:
        """Assemble a bounded ContextIR object without dumping arbitrary database contents."""
        effective_budget = token_budget or self._default_token_budget

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
                    token_budget=effective_budget,
                )
                if isinstance(compiled.context_ir, ContextIR):
                    base_ir = compiled.context_ir
                    if self._unified_memory is not None:
                        try:
                            um_res = self._unified_memory.recall(query_text=goal, project_id=project_id, limit=5)
                            new_prefs = list(base_ir.relevant_preferences)
                            new_facts = list(base_ir.relevant_facts)
                            for mem in getattr(um_res, "records", []):
                                cat_val = mem.cognitive_category.value if hasattr(mem.cognitive_category, "value") else str(mem.cognitive_category)
                                if cat_val in ("personal", "preference") and len(new_prefs) < 5:
                                    pref_entry = f"{mem.subject}: {mem.content}" if mem.subject else mem.content
                                    if pref_entry not in new_prefs:
                                        new_prefs.append(pref_entry)
                                elif cat_val in ("semantic", "fact") and len(new_facts) < 5:
                                    fact_entry = f"{mem.subject} {mem.predicate or 'is'} {mem.content}" if mem.subject else mem.content
                                    if fact_entry not in new_facts:
                                        new_facts.append(fact_entry)
                            base_ir = ContextIR(
                                active_goal=base_ir.active_goal,
                                project_id=base_ir.project_id,
                                task_id=base_ir.task_id,
                                relevant_preferences=new_prefs,
                                relevant_facts=new_facts,
                                relevant_lessons=base_ir.relevant_lessons,
                                recent_events=base_ir.recent_events,
                                constraints=base_ir.constraints,
                                observation_history=base_ir.observation_history,
                                available_token_budget=effective_budget,
                            )
                        except Exception as exc:
                            logger.debug("Unified memory recall context integration failed: %s", exc)
                    # If caller supplied observations or events, merge and compact
                    if observation_history or recent_events:
                        merged_obs = list(base_ir.observation_history) + list(
                            observation_history or []
                        )
                        merged_events = list(base_ir.recent_events) + list(
                            recent_events or []
                        )
                        return self.compact_context(
                            ContextIR(
                                active_goal=base_ir.active_goal,
                                project_id=base_ir.project_id,
                                task_id=base_ir.task_id,
                                relevant_preferences=base_ir.relevant_preferences,
                                relevant_facts=base_ir.relevant_facts,
                                relevant_lessons=base_ir.relevant_lessons,
                                recent_events=merged_events,
                                constraints=base_ir.constraints,
                                observation_history=merged_obs,
                                available_token_budget=effective_budget,
                            ),
                            target_budget=effective_budget,
                        )
                    return base_ir
            except Exception as exc:
                logger.debug("EKM compilation fallback: %s", exc)

        # 2. Fallback to basic connection-level queries or safe defaults
        preferences: List[str] = []
        facts: List[str] = []
        lessons: List[ExecutionLessonContext] = []
        events: List[str] = list(recent_events or [])
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

        # Retrieve approved, non-superseded, relevant lessons
        lessons = self.retrieve_relevant_lessons(goal=goal, project_id=project_id, limit=5)

        # 3. Augment with Unified Memory if available
        if self._unified_memory is not None:
            try:
                um_res = self._unified_memory.recall(query_text=goal, project_id=project_id, limit=5)
                for mem in getattr(um_res, "records", []):
                    cat_val = mem.category.value if hasattr(mem.category, "value") else str(mem.category)
                    if cat_val in ("personal", "preference") and len(preferences) < 5:
                        pref_entry = f"{mem.subject}: {mem.content}"
                        if pref_entry not in preferences:
                            preferences.append(pref_entry)
                    elif cat_val in ("semantic", "fact") and len(facts) < 5:
                        fact_entry = f"{mem.subject} {mem.predicate or 'is'} {mem.content}"
                        if fact_entry not in facts:
                            facts.append(fact_entry)
            except Exception as exc:
                logger.debug("Unified memory recall context integration failed: %s", exc)

        if self._conn is not None:
            # If task_id is supplied and no recent events were passed, query recent task events
            if task_id is not None and not events:
                try:
                    cursor = self._conn.execute(
                        "SELECT event_type, payload FROM task_events "
                        "WHERE task_id = ? ORDER BY event_seq DESC LIMIT 5;",
                        (task_id,),
                    )
                    rows = cursor.fetchall()
                    for r in reversed(rows):
                        payload_str = str(r[1])[:100] if r[1] else ""
                        events.append(f"{r[0]}: {payload_str}")
                except Exception:
                    pass

        uncompacted_ir = ContextIR(
            active_goal=goal,
            project_id=project_id,
            task_id=task_id,
            relevant_preferences=preferences,
            relevant_facts=facts,
            relevant_lessons=lessons,
            recent_events=events,
            constraints=constraints,
            observation_history=list(observation_history or []),
            available_token_budget=effective_budget,
        )

        return self.compact_context(uncompacted_ir, target_budget=effective_budget)
