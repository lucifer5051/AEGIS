"""SAGE Fast Decision Engine - Domain Models.

Document Reference: docs/specifications/AEGIS_FAST_DECISION_ENGINE.md
Constraints:
- Fast Decision is STRICTLY ADVISORY. Zero authority to execute, approve, or bypass Trust Core.
- The advisory invariant `is_advisory_only: Literal[True] = True` is enforced at the type level.
- Maximum packed sequence budget is hard-capped at 2048 tokens; callers cannot raise it.
- Route names represent intent classification only, completely decoupled from tools.
"""

from enum import Enum
from typing import List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# ==============================================================================
# HARD CONSTANTS
# ==============================================================================

AEGIS_FAST_PATH_MAX_BUDGET: int = 2048


# ==============================================================================
# ENUMERATIONS
# ==============================================================================


class DecisionRoute(str, Enum):
    """Categorical user intent classification routes.

    Note: These represent INTENT classification, not capability execution or tool selection.
    """

    BROWSER_INTENT = "browser_intent"
    FILE_INTENT = "file_intent"
    TERMINAL_INTENT = "terminal_intent"
    MEMORY_INTENT = "memory_intent"
    COMMUNICATION_INTENT = "communication_intent"
    SETTINGS_INTENT = "settings_intent"
    REASONING_INTENT = "reasoning_intent"
    UNKNOWN_INTENT = "unknown_intent"


class ProviderStatus(str, Enum):
    """Lifecycle status of a Fast Decision Engine provider."""

    UNINITIALIZED = "UNINITIALIZED"
    LOADING = "LOADING"
    READY = "READY"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class DecisionOutcome(str, Enum):
    """High-level dispatch outcome of the Fast Decision triage."""

    FAST_PATH = "FAST_PATH"
    FALLBACK_TO_MANAGER = "FALLBACK_TO_MANAGER"


class FallbackReason(str, Enum):
    """Structured rationale emitted when triage falls back to the Manager loop."""

    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MODEL_NOT_LOADED = "MODEL_NOT_LOADED"
    CHECKSUM_FAILURE = "CHECKSUM_FAILURE"
    INCOMPATIBLE_MODEL = "INCOMPATIBLE_MODEL"
    HARDWARE_BUDGET_EXCEEDED = "HARDWARE_BUDGET_EXCEEDED"
    TOKEN_BUDGET_EXCEEDED = "TOKEN_BUDGET_EXCEEDED"
    ADMISSION_BUSY = "ADMISSION_BUSY"
    INFERENCE_TIMEOUT = "INFERENCE_TIMEOUT"
    RESOURCE_PRESSURE = "RESOURCE_PRESSURE"
    INFERENCE_EXCEPTION = "INFERENCE_EXCEPTION"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNSUPPORTED_INPUT = "UNSUPPORTED_INPUT"
    POLICY_RESTRICTION = "POLICY_RESTRICTION"


# ==============================================================================
# BASE SCHEMA
# ==============================================================================


class DecisionBaseModel(BaseModel):
    """Base model enforcing strict key validation and deterministic behavior."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
    )


# ==============================================================================
# REQUEST & CONTEXT
# ==============================================================================


class RequestContext(DecisionBaseModel):
    """Strictly typed execution context for fast decision triage.

    Free-form metadata dictionaries are strictly forbidden to prevent untrusted
    external text injection into the classifier context.
    """

    channel: Optional[str] = Field(
        default=None,
        description="Originating channel identifier (e.g. 'cli', 'desktop', 'whatsapp').",
    )
    project_scope: Optional[str] = Field(
        default=None,
        description="Active project identifier slug (opaque, non-arbitrary).",
    )
    input_kind: Optional[str] = Field(
        default=None,
        description="Category of input (e.g. 'chat', 'command', 'shortcut').",
    )
    locale: Optional[str] = Field(
        default="en",
        description="Locale code (default 'en').",
    )


class FastDecisionRequest(DecisionBaseModel):
    """Incoming request evaluated by the Fast Decision Engine."""

    user_input: str = Field(
        ...,
        description="Untrusted user input string to classify.",
    )
    context: RequestContext = Field(
        default_factory=RequestContext,
        description="Bounded, typed contextual attributes.",
    )
    requested_budget: Optional[int] = Field(
        default=None,
        ge=1,
        le=AEGIS_FAST_PATH_MAX_BUDGET,
        description="Caller token ceiling (can only lower budget, never raise above 2048).",
    )

    @property
    def effective_budget(self) -> int:
        """Calculate effective sequence token budget capped at AEGIS_FAST_PATH_MAX_BUDGET."""
        if self.requested_budget is None:
            return AEGIS_FAST_PATH_MAX_BUDGET
        return min(self.requested_budget, AEGIS_FAST_PATH_MAX_BUDGET)


# ==============================================================================
# RESULTS & METRICS
# ==============================================================================


class AlternativeRoute(DecisionBaseModel):
    """Candidate alternative route and associated probability."""

    route: DecisionRoute
    confidence: float = Field(ge=0.0, le=1.0)


class FastDecisionResult(DecisionBaseModel):
    """Typed, advisory-only decision record emitted by the Fast Decision Engine.

    SECURITY INVARIANT:
    `is_advisory_only` is literal True. Fast Decision Engine can NEVER authorize
    an action, bypass Trust Core, or execute tools.
    """

    decision_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique UUIDv4 identifier for correlation and telemetry.",
    )
    outcome: DecisionOutcome = Field(
        ...,
        description="High-level dispatch decision (FAST_PATH or FALLBACK_TO_MANAGER).",
    )
    route: DecisionRoute = Field(
        ...,
        description="Identified user intent route.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Empirical model confidence score for selected route.",
    )
    alternatives: List[AlternativeRoute] = Field(
        default_factory=list,
        description="Alternative candidate routes evaluated during inference.",
    )
    provider_id: str = Field(
        ...,
        description="Identifier of provider generating decision (e.g. 'von', 'fallback').",
    )
    provider_version: str = Field(
        ...,
        description="Provider implementation/model version string.",
    )
    latency_ms: float = Field(
        ...,
        ge=0.0,
        description="Wall-clock inference / evaluation latency in milliseconds.",
    )
    fallback_reason: Optional[FallbackReason] = Field(
        default=None,
        description="Structured reason when outcome is FALLBACK_TO_MANAGER.",
    )
    is_advisory_only: Literal[True] = Field(
        default=True,
        description="Type-level invariant: Fast Decision output is advisory only.",
    )


class ProviderMetrics(DecisionBaseModel):
    """Operational latency and resource utilization metrics for a decision provider."""

    cold_load_time_ms: float = Field(default=0.0, ge=0.0)
    warm_inference_p50_ms: float = Field(default=0.0, ge=0.0)
    warm_inference_p95_ms: float = Field(default=0.0, ge=0.0)
    warm_inference_p99_ms: float = Field(default=0.0, ge=0.0)
    cpu_usage_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    ram_usage_mb: float = Field(default=0.0, ge=0.0)
    vram_usage_mb: Optional[float] = Field(default=None, ge=0.0)
    inference_count: int = Field(default=0, ge=0)
