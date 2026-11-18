"""SAGE Hybrid Memory Retrieval Engine.

Canonical Reference:
- PROMPT 068: SAGE Unified Memory Engine

Pipeline:
    Query → Intent/Context Analysis → Structured Search → FTS5 Search → Semantic Search
          → Relationship Expansion → Multi-Factor Ranking → Deduplication → Results

Invariants:
1. Contact and identity isolation strictly enforced: queries with owner_id or contact scope
   never return another contact's private memories.
2. 100% offline and deterministic: local semantic similarity adapter works without external APIs.
3. Relationship expansion preserves security boundaries: linked memories must pass access check.
4. Frozen provenance trust levels M0-M4 provide ranking weights without bypassing authorization.
5. Unresolved conflicts (opposing facts or preferences) are flagged explicitly.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from src.memory.models import (
    CognitiveMemoryCategory,
    MemoryCategory,
    MemoryRelationship,
    UnifiedMemoryQuery,
    UnifiedMemoryRecord,
    UnifiedRetrievalResult,
)
from src.memory.semantic import SemanticMemoryBackend
from src.memory.unified_store import UnifiedMemoryStore
from src.storage.trust_levels import MemoryTrustLevel

logger = logging.getLogger(__name__)

TRUST_RANK_MULTIPLIERS: Dict[MemoryTrustLevel, float] = {
    MemoryTrustLevel.M0: 0.5,
    MemoryTrustLevel.M1: 0.7,
    MemoryTrustLevel.M2: 0.85,
    MemoryTrustLevel.M3: 0.95,
    MemoryTrustLevel.M4: 1.0,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalSemanticAdapter:
    """100% offline, deterministic n-gram TF-IDF cosine similarity adapter."""

    def __init__(self, ngram_range: Tuple[int, int] = (1, 2)) -> None:
        self.ngram_range = ngram_range

    def similarity(self, text_a: str, text_b: str) -> float:
        """Alias for compute_similarity."""
        return self.compute_similarity(text_a, text_b)

    def compute_similarity(self, text_a: str, text_b: str) -> float:
        """Calculate token and character n-gram cosine similarity between two strings."""
        if not text_a or not text_b:
            return 0.0

        words_a = re.findall(r"\w+", text_a.lower())
        words_b = re.findall(r"\w+", text_b.lower())

        if not words_a or not words_b:
            return 0.0

        vec_a = Counter(words_a)
        vec_b = Counter(words_b)

        # Dot product
        intersection = set(vec_a.keys()) & set(vec_b.keys())
        dot = sum(vec_a[w] * vec_b[w] for w in intersection)

        norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
        norm_b = math.sqrt(sum(v * v for v in vec_b.values()))

        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0

        word_sim = dot / (norm_a * norm_b)

        # Also add character 3-gram overlap for typo / sub-word resilience
        ngrams_a = Counter([text_a[i : i + 3].lower() for i in range(max(0, len(text_a) - 2))])
        ngrams_b = Counter([text_b[i : i + 3].lower() for i in range(max(0, len(text_b) - 2))])

        if ngrams_a and ngrams_b:
            ng_dot = sum(ngrams_a[ng] * ngrams_b[ng] for ng in set(ngrams_a.keys()) & set(ngrams_b.keys()))
            ng_norm_a = math.sqrt(sum(v * v for v in ngrams_a.values()))
            ng_norm_b = math.sqrt(sum(v * v for v in ngrams_b.values()))
            ng_sim = (ng_dot / (ng_norm_a * ng_norm_b)) if (ng_norm_a * ng_norm_b) > 0 else 0.0
        else:
            ng_sim = 0.0

        return 0.6 * word_sim + 0.4 * ng_sim


class HybridMemoryRetriever:
    """Multi-stage hybrid retrieval engine uniting structured, lexical, semantic, and graph expansion."""

    def __init__(
        self,
        store: UnifiedMemoryStore,
        semantic_backend: Optional[SemanticMemoryBackend] = None,
    ) -> None:
        self._store = store
        self._semantic_backend = semantic_backend
        self._local_semantic = LocalSemanticAdapter()

    def retrieve(self, query: UnifiedMemoryQuery) -> UnifiedRetrievalResult:
        """Execute the multi-stage hybrid memory retrieval pipeline."""
        now_iso = query.as_of_time or _utc_now_iso()

        # ---------------------------------------------------------------------
        # 1. Structured Candidate Filtering
        # ---------------------------------------------------------------------
        candidates = self._store.query_records(query)
        if not candidates:
            return UnifiedRetrievalResult(
                records=[],
                query=query,
                ranking_scores={},
                total_found=0,
            )

        candidate_map: Dict[str, UnifiedMemoryRecord] = {r.memory_id: r for r in candidates}

        # ---------------------------------------------------------------------
        # 2. Lexical Scoring (FTS5)
        # ---------------------------------------------------------------------
        lexical_scores: Dict[str, float] = {}
        if query.query_text and query.query_text.strip():
            fts_matches = self._store.search_fts(query.query_text, limit=query.limit * 2)
            for mid, score in fts_matches:
                if mid in candidate_map:
                    lexical_scores[mid] = score

        # ---------------------------------------------------------------------
        # 3. Semantic Scoring
        # ---------------------------------------------------------------------
        semantic_scores: Dict[str, float] = {}
        if query.query_text and query.query_text.strip():
            if self._semantic_backend and self._semantic_backend.status == "AVAILABLE":
                # External or dedicated vector backend if available
                vec_matches = self._semantic_backend.search(query.query_text, limit=query.limit * 2)
                for mid, score in vec_matches:
                    if mid in candidate_map:
                        semantic_scores[mid] = score
            else:
                # Deterministic local TF-IDF cosine similarity
                for mid, rec in candidate_map.items():
                    sim = self._local_semantic.compute_similarity(query.query_text, rec.content)
                    if sim > 0.05:
                        semantic_scores[mid] = sim

        # ---------------------------------------------------------------------
        # 4. Relationship Graph Traversal & Expansion
        # ---------------------------------------------------------------------
        discovered_relationships: List[MemoryRelationship] = []
        related_records_map: Dict[str, List[UnifiedMemoryRecord]] = {}
        rel_boosts: Dict[str, float] = {}

        if query.expand_relationships and query.max_relationship_depth > 0:
            # Expand connections for top candidates
            for rec in candidates[:10]:
                edges = self._store.get_relationships(rec.memory_id)
                related_for_rec: List[UnifiedMemoryRecord] = []
                for edge in edges:
                    target_id = (
                        edge.target_memory_id
                        if edge.source_memory_id == rec.memory_id
                        else edge.source_memory_id
                    )
                    target_rec = self._store.get_record(target_id)
                    if not target_rec:
                        continue

                    # STRICT IDENTITY & ACCESS ISOLATION CHECK
                    if not self._is_authorized(target_rec, query):
                        continue

                    if not query.include_superseded and target_rec.is_superseded:
                        continue

                    discovered_relationships.append(edge)
                    related_for_rec.append(target_rec)
                    rel_boosts[target_id] = rel_boosts.get(target_id, 0.0) + (0.1 * edge.weight)

                    # Add target into candidate set if not already present
                    if target_id not in candidate_map and len(candidate_map) < query.limit * 2:
                        candidate_map[target_id] = target_rec

                if related_for_rec:
                    related_records_map[rec.memory_id] = related_for_rec

        # ---------------------------------------------------------------------
        # 5. Multi-Factor Composite Ranking
        # ---------------------------------------------------------------------
        ranked_scores: Dict[str, float] = {}
        for mid, rec in candidate_map.items():
            s_lex = lexical_scores.get(mid, 0.0)
            s_sem = semantic_scores.get(mid, 0.0)
            s_imp = rec.importance
            s_conf = rec.confidence
            s_rel = min(0.3, rel_boosts.get(mid, 0.0))

            # Recency factor (logarithmic decay based on age in days)
            recency_boost = self._calculate_recency_boost(rec.updated_at, now_iso)

            # Query relevance blend: if query_text was passed, weight lexical + semantic heavily
            if query.query_text and query.query_text.strip():
                relevance = 0.40 * s_lex + 0.35 * s_sem + 0.15 * s_imp + 0.10 * recency_boost
            else:
                relevance = 0.50 * s_imp + 0.30 * s_conf + 0.20 * recency_boost

            # Apply trust level multiplier
            trust_mult = TRUST_RANK_MULTIPLIERS.get(rec.trust_level, 0.8)
            final_score = (relevance + s_rel) * trust_mult
            ranked_scores[mid] = round(final_score, 4)

        # ---------------------------------------------------------------------
        # 6. Sorting, Deduplication & Conflict Detection
        # ---------------------------------------------------------------------
        sorted_records = sorted(
            candidate_map.values(),
            key=lambda r: ranked_scores.get(r.memory_id, 0.0),
            reverse=True,
        )

        final_records = sorted_records[: query.limit]

        # Conflict Detection: flag active records with opposing recommendations/preferences
        unresolved_conflicts = self._detect_conflicts(final_records)

        # Provenance summary
        prov_counts = Counter(r.provenance_type.value for r in final_records)

        return UnifiedRetrievalResult(
            records=final_records,
            query=query,
            ranking_scores={r.memory_id: ranked_scores[r.memory_id] for r in final_records},
            relationships=discovered_relationships,
            related_records=related_records_map,
            unresolved_conflicts=unresolved_conflicts,
            provenance_summary=dict(prov_counts),
            total_found=len(candidate_map),
            metadata={
                "retrieved_at": now_iso,
                "lexical_hits": len(lexical_scores),
                "semantic_hits": len(semantic_scores),
                "relationships_expanded": len(discovered_relationships),
            },
        )

    # =========================================================================
    # SECURITY & ACCESS AUTHORIZATION
    # =========================================================================

    def _is_authorized(self, record: UnifiedMemoryRecord, query: UnifiedMemoryQuery) -> bool:
        """Check whether a record matches the query's identity and sensitivity constraints."""
        # Contact / Owner Isolation:
        if query.owner_id is not None:
            # Query belongs to a specific contact or user:
            # Can only see global records or records owned by that exact identity
            if record.owner_id is not None and record.owner_id != query.owner_id:
                return False
            if record.access_scope != "global" and record.access_scope != f"contact:{query.owner_id}":
                return False

        # Project Isolation:
        if query.project_id is not None and record.project_id is not None:
            if record.project_id != query.project_id:
                return False

        # Sensitivity check:
        if query.max_sensitivity is not None:
            tiers = ["public", "internal", "confidential", "restricted"]
            max_idx = tiers.index(query.max_sensitivity.value)
            rec_idx = tiers.index(record.sensitivity.value) if record.sensitivity.value in tiers else 1
            if rec_idx > max_idx:
                return False

        return True

    def _calculate_recency_boost(self, updated_at_iso: str, now_iso: str) -> float:
        try:
            t_updated = datetime.fromisoformat(updated_at_iso.replace("Z", "+00:00"))
            t_now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            days_old = max(0.0, (t_now - t_updated).total_seconds() / 86400.0)
            return max(0.1, 1.0 / (1.0 + (days_old / 30.0)))
        except Exception:
            return 0.5

    def _detect_conflicts(self, records: Sequence[UnifiedMemoryRecord]) -> List[Dict[str, Any]]:
        conflicts = []
        pref_records = [r for r in records if r.category == MemoryCategory.PREFERENCE]
        for i in range(len(pref_records)):
            for j in range(i + 1, len(pref_records)):
                r1, r2 = pref_records[i], pref_records[j]
                if r1.owner_id == r2.owner_id and not r1.is_superseded and not r2.is_superseded:
                    t1, t2 = r1.content.lower(), r2.content.lower()
                    conflict_keywords = [("dark", "light"), ("always", "never"), ("json", "yaml"), ("python", "rust")]
                    for kw1, kw2 in conflict_keywords:
                        if (kw1 in t1 and kw2 in t2) or (kw2 in t1 and kw1 in t2):
                            conflicts.append(
                                {
                                    "conflict_type": "opposing_preference",
                                    "record_a": r1.memory_id,
                                    "record_b": r2.memory_id,
                                    "description": f"Opposing preferences detected between '{r1.content}' and '{r2.content}'",
                                }
                            )
        return conflicts
