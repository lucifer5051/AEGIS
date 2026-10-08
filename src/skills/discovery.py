"""SAGE Skills Subsystem -- Tool Discovery & Filtering.

Follows the ToolScope efficiency principle:
- Avoids loading the complete tool catalog into LLM prompts.
- Deterministic filtering first (capability, skill bindings, risk threshold).
- Deterministic heuristic relevance scoring.
- Limits results to top-k relevant tools.
"""

from __future__ import annotations

import re
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from src.tools.models import ToolDefinition
from src.trust.capability import ActionRisk

# Severity ranking for ActionRisk
_RISK_ORDER = {
    ActionRisk.LOW: 1,
    ActionRisk.MEDIUM: 2,
    ActionRisk.HIGH: 3,
    ActionRisk.CRITICAL: 4,
}


def _tokenize(text: str) -> set[str]:
    """Extract lowercased alphanumeric tokens from text."""
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


class ToolDiscoveryService:
    """Deterministic tool discovery and context reduction service."""

    def __init__(
        self,
        custom_ranker: Optional[Callable[[ToolDefinition, str], float]] = None,
    ) -> None:
        self._custom_ranker = custom_ranker

    def filter_tools(
        self,
        catalog: Mapping[str, ToolDefinition],
        intent: Optional[str] = None,
        capability: Optional[str] = None,
        allowed_bindings: Optional[Sequence[str]] = None,
        max_risk: Optional[ActionRisk] = None,
        top_k: Optional[int] = None,
    ) -> List[ToolDefinition]:
        """Filter and rank tools deterministically.

        Steps:
        1. Bound by allowed_bindings if specified.
        2. Filter by capability (action_name) if specified.
        3. Filter by max_risk if specified (fail-closed).
        4. Score tools against intent query deterministically.
        5. Return top_k tools sorted by score DESC, tool_name ASC.

        Args:
            catalog: Complete dictionary of available ToolDefinition instances.
            intent: Optional natural language intent string.
            capability: Optional action_name filter.
            allowed_bindings: Optional explicit tool names allowed by a skill manifest.
            max_risk: Maximum permitted ActionRisk.
            top_k: Maximum number of tools to return.

        Returns:
            Filtered and ordered list of ToolDefinition instances.
        """
        candidates: List[ToolDefinition] = []
        allowed_set = set(allowed_bindings) if allowed_bindings is not None else None

        for name, tool in catalog.items():
            # 1. Allowed bindings constraint
            if allowed_set is not None and name not in allowed_set:
                continue

            # 2. Capability constraint
            if capability is not None and tool.action_name != capability:
                continue

            # 3. Risk filter
            if max_risk is not None:
                tool_sev = _RISK_ORDER.get(tool.risk, 99)
                max_sev = _RISK_ORDER.get(max_risk, 0)
                if tool_sev > max_sev:
                    continue

            candidates.append(tool)

        # 4. Scoring and ranking
        scored: List[Tuple[float, ToolDefinition]] = []
        intent_tokens = _tokenize(intent) if intent else set()

        for tool in candidates:
            if self._custom_ranker is not None and intent:
                score = self._custom_ranker(tool, intent)
            else:
                score = self._calculate_deterministic_score(tool, intent, intent_tokens)
            scored.append((score, tool))

        # Sort: score DESC, tool.name ASC (deterministic tie-breaking)
        scored.sort(key=lambda item: (-item[0], item[1].name))

        results = [tool for _, tool in scored]
        if top_k is not None and top_k > 0:
            results = results[:top_k]

        return results

    def _calculate_deterministic_score(
        self,
        tool: ToolDefinition,
        intent: Optional[str],
        intent_tokens: set[str],
    ) -> float:
        """Compute deterministic relevance score without LLM calls."""
        if not intent:
            return 1.0

        score = 0.0
        tool_name_lower = tool.name.lower()
        intent_lower = intent.lower()

        # Exact name match
        if tool_name_lower == intent_lower:
            score += 100.0
        elif tool_name_lower in intent_lower:
            score += 50.0

        # Action name match
        if tool.action_name and tool.action_name.lower() in intent_lower:
            score += 30.0

        # Token overlap with description
        if tool.description:
            desc_tokens = _tokenize(tool.description)
            overlap = len(intent_tokens.intersection(desc_tokens))
            score += overlap * 10.0

        # Token overlap with parameter names
        for param in tool.parameters:
            if param.name.lower() in intent_tokens:
                score += 5.0

        return score
