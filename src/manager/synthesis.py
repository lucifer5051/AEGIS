"""SAGE Task Synthesizer.

Uses the local reasoning provider to generate a concise, user-facing result
from original goal, validated plan, observations, and verification outcomes.

Remediates ARCH-060-05:
- Distinguishes model synthesis from fallback output.
- Configurable degradation policies: RESILIENT, STRICT, DISABLED.
- Preserves structured exception diagnostics.
- Sanitizes all diagnostic and user-facing content (Zero-Secret Invariant).
"""

from __future__ import annotations

import logging
import re
from enum import Enum, unique
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from src.manager.observation import StepObservation
from src.manager.planner import Plan
from src.manager.verifier import StepVerificationResult
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import ReasoningRequest
from src.tools.sanitizer import sanitize_output
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"bot\d+:[A-Za-z0-9_-]+"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b"),
]


@unique
class SynthesisDegradationPolicy(str, Enum):
    """Configurable policy for handling task synthesis failures."""

    RESILIENT = "RESILIENT"  # Substitute deterministic fallback, mark as degraded, continue
    STRICT = "STRICT"  # Propagate typed SynthesisError, never substitute silent fallback
    DISABLED = "DISABLED"  # Skip provider invocation entirely, produce explicit fallback


@unique
class SynthesisFailureType(str, Enum):
    """Categorization of synthesis provider failure modes."""

    NONE = "NONE"
    TIMEOUT = "TIMEOUT"
    CONTEXT_LENGTH = "CONTEXT_LENGTH"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class ProviderErrorDiagnostics(BaseModel):
    """Structured, sanitized diagnostic telemetry for synthesis failures."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_type: SynthesisFailureType = Field(
        description="High-level category of synthesis provider failure."
    )
    error_class: str = Field(description="Python exception class name.")
    error_message: str = Field(description="Sanitized exception message string.")
    sanitized: bool = Field(default=True, description="Whether message was verified secret-free.")
    details: Dict[str, Any] = Field(
        default_factory=dict, description="Structured failure context and metadata."
    )


class SynthesisResult(BaseModel):
    """Observable, structured result of task synthesis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(description="Synthesized natural language summary or deterministic fallback.")
    is_degraded: bool = Field(
        default=False,
        description="Whether synthesis experienced failure or degraded fidelity.",
    )
    fallback_used: bool = Field(
        default=False,
        description="Whether deterministic fallback was substituted for model output.",
    )
    provider_error: Optional[ProviderErrorDiagnostics] = Field(
        default=None,
        description="Diagnostic information if provider failed.",
    )
    provider_name: Optional[str] = Field(
        default=None,
        description="Reasoning provider identifier responsible for the attempt.",
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Model name/identifier used for synthesis if known.",
    )
    policy: SynthesisDegradationPolicy = Field(
        default=SynthesisDegradationPolicy.RESILIENT,
        description="Degradation policy applied during synthesis.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Execution and performance metadata.",
    )

    def __str__(self) -> str:
        """String representation returns the synthesized text for transparent compatibility."""
        return self.text

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.text == other
        return super().__eq__(other)


# ==============================================================================
# TYPED SYNTHESIS EXCEPTIONS (STRICT MODE)
# ==============================================================================


