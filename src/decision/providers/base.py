"""SAGE Fast Decision Engine - Provider Abstraction Base.

Document Reference: docs/specifications/AEGIS_FAST_DECISION_ENGINE.md
Constraints:
- All Fast Decision providers must conform to this abstract lifecycle and interface.
- Providers are STRICTLY ADVISORY. They can never authorize or execute actions.
- Providers must operate 100% offline; inference network calls are strictly prohibited.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from src.decision.models import (
    DecisionRoute,
    FastDecisionRequest,
    FastDecisionResult,
    ProviderMetrics,
    ProviderStatus,
)


class FastDecisionProvider(ABC):
    """Abstract base class for all Fast Decision Engine providers."""

    def __init__(self, provider_id: str, provider_version: str) -> None:
        self.provider_id = provider_id
        self.provider_version = provider_version
        self._status = ProviderStatus.UNINITIALIZED

    @property
    def status(self) -> ProviderStatus:
        """Current operational lifecycle status of the provider."""
        return self._status

    @abstractmethod
    def initialize(self) -> bool:
        """Preload, warm, and verify provider runtime and model residency.

        Must be idempotent and non-blocking if already loaded.
        Returns:
            True if initialization succeeded and provider is ready, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    def is_ready(self) -> bool:
        """Check whether the provider is currently ready to serve inference."""
        raise NotImplementedError

    @abstractmethod
    def evaluate(
        self,
        request: FastDecisionRequest,
        criteria: Dict[DecisionRoute, str],
    ) -> FastDecisionResult:
        """Perform advisory intent routing classification over trusted criteria.

        Args:
            request: The untrusted user request and bounded context.
            criteria: Trusted mapping of DecisionRoute to semantic description.

        Returns:
            A typed advisory FastDecisionResult.
        """
        raise NotImplementedError

    @abstractmethod
    def unload(self) -> None:
        """Unload resident model weights, release concurrency handles, and free memory."""
        raise NotImplementedError

    @abstractmethod
    def get_metrics(self) -> ProviderMetrics:
        """Return operational metrics including cold load and warm latency distributions."""
        raise NotImplementedError

    @abstractmethod
    def verify_integrity(self) -> bool:
        """Verify local checkpoint artifacts against the trusted source-controlled manifest.

        Returns:
            True if all required files exist and SHA-256 digests match exactly.
        """
        raise NotImplementedError

    @abstractmethod
    def qualify(self, hardware_profile: Optional[Any] = None) -> bool:
        """Perform hardware-aware qualification using measured load and latency.

        Returns:
            True if provider meets memory fit and latency thresholds on this host.
        """
        raise NotImplementedError
