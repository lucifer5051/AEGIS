"""SAGE Model Gateway -- Google Gemini Provider Adapter.

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


class GeminiProviderAdapter(BaseModelProvider):
    """Adapter communicating with Google Gemini REST API."""

    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        provider_id: str = "gemini",
        default_model: str = "gemini-1.5-pro",
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
        headers = {"Content-Type": "application/json"}
        # Pass key via query param or header
        url = f"{self.base_url}{endpoint}?key={self.api_key or ''}"
        if self._http_client:
            return self._http_client(endpoint, payload, headers, self.timeout_seconds)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 400 and "API_KEY_INVALID" in err_body or exc.code == 401:
                raise ModelAuthenticationError(f"Gemini API key invalid: {err_body}", provider_id=self.provider_id) from exc
            elif exc.code == 403:
                raise ModelAuthorizationError(f"Gemini forbidden: {err_body}", provider_id=self.provider_id) from exc
            elif exc.code == 429:
                raise ModelRateLimitError(f"Gemini rate limited: {err_body}", provider_id=self.provider_id) from exc
            elif exc.code in (502, 503, 504):
                raise ModelProviderUnavailableError(f"Gemini unavailable ({exc.code}): {err_body}", provider_id=self.provider_id) from exc
            raise ModelGatewayError(f"Gemini error ({exc.code}): {err_body}", provider_id=self.provider_id) from exc
        except urllib.error.URLError as exc:
            raise ModelProviderUnavailableError(f"Gemini connection failed: {exc}", provider_id=self.provider_id) from exc
        except TimeoutError as exc:
            raise ModelTimeoutError(f"Gemini timed out after {self.timeout_seconds}s", provider_id=self.provider_id) from exc

    def list_models(self) -> List[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id="gemini-1.5-pro",
                provider_id=self.provider_id,
                display_name="Gemini 1.5 Pro",
                provider_type=ProviderType.CLOUD,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    vision=True,
                    reasoning=True,
                    context_window=1000000,
                    max_output_tokens=8192,
                ),
            ),
            ModelDescriptor(
                model_id="gemini-1.5-flash",
                provider_id=self.provider_id,
                display_name="Gemini 1.5 Flash",
                provider_type=ProviderType.CLOUD,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    vision=True,
                    reasoning=False,
                    context_window=1000000,
                    max_output_tokens=8192,
                ),
            ),
        ]

    def generate(self, request: ModelRequest) -> ModelResponse:
        model = request.model_id or self.default_model
        parts = [{"text": request.prompt}]

        payload: Dict[str, Any] = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": request.temperature,
                "topP": request.top_p,
                "maxOutputTokens": request.max_tokens,
            },
        }
        if request.system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": request.system_prompt}]}
        if request.stop_sequences:
            payload["generationConfig"]["stopSequences"] = request.stop_sequences

        start = time.perf_counter()
        data = self._post(f"/models/{model}:generateContent", payload)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        candidates = data.get("candidates", [{}])
        candidate = candidates[0] if candidates else {}
        parts_resp = candidate.get("content", {}).get("parts", [])
        content = "".join(p.get("text", "") for p in parts_resp)

        usage_meta = data.get("usageMetadata", {})
        prompt_tokens = usage_meta.get("promptTokenCount", 0)
        completion_tokens = usage_meta.get("candidatesTokenCount", 0)

        return ModelResponse(
            content=content,
            model_id=model,
            provider_id=self.provider_id,
            request_id=request.request_id,
            finish_reason=candidate.get("finishReason", "STOP"),
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
                error_message="Missing required Gemini API key",
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
