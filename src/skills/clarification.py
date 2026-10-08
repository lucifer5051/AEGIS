"""SAGE Skills Subsystem -- Deterministic Clarification Gate.

Follows the research reference:
"Structured Uncertainty-guided Clarification for LLM Agents"

Responsibilities:
- Fast, deterministic parameter & ambiguity checks BEFORE invoking expensive Reasoning Core.
- Identifies: missing required parameters, multiple valid targets, ambiguous accounts,
  ambiguous files, ambiguous datetime, unsafe assumptions, and conflicting constraints.
- Emits minimal, targeted clarification questions ("What time should I set the alarm?").
- Returns CLARIFY or CONTINUE.
- Zero LLM agent overhead on the critical decision path.
"""

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field


@unique
class ClarificationAction(str, Enum):
    """Action dictated by the clarification gate."""

    CLARIFY = "CLARIFY"
    CONTINUE = "CONTINUE"


@unique
class ClarificationReason(str, Enum):
    """Deterministic classification of why clarification is needed."""

    MISSING_REQUIRED_PARAMETER = "MISSING_REQUIRED_PARAMETER"
    MULTIPLE_VALID_TARGETS = "MULTIPLE_VALID_TARGETS"
    AMBIGUOUS_ACCOUNT = "AMBIGUOUS_ACCOUNT"
    AMBIGUOUS_FILE = "AMBIGUOUS_FILE"
    AMBIGUOUS_DATETIME = "AMBIGUOUS_DATETIME"
    UNSAFE_ASSUMPTION = "UNSAFE_ASSUMPTION"
    CONFLICTING_CONSTRAINTS = "CONFLICTING_CONSTRAINTS"


class ClarificationResult(BaseModel):
    """Immutable outcome of the clarification evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ClarificationAction
    reason: Optional[ClarificationReason] = None
    clarification_prompt: Optional[str] = None
    missing_parameters: Tuple[str, ...] = Field(default_factory=tuple)
    ambiguous_candidates: Tuple[str, ...] = Field(default_factory=tuple)
    entity_type: Optional[str] = None

    @classmethod
    def proceed(cls) -> "ClarificationResult":
        """Convenience constructor when no clarification is required."""
        return cls(action=ClarificationAction.CONTINUE)

    @classmethod
    def clarify(
        cls,
        reason: ClarificationReason,
        prompt: str,
        missing: Sequence[str] = (),
        candidates: Sequence[str] = (),
        entity_type: Optional[str] = None,
    ) -> "ClarificationResult":
        """Convenience constructor when clarification is required."""
        return cls(
            action=ClarificationAction.CLARIFY,
            reason=reason,
            clarification_prompt=prompt,
            missing_parameters=tuple(missing),
            ambiguous_candidates=tuple(candidates),
            entity_type=entity_type,
        )


class ClarificationGate:
    """Lightweight deterministic gate evaluating whether user clarification is needed."""

    def evaluate(
        self,
        provided_args: Mapping[str, Any],
        required_keys: Sequence[str] = (),
        candidate_matches: Optional[Mapping[str, Sequence[str]]] = None,
        conflicting_pairs: Optional[Sequence[Tuple[str, str]]] = None,
    ) -> ClarificationResult:
        """Evaluate inputs deterministically against clarification criteria.

        Order of evaluation:
        1. Missing required parameters
        2. Ambiguous target candidates (multiple matching entities)
        3. Conflicting constraints
        4. Ambiguous temporal/account fields
        """
        # 1. Missing required parameter check
        missing = [
            k
            for k in required_keys
            if k not in provided_args or provided_args[k] is None or provided_args[k] == ""
        ]
        if missing:
            param_names = ", ".join(f"'{p}'" for p in missing)
            if len(missing) == 1 and missing[0] in ("time", "target_time", "alarm_time"):
                prompt = "What time should I set the alarm?"
            elif len(missing) == 1:
                prompt = f"Please specify the required parameter: {param_names}."
            else:
                prompt = f"Please specify the missing parameters: {param_names}."

            return ClarificationResult.clarify(
                reason=ClarificationReason.MISSING_REQUIRED_PARAMETER,
                prompt=prompt,
                missing=missing,
            )

        # 2. Ambiguity check (e.g. multiple matching files, accounts, or targets)
        if candidate_matches:
            for entity_type, candidates in candidate_matches.items():
                if len(candidates) > 1:
                    candidates_str = ", ".join(f"'{c}'" for c in candidates)
                    prompt = (
                        f"Multiple matching {entity_type}s found ({candidates_str}). "
                        f"Which {entity_type} did you mean?"
                    )
                    reason_map = {
                        "file": ClarificationReason.AMBIGUOUS_FILE,
                        "account": ClarificationReason.AMBIGUOUS_ACCOUNT,
                        "time": ClarificationReason.AMBIGUOUS_DATETIME,
                    }
                    reason = reason_map.get(
                        entity_type.lower(), ClarificationReason.MULTIPLE_VALID_TARGETS
                    )
                    return ClarificationResult.clarify(
                        reason=reason,
                        prompt=prompt,
                        candidates=candidates,
                        entity_type=entity_type,
                    )

        # 3. Conflicting constraints check
        if conflicting_pairs:
            for key1, key2 in conflicting_pairs:
                val1 = provided_args.get(key1)
                val2 = provided_args.get(key2)
                if val1 is not None and val2 is not None:
                    # Check for obvious logical contradiction
                    if (val1 is True and val2 is True) or val1 == val2:
                        prompt = (
                            f"Conflicting constraints detected between '{key1}' and '{key2}'. "
                            "Please clarify."
                        )
                        return ClarificationResult.clarify(
                            reason=ClarificationReason.CONFLICTING_CONSTRAINTS,
                            prompt=prompt,
                        )

        # 4. Unsafe assumption check (empty destination with destructive intent)
        if provided_args.get("action") in (
            "delete",
            "remove",
            "truncate",
        ) and not provided_args.get("target"):
            return ClarificationResult.clarify(
                reason=ClarificationReason.UNSAFE_ASSUMPTION,
                prompt=(
                    "Destructive action specified without a precise target. What should be deleted?"
                ),
            )

        return ClarificationResult.proceed()
