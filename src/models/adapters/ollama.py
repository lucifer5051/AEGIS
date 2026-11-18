"""SAGE Model Gateway -- Ollama Local REST API Adapter.

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
    ModelGatewayError,
    ModelProviderUnavailableError,
    ModelTimeoutError,
)


class OllamaProviderAdapter(BaseModelProvider):
    """Adapter communicating with local Ollama daemon via REST endpoints."""

    DEFAULT_BASE_URL = "http://127.0.0.1:11434"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        provider_id: str = "ollama",
        default_model: str = "llama3:8b",
        http_client: Optional[Callable[[str, Optional[Dict[str, Any]], float], Dict[str, Any]]] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_type=ProviderType.LOCAL,
            base_url=base_url.rstrip("/"),
            timeout_seconds=timeout_seconds,
        )
        self.default_model = default_model
        self._http_client = http_client

    def _request(self, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self._http_client:
            return self._http_client(endpoint, payload, self.timeout_seconds)

        url = f"{self.base_url}{endpoint}"
        data = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.URLError as exc:
            raise ModelProviderUnavailableError(
                f"Ollama daemon unreachable at {self.base_url}: {exc}",
                provider_id=self.provider_id,
            ) from exc
        except TimeoutError as exc:
            raise ModelTimeoutError(
                f"Ollama request timed out after {self.timeout_seconds}s",
                provider_id=self.provider_id,
            ) from exc

    def list_models(self) -> List[ModelDescriptor]:
        try:
            data = self._request("/api/tags")
            models = []
            for item in data.get("models", []):
                name = item.get("name", "unknown")
                models.append(
                    ModelDescriptor(
                        model_id=name,
                        provider_id=self.provider_id,
                        display_name=f"Ollama: {name}",
                        provider_type=ProviderType.LOCAL,
                        capabilities=ModelCapabilities(
                            text_generation=True,
                            streaming=True,
                            tool_calling="tools" in item.get("details", {}).get("families", []),
                            structured_output=True,
                            vision="clip" in item.get("details", {}).get("families", []),
                            reasoning="deepseek" in name.lower() or "qwq" in name.lower(),
                            context_window=item.get("details", {}).get("context_window", 8192),
                        ),
                        is_available=True,
                    )
                )
            if not models:
                models.append(self._default_descriptor())
            return models
        except Exception:
            return [self._default_descriptor()]

    def _default_descriptor(self) -> ModelDescriptor:
        return ModelDescriptor(
            model_id=self.default_model,
            provider_id=self.provider_id,
            display_name=f"Ollama: {self.default_model}",
            provider_type=ProviderType.LOCAL,
            capabilities=ModelCapabilities(
                text_generation=True,
                streaming=True,
                tool_calling=True,
                structured_output=True,
                context_window=8192,
            ),
            is_available=True,
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        model = request.model_id or self.default_model
        payload = {
            "model": model,
            "prompt": request.prompt,
            "system": request.system_prompt or "",
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "top_p": request.top_p,
                "num_predict": request.max_tokens,
                "stop": request.stop_sequences,
            },
        }
        if request.response_format and request.response_format.get("type") == "json_object":
            payload["format"] = "json"

        start = time.perf_counter()
        data = self._request("/api/generate", payload)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        content = data.get("response", "")
        prompt_tokens = data.get("prompt_eval_count", 0)
        eval_tokens = data.get("eval_count", 0)

        return ModelResponse(
            content=content,
            model_id=model,
            provider_id=self.provider_id,
            request_id=request.request_id,
            finish_reason="stop" if data.get("done") else "length",
            usage=ModelUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=eval_tokens,
                total_tokens=prompt_tokens + eval_tokens,
            ),
            execution_duration_ms=elapsed_ms,
        )

    def generate_stream(self, request: ModelRequest) -> Iterator[ModelStreamChunk]:
        # For simplicity and offline reproducibility, stream yields word-based chunks from generate
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
        try:
            self._request("/api/version")
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
