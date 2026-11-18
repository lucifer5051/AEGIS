"""SAGE Model Gateway -- Llama.cpp Local Reasoning Provider Adapter.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13
"""

from __future__ import annotations

import time
from typing import Iterator, List, Optional

from src.models.adapters.base import BaseModelProvider
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
)
from src.models.errors import ModelProviderUnavailableError
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import ReasoningRequest


class LlamaCppProviderAdapter(BaseModelProvider):
    """Adapter bridging SAGE's local reasoning provider (llama.cpp) into ModelGateway."""

    def __init__(
        self,
        reasoning_provider: LocalReasoningProvider,
        provider_id: str = "llamacpp",
        model_id: str = "qwen35-4b-q4km",
    ) -> None:
        super().__init__(provider_id=provider_id, provider_type=ProviderType.LOCAL)
        self._provider = reasoning_provider
        self._default_model_id = model_id

    def list_models(self) -> List[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id=self._default_model_id,
                provider_id=self.provider_id,
                display_name="Qwen 2.5 4B Instruct (Q4_K_M GGUF)",
                provider_type=ProviderType.LOCAL,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    vision=False,
                    reasoning=True,
                    context_window=4096,
                    max_output_tokens=2048,
                ),
                is_available=self._provider.is_ready(),
            )
        ]

    def generate(self, request: ModelRequest) -> ModelResponse:
        if not self._provider.is_ready():
            raise ModelProviderUnavailableError(
                f"Local reasoning provider '{self.provider_id}' is not ready or resident.",
                provider_id=self.provider_id,
                model_id=self._default_model_id,
            )

        start = time.perf_counter()
        req = ReasoningRequest(
            prompt=request.prompt,
            system_prompt=request.system_prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            stop_sequences=request.stop_sequences,
            response_format=request.response_format,
            request_id=request.request_id,
            task_id=request.metadata.get("task_id"),
            step_id=request.metadata.get("step_id"),
        )
        res = self._provider.reason(req)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        return ModelResponse(
            content=res.content,
            model_id=res.model_id or self._default_model_id,
            provider_id=self.provider_id,
            request_id=request.request_id,
            finish_reason=res.finish_reason,
            usage=ModelUsage(
                prompt_tokens=res.prompt_tokens,
                completion_tokens=res.completion_tokens,
                total_tokens=res.total_tokens,
            ),
            execution_duration_ms=elapsed_ms,
            first_token_duration_ms=res.first_token_duration_ms,
        )

    def generate_stream(self, request: ModelRequest) -> Iterator[ModelStreamChunk]:
        resp = self.generate(request)
        words = resp.content.split(" ")
        for i, word in enumerate(words):
            chunk_text = word if i == 0 else " " + word
            is_final = (i == len(words) - 1)
            yield ModelStreamChunk(
                content_delta=chunk_text,
                chunk_index=i,
                is_final=is_final,
                finish_reason=resp.finish_reason if is_final else None,
                usage=resp.usage if is_final else None,
            )

    def check_health(self) -> ProviderHealth:
        start = time.perf_counter()
        ready = self._provider.is_ready()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return ProviderHealth(
            provider_id=self.provider_id,
            status=ProviderHealthStatus.HEALTHY if ready else ProviderHealthStatus.UNAVAILABLE,
            latency_ms=elapsed_ms,
            error_message=None if ready else "Provider uninitialized or model unloaded",
        )
