"""SAGE Experience & Knowledge Memory (EKM) Subsystem.

Canonical Reference:
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
- docs/specifications/AEGIS_DOMAIN_SCHEMA_FOUNDATION.md

Exported Interfaces:
- EKMMemoryStore: Authoritative SQLite storage for facts, preferences, entities, and evidence.
- EKMRetriever: Multi-stage selective retrieval with trust-aware ranking and conflict detection.
- ContextCompiler: Bounded ContextIR compiler with token budget enforcement.
- LessonManager: Evidence-gated lifecycle and promotion of operational lessons.
- FTS5IndexManager: Derived auxiliary full-text search indexer.
- SemanticMemoryBackend, NullSemanticBackend: Optional semantic backend abstractions.
- MemoryRecord, MemoryQuery, RetrievalResult, MemoryCategory, MemoryScope.
- ExecutionLesson, LessonProposal, LessonRiskLevel, LessonState.
"""

from __future__ import annotations

from src.memory.compiler import CompiledContext, ContextCompiler, estimate_tokens
from src.memory.indexer import FTS5IndexManager, sanitize_fts5_query
from src.memory.lessons import (
    ExecutionLesson,
    LessonManager,
    LessonProposal,
    LessonRiskLevel,
    LessonState,
)
from src.memory.models import (
    EntityCreate,
    EvidenceCreate,
    FactCreate,
    MemoryCategory,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    PreferenceCreate,
    RelationCreate,
    RetrievalResult,
)
from src.memory.retriever import EKMRetriever
from src.memory.semantic import NullSemanticBackend, SemanticMemoryBackend
from src.memory.store import EKMMemoryStore

__all__ = [
    "CompiledContext",
    "ContextCompiler",
    "EKMMemoryStore",
    "EKMRetriever",
    "EntityCreate",
    "EvidenceCreate",
    "ExecutionLesson",
    "FactCreate",
    "FTS5IndexManager",
    "LessonManager",
    "LessonProposal",
    "LessonRiskLevel",
    "LessonState",
    "MemoryCategory",
    "MemoryQuery",
    "MemoryRecord",
    "MemoryScope",
    "NullSemanticBackend",
    "PreferenceCreate",
    "RelationCreate",
    "RetrievalResult",
    "SemanticMemoryBackend",
    "estimate_tokens",
    "sanitize_fts5_query",
]
