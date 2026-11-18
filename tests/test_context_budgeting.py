"""Integration and unit tests for bounded context window and event history compaction.

Canonical Reference:
    PROMPT 064: Bounded Context Window & Event History Compaction (ARCH-060-03 Remediation)

Covers:
1. Small histories (preserved verbatim).
2. Very large histories (compacted and bounded).
3. Context budget enforcement in to_prompt_context() and compact_context().
4. Recent observation preservation (most recent steps retained with high fidelity).
5. Critical instruction and security constraint preservation (never truncated or omitted).
6. Oversized tool output truncation (head/tail preservation and failure alerts).
7. Summarization failures (graceful fallback to deterministic rule-based compaction).
8. Missing or unavailable tokenizers (robust fallback estimator and custom tokenizer support).
9. Repeated compaction idempotency (no recursive nested summaries).
10. Chronological and causal consistency.
11. Security-sensitive information preservation.
12. Full Supervisor integration during observation-driven replanning.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from src.manager.context import (
    ContextAssembler,
    ContextIR,
    ExecutionLessonContext,
    HistoryCompactor,
    estimate_tokens,
    truncate_tool_output,
)
from src.manager.models import StepState, TaskState
from src.manager.observation import ObservationStatus, StepObservation
from src.manager.planner import PlannedStep, StructuredPlanner
from src.manager.state_store import StateStore
from src.manager.supervisor import GoalExecutionResult, Supervisor
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import ReasoningMetrics, ReasoningProviderStatus, ReasoningRequest, ReasoningResponse
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.policy import AutonomyTier


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_context_budgeting.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    root = tmp_path / "budget_outputs"
    root.mkdir(parents=True, exist_ok=True)
    return root


class MockReasoningProvider(LocalReasoningProvider):
    """Deterministic mock reasoning provider."""

    def __init__(self, canned_response: str) -> None:
        super().__init__(provider_id="mock_reasoner", model_id="mock_model")
        self.canned_response = canned_response
        self.calls: List[ReasoningRequest] = []

    def initialize(self) -> bool:
        return True

    def is_ready(self) -> bool:
        return True

    def reason(self, request: ReasoningRequest) -> ReasoningResponse:
        self.calls.append(request)
        return ReasoningResponse(
            content=self.canned_response,
            finish_reason="stop",
            prompt_tokens=50,
            completion_tokens=50,
            total_tokens=100,
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


def test_01_small_histories_preserved_verbatim() -> None:
    """Small observation and event lists within budget are preserved verbatim without compaction."""
    compactor = HistoryCompactor(preserve_recent_count=3)
    obs = [
        "Step 1: read_file('config.json') -> success",
        "Step 2: parse_json() -> valid",
    ]
    compacted = compactor.compact_observations(obs, token_budget=1024)
    assert compacted == obs
    assert not any(x.startswith("[COMPACTED HISTORY") for x in compacted)


def test_02_very_large_histories_compacted() -> None:
    """A history of 50 steps is compacted into a summary plus recent steps, staying within budget."""
    compactor = HistoryCompactor(preserve_recent_count=3)
    large_obs = [
        f"Step {i}: tool_{i}() -> status {'success' if i % 2 == 0 else 'failed'}"
        for i in range(1, 51)
    ]
    compacted = compactor.compact_observations(large_obs, token_budget=512)

    assert len(compacted) <= 4  # 1 summary + up to 3 recent
    assert compacted[0].startswith("[COMPACTED HISTORY")
    assert "earlier steps succeeded" in compacted[0]
    # Recent steps are preserved at the end
    assert "Step 50" in compacted[-1]
    assert "Step 49" in compacted[-2]


def test_03_context_budget_enforcement_in_to_prompt_context() -> None:
    """to_prompt_context() strictly honors available_token_budget even under heavy content."""
    heavy_obs = [f"Step {i}: large output " + ("x" * 500) for i in range(20)]
    heavy_events = [f"Event {i}: milestone " + ("y" * 200) for i in range(15)]
    facts = [f"Fact {i}: domain knowledge " + ("z" * 100) for i in range(10)]

    context = ContextIR(
        active_goal="Verify bounded token budget enforcement",
        relevant_facts=facts,
        recent_events=heavy_events,
        observation_history=heavy_obs,
        constraints=["Operate 100% offline", "Never delete root directory"],
        available_token_budget=600,
    )

    rendered = context.to_prompt_context()
    rendered_tokens = estimate_tokens(rendered)
    # Must strictly be within the 600 token budget (with modest margin)
    assert rendered_tokens <= 650
    # Must strictly preserve goal and constraints
    assert "GOAL: Verify bounded token budget enforcement" in rendered
    assert "Operate 100% offline" in rendered
    assert "Never delete root directory" in rendered


def test_04_recent_observation_preservation() -> None:
    """Most recent failure observations are preserved with high fidelity."""
    compactor = HistoryCompactor(preserve_recent_count=2)
    obs = [
        "Step 1: file_read('a.txt') failed",
        "Step 2: file_read('b.txt') failed",
        "Step 3: file_read('c.txt') failed: Critical permission denied on /protected/c.txt",
    ]
    compacted = compactor.compact_observations(obs, token_budget=256)
    # Last item must be fully preserved
    assert "Critical permission denied on /protected/c.txt" in compacted[-1]


def test_05_critical_instruction_and_security_constraint_preservation() -> None:
    """Security constraints and system instructions are NEVER omitted, even under budget pressure."""
    constraints = [
        "Operate 100% offline; zero network calls allowed.",
        "All filesystem operations must remain within allowed local project directories.",
        "Intelligence proposes; deterministic authority decides.",
    ]
    context = ContextIR(
        active_goal="Secure operation task",
        constraints=constraints,
        relevant_facts=["Fact 1: info", "Fact 2: info"],
        observation_history=["Obs 1", "Obs 2"],
        available_token_budget=512,
    )
    rendered = context.to_prompt_context(max_tokens=512)
    for c in constraints:
        assert c in rendered


def test_06_oversized_tool_output_truncation() -> None:
    """Oversized tool outputs are truncated, preserving head, tail, and alert signals."""
    # Create 50,000 character output with an error in the middle and exit code at end
    header = "=== Command Output Start ===\nStatus: Initializing runner..."
    middle = "Log data " * 3000 + "\nException: DatabaseConnectionRefused at line 42\n" + "Log data " * 2000
    footer = "=== Process Finished with exit code 1 ==="
    full_output = header + "\n" + middle + "\n" + footer

    truncated = truncate_tool_output(full_output, max_chars=1200)

    assert len(truncated) <= 1600
    # Head and tail are preserved
    assert "Command Output Start" in truncated
    assert "exit code 1" in truncated
    # Truncation boundary is marked
    assert "TRUNCATED" in truncated
    # Alert for Exception is noted
    assert "Exception" in truncated


def test_07_summarization_failures_fallback_gracefully() -> None:
    """When a custom summarizer raises an exception, compactor falls back to deterministic rule."""
    def broken_summarizer(items: Any) -> str:
        raise RuntimeError("LLM summarization service offline")

    compactor = HistoryCompactor(
        preserve_recent_count=2,
        summarizer=broken_summarizer,
    )
    items = [
        "Step 1: read() -> success",
        "Step 2: write() -> failed: timeout",
        "Step 3: check() -> success",
    ]
    # Must not raise RuntimeError
    compacted = compactor.compact_observations(items, token_budget=512)
    assert len(compacted) >= 2
    assert compacted[0].startswith("[COMPACTED HISTORY")
    assert "earlier steps succeeded" in compacted[0] or "failures noted" in compacted[0]


def test_08_token_estimation_with_and_without_tokenizer() -> None:
    """Token estimation operates accurately with custom tokenizers or heuristic fallback."""
    sample_text = "The quick brown fox jumps over the lazy dog. A bounded agent loop prevents context saturation."

    # 1. Fallback heuristic
    fallback_est = estimate_tokens(sample_text, tokenizer=None)
    assert 15 <= fallback_est <= 30

    # Empty string
    assert estimate_tokens("") == 0

    # 2. Custom callable tokenizer
    def mock_char_tokenizer(text: str) -> int:
        return len(text.split())

    custom_est = estimate_tokens(sample_text, tokenizer=mock_char_tokenizer)
    assert custom_est == len(sample_text.split())

    # 3. Faulty tokenizer falls back safely
    def broken_tokenizer(text: str) -> int:
        raise ValueError("Tokenizer error")

    res = estimate_tokens(sample_text, tokenizer=broken_tokenizer)
    assert res == fallback_est


def test_09_repeated_compaction_prevents_nested_summaries() -> None:
    """Repeatedly compacting already-compacted history does not create nested summaries."""
    compactor = HistoryCompactor(preserve_recent_count=2)
    initial_obs = [
        f"Step {i}: action_{i}() -> {'success' if i < 8 else 'failed: err'}"
        for i in range(1, 10)
    ]
    round_1 = compactor.compact_observations(initial_obs, token_budget=256)
    assert round_1[0].startswith("[COMPACTED HISTORY")

    # Round 2: add a new step and re-compact
    round_2_input = round_1 + ["Step 10: action_10() -> failed: second error"]
    round_2 = compactor.compact_observations(round_2_input, token_budget=256)

    # Must NOT contain nested "[COMPACTED HISTORY ([COMPACTED HISTORY"
    assert round_2[0].count("[COMPACTED HISTORY") == 1
    assert "([COMPACTED HISTORY" not in round_2[0]


def test_10_chronological_and_causal_consistency() -> None:
    """Compacted summary accurately records chronological order and causality."""
    compactor = HistoryCompactor(preserve_recent_count=1)
    obs = [
        "Step 1: check_permissions() -> success",
        "Step 2: read_source() -> success",
        "Step 3: write_destination() -> failed: disk full",
        "Step 4: cleanup_temp() -> success",
    ]
    compacted = compactor.compact_observations(obs, token_budget=256)
    summary = compacted[0]
    # Succeeded count and failures noted
    assert "earlier steps succeeded" in summary
    assert "write_destination" in summary or "disk full" in summary
    assert "Step 4: cleanup_temp" in compacted[-1]


def test_11_assembler_compact_context_method(test_db: sqlite3.Connection) -> None:
    """ContextAssembler.compact_context() returns a bounded ContextIR respecting budget."""
    assembler = ContextAssembler(conn=test_db, default_token_budget=512)
    large_obs = [f"Step {i}: extensive observation data " * 20 for i in range(20)]
    large_events = [f"Event {i}: milestone data " * 10 for i in range(15)]

    raw_ir = ContextIR(
        active_goal="Test compact_context method",
        observation_history=large_obs,
        recent_events=large_events,
        constraints=["Rule 1: Offline", "Rule 2: Bound"],
        available_token_budget=512,
    )

    compacted_ir = assembler.compact_context(raw_ir, target_budget=512)
    assert isinstance(compacted_ir, ContextIR)
    assert len(compacted_ir.observation_history) < len(large_obs)
    assert len(compacted_ir.recent_events) < len(large_events)

    prompt = compacted_ir.to_prompt_context()
    assert estimate_tokens(prompt) <= 550
    assert "Rule 1: Offline" in prompt
    assert "Rule 2: Bound" in prompt


def test_12_supervisor_replanning_uses_bounded_context(
    test_db: sqlite3.Connection,
    output_root: Path,
) -> None:
    """Full supervisor execution with replan bounds observations and prevents context explosion."""
    auth = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(auth)
    register_builtin_tools(broker, allowed_roots=[output_root])
    store = StateStore(test_db)

    good_target = str(output_root / "final_success.txt")

    # Step 1 fails, Step 2 succeeds
    initial_plan = json.dumps({
        "goal": "Replan with bounded context",
        "rationale": "Initial try",
        "steps": [
            {
                "step_number": 1,
                "description": "Read missing file",
                "tool": "file_read",
                "arguments": {"path": str(output_root / "missing.txt")},
                "is_idempotent": False,
                "acceptance_criteria": "done",
                "estimated_risk": "LOW",
                "target_resource": "*",
            }
        ],
    })

    replan_plan = json.dumps({
        "goal": "Replan with bounded context",
        "rationale": "Revised try",
        "steps": [
            {
                "step_number": 1,
                "description": "Write target file on replan",
                "tool": "file_write",
                "arguments": {"path": good_target, "content": "bounded success"},
                "is_idempotent": True,
                "acceptance_criteria": "done",
                "estimated_risk": "LOW",
                "target_resource": good_target,
            }
        ],
    })

    reasoner = MockReasoningProvider(canned_response=initial_plan)
    # Feed replan plan on second call
    def dynamic_reason(req: ReasoningRequest) -> ReasoningResponse:
        reasoner.calls.append(req)
        content = replan_plan if len(reasoner.calls) > 1 else initial_plan
        return ReasoningResponse(
            content=content,
            finish_reason="stop",
            prompt_tokens=40,
            completion_tokens=40,
            total_tokens=80,
            model_id="mock",
            provider_id="mock",
        )
    reasoner.reason = dynamic_reason

    assembler = ContextAssembler(conn=test_db, default_token_budget=1024)
    supervisor = Supervisor(
        state_store=store,
        trust_authority=auth,
        tool_broker=broker,
        reasoning_provider=reasoner,
        context_assembler=assembler,
    )

    result = supervisor.execute_goal("Replan with bounded context")
    assert result.success is True
    assert Path(good_target).exists()
    assert Path(good_target).read_text(encoding="utf-8") == "bounded success"

    # Verify that the replan prompt sent to the model was bounded
    replan_request = reasoner.calls[1]
    prompt_text = replan_request.prompt
    assert "STEP THAT FAILED OR REQUIRED REPLAN:" in prompt_text
    assert estimate_tokens(prompt_text) < 1000

