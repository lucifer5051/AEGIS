"""SAGE Local Reasoning Subsystem.

Canonical Reference:
    docs/specifications/AEGIS_MODEL_RESOURCE_POLICY.md
    schemas/model-manifests/qwen35-4b-q4km.json

Public API for:
- Provider-neutral local reasoning abstraction (LocalReasoningProvider)
- llama-cpp-python implementation (LlamaCppReasoningProvider)
- Typed requests, responses, and metrics (ReasoningRequest, ReasoningResponse, ReasoningMetrics)
- Runtime and model artifact descriptors (LLAMACPP_CPU_RUNTIME, QWEN35_4B_MODEL,
  SKILL_LOCAL_REASONING)
- Fail-closed error hierarchy (ReasoningError, ReasoningIntegrityError,
  ReasoningAdmissionError, etc.)
- Empirical hardware qualification (qualify_local_reasoning_provider)
"""

from __future__ import annotations

from typing import Optional

from src.model_policy.concurrency_manager import SkillConcurrencyManager
from src.model_policy.residency_manager import ModelResidencyManager
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.errors import (
    ReasoningAdmissionError,
    ReasoningError,
    ReasoningExecutionError,
    ReasoningIntegrityError,
    ReasoningResidencyError,
    ReasoningStorageError,
)
from src.reasoning.models import (
    ReasoningMetrics,
    ReasoningProviderStatus,
    ReasoningRequest,
    ReasoningResponse,
)
from src.reasoning.provider import LlamaCppReasoningProvider, get_default_local_model_dir
from src.reasoning.qualification import (
    get_default_qualification_dir,
    qualify_local_reasoning_provider,
)
from src.reasoning.runtime import (
    LLAMACPP_CPU_RUNTIME,
    QWEN35_4B_EXACT_BYTES,
    QWEN35_4B_EXPECTED_SHA256,
    QWEN35_4B_FILENAME,
    QWEN35_4B_MODEL,
    SKILL_LOCAL_REASONING,
    register_reasoning_models,
)

__all__ = [
    # Base Abstraction & Provider
    "LocalReasoningProvider",
    "LlamaCppReasoningProvider",
    "get_default_local_model_dir",
    "build_default_reasoning_provider",
    # Models & Enums
    "ReasoningRequest",
    "ReasoningResponse",
    "ReasoningProviderStatus",
    "ReasoningMetrics",
    # Errors
    "ReasoningError",
    "ReasoningStorageError",
    "ReasoningIntegrityError",
    "ReasoningAdmissionError",
    "ReasoningExecutionError",
    "ReasoningResidencyError",
    # Runtime & Model Descriptors
    "LLAMACPP_CPU_RUNTIME",
    "QWEN35_4B_MODEL",
    "SKILL_LOCAL_REASONING",
    "QWEN35_4B_FILENAME",
    "QWEN35_4B_EXACT_BYTES",
    "QWEN35_4B_EXPECTED_SHA256",
    "register_reasoning_models",
    # Qualification
    "qualify_local_reasoning_provider",
    "get_default_qualification_dir",
]


def build_default_reasoning_provider(
    concurrency_manager: Optional[SkillConcurrencyManager] = None,
    residency_manager: Optional[ModelResidencyManager] = None,
    n_ctx: int = 4096,
) -> LlamaCppReasoningProvider:
    """Build and return canonical default LlamaCppReasoningProvider instance."""
    return LlamaCppReasoningProvider(
        concurrency_manager=concurrency_manager,
        residency_manager=residency_manager,
        n_ctx=n_ctx,
    )
