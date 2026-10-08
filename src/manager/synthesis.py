"""SAGE Task Synthesizer.

Uses the local reasoning provider to generate a concise, user-facing result
from original goal, validated plan, observations, and verification outcomes.

Invariants:
- Zero secrets in user-facing output (sanitized).
- No capability tokens, hashes, or raw internal telemetry exposed.
- Deterministic fallback if provider is unavailable.
"""

from __future__ import annotations

import logging
from typing import List

from src.manager.observation import StepObservation
from src.manager.planner import Plan
from src.manager.verifier import StepVerificationResult
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import ReasoningRequest
from src.tools.sanitizer import sanitize_output

logger = logging.getLogger(__name__)


class TaskSynthesizer:
    """Produces clean, user-facing natural language summaries of execution outcomes."""

    def __init__(self, provider: LocalReasoningProvider) -> None:
        self.provider = provider

    def synthesize(
        self,
        goal: str,
        plan: Plan,
        observations: List[StepObservation],
        verifications: List[StepVerificationResult],
    ) -> str:
        """Produce a clean, concise result description.

        Args:
            goal: Original user goal string.
            plan: The planned steps executed.
            observations: Empirical observations from each step execution.
            verifications: Verification results for each step.

        Returns:
            Concise, user-facing result text.
        """
        # Assemble execution report
        step_reports = []
        for step, obs, ver in zip(plan.steps, observations, verifications, strict=False):
            step_reports.append(
                f"- Step {step.step_number}: {step.description}\n"
                f"  Tool: {step.tool}\n"
                f"  Outcome: {ver.outcome.value}\n"
                f"  Observation: {obs.output_summary}\n"
            )
        history_text = "\n".join(step_reports)

        prompt = (
            f"USER GOAL: {goal}\n\n"
            f"EXECUTION SUMMARY:\n{history_text}\n\n"
            "Provide a concise, friendly, and factual summary explaining what was "
            "accomplished for the user.\n"
            "Do not include technical debugging data, internal IDs, or capability tokens."
        )
        system_prompt = (
            "You are SAGE, an autonomous desktop assistant. "
            "Explain what action was taken and the final result clearly and concisely."
        )

        try:
            req = ReasoningRequest(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.2,
                max_tokens=256,
            )
            response = self.provider.reason(req)
            content = response.content.strip()
            # Ensure output is sanitized of any accidental secret leakage
            return sanitize_output(content)
        except Exception as exc:
            logger.warning(
                "Synthesis via local reasoning provider failed: %s. Using deterministic fallback.",
                exc,
            )
            return self._deterministic_fallback(goal, observations, verifications)

    def _deterministic_fallback(
        self,
        goal: str,
        observations: List[StepObservation],
        verifications: List[StepVerificationResult],
    ) -> str:
        """Deterministic fallback summary when LLM inference is bypassed or unavailable."""
        all_success = all(v.outcome.value == "VERIFIED_SUCCESS" for v in verifications)
        if all_success:
            return (
                f"Goal completed successfully: '{goal}'. "
                f"Executed {len(observations)} step(s) with all verification criteria satisfied."
            )
        return (
            f"Execution halted for goal: '{goal}'. "
            f"Completed {len(observations)} step(s) before an unverified or "
            "blocked outcome occurred."
        )
