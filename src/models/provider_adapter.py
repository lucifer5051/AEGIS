"""SAGE Model Gateway -- Gateway Reasoning Provider Adapter.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13

Exposes a LocalReasoningProvider interface backed by the ModelGateway so that
Supervisor, StructuredPlanner, and TaskSynthesizer can transparently route through
the central Model Gateway without architectural fragmentation or breaking backward compatibility.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.models.domain import ModelRequest, TaskCategory
from src.models.gateway import ModelGateway
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import (
    ReasoningMetrics,
    ReasoningProviderStatus,
    ReasoningRequest,
    ReasoningResponse,
)

logger = logging.getLogger(__name__)


class GatewayReasoningProvider(LocalReasoningProvider):
    """LocalReasoningProvider implementation powered by the central ModelGateway."""

    def __init__(
        self,
        gateway: ModelGateway,
        preferred_model_or_alias: Optional[str] = "reasoning",
        task_category: TaskCategory = TaskCategory.REASONING,
        provider_id: str = "gateway_provider",
    ) -> None:
        super().__init__(provider_id=provider_id, model_id=preferred_model_or_alias or "default")
        self.gateway = gateway
        self.preferred_model_or_alias = preferred_model_or_alias
        self.task_category = task_category
        self._status = ReasoningProviderStatus.READY

    def initialize(self) -> bool:
        self._status = ReasoningProviderStatus.READY
        return True

    def is_ready(self) -> bool:
        return self._status == ReasoningProviderStatus.READY

    def reason(self, request: ReasoningRequest) -> ReasoningResponse:
        """Route reasoning prompt through ModelGateway."""
        model_req = ModelRequest(
            prompt=request.prompt,
            system_prompt=request.system_prompt,
            task_category=self.task_category,
            model_alias=self.preferred_model_or_alias,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            stop_sequences=request.stop_sequences,
            response_format=request.response_format,
            request_id=request.request_id or "reasoning-request",
            metadata={
                "task_id": request.task_id,
                "step_id": request.step_id,
            },
        )

        resp = self.gateway.generate(model_req, allow_fallback=True)

        return ReasoningResponse(
            content=resp.content,
            finish_reason=resp.finish_reason,
            prompt_tokens=resp.usage.prompt_tokens,
            completion_tokens=resp.usage.completion_tokens,
            total_tokens=resp.usage.total_tokens,
            inference_duration_ms=resp.execution_duration_ms,
            first_token_duration_ms=resp.first_token_duration_ms,
            model_id=resp.model_id,
            provider_id=resp.provider_id,
        )

    def unload(self) -> bool:
        self._status = ReasoningProviderStatus.UNLOADED
        return True

    def verify_integrity(self) -> bool:
        return True

    def get_metrics(self) -> ReasoningMetrics:
        telemetry = self.gateway.get_telemetry()
        return ReasoningMetrics(
            total_inferences=telemetry.get("total_requests", 0),
            successful_inferences=telemetry.get("successful_requests", 0),
            failed_inferences=telemetry.get("failed_requests", 0),
            total_prompt_tokens=telemetry.get("total_prompt_tokens", 0),
            total_completion_tokens=telemetry.get("total_completion_tokens", 0),
        )