class SynthesisError(RuntimeError):
    """Base exception raised when TaskSynthesizer fails under STRICT degradation policy."""

    def __init__(
        self,
        message: str,
        diagnostics: Optional[ProviderErrorDiagnostics] = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class SynthesisTimeoutError(SynthesisError):
    """Raised when the reasoning provider times out during synthesis."""


class SynthesisContextLengthError(SynthesisError):
    """Raised when synthesis prompt or response exceeds provider context/token limits."""


class SynthesisProviderUnavailableError(SynthesisError):
    """Raised when reasoning provider is offline, uninitialized, or missing."""


# ==============================================================================
# TASK SYNTHESIZER
# ==============================================================================


class TaskSynthesizer:
    """Produces clean, user-facing summaries of execution outcomes with explicit degradation."""

    def __init__(
        self,
        provider: Optional[LocalReasoningProvider] = None,
        policy: SynthesisDegradationPolicy = SynthesisDegradationPolicy.RESILIENT,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self._provider_name = provider_name
        self._model_name = model_name

    def synthesize(
        self,
        goal: str,
        plan: Plan,
        observations: List[StepObservation],
        verifications: List[StepVerificationResult],
    ) -> SynthesisResult:
        """Produce a clean, concise result description with explicit degradation tracking.

        Args:
            goal: Original user goal string.
            plan: The planned steps executed.
            observations: Empirical observations from each step execution.
            verifications: Verification results for each step.

        Returns:
            SynthesisResult: Observable result model containing output text, degradation status,
            and provider diagnostics.

        Raises:
            SynthesisTimeoutError: Under STRICT policy if provider times out.
            SynthesisContextLengthError: Under STRICT policy if context budget overflows.
            SynthesisProviderUnavailableError: Under STRICT policy if provider is missing/offline.
            SynthesisError: Under STRICT policy for other provider failures.
        """
        # 1. DISABLED POLICY: Explicitly skip provider invocation
        if self.policy == SynthesisDegradationPolicy.DISABLED:
            logger.info("Task synthesis provider invocation DISABLED by configuration.")
            fallback_text = self._deterministic_fallback(goal, observations, verifications)
            return SynthesisResult(
                text=fallback_text,
                is_degraded=False,
                fallback_used=True,
                provider_error=None,
                provider_name=self._resolve_provider_name(),
                model_name=self._resolve_model_name(),
                policy=SynthesisDegradationPolicy.DISABLED,
                metadata={"reason": "policy_disabled"},
            )

        # 2. PROVIDER CHECK
        if self.provider is None:
            diag = ProviderErrorDiagnostics(
                failure_type=SynthesisFailureType.PROVIDER_UNAVAILABLE,
                error_class="NoneType",
                error_message="No local reasoning provider configured for TaskSynthesizer.",
                sanitized=True,
                details={"provider": None},
            )
            if self.policy == SynthesisDegradationPolicy.STRICT:
                raise SynthesisProviderUnavailableError(
                    "Synthesis failed: No reasoning provider configured (STRICT policy).",
                    diagnostics=diag,
                )
            logger.warning("No reasoning provider available for synthesis. Using fallback.")
            fallback_text = self._deterministic_fallback(goal, observations, verifications)
            return SynthesisResult(
                text=fallback_text,
                is_degraded=True,
                fallback_used=True,
                provider_error=diag,
                provider_name=self._resolve_provider_name(),
                model_name=self._resolve_model_name(),
                policy=self.policy,
                metadata={"reason": "missing_provider"},
            )

        # 3. BUILD SYNTHESIS PROMPT
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

        # 4. EXECUTE MODEL REASONING
        try:
            req = ReasoningRequest(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.2,
                max_tokens=256,
            )
            response = self.provider.reason(req)
            content = response.content.strip() if response and response.content else ""

            if not content:
                diag = ProviderErrorDiagnostics(
                    failure_type=SynthesisFailureType.MALFORMED_RESPONSE,
                    error_class="ValueError",
                    error_message="Reasoning provider returned empty or whitespace completion.",
                    sanitized=True,
                    details={"finish_reason": getattr(response, "finish_reason", "unknown")},
                )
                if self.policy == SynthesisDegradationPolicy.STRICT:
                    raise SynthesisError("Reasoning provider returned empty completion.", diagnostics=diag)
                logger.warning("Synthesis returned empty completion. Using fallback.")
                fallback_text = self._deterministic_fallback(goal, observations, verifications)
                return SynthesisResult(
                    text=fallback_text,
                    is_degraded=True,
                    fallback_used=True,
                    provider_error=diag,
                    provider_name=self._resolve_provider_name(),
                    model_name=self._resolve_model_name(),
                    policy=self.policy,
                    metadata={"reason": "empty_content"},
                )

            # Check for context truncation in response
            is_length_finish = getattr(response, "finish_reason", "") == "length"
            sanitized_content = sanitize_output(content)

            return SynthesisResult(
                text=sanitized_content,
                is_degraded=is_length_finish,
                fallback_used=False,
                provider_error=None if not is_length_finish else ProviderErrorDiagnostics(
                    failure_type=SynthesisFailureType.CONTEXT_LENGTH,
                    error_class="ContextTruncation",
                    error_message="Synthesis output was truncated due to token limit.",
                    sanitized=True,
                    details={"finish_reason": "length"},
                ),
                provider_name=self._resolve_provider_name(),
                model_name=self._resolve_model_name(),
                policy=self.policy,
                metadata={
                    "prompt_tokens": getattr(response, "prompt_tokens", 0),
                    "completion_tokens": getattr(response, "completion_tokens", 0),
                    "finish_reason": getattr(response, "finish_reason", "stop"),
                },
            )

        except Exception as exc:
            failure_type, exc_class = self._classify_exception(exc)
            sanitized_msg = self._sanitize_error_message(str(exc))
            error_cls_name = exc.__class__.__name__

            diag = ProviderErrorDiagnostics(
                failure_type=failure_type,
                error_class=error_cls_name,
                error_message=sanitized_msg,
                sanitized=True,
                details={"original_exception": error_cls_name},
            )

            if self.policy == SynthesisDegradationPolicy.STRICT:
                logger.error(
                    "Task synthesis failed under STRICT policy [%s: %s]. Propagating %s.",
                    error_cls_name,
                    sanitized_msg,
                    exc_class.__name__,
                )
                raise exc_class(
                    f"Task synthesis failed ({failure_type.value}): {sanitized_msg}",
                    diagnostics=diag,
                ) from exc

            # RESILIENT POLICY: Explicitly marked degraded fallback
            logger.warning(
                "Task synthesis failed [%s: %s]. Degrading safely to deterministic fallback.",
                error_cls_name,
                sanitized_msg,
            )
            fallback_text = self._deterministic_fallback(goal, observations, verifications)
            return SynthesisResult(
                text=fallback_text,
                is_degraded=True,
                fallback_used=True,
                provider_error=diag,
                provider_name=self._resolve_provider_name(),
                model_name=self._resolve_model_name(),
                policy=self.policy,
                metadata={"reason": "provider_exception", "exception_class": error_cls_name},
            )

    def _classify_exception(
        self,
        exc: Exception,
    ) -> Tuple[SynthesisFailureType, type]:
        """Classify provider exception into typed failure and corresponding exception class."""
        msg_lower = str(exc).lower()
        cls_name = exc.__class__.__name__.lower()

        # 1. Timeout detection
        if isinstance(exc, TimeoutError) or "timeout" in msg_lower or "timed out" in msg_lower:
            return SynthesisFailureType.TIMEOUT, SynthesisTimeoutError

        # 2. Context length / token budget detection
        if (
            ("context" in msg_lower and ("length" in msg_lower or "limit" in msg_lower or "window" in msg_lower))
            or "max_tokens" in msg_lower
            or "token limit" in msg_lower
            or "maximum context" in msg_lower
        ):
            return SynthesisFailureType.CONTEXT_LENGTH, SynthesisContextLengthError

        # 3. Provider availability / admission / loading detection
        if (
            "unavailable" in msg_lower
            or "not ready" in msg_lower
            or "uninitialized" in msg_lower
            or "offline" in msg_lower
            or "admission" in cls_name
            or "residency" in cls_name
            or "storage" in cls_name
            or "integrity" in cls_name
            or "connection" in msg_lower
        ):
            return SynthesisFailureType.PROVIDER_UNAVAILABLE, SynthesisProviderUnavailableError

        # 4. General execution / model crash error
        return SynthesisFailureType.EXECUTION_ERROR, SynthesisError

    def _sanitize_error_message(self, msg: str) -> str:
        """Sanitize error messages to enforce the Zero-Secret Invariant."""
        cleaned = sanitize_output(msg)
        for pat in _SECRET_PATTERNS:
            cleaned = pat.sub("[REDACTED_SECRET]", cleaned)
        try:
            _check_no_secrets(cleaned)
        except Exception:
            return "[REDACTED_ERROR_TELEMETRY]"
        return cleaned

    def _resolve_provider_name(self) -> Optional[str]:
        if self._provider_name:
            return self._provider_name
        if self.provider is not None:
            return getattr(self.provider, "provider_id", self.provider.__class__.__name__)
        return None

    def _resolve_model_name(self) -> Optional[str]:
        if self._model_name:
            return self._model_name
        if self.provider is not None:
            return getattr(self.provider, "model_id", None)
        return None

    def _deterministic_fallback(
        self,
        goal: str,
        observations: List[StepObservation],
        verifications: List[StepVerificationResult],
    ) -> str:
        """Deterministic fallback summary when LLM inference is bypassed, disabled, or failed."""
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
