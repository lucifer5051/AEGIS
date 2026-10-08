"""SAGE Local Reasoning Model - Data Models.

Defines typed schemas for provider-neutral local reasoning requests,
responses, lifecycle statuses, and empirical execution metrics.
All models use Pydantic v2 with extra='forbid' and deterministic defaults.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ReasoningProviderStatus(str, Enum):
    """Lifecycle status of a local reasoning provider."""

    UNINITIALIZED = "UNINITIALIZED"
    LOADING = "LOADING"
    READY = "READY"
    FAILED = "FAILED"
    UNLOADED = "UNLOADED"


class ReasoningRequest(BaseModel):
    """Request payload for local reasoning inference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(description="User prompt or reasoning task input.")
    system_prompt: Optional[str] = Field(
        default=None, description="Optional system instruction prefix."
    )
    max_tokens: int = Field(
        default=512, ge=1, le=4096, description="Maximum completion tokens to generate."
    )
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Sampling temperature (0.0 for deterministic)."
    )
    top_p: float = Field(
        default=0.95, ge=0.0, le=1.0, description="Nucleus sampling probability threshold."
    )
    stop_sequences: List[str] = Field(
        default_factory=list, description="Optional list of stop sequences."
    )
    response_format: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional structured format specifier (e.g. {'type': 'json_object'}).",
    )
    request_id: Optional[str] = Field(
        default=None, description="Optional correlation ID for the request."
    )
    task_id: Optional[str] = Field(
        default=None, description="Optional associated supervisor task ID."
    )
    step_id: Optional[str] = Field(
        default=None, description="Optional associated supervisor step ID."
    )


class ReasoningResponse(BaseModel):
    """Result of local reasoning inference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(description="Generated reasoning text content.")
    finish_reason: str = Field(
        default="stop", description="Generation completion reason ('stop', 'length', etc.)."
    )
    prompt_tokens: int = Field(ge=0, default=0, description="Number of tokens in prompt.")
    completion_tokens: int = Field(
        ge=0, default=0, description="Number of tokens generated in completion."
    )
    total_tokens: int = Field(
        ge=0, default=0, description="Total tokens processed (prompt + completion)."
    )
    load_duration_ms: float = Field(
        ge=0.0, default=0.0, description="Model load latency in milliseconds."
    )
    inference_duration_ms: float = Field(
        ge=0.0, default=0.0, description="Inference execution duration in milliseconds."
    )
    first_token_duration_ms: float = Field(
        ge=0.0, default=0.0, description="Latency to produce first output token in milliseconds."
    )
    model_id: str = Field(
        default="qwen35-4b-q4km", description="Canonical internal model identifier."
    )
    provider_id: str = Field(
        default="llamacpp", description="Underlying provider execution engine."
    )


class ReasoningMetrics(BaseModel):
    """Cumulative operational metrics for the reasoning provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_inferences: int = Field(ge=0, default=0, description="Total inference calls completed.")
    total_tokens_generated: int = Field(
        ge=0, default=0, description="Total completion tokens generated."
    )
    load_latency_ms: float = Field(
        ge=0.0, default=0.0, description="Cold load duration in milliseconds."
    )
    last_inference_latency_ms: float = Field(
        ge=0.0, default=0.0, description="Latency of most recent inference in milliseconds."
    )
    last_first_token_latency_ms: float = Field(
        ge=0.0, default=0.0, description="Latency to first token of most recent inference."
    )
    resident_ram_delta_bytes: int = Field(
        ge=0, default=0, description="Measured resident RAM delta after loading."
    )
