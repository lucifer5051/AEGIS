"""SAGE Model Gateway -- Intelligent Model Router.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.config.policy_models import NetworkMode, ResolvedConfiguration, SecurityPolicy
from src.models.domain import (
    ModelDescriptor,
    ModelRequest,
    ProviderHealthStatus,
    ProviderType,
    RoutingDecision,
    TaskCategory,
)
from src.models.errors import (
    ModelCapabilityUnsupportedError,
    ModelPolicyDeniedError,
    ModelProviderUnavailableError,
    ModelRoutingError,
)
from src.models.registry import ModelRegistry

logger = logging.getLogger(__name__)


class ModelRouter:
    """Intelligently routes model requests based on task category, capabilities, and security policies."""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

        # Default task category mappings to model aliases
        self._task_aliases: Dict[TaskCategory, str] = {
            TaskCategory.GENERAL_CHAT: "balanced",
            TaskCategory.REASONING: "reasoning",
            TaskCategory.CODING: "coding",
            TaskCategory.RESEARCH: "reasoning",
            TaskCategory.SUMMARIZATION: "fast",
            TaskCategory.CLASSIFICATION: "fast",
            TaskCategory.STRUCTURED_OUTPUT: "balanced",
            TaskCategory.VISION: "vision",
        }

    def route(
        self,
        request: ModelRequest,
        effective_config: Optional[ResolvedConfiguration] = None,
        security_policy: Optional[SecurityPolicy] = None,
        allow_fallback: bool = False,
    ) -> RoutingDecision:
        """Deterministically select the appropriate provider and model for a request."""
        # 1. Enforce Network & Local-Only Security Boundaries
        local_only = False
        if security_policy and security_policy.network:
            if security_policy.network.mode == NetworkMode.LOCAL_ONLY:
                local_only = True

        # Check effective config policy overrides
        if effective_config and effective_config.security_policy and effective_config.security_policy.network:
            if effective_config.security_policy.network.mode == NetworkMode.LOCAL_ONLY:
                local_only = True

        # 2. Check Explicit User Model Selection
        explicit_target = request.model_id or request.model_alias
        if explicit_target:
            resolved_id = self._registry.resolve_alias(explicit_target)
            desc = self._registry.get_model(resolved_id)
            if not desc:
                raise ModelRoutingError(
                    f"Requested model or alias '{explicit_target}' (resolved to '{resolved_id}') not found in registry."
                )

            # Security Policy Boundary Enforcement
            if local_only and desc.provider_type == ProviderType.CLOUD:
                raise ModelPolicyDeniedError(
                    f"Model '{desc.model_id}' is a cloud model provided by '{desc.provider_id}', "
                    "which is strictly prohibited under the current LOCAL_ONLY network security policy.",
                    provider_id=desc.provider_id,
                    model_id=desc.model_id,
                )

            # Validate Required Capabilities
            self._validate_capabilities(desc, request.required_capabilities)

            # Provider Health and Circuit Breaker Check
            if self._registry.is_circuit_open(desc.provider_id):
                if not allow_fallback:
                    raise ModelProviderUnavailableError(
                        f"Provider '{desc.provider_id}' circuit breaker is open (consecutive failures).",
                        provider_id=desc.provider_id,
                        model_id=desc.model_id,
                    )
                # Fallback to local candidate if allowed
                return self._select_fallback(request, local_only=local_only, failed_provider_id=desc.provider_id)

            return RoutingDecision(
                selected_provider_id=desc.provider_id,
                selected_model_id=desc.model_id,
                reason=f"Explicit selection '{explicit_target}' satisfied all capability and policy constraints.",
                task_category=request.task_category,
                fallback_used=False,
                is_local=(desc.provider_type == ProviderType.LOCAL),
            )

        # 3. Task Category & Profile-Driven Selection
        preferred_alias = self._task_aliases.get(request.task_category, "balanced")
        target_model_id = self._registry.resolve_alias(preferred_alias)
        candidate = self._registry.get_model(target_model_id)

        # If preferred model violates local-only, pick a local alternative
        if candidate and candidate.provider_type == ProviderType.CLOUD and local_only:
            candidate = None

        if candidate:
            try:
                self._validate_capabilities(candidate, request.required_capabilities)
                if not self._registry.is_circuit_open(candidate.provider_id):
                    return RoutingDecision(
                        selected_provider_id=candidate.provider_id,
                        selected_model_id=candidate.model_id,
                        reason=f"Selected task-category default for {request.task_category.value} ({preferred_alias}).",
                        task_category=request.task_category,
                        fallback_used=False,
                        is_local=(candidate.provider_type == ProviderType.LOCAL),
                    )
            except ModelCapabilityUnsupportedError:
                candidate = None

        # 4. Search Available Candidates
        available_models = self._registry.list_models(
            required_capabilities=request.required_capabilities,
            local_only=local_only,
        )

        healthy_candidates = [
            m for m in available_models
            if not self._registry.is_circuit_open(m.provider_id)
        ]

        if not healthy_candidates:
            if available_models and not allow_fallback:
                first = available_models[0]
                raise ModelProviderUnavailableError(
                    f"No healthy providers available for model requirements. Candidate '{first.model_id}' provider circuit is open.",
                    provider_id=first.provider_id,
                )
            raise ModelRoutingError(
                f"No compatible, healthy, and authorized models found for task '{request.task_category.value}' (local_only={local_only})."
            )

        # Choose best candidate (prefer local models for data sovereignty)
        local_candidates = [m for m in healthy_candidates if m.provider_type == ProviderType.LOCAL]
        chosen = local_candidates[0] if local_candidates else healthy_candidates[0]

        return RoutingDecision(
            selected_provider_id=chosen.provider_id,
            selected_model_id=chosen.model_id,
            reason=f"Selected optimal compatible model '{chosen.model_id}' from provider '{chosen.provider_id}'.",
            task_category=request.task_category,
            fallback_used=False,
            alternative_candidates=[m.model_id for m in healthy_candidates if m.model_id != chosen.model_id],
            is_local=(chosen.provider_type == ProviderType.LOCAL),
        )

    def _select_fallback(
        self,
        request: ModelRequest,
        local_only: bool,
        failed_provider_id: str,
    ) -> RoutingDecision:
        """Select a resilient fallback model when preferred provider is unavailable."""
        candidates = self._registry.list_models(
            required_capabilities=request.required_capabilities,
            local_only=local_only,
        )
        healthy = [m for m in candidates if m.provider_id != failed_provider_id and not self._registry.is_circuit_open(m.provider_id)]
        if not healthy:
            raise ModelProviderUnavailableError(
                f"Preferred provider '{failed_provider_id}' is unavailable and no viable fallback candidate exists.",
                provider_id=failed_provider_id,
            )

        chosen = healthy[0]
        return RoutingDecision(
            selected_provider_id=chosen.provider_id,
            selected_model_id=chosen.model_id,
            reason=f"Fallback from unavailable provider '{failed_provider_id}' to '{chosen.provider_id}'.",
            task_category=request.task_category,
            fallback_used=True,
            is_local=(chosen.provider_type == ProviderType.LOCAL),
        )

    def _validate_capabilities(self, descriptor: ModelDescriptor, required: List[str]) -> None:
        """Verify that descriptor supports all required capabilities."""
        caps = descriptor.capabilities
        missing = [cap for cap in required if not getattr(caps, cap, False)]
        if missing:
            raise ModelCapabilityUnsupportedError(
                f"Model '{descriptor.model_id}' does not support required capabilities: {missing}.",
                provider_id=descriptor.provider_id,
                model_id=descriptor.model_id,
            )
