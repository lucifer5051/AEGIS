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
from src.memory.context_builder import MemoryContextBuilder, MemoryContextResult
from src.memory.engine import UnifiedMemoryEngine
from src.memory.formation import CandidateMemory, MemoryFormationEngine
from src.memory.hybrid_retriever import HybridMemoryRetriever, LocalSemanticAdapter
from src.memory.indexer import FTS5IndexManager, sanitize_fts5_query
from src.memory.lessons import (
    ExecutionLesson,
    LessonManager,
    LessonProposal,
    LessonRiskLevel,
    LessonState,
)
from src.memory.models import (
    CognitiveMemoryCategory,
    EntityCreate,
    EvidenceCreate,
    FactCreate,
    MemoryCategory,
    MemoryProvenanceType,
    MemoryQuery,
    MemoryRecord,
    MemoryRelationship,
    MemoryScope,
    MemorySensitivity,
    PreferenceCreate,
    RelationCreate,
    RetrievalResult,
    UnifiedMemoryQuery,
    UnifiedMemoryRecord,
    UnifiedRetrievalResult,
    map_to_cognitive_category,
)
from src.memory.contact_memory import EncryptedContactMemoryStore
from src.memory.retriever import EKMRetriever
from src.memory.semantic import NullSemanticBackend, SemanticMemoryBackend
from src.memory.store import EKMMemoryStore
from src.memory.unified_store import UnifiedMemoryStore

__all__ = [
    "CandidateMemory",
    "CognitiveMemoryCategory",
    "CompiledContext",
    "ContextCompiler",
    "EKMMemoryStore",
    "EKMRetriever",
    "EncryptedContactMemoryStore",
    "EntityCreate",
    "EvidenceCreate",
    "ExecutionLesson",
    "FactCreate",
    "FTS5IndexManager",
    "HybridMemoryRetriever",
    "LessonManager",
    "LessonProposal",
    "LessonRiskLevel",
    "LessonState",
    "LocalSemanticAdapter",
    "MemoryCategory",
    "MemoryContextBuilder",
    "MemoryContextResult",
    "MemoryFormationEngine",
    "MemoryProvenanceType",
    "MemoryQuery",
    "MemoryRecord",
    "MemoryRelationship",
    "MemoryScope",
    "MemorySensitivity",
    "NullSemanticBackend",
    "PreferenceCreate",
    "RelationCreate",
    "RetrievalResult",
    "SemanticMemoryBackend",
    "UnifiedMemoryEngine",
    "UnifiedMemoryQuery",
    "UnifiedMemoryRecord",
    "UnifiedMemoryStore",
    "UnifiedRetrievalResult",
    "estimate_tokens",
    "map_to_cognitive_category",
    "sanitize_fts5_query",
]
