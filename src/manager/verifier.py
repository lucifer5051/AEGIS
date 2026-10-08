"""SAGE Step Verifier.

Evaluates post-execution observations against step acceptance criteria.
Enforces the invariant:
- Unknown/ambiguous results must never be treated as success.
- Never retry ambiguous non-idempotent side effects blindly.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src.manager.observation import ObservationStatus, StepObservation
from src.manager.planner import PlannedStep

logger = logging.getLogger(__name__)


class VerificationOutcome(str, Enum):
    """Deterministic classification of step verification."""

    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    SEMANTIC_FAILURE = "SEMANTIC_FAILURE"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class StepVerificationResult(BaseModel):
    """Outcome of verifying a step observation against acceptance criteria."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: VerificationOutcome = Field(description="Deterministic verification outcome.")
    rationale: str = Field(description="Explanation of why this verification outcome was assigned.")
    can_retry: bool = Field(
        default=False, description="Whether this step is safe and eligible for retry."
    )
    requires_replan: bool = Field(
        default=False, description="Whether the supervisor must replan rather than retry."
    )
    requires_approval: bool = Field(
        default=False, description="Whether human approval is required to proceed."
    )


class StepVerifier:
    """Evaluates step observation against planned step acceptance criteria."""

    def verify(self, step: PlannedStep, observation: StepObservation) -> StepVerificationResult:
        """Deterministically assess whether a step succeeded according to its criteria.

        Args:
            step: The planned step with acceptance criteria and idempotency flag.
            observation: The post-execution empirical observation.

        Returns:
            StepVerificationResult classifying the outcome.
        """
        # 1. Denials & Policy blocks
        if observation.status == ObservationStatus.DENIED:
            return StepVerificationResult(
                outcome=VerificationOutcome.BLOCKED,
                rationale=f"Step execution was denied by Trust Core: {observation.output_summary}",
                can_retry=False,
                requires_replan=False,
                requires_approval=True,
            )

        # 2. Timeouts
        if observation.status == ObservationStatus.TIMEOUT:
            if step.is_idempotent:
                return StepVerificationResult(
                    outcome=VerificationOutcome.RETRYABLE_FAILURE,
                    rationale="Execution timed out; step is idempotent and can be retried.",
                    can_retry=True,
                    requires_replan=False,
                    requires_approval=False,
                )
            return StepVerificationResult(
                outcome=VerificationOutcome.SEMANTIC_FAILURE,
                rationale="Execution timed out on non-idempotent step; cannot retry blindly.",
                can_retry=False,
                requires_replan=True,
                requires_approval=False,
            )

        # 3. Execution Errors
        if observation.status == ObservationStatus.ERROR or observation.exit_code != 0:
            if step.is_idempotent:
                return StepVerificationResult(
                    outcome=VerificationOutcome.RETRYABLE_FAILURE,
                    rationale=f"Tool error on idempotent step: {observation.output_summary}",
                    can_retry=True,
                    requires_replan=False,
                    requires_approval=False,
                )
            return StepVerificationResult(
                outcome=VerificationOutcome.SEMANTIC_FAILURE,
                rationale=f"Tool error on non-idempotent step: {observation.output_summary}",
                can_retry=False,
                requires_replan=True,
                requires_approval=False,
            )

        # 4. Status is SUCCESS: verify acceptance criteria
        if observation.status == ObservationStatus.SUCCESS:
            criteria_lower = step.acceptance_criteria.lower()

            # Check if step expected a file to be written or exist
            path_arg = step.arguments.get("path")
            if path_arg and (
                "file exists" in criteria_lower
                or "created" in criteria_lower
                or "written" in criteria_lower
            ):
                target_p = Path(path_arg)
                if not target_p.exists():
                    return StepVerificationResult(
                        outcome=VerificationOutcome.SEMANTIC_FAILURE,
                        rationale=(
                            f"Acceptance criteria violation: expected file '{path_arg}' "
                            "to exist, but it was not found on disk."
                        ),
                        can_retry=step.is_idempotent,
                        requires_replan=not step.is_idempotent,
                        requires_approval=False,
                    )

            # Check for error signals inside the raw output despite SUCCESS exit code
            out_lower = observation.raw_output.lower()
            if "traceback (most recent call last)" in out_lower or "[exit_code: 1]" in out_lower:
                return StepVerificationResult(
                    outcome=VerificationOutcome.SEMANTIC_FAILURE,
                    rationale="Semantic failure: error traceback detected in tool output.",
                    can_retry=step.is_idempotent,
                    requires_replan=not step.is_idempotent,
                    requires_approval=False,
                )

            # Criteria passed
            return StepVerificationResult(
                outcome=VerificationOutcome.VERIFIED_SUCCESS,
                rationale=(
                    f"Step succeeded and satisfied acceptance criteria: "
                    f"'{step.acceptance_criteria}'."
                ),
                can_retry=False,
                requires_replan=False,
                requires_approval=False,
            )

        # 5. Unknown fallback - NEVER treat unknown as success
        return StepVerificationResult(
            outcome=VerificationOutcome.UNKNOWN,
            rationale=f"Ambiguous or unrecognized observation state: status={observation.status}",
            can_retry=False,
            requires_replan=True,
            requires_approval=False,
        )
