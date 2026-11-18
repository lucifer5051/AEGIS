"""SAGE Model Gateway -- Base Provider Adapter Interface.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Iterator, List, Optional

from src.models.domain import (
    ModelDescriptor,
    ModelRequest,
    ModelResponse,
    ModelStreamChunk,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderType,
)


class BaseModelProvider(ABC):
    """Abstract base class for all SAGE model provider adapters."""

    def __init__(
        self,
        provider_id: str,
        provider_type: ProviderType,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self.provider_id = provider_id
        self.provider_type = provider_type
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    @abstractmethod
    def list_models(self) -> List[ModelDescriptor]:
        """Return list of models supported and discovered by this provider."""
        raise NotImplementedError

    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResponse:
        """Execute a text or structured generation request."""
        raise NotImplementedError

    @abstractmethod
    def generate_stream(self, request: ModelRequest) -> Iterator[ModelStreamChunk]:
        """Stream generated tokens iteratively."""
        raise NotImplementedError

    @abstractmethod
    def check_health(self) -> ProviderHealth:
        """Check provider connectivity, responsiveness, and credential validity."""
        raise NotImplementedError

    def validate_configuration(self) -> bool:
        """Validate adapter setup and required settings."""
        if self.provider_type == ProviderType.CLOUD and not self.api_key:
            return False
        return True
