"""SAGE Unified Model Gateway Subsystem.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13
"""

from __future__ import annotations

from src.models.adapters.anthropic import AnthropicProviderAdapter
from src.models.adapters.base import BaseModelProvider
from src.models.adapters.gemini import GeminiProviderAdapter
from src.models.adapters.llamacpp import LlamaCppProviderAdapter
from src.models.adapters.ollama import OllamaProviderAdapter
from src.models.adapters.openai import OpenAIProviderAdapter
from src.models.credentials import ModelCredentialStore
from src.models.domain import (
    ModelCapabilities,
    ModelDescriptor,
    ModelRequest,
    ModelResponse,
    ModelStreamChunk,
    ModelUsage,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderType,
    RoutingDecision,
    TaskCategory,
)
from src.models.errors import (
    ModelAuthenticationError,
    ModelAuthorizationError,
    ModelCapabilityUnsupportedError,
    ModelContextLengthExceededError,
    ModelGatewayError,
    ModelPolicyDeniedError,
    ModelProviderUnavailableError,
    ModelRateLimitError,
    ModelRoutingError,
    ModelTimeoutError,
    redact_secrets_from_text,
)
from src.models.gateway import ModelGateway
from src.models.provider_adapter import GatewayReasoningProvider
from src.models.registry import ModelRegistry
from src.models.router import ModelRouter

__all__ = [
    "ModelGateway",
    "ModelRegistry",
    "ModelRouter",
    "ModelCredentialStore",
    "GatewayReasoningProvider",
    "BaseModelProvider",
    "LlamaCppProviderAdapter",
    "OllamaProviderAdapter",
    "OpenAIProviderAdapter",
    "AnthropicProviderAdapter",
    "GeminiProviderAdapter",
    "ModelRequest",
    "ModelResponse",
    "ModelStreamChunk",
    "ModelUsage",
    "ModelCapabilities",
    "ModelDescriptor",
    "ProviderHealth",
    "RoutingDecision",
    "ProviderType",
    "TaskCategory",
    "ProviderHealthStatus",
    "ModelGatewayError",
    "ModelAuthenticationError",
    "ModelAuthorizationError",
    "ModelRateLimitError",
    "ModelProviderUnavailableError",
    "ModelTimeoutError",
    "ModelContextLengthExceededError",
    "ModelCapabilityUnsupportedError",
    "ModelPolicyDeniedError",
    "ModelRoutingError",
    "redact_secrets_from_text",
]
