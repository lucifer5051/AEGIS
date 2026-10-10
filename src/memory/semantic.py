"""SAGE EKM Semantic Backend Abstraction.

Canonical Reference:
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 2 (Vector/Semantic Status)

Invariants:
- Semantic backends are optional derived extensions.
- When no local embedding provider is configured, the system uses NullSemanticBackend.
- NullSemanticBackend reports status 'DESIGNED' without external network or GPU overhead.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class SemanticMemoryBackend(Protocol):
    """Protocol for optional semantic/vector indexing backends."""

    @property
    def status(self) -> str:
        """Status string of the backend (e.g., 'DESIGNED', 'AVAILABLE', 'UNAVAILABLE')."""
        ...

    def search(
        self,
        query_text: str,
        limit: int = 10,
        min_similarity: float = 0.0,
    ) -> List[Tuple[str, float]]:
        """Return list of (record_id, similarity_score) matches."""
        ...

    def index(
        self,
        record_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Index or update a record in the vector store."""
        ...

    def remove(self, record_id: str) -> None:
        """Remove a record from the vector index."""
        ...


class NullSemanticBackend:
    """Default offline null semantic backend indicating DESIGNED architectural state."""

    @property
    def status(self) -> str:
        return "DESIGNED"

    def search(
        self,
        query_text: str,
        limit: int = 10,
        min_similarity: float = 0.0,
    ) -> List[Tuple[str, float]]:
        return []

    def index(
        self,
        record_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        pass

    def remove(self, record_id: str) -> None:
        pass
