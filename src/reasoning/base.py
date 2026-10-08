"""SAGE Local Reasoning Subsystem - Provider-Neutral Abstraction Base.

Defines the abstract interface for all local reasoning providers in SAGE.
Key architectural constraints:
1. Provider-neutral: can be backed by llama-cpp-python, ONNX, or other offline backends.
2. Distinct from Fast Decision Engine: this is for Manager-level local reasoning,
   plan generation, and task analysis, NOT intent routing / fast decision.
3. Completely offline: zero network access permitted during runtime.
4. Integrated with SAGE Model Policy: enforces dynamic RAM gating, residency tracking,
   and clean eviction.
"""

from abc import ABC, abstractmethod

from src.reasoning.models import (
    ReasoningMetrics,
    ReasoningProviderStatus,
    ReasoningRequest,
    ReasoningResponse,
)


class LocalReasoningProvider(ABC):
    """Abstract base class for all SAGE local reasoning providers."""

    def __init__(self, provider_id: str, model_id: str) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self._status: ReasoningProviderStatus = ReasoningProviderStatus.UNINITIALIZED

    @property
    def status(self) -> ReasoningProviderStatus:
        """Current operational lifecycle status of the provider."""
        return self._status

    @abstractmethod
    def initialize(self) -> bool:
        """Preload, verify integrity, admit via resource policy, and load weights.

        Returns:
            True if initialization succeeded and provider is ready, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    def is_ready(self) -> bool:
        """Check whether the provider is currently ready to perform reasoning."""
        raise NotImplementedError

    @abstractmethod
    def reason(self, request: ReasoningRequest) -> ReasoningResponse:
        """Execute a local reasoning request and return structured response.

        Args:
            request: The typed reasoning request.

        Returns:
            A typed ReasoningResponse.
        """
        raise NotImplementedError

    @abstractmethod
    def unload(self) -> bool:
        """Unload resident model weights, release concurrency handles, and free RAM.

        Returns:
            True if model was successfully evicted, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    def verify_integrity(self) -> bool:
        """Verify on-disk artifact byte size and cryptographic hash against expected values.

        Returns:
            True if artifact is intact and authentic, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    def get_metrics(self) -> ReasoningMetrics:
        """Return operational metrics including load latency, inference latency, and memory."""
        raise NotImplementedError
