"""SAGE Model Gateway -- Model & Provider Registry.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from src.models.adapters.base import BaseModelProvider
from src.models.domain import (
    ModelDescriptor,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderType,
)

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Central registry managing model provider adapters, models, capabilities, and aliases."""

    def __init__(self) -> None:
        self._providers: Dict[str, BaseModelProvider] = {}
        self._custom_models: Dict[str, ModelDescriptor] = {}
        self._aliases: Dict[str, str] = {
            "fast": "qwen35-4b-q4km",
            "balanced": "qwen35-4b-q4km",
            "reasoning": "qwen35-4b-q4km",
            "coding": "qwen35-4b-q4km",
            "local": "qwen35-4b-q4km",
            "vision": "gpt-4o",
        }
        self._health_cache: Dict[str, ProviderHealth] = {}
        self._failure_counts: Dict[str, int] = {}
        self._circuit_open_until: Dict[str, float] = {}
        self._circuit_cooldown_seconds: float = 30.0

    def register_provider(self, provider: BaseModelProvider) -> None:
        """Register a model provider adapter."""
        self._providers[provider.provider_id] = provider
        logger.info("Registered model provider: %s (%s)", provider.provider_id, provider.provider_type.value)

    def unregister_provider(self, provider_id: str) -> bool:
        """Unregister a model provider."""
        return self._providers.pop(provider_id, None) is not None

    def get_provider(self, provider_id: str) -> Optional[BaseModelProvider]:
        """Fetch a registered provider by ID."""
        return self._providers.get(provider_id)

    def list_providers(self) -> List[BaseModelProvider]:
        """List all registered providers."""
        return list(self._providers.values())

    def set_alias(self, alias: str, model_id: str) -> None:
        """Set or update a model alias (e.g. 'coding' -> 'claude-3-5-sonnet-20241022')."""
        self._aliases[alias.lower().strip()] = model_id.strip()

    def resolve_alias(self, alias_or_name: str) -> str:
        """Resolve a model alias to its target model_id, returning input if not an alias."""
        norm = alias_or_name.lower().strip()
        return self._aliases.get(norm, alias_or_name)

    def get_aliases(self) -> Dict[str, str]:
        """Return all registered model aliases."""
        return dict(self._aliases)

    def list_models(
        self,
        provider_id: Optional[str] = None,
        required_capabilities: Optional[List[str]] = None,
        local_only: bool = False,
    ) -> List[ModelDescriptor]:
        """Discover and list all registered models matching criteria."""
        results: List[ModelDescriptor] = []
        providers_to_check = (
            [self._providers[provider_id]] if provider_id and provider_id in self._providers
            else list(self._providers.values())
        )

        for prov in providers_to_check:
            if local_only and prov.provider_type != ProviderType.LOCAL:
                continue

            try:
                for desc in prov.list_models():
                    if required_capabilities:
                        caps = desc.capabilities
                        matches = True
                        for cap in required_capabilities:
                            if not getattr(caps, cap, False):
                                matches = False
                                break
                        if not matches:
                            continue
                    results.append(desc)
            except Exception as exc:
                logger.warning("Error listing models for provider %s: %s", prov.provider_id, exc)

        return results

    def get_model(self, model_id: str) -> Optional[ModelDescriptor]:
        """Find a model descriptor by exact model ID or alias."""
        resolved_id = self.resolve_alias(model_id)
        for prov in self._providers.values():
            try:
                for desc in prov.list_models():
                    if desc.model_id == resolved_id:
                        return desc
            except Exception:
                continue
        return None

    # =========================================================================
    # HEALTH & CIRCUIT BREAKER
    # =========================================================================

    def record_success(self, provider_id: str) -> None:
        """Record successful call, resetting circuit breaker failure count."""
        self._failure_counts[provider_id] = 0
        self._circuit_open_until.pop(provider_id, None)

    def record_failure(self, provider_id: str) -> None:
        """Record failure, incrementing failure counter and tripping circuit if threshold reached."""
        count = self._failure_counts.get(provider_id, 0) + 1
        self._failure_counts[provider_id] = count
        if count >= 3:
            self._circuit_open_until[provider_id] = time.time() + self._circuit_cooldown_seconds
            logger.warning("Circuit breaker tripped for provider '%s' (3 consecutive failures).", provider_id)

    def is_circuit_open(self, provider_id: str) -> bool:
        """Check whether the circuit breaker is currently open for a provider."""
        open_until = self._circuit_open_until.get(provider_id)
        if not open_until:
            return False
        if time.time() > open_until:
            # Cooldown passed, allow probe
            self._circuit_open_until.pop(provider_id, None)
            self._failure_counts[provider_id] = 2  # one more failure trips again
            return False
        return True

    def get_provider_health(self, provider_id: str, force_refresh: bool = False) -> ProviderHealth:
        """Return provider health status, utilizing circuit breaker and health checks."""
        if not force_refresh and self.is_circuit_open(provider_id):
            return ProviderHealth(
                provider_id=provider_id,
                status=ProviderHealthStatus.UNAVAILABLE,
                error_message="Circuit breaker open due to consecutive failures",
            )

        provider = self.get_provider(provider_id)
        if not provider:
            return ProviderHealth(
                provider_id=provider_id,
                status=ProviderHealthStatus.UNAVAILABLE,
                error_message=f"Provider '{provider_id}' is not registered",
            )

        try:
            health = provider.check_health()
            self._health_cache[provider_id] = health
            return health
        except Exception as exc:
            return ProviderHealth(
                provider_id=provider_id,
                status=ProviderHealthStatus.UNAVAILABLE,
                error_message=str(exc),
            )
