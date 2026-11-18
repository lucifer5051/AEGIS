"""SAGE Model Gateway -- Anthropic Claude Provider Adapter.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterator, List, Optional

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
from src.models.errors import (
    ModelAuthenticationError,
    ModelAuthorizationError,
    ModelGatewayError,
    ModelProviderUnavailableError,
    ModelRateLimitError,
    ModelTimeoutError,
)


class AnthropicProviderAdapter(BaseModelProvider):
    """Adapter communicating with Anthropic's Claude Messages API."""

    DEFAULT_BASE_URL = "https://api.anthropic.com/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        provider_id: str = "anthropic",
        default_model: str = "claude-3-5-sonnet-20241022",
        http_client: Optional[Callable[[str, Optional[Dict[str, Any]], Dict[str, str], float], Dict[str, Any]]] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_type=ProviderType.CLOUD,
            api_key=api_key,
            base_url=(base_url or self.DEFAULT_BASE_URL).rstrip("/"),
            timeout_seconds=timeout_seconds,
        )
        self.default_model = default_model
        self._http_client = http_client

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key or "",
            "anthropic-version": "2023-06-01",
        }
        if self._http_client:
            return self._http_client(endpoint, payload, headers, self.timeout_seconds)

        url = f"{self.base_url}{endpoint}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 401:
                raise ModelAuthenticationError(f"Anthropic key invalid: {err_body}", provider_id=self.provider_id) from exc
            elif exc.code == 403:
                raise ModelAuthorizationError(f"Anthropic forbidden: {err_body}", provider_id=self.provider_id) from exc
            elif exc.code == 429:
                raise ModelRateLimitError(f"Anthropic rate limited: {err_body}", provider_id=self.provider_id) from exc
            elif exc.code in (502, 503, 504):
                raise ModelProviderUnavailableError(f"Anthropic unavailable ({exc.code}): {err_body}", provider_id=self.provider_id) from exc
            raise ModelGatewayError(f"Anthropic error ({exc.code}): {err_body}", provider_id=self.provider_id) from exc
        except urllib.error.URLError as exc:
            raise ModelProviderUnavailableError(f"Connection failed: {exc}", provider_id=self.provider_id) from exc
        except TimeoutError as exc:
            raise ModelTimeoutError(f"Anthropic timed out after {self.timeout_seconds}s", provider_id=self.provider_id) from exc

    def list_models(self) -> List[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id="claude-3-5-sonnet-20241022",
                provider_id=self.provider_id,
                display_name="Claude 3.5 Sonnet",
                provider_type=ProviderType.CLOUD,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    vision=True,
                    reasoning=True,
                    context_window=200000,
                    max_output_tokens=8192,
                ),
            ),
            ModelDescriptor(
                model_id="claude-3-5-haiku-20241022",
                provider_id=self.provider_id,
                display_name="Claude 3.5 Haiku",
                provider_type=ProviderType.CLOUD,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    vision=True,
                    reasoning=False,
                    context_window=200000,
                    max_output_tokens=4096,
                ),
            ),
        ]

    def generate(self, request: ModelRequest) -> ModelResponse:
        model = request.model_id or self.default_model
        messages = [{"role": "user", "content": request.prompt}]

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "top_p": request.top_p,
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt
        if request.stop_sequences:
            payload["stop_sequences"] = request.stop_sequences

        start = time.perf_counter()
        data = self._post("/messages", payload)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        content_blocks = data.get("content", [])
        text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        content = "".join(text_parts)

        raw_usage = data.get("usage", {})
        prompt_tokens = raw_usage.get("input_tokens", 0)
        completion_tokens = raw_usage.get("output_tokens", 0)

        return ModelResponse(
            content=content,
            model_id=data.get("model", model),
            provider_id=self.provider_id,
            request_id=request.request_id,
            finish_reason=data.get("stop_reason", "end_turn"),
            usage=ModelUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            execution_duration_ms=elapsed_ms,
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
        if not self.validate_configuration():
            return ProviderHealth(
                provider_id=self.provider_id,
                status=ProviderHealthStatus.UNAVAILABLE,
                error_message="Missing required Anthropic API key",
            )
        try:
            self.generate(ModelRequest(prompt="ping", max_tokens=2, model_id=self.default_model))
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return ProviderHealth(
                provider_id=self.provider_id,
                status=ProviderHealthStatus.HEALTHY,
                latency_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return ProviderHealth(
                provider_id=self.provider_id,
                status=ProviderHealthStatus.UNAVAILABLE,
                latency_ms=elapsed_ms,
                error_message=str(exc),
            )
