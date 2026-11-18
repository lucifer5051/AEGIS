"""SAGE Model Gateway -- OpenAI & OpenAI-Compatible Provider Adapter.

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
    ModelContextLengthExceededError,
    ModelGatewayError,
    ModelProviderUnavailableError,
    ModelRateLimitError,
    ModelTimeoutError,
)


class OpenAIProviderAdapter(BaseModelProvider):
    """Adapter for OpenAI API and OpenAI-compatible inference servers (LM Studio, LocalAI, vLLM)."""

    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        provider_id: str = "openai",
        default_model: str = "gpt-4o",
        is_local: bool = False,
        http_client: Optional[Callable[[str, Optional[Dict[str, Any]], Dict[str, str], float], Dict[str, Any]]] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        p_type = ProviderType.LOCAL if is_local else ProviderType.CLOUD
        super().__init__(
            provider_id=provider_id,
            provider_type=p_type,
            api_key=api_key,
            base_url=(base_url or self.DEFAULT_BASE_URL).rstrip("/"),
            timeout_seconds=timeout_seconds,
        )
        self.default_model = default_model
        self.is_local = is_local
        self._http_client = http_client

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key or 'local'}",
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
                raise ModelAuthenticationError(f"Invalid API key: {err_body}", provider_id=self.provider_id) from exc
            elif exc.code == 403:
                raise ModelAuthorizationError(f"Forbidden: {err_body}", provider_id=self.provider_id) from exc
            elif exc.code == 429:
                raise ModelRateLimitError(f"Rate limited: {err_body}", provider_id=self.provider_id) from exc
            elif "context_length_exceeded" in err_body or exc.code == 400:
                raise ModelContextLengthExceededError(f"Context length error: {err_body}", provider_id=self.provider_id) from exc
            elif exc.code in (502, 503, 504):
                raise ModelProviderUnavailableError(f"Provider unavailable ({exc.code}): {err_body}", provider_id=self.provider_id) from exc
            raise ModelGatewayError(f"OpenAI error ({exc.code}): {err_body}", provider_id=self.provider_id) from exc
        except urllib.error.URLError as exc:
            raise ModelProviderUnavailableError(f"Connection failed: {exc}", provider_id=self.provider_id) from exc
        except TimeoutError as exc:
            raise ModelTimeoutError(f"Request timed out after {self.timeout_seconds}s", provider_id=self.provider_id) from exc

    def list_models(self) -> List[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id="gpt-4o",
                provider_id=self.provider_id,
                display_name="GPT-4o",
                provider_type=self.provider_type,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    vision=True,
                    reasoning=True,
                    context_window=128000,
                    max_output_tokens=4096,
                ),
            ),
            ModelDescriptor(
                model_id="gpt-4o-mini",
                provider_id=self.provider_id,
                display_name="GPT-4o Mini",
                provider_type=self.provider_type,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    vision=True,
                    reasoning=False,
                    context_window=128000,
                    max_output_tokens=4096,
                ),
            ),
            ModelDescriptor(
                model_id=self.default_model,
                provider_id=self.provider_id,
                display_name=f"{self.provider_id}: {self.default_model}",
                provider_type=self.provider_type,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    context_window=32768,
                ),
            ),
        ]

    def generate(self, request: ModelRequest) -> ModelResponse:
        model = request.model_id or self.default_model
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_tokens,
        }
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences
        if request.response_format:
            payload["response_format"] = request.response_format

        start = time.perf_counter()
        data = self._post("/chat/completions", payload)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        choices = data.get("choices", [{}])
        choice = choices[0] if choices else {}
        message = choice.get("message", {})
        content = message.get("content", "")
        finish_reason = choice.get("finish_reason", "stop")

        raw_usage = data.get("usage", {})
        usage = ModelUsage(
            prompt_tokens=raw_usage.get("prompt_tokens", 0),
            completion_tokens=raw_usage.get("completion_tokens", 0),
            total_tokens=raw_usage.get("total_tokens", 0),
        )

        return ModelResponse(
            content=content,
            model_id=data.get("model", model),
            provider_id=self.provider_id,
            request_id=request.request_id,
            finish_reason=finish_reason,
            usage=usage,
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
                error_message="Missing required API key",
            )
        try:
            # Send minimal validation request
            self.generate(
                ModelRequest(
                    prompt="ping",
                    max_tokens=2,
                    model_id=self.default_model,
                )
            )
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
