"""SAGE Unified Memory Engine -- Core Coordination Facade.

Canonical Reference:
- PROMPT 068: SAGE Unified Memory Engine

Provides a unified interface across:
- Unified storage and full-text FTS5 indexing.
- Automatic memory formation from conversations and task execution.
- Hybrid lexical, semantic, and graph retrieval.
- Bounded context integration.
- Identity and contact memory isolation.
- Zero-secret data boundary enforcement.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from src.memory.context_builder import MemoryContextBuilder, MemoryContextResult
from src.memory.formation import MemoryFormationEngine
from src.memory.hybrid_retriever import HybridMemoryRetriever
from src.memory.models import (
    CognitiveMemoryCategory,
    MemoryCategory,
    MemoryProvenanceType,
    MemorySensitivity,
    UnifiedMemoryQuery,
    UnifiedMemoryRecord,
    UnifiedRetrievalResult,
    map_to_cognitive_category,
)
from src.memory.semantic import SemanticMemoryBackend
from src.memory.unified_store import UnifiedMemoryStore
from src.storage.trust_levels import MemoryTrustLevel

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class UnifiedMemoryEngine:
    """Foundational Unified Memory Engine for SAGE."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        semantic_backend: Optional[SemanticMemoryBackend] = None,
    ) -> None:
        """Initialize the unified memory engine backed by a shared SQLite connection."""
        self._conn = conn
        self._store = UnifiedMemoryStore(conn)
        self._formation = MemoryFormationEngine(self._store)
        self._retriever = HybridMemoryRetriever(self._store, semantic_backend=semantic_backend)
        self._context_builder = MemoryContextBuilder(self._retriever)

    @property
    def store(self) -> UnifiedMemoryStore:
        """Underlying relational and FTS5 storage layer."""
        return self._store

    @property
    def formation(self) -> MemoryFormationEngine:
        """Automatic memory extraction and formation pipeline."""
        return self._formation

    @property
    def retriever(self) -> HybridMemoryRetriever:
        """Multi-stage hybrid memory retrieval engine."""
        return self._retriever

    @property
    def context_builder(self) -> MemoryContextBuilder:
        """Bounded context synthesis and token budgeting engine."""
        return self._context_builder

    # =========================================================================
    # CORE MEMORY OPERATIONS
    # =========================================================================

    def remember(
        self,
        content: str,
        category: MemoryCategory = MemoryCategory.FACT,
        cognitive_category: Optional[CognitiveMemoryCategory] = None,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        provenance_type: MemoryProvenanceType = MemoryProvenanceType.EXPLICIT_USER,
        importance: float = 0.9,
        confidence: float = 1.0,
        sensitivity: MemorySensitivity = MemorySensitivity.INTERNAL,
        access_scope: str = "global",
        owner_id: Optional[str] = None,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        tags: Tuple[str, ...] = ("explicit_user",),
        metadata: Optional[Dict[str, Any]] = None,
    ) -> UnifiedMemoryRecord:
        """Explicitly record a memory proposition with high priority."""
        cog_cat = cognitive_category or map_to_cognitive_category(category)
        trust_level = (
            MemoryTrustLevel.M4
            if provenance_type == MemoryProvenanceType.EXPLICIT_USER
            else MemoryTrustLevel.M2
        )
        now = _utc_now_iso()
        record = UnifiedMemoryRecord(
            memory_id=str(uuid4()),
            category=category,
            cognitive_category=cog_cat,
            content=content.strip(),
            summary=None,
            subject=subject,
            predicate=predicate,
            source="user_explicit",
            source_reference=task_id or session_id,
            provenance_type=provenance_type,
            trust_level=trust_level,
            confidence=confidence,
            importance=importance,
            sensitivity=sensitivity,
            access_scope=access_scope,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            retention_policy="permanent",
            is_superseded=False,
            superseded_by=None,
            superseded_reason=None,
            valid_from=now,
            valid_until=None,
            tags=tags,
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        self._store.store_record(record)
        return record

    def recall(
        self,
        query_text: str,
        limit: int = 10,
        owner_id: Optional[str] = None,
        project_id: Optional[str] = None,
        categories: Optional[Tuple[CognitiveMemoryCategory, ...]] = None,
    ) -> UnifiedRetrievalResult:
        """Convenience method to retrieve relevant memories matching a query."""
        query = UnifiedMemoryQuery(
            query_text=query_text,
            cognitive_categories=categories,
            owner_id=owner_id,
            project_id=project_id,
            limit=limit,
        )
        return self._retriever.retrieve(query)

    def forget(
        self,
        memory_id_or_query: str,
        hard_delete: bool = False,
        owner_id: Optional[str] = None,
    ) -> bool:
        """Delete or supersede a specific memory by ID or by exact match."""
        # Check if argument is a specific UUID
        rec = self._store.get_record(memory_id_or_query)
        if rec:
            if owner_id is not None and rec.owner_id != owner_id:
                # Identity security boundary: cannot delete someone else's memory
                return False
            return self._store.delete_record(rec.memory_id, hard_delete=hard_delete)

        # Otherwise search for matching memories to forget
        query = UnifiedMemoryQuery(
            query_text=memory_id_or_query,
            owner_id=owner_id,
            limit=5,
        )
        matches = self._retriever.retrieve(query)
        deleted = False
        for m in matches.records:
            if owner_id is not None and m.owner_id != owner_id:
                continue
            if self._store.delete_record(m.memory_id, hard_delete=hard_delete):
                deleted = True
        return deleted

    def retrieve(self, query: UnifiedMemoryQuery) -> UnifiedRetrievalResult:
        """Execute structured hybrid memory retrieval."""
        return self._retriever.retrieve(query)

    def build_context(
        self,
        query_text: Optional[str] = None,
        token_budget: int = 1024,
        owner_id: Optional[str] = None,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        categories: Optional[Tuple[CognitiveMemoryCategory, ...]] = None,
        max_sensitivity: MemorySensitivity = MemorySensitivity.INTERNAL,
    ) -> MemoryContextResult:
        """Compile a bounded context representation for inclusion in model prompts."""
        return self._context_builder.build_memory_context(
            query_text=query_text,
            token_budget=token_budget,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            categories=categories,
            max_sensitivity=max_sensitivity,
        )

    # =========================================================================
    # AUTOMATIC INTERACTION & EXECUTION LOGGING
    # =========================================================================

    def record_interaction(
        self,
        text: str,
        role: str = "user",
        source: str = "conversation",
        source_reference: Optional[str] = None,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        access_scope: Optional[str] = None,
    ) -> List[UnifiedMemoryRecord]:
        """Automatically identify and form memories from an interaction turn."""
        return self._formation.process_interaction(
            text=text,
            role=role,
            source=source,
            source_reference=source_reference,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            owner_id=owner_id,
            access_scope=access_scope,
        )

    def record_task_outcome(
        self,
        task_id: str,
        goal: Optional[str] = None,
        success: bool = True,
        synthesis: str = "",
        error: Optional[str] = None,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        intent: Optional[str] = None,
        details: str = "",
        actor: str = "supervisor",
    ) -> List[UnifiedMemoryRecord]:
        """Automatically form episodic or procedural memory from task execution outcome."""
        effective_goal = goal or intent or f"Task {task_id}"
        effective_synth = synthesis or details or ""
        rec = self._formation.record_task_outcome(
            task_id=task_id,
            goal=effective_goal,
            success=success,
            synthesis=effective_synth,
            error=error,
            project_id=project_id,
            session_id=session_id,
        )
        return [rec] if rec is not None else []

    # =========================================================================
    # MAINTENANCE & CONSOLIDATION
    # =========================================================================

    def consolidate_memories(
        self,
        project_id: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Perform memory consolidation, reporting active counts, superseded items, and cleanup."""
        now_iso = _utc_now_iso()
        expired_pruned = 0
        try:
            cur = self._conn.execute(
                "SELECT memory_id FROM unified_memories WHERE valid_until IS NOT NULL AND valid_until < ? AND is_superseded = 0;",
                (now_iso,),
            )
            for row in cur.fetchall():
                self._store.delete_record(row[0], hard_delete=False)
                expired_pruned += 1
        except Exception:
            pass

        all_records = self._store.list_all(limit=500)
        active_count = 0
        superseded_count = 0
        categories_count: Dict[str, int] = {}

        for r in all_records:
            if owner_id is not None and r.owner_id != owner_id:
                continue
            if project_id is not None and r.project_id != project_id:
                continue

            if r.is_superseded:
                superseded_count += 1
            else:
                active_count += 1
                cat_key = r.cognitive_category.value
                categories_count[cat_key] = categories_count.get(cat_key, 0) + 1

        return {
            "total_inspected": len(all_records),
            "active_memories": active_count,
            "superseded_memories": superseded_count,
            "expired_pruned": expired_pruned,
            "category_distribution": categories_count,
            "consolidated_at": _utc_now_iso(),
        }
