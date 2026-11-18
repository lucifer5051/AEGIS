"""SAGE Unified Model Gateway -- Domain Models & Typed Schemas.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@unique
class ProviderType(str, Enum):
    """Categorization of model execution provider."""

    LOCAL = "LOCAL"
    CLOUD = "CLOUD"


@unique
class TaskCategory(str, Enum):
    """Functional task categories for model routing."""

    GENERAL_CHAT = "GENERAL_CHAT"
    REASONING = "REASONING"
    CODING = "CODING"
    RESEARCH = "RESEARCH"
    SUMMARIZATION = "SUMMARIZATION"
    CLASSIFICATION = "CLASSIFICATION"
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"
    VISION = "VISION"


@unique
class ProviderHealthStatus(str, Enum):
    """Operational health status of a provider."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class ModelCapabilities(BaseModel):
    """Declared or discovered capabilities of an AI model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text_generation: bool = True
    streaming: bool = True
    tool_calling: bool = False
    structured_output: bool = False
    vision: bool = False
    reasoning: bool = False
    context_window: int = 4096
    max_output_tokens: int = 2048


class ModelDescriptor(BaseModel):
    """Metadata and capability specification for a registered model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    provider_id: str
    display_name: str
    provider_type: ProviderType
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    is_available: bool = True
    cost_per_million_input_tokens: float = 0.0
    cost_per_million_output_tokens: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProviderHealth(BaseModel):
    """Health check outcome for a model provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    status: ProviderHealthStatus
    latency_ms: float = 0.0
    error_message: Optional[str] = None
    last_checked_at: str = Field(default_factory=_utc_now_iso)


class ModelUsage(BaseModel):
    """Token consumption accounting for a model invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ModelRequest(BaseModel):
    """Normalized input request for text or structured generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    system_prompt: Optional[str] = None
    task_category: TaskCategory = TaskCategory.GENERAL_CHAT
    model_id: Optional[str] = None  # Explicit model request (e.g. "qwen35-4b-q4km" or "claude-3-5-sonnet")
    model_alias: Optional[str] = None  # e.g. "fast", "balanced", "reasoning", "coding"
    max_tokens: int = 1024
    temperature: float = 0.0
    top_p: float = 1.0
    stop_sequences: List[str] = Field(default_factory=list)
    response_format: Optional[Dict[str, Any]] = None  # e.g. {"type": "json_object", "schema": ...}
    stream: bool = False
    required_capabilities: List[str] = Field(default_factory=list)
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    actor: str = "user"
    project_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    """Normalized response from any model provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    model_id: str
    provider_id: str
    request_id: str
    finish_reason: str = "stop"
    usage: ModelUsage = Field(default_factory=ModelUsage)
    execution_duration_ms: float = 0.0
    first_token_duration_ms: float = 0.0
    is_fallback: bool = False
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ModelStreamChunk(BaseModel):
    """Single incremental token chunk during streaming generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_delta: str
    chunk_index: int
    is_final: bool = False
    finish_reason: Optional[str] = None
    usage: Optional[ModelUsage] = None


class RoutingDecision(BaseModel):
    """Explainable routing decision produced by ModelRouter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_provider_id: str
    selected_model_id: str
    reason: str
    task_category: TaskCategory
    fallback_used: bool = False
    alternative_candidates: List[str] = Field(default_factory=list)
    latency_preference: str = "balanced"
    is_local: bool = True
