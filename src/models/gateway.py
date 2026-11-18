"""SAGE Model Gateway -- Central Model Gateway Subsystem.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterator, List, Optional

from src.config.policy_models import ResolvedConfiguration, SecurityPolicy
from src.models.adapters.base import BaseModelProvider
from src.models.credentials import ModelCredentialStore
from src.models.domain import (
    ModelDescriptor,
    ModelRequest,
    ModelResponse,
    ModelStreamChunk,
    ProviderHealth,
    RoutingDecision,
)
from src.models.errors import (
    ModelAuthenticationError,
    ModelAuthorizationError,
    ModelContextLengthExceededError,
    ModelGatewayError,
    ModelPolicyDeniedError,
    ModelProviderUnavailableError,
    ModelRateLimitError,
    ModelTimeoutError,
    redact_secrets_from_text,
)
from src.models.registry import ModelRegistry
from src.models.router import ModelRouter
from src.trust.audit import _check_no_secrets
from src.trust.authority import TrustAuthority
from src.trust.policy import PolicyDecision

logger = logging.getLogger(__name__)


class ModelGateway:
    """Central unified Model Gateway coordinating routing, execution, security, and credentials."""

    def __init__(
        self,
        registry: Optional[ModelRegistry] = None,
        router: Optional[ModelRouter] = None,
        credential_store: Optional[ModelCredentialStore] = None,
        config_manager: Optional[Any] = None,
        trust_authority: Optional[TrustAuthority] = None,
        max_retries: int = 2,
    ) -> None:
        self.registry = registry or ModelRegistry()
        self.router = router or ModelRouter(self.registry)
        self.credential_store = credential_store
        self.config_manager = config_manager
        self.trust_authority = trust_authority
        self.max_retries = max_retries

        # Telemetry accounting
        self._telemetry: Dict[str, Any] = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "fallback_events": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "provider_counts": {},
        }

    # =========================================================================
    # CORE INVOCATION APIS
    # =========================================================================

    def generate(
        self,
        request: ModelRequest,
        allow_fallback: bool = False,
    ) -> ModelResponse:
        """Route and execute a model text generation request."""
        self._telemetry["total_requests"] += 1

        # 1. Resolve Effective Configuration & Security Policy
        effective_config, security_policy = self._resolve_config_and_policy(request)

        # 2. TrustCore Pre-Invocation Check
        if self.trust_authority is not None:
            decision = self.trust_authority._policy.evaluate(
                "model.generate",
                project_id=request.project_id,
            )
            if decision.decision == PolicyDecision.DENY:
                self._telemetry["failed_requests"] += 1
                raise ModelPolicyDeniedError(
                    f"TrustCore policy denied model generation: {decision.rationale}"
                )

        # 3. Intelligent Model Routing
        decision = self.router.route(
            request=request,
            effective_config=effective_config,
            security_policy=security_policy,
            allow_fallback=allow_fallback,
        )

        if decision.fallback_used:
            self._telemetry["fallback_events"] += 1

        # 4. Acquire Provider & Apply Credential
        provider = self.registry.get_provider(decision.selected_provider_id)
        if not provider:
            self._telemetry["failed_requests"] += 1
            raise ModelProviderUnavailableError(
                f"Resolved provider '{decision.selected_provider_id}' is not loaded.",
                provider_id=decision.selected_provider_id,
                model_id=decision.selected_model_id,
            )

        self._ensure_provider_credentials(provider)

        # 5. Targeted Request Execution with Bounded Retry
        attempt = 0
        last_error: Optional[Exception] = None
        current_request = request.model_copy(update={"model_id": decision.selected_model_id})

        while attempt <= self.max_retries:
            try:
                response = provider.generate(current_request)
                self.registry.record_success(provider.provider_id)

                # Record usage telemetry
                self._telemetry["successful_requests"] += 1
                self._telemetry["total_prompt_tokens"] += response.usage.prompt_tokens
                self._telemetry["total_completion_tokens"] += response.usage.completion_tokens
                prov_count = self._telemetry["provider_counts"].get(provider.provider_id, 0) + 1
                self._telemetry["provider_counts"][provider.provider_id] = prov_count

                if decision.fallback_used:
                    response = response.model_copy(update={"is_fallback": True})
                return response

            except (ModelAuthenticationError, ModelAuthorizationError, ModelPolicyDeniedError, ModelContextLengthExceededError) as exc:
                # Non-retryable errors fail immediately
                self.registry.record_failure(provider.provider_id)
                self._telemetry["failed_requests"] += 1
                raise exc

            except (ModelRateLimitError, ModelProviderUnavailableError, ModelTimeoutError, ModelGatewayError, Exception) as exc:
                last_error = exc
                self.registry.record_failure(provider.provider_id)
                attempt += 1
                if attempt <= self.max_retries:
                    # Exponential backoff
                    sleep_time = 0.5 * (2 ** (attempt - 1))
                    time.sleep(sleep_time)
                    logger.warning(
                        "Retrying model execution on provider %s (attempt %d/%d) after error: %s",
                        provider.provider_id,
                        attempt,
                        self.max_retries,
                        redact_secrets_from_text(str(exc)),
                    )

        if allow_fallback and not decision.fallback_used:
            try:
                from src.config.policy_models import NetworkMode
                local_only = False
                if security_policy and security_policy.network and security_policy.network.mode == NetworkMode.LOCAL_ONLY:
                    local_only = True
                fallback_dec = self.router._select_fallback(request, local_only=local_only, failed_provider_id=provider.provider_id)
                fallback_prov = self.registry.get_provider(fallback_dec.selected_provider_id)
                if fallback_prov:
                    self._ensure_provider_credentials(fallback_prov)
                    fb_req = request.model_copy(update={"model_id": fallback_dec.selected_model_id})
                    fb_resp = fallback_prov.generate(fb_req)
                    self.registry.record_success(fallback_prov.provider_id)
                    self._telemetry["successful_requests"] += 1
                    self._telemetry["fallback_events"] += 1
                    return fb_resp.model_copy(update={"is_fallback": True})
            except Exception as fb_exc:
                logger.warning("Fallback attempt failed: %s", fb_exc)

        self._telemetry["failed_requests"] += 1
        raise ModelGatewayError(
            f"Execution failed after {self.max_retries + 1} attempts on provider '{provider.provider_id}': {last_error}",
            provider_id=provider.provider_id,
            model_id=decision.selected_model_id,
        ) from last_error

    def generate_stream(
        self,
        request: ModelRequest,
        allow_fallback: bool = False,
    ) -> Iterator[ModelStreamChunk]:
        """Stream generated tokens iteratively from routed provider."""
        self._telemetry["total_requests"] += 1
        effective_config, security_policy = self._resolve_config_and_policy(request)

        if self.trust_authority is not None:
            decision = self.trust_authority._policy.evaluate(
                "model.generate",
                project_id=request.project_id,
            )
            if decision.decision == PolicyDecision.DENY:
                self._telemetry["failed_requests"] += 1
                raise ModelPolicyDeniedError(
                    f"TrustCore policy denied model generation: {decision.rationale}"
                )

        decision = self.router.route(
            request=request,
            effective_config=effective_config,
            security_policy=security_policy,
            allow_fallback=allow_fallback,
        )

        provider = self.registry.get_provider(decision.selected_provider_id)
        if not provider:
            raise ModelProviderUnavailableError(f"Provider '{decision.selected_provider_id}' not loaded.")

        self._ensure_provider_credentials(provider)
        current_request = request.model_copy(update={"model_id": decision.selected_model_id, "stream": True})

        try:
            for chunk in provider.generate_stream(current_request):
                yield chunk
            self.registry.record_success(provider.provider_id)
            self._telemetry["successful_requests"] += 1
        except Exception as exc:
            self.registry.record_failure(provider.provider_id)
            self._telemetry["failed_requests"] += 1
            raise exc

    # =========================================================================
    # MANAGEMENT & DISCOVERY
    # =========================================================================

    def list_models(self, local_only: bool = False) -> List[ModelDescriptor]:
        """Discover and list all registered models."""
        return self.registry.list_models(local_only=local_only)

    def get_provider_health(self, provider_id: str) -> ProviderHealth:
        """Inspect health status for a specific model provider."""
        return self.registry.get_provider_health(provider_id)

    def get_telemetry(self) -> Dict[str, Any]:
        """Return structured execution and token usage telemetry."""
        return dict(self._telemetry)

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _resolve_config_and_policy(
        self, request: ModelRequest
    ) -> tuple[Optional[ResolvedConfiguration], Optional[SecurityPolicy]]:
        effective_config = None
        security_policy = None

        if self.config_manager is not None:
            try:
                effective_config = self.config_manager.get_effective_config()
                if effective_config and effective_config.security_policy:
                    security_policy = effective_config.security_policy
            except Exception as exc:
                logger.warning("Could not resolve config for model request: %s", exc)

        return effective_config, security_policy

    def _ensure_provider_credentials(self, provider: BaseModelProvider) -> None:
        """Inject encrypted API credentials into provider adapter if not already present."""
        if not provider.api_key and self.credential_store:
            stored_key = self.credential_store.get_credential(provider.provider_id)
            if stored_key:
                provider.api_key = stored_key
