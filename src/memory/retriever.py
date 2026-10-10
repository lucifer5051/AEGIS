"""SAGE EKM Selective Retrieval Pipeline.

Canonical Reference:
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
- docs/specifications/AEGIS_DOMAIN_SCHEMA_FOUNDATION.md

Invariants:
- Multi-stage retrieval: Scope/temporal filtering -> Lexical/Graph retrieval ->
  Trust ranking -> Deduplication/Conflict detection.
- Frozen trust levels M0-M4 provide provenance weighting, not authorization permissions.
- Specific project scope earns a relevance boost over global scope.
- Unresolved conflicts (opposing facts/preferences) are flagged explicitly.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from src.memory.lessons import LessonManager
from src.memory.models import (
    ExecutionLesson,
    LessonState,
    MemoryCategory,
    MemoryQuery,
    MemoryRecord,
    RetrievalResult,
)
from src.memory.store import EKMMemoryStore
from src.storage.trust_levels import MemoryTrustLevel

logger = logging.getLogger(__name__)

TRUST_RANKS: Dict[MemoryTrustLevel, int] = {
    MemoryTrustLevel.M0: 0,
    MemoryTrustLevel.M1: 1,
    MemoryTrustLevel.M2: 2,
    MemoryTrustLevel.M3: 3,
    MemoryTrustLevel.M4: 4,
}

TRUST_WEIGHTS: Dict[MemoryTrustLevel, float] = {
    MemoryTrustLevel.M0: 0.2,
    MemoryTrustLevel.M1: 0.4,
    MemoryTrustLevel.M2: 0.6,
    MemoryTrustLevel.M3: 0.8,
    MemoryTrustLevel.M4: 1.0,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EKMRetriever:
    """Multi-stage selective retrieval engine for Experience & Knowledge Memory."""

    def __init__(
        self,
        store: EKMMemoryStore,
        lesson_manager: Optional[LessonManager] = None,
    ) -> None:
        self._store = store
        self._conn = store._conn
        self._lesson_manager = (
            lesson_manager if lesson_manager is not None else LessonManager(self._conn, store)
        )

    def retrieve(self, query: MemoryQuery) -> RetrievalResult:
        """Execute multi-stage selective retrieval based on query constraints."""
        now_iso = _utc_now_iso()
        as_of = query.as_of_time or now_iso

        # ---------------------------------------------------------------------
        # 1. Candidate Retrieval (Lexical via FTS5 or Full Table Scan)
        # ---------------------------------------------------------------------
        candidate_scores: Dict[str, float] = {}

        if query.query_text and query.query_text.strip():
            fts_matches = self._store.indexer.search(
                query_text=query.query_text,
                project_id=query.project_id,
                include_global=query.include_global,
                include_superseded=query.include_superseded,
                limit=max(50, query.limit * 3),
            )
            for record_id, raw_score in fts_matches:
                # Normalize raw FTS score (FTS bm25 inverted is positive, typically 0.0 - 15.0)
                norm_score = max(0.1, min(raw_score / 10.0, 1.0))
                candidate_scores[record_id] = norm_score
        elif not query.entity_slugs:
            # No text query and no entity slugs: retrieve recent records
            self._load_unindexed_candidates(query, candidate_scores)

        # ---------------------------------------------------------------------
        # 2. Entity Slug / Graph Traversal Expansion
        # ---------------------------------------------------------------------
        if query.entity_slugs:
            self._expand_entity_slugs(query, candidate_scores)

        # ---------------------------------------------------------------------
        # 3. Fetch Full Records & Apply Scope / Temporal Filters
        # ---------------------------------------------------------------------
        filtered_records: List[MemoryRecord] = []
        for record_id in list(candidate_scores.keys()):
            record = self._store.get_record(record_id)
            if not record:
                continue

            # Scope filtering
            if query.project_id is not None:
                if not query.include_global and record.project_id != query.project_id:
                    continue
                if query.include_global and (
                    record.project_id is not None and record.project_id != query.project_id
                ):
                    continue
            elif not query.include_global and record.project_id is None:
                continue

            # Category filtering
            if query.categories and record.category not in query.categories:
                continue

            # Trust level filtering
            if query.min_trust_level is not None:
                if TRUST_RANKS[record.trust_level] < TRUST_RANKS[query.min_trust_level]:
                    continue

            # Supersession filtering
            if not query.include_superseded and record.is_superseded:
                continue

            # Temporal validity filtering
            if query.as_of_time is not None:
                if record.valid_from > as_of:
                    continue
                if record.valid_until is not None and record.valid_until < as_of:
                    continue
            elif not query.include_expired:
                if record.valid_until is not None and record.valid_until < now_iso:
                    continue

            filtered_records.append(record)

        # ---------------------------------------------------------------------
        # 4. Graph Relationship Traversal (if entities are present)
        # ---------------------------------------------------------------------
        if query.max_relationship_depth > 0:
            filtered_records = self._traverse_entity_relations(
                filtered_records, query, candidate_scores
            )

        # ---------------------------------------------------------------------
        # 5. Trust-Aware Ranking
        # ---------------------------------------------------------------------
        ranking_scores: Dict[str, float] = {}
        for rec in filtered_records:
            lexical_score = candidate_scores.get(rec.memory_id, 0.5)
            trust_weight = TRUST_WEIGHTS.get(rec.trust_level, 0.5)
            importance = rec.importance
            confidence = rec.confidence

            # Base composite relevance
            base_score = (lexical_score * 0.4 + importance * 0.3 + confidence * 0.3) * trust_weight

            # Specificity bonus for project-specific records when querying within project
            if query.project_id is not None and rec.project_id == query.project_id:
                base_score += 0.15

            ranking_scores[rec.memory_id] = round(base_score, 4)

        # Sort descending by ranking score
        filtered_records.sort(key=lambda r: ranking_scores.get(r.memory_id, 0.0), reverse=True)

        # ---------------------------------------------------------------------
        # 6. Deduplication & Conflict Detection
        # ---------------------------------------------------------------------
        deduped_records: List[MemoryRecord] = []
        seen_ids: Set[str] = set()
        conflicts: List[Dict[str, Any]] = []

        # Maps for conflict detection: (subject, predicate) -> (object, record_id, trust_level)
        fact_propositions: Dict[Tuple[str, str], Tuple[str, str, MemoryTrustLevel]] = {}
        # Preference keys: key -> (value, record_id, trust_level)
        pref_keys: Dict[str, Tuple[str, str, MemoryTrustLevel]] = {}

        for rec in filtered_records:
            if rec.memory_id in seen_ids:
                continue
            seen_ids.add(rec.memory_id)
            deduped_records.append(rec)

            if rec.category == MemoryCategory.FACT:
                subj = str(rec.payload.get("subject", "")).strip().lower()
                pred = str(rec.payload.get("predicate", "")).strip().lower()
                obj = str(rec.payload.get("object", "")).strip()
                if subj and pred:
                    pair = (subj, pred)
                    if pair in fact_propositions:
                        prev_obj, prev_id, prev_trust = fact_propositions[pair]
                        if prev_obj != obj:
                            conflicts.append(
                                {
                                    "conflict_type": "fact_opposition",
                                    "subject": subj,
                                    "predicate": pred,
                                    "candidate_1": {
                                        "record_id": prev_id,
                                        "object": prev_obj,
                                        "trust_level": prev_trust.value,
                                    },
                                    "candidate_2": {
                                        "record_id": rec.memory_id,
                                        "object": obj,
                                        "trust_level": rec.trust_level.value,
                                    },
                                }
                            )
                    else:
                        fact_propositions[pair] = (obj, rec.memory_id, rec.trust_level)

            elif rec.category == MemoryCategory.PREFERENCE:
                key = str(rec.payload.get("key", "")).strip().lower()
                val = str(rec.payload.get("value", "")).strip()
                if key:
                    if key in pref_keys:
                        prev_val, prev_id, prev_trust = pref_keys[key]
                        if prev_val != val:
                            conflicts.append(
                                {
                                    "conflict_type": "preference_opposition",
                                    "key": key,
                                    "candidate_1": {
                                        "record_id": prev_id,
                                        "value": prev_val,
                                        "trust_level": prev_trust.value,
                                    },
                                    "candidate_2": {
                                        "record_id": rec.memory_id,
                                        "value": val,
                                        "trust_level": rec.trust_level.value,
                                    },
                                }
                            )
                    else:
                        pref_keys[key] = (val, rec.memory_id, rec.trust_level)

        # Slice to query.limit
        final_records = deduped_records[: query.limit]

        # ---------------------------------------------------------------------
        # 7. Operational Lesson Retrieval (Strictly Approved, Scoped, Ranked)
        # ---------------------------------------------------------------------
        selected_lessons: List[ExecutionLesson] = []
        lesson_scores: Dict[str, float] = {}
        if query.categories is None or MemoryCategory.LESSON in query.categories:
            selected_lessons, lesson_scores = self._retrieve_lessons(query)

        # ---------------------------------------------------------------------
        # 8. Construct Provenance Map & Result
        # ---------------------------------------------------------------------
        provenance_map: Dict[str, Dict[str, Any]] = {}
        for rec in final_records:
            provenance_map[rec.memory_id] = {
                "trust_level": rec.trust_level.value,
                "provenance_source": rec.provenance_source,
                "evidence_id": rec.evidence_id,
                "confidence": rec.confidence,
                "scope": rec.scope.value,
                "project_id": rec.project_id,
            }

        for les in selected_lessons:
            provenance_map[les.lesson_id] = {
                "type": "lesson",
                "trust_level": les.trust_level.value,
                "provenance_source": "system",
                "risk_level": les.risk_level.value,
                "state": les.state.value,
                "supporting_evidence_ids": list(les.supporting_evidence_ids),
                "supporting_event_ids": list(les.supporting_event_ids),
                "confirmation_count": les.confirmation_count,
                "contradiction_count": les.contradiction_count,
                "fact_id": les.fact_id,
                "project_id": les.project_id,
                "skill_id": les.skill_id,
            }

        combined_ranking = {r.memory_id: ranking_scores[r.memory_id] for r in final_records}
        for les_id, score in lesson_scores.items():
            combined_ranking[les_id] = score

        return RetrievalResult(
            records=final_records,
            query=query,
            ranking_scores=combined_ranking,
            provenance_map=provenance_map,
            unresolved_conflicts=conflicts,
            metadata={
                "total_candidates_examined": len(filtered_records),
                "total_returned": len(final_records),
                "total_lessons_returned": len(selected_lessons),
                "as_of_time": as_of,
            },
            lessons=selected_lessons,
        )

    # =========================================================================
    # LESSON RETRIEVAL
    # =========================================================================

    def _retrieve_lessons(
        self, query: MemoryQuery
    ) -> Tuple[List[ExecutionLesson], Dict[str, float]]:
        """Retrieve and rank approved operational lessons matching task scope and relevance."""
        # 1. Fetch lessons from lesson manager filtering by state APPROVED
        raw_lessons = self._lesson_manager.list_lessons(
            project_id=query.project_id,
            skill_id=query.skill_id,
            states=[LessonState.APPROVED],
            include_global=query.include_global,
            limit=max(50, query.limit * 3),
        )

        now_iso = _utc_now_iso()
        as_of = query.as_of_time or now_iso

        filtered_lessons: List[ExecutionLesson] = []
        for les in raw_lessons:
            # Strictly APPROVED state
            if les.state != LessonState.APPROVED:
                continue

            # Superseded exclusion
            if not query.include_superseded and les.is_superseded:
                continue

            # Strict project isolation to prevent cross-project leakage
            if query.project_id is not None:
                if not query.include_global and les.project_id != query.project_id:
                    continue
                if query.include_global and (
                    les.project_id is not None and les.project_id != query.project_id
                ):
                    continue
            else:
                # When no project is queried, only global lessons (project_id IS NULL) are allowed
                if les.project_id is not None:
                    continue

            # Skill isolation
            if query.skill_id is not None and les.skill_id is not None:
                if les.skill_id != query.skill_id:
                    continue

            # Minimum trust level
            if query.min_trust_level is not None:
                if TRUST_RANKS[les.trust_level] < TRUST_RANKS[query.min_trust_level]:
                    continue

            # Temporal validity
            if query.as_of_time is not None:
                if les.created_at > as_of:
                    continue

            filtered_lessons.append(les)

        if not filtered_lessons:
            return [], {}

        # 2. Relevance scoring and ranking
        query_words: Set[str] = set()
        if query.query_text and query.query_text.strip():
            query_words = {w.lower() for w in query.query_text.split() if len(w) > 2}

        scored_lessons: List[Tuple[float, ExecutionLesson]] = []
        scores_by_id: Dict[str, float] = {}

        for les in filtered_lessons:
            les_text = (
                f"{les.title} {les.trigger_condition} {les.recommended_action} {les.observation}"
            ).lower()
            les_tokens = set(les_text.split())

            if query_words:
                overlap = len(query_words.intersection(les_tokens))
                trigger_overlap = len(
                    query_words.intersection(set(les.trigger_condition.lower().split()))
                )
                text_relevance = min(
                    1.0, (overlap + trigger_overlap * 2) / max(1, len(query_words))
                )

                # If query_text is specified but zero text match and not matching explicit skill_id
                if text_relevance == 0.0 and (
                    query.skill_id is None or les.skill_id != query.skill_id
                ):
                    continue
            else:
                text_relevance = 0.5

            # Scope specificity bonus: project-scoped lesson earns a bonus within that project
            scope_bonus = 0.15 if (query.project_id and les.project_id == query.project_id) else 0.0

            # Evidence quality: confirmation ratio, count factor, and trust weight
            total_evals = les.confirmation_count + les.contradiction_count
            conf_ratio = les.confirmation_count / max(1, total_evals)
            count_factor = min(1.0, les.confirmation_count / 5.0)
            trust_wt = TRUST_WEIGHTS.get(les.trust_level, 0.5)
            evidence_score = (conf_ratio * 0.6 + count_factor * 0.4) * trust_wt

            # Applicable recency baseline
            recency_score = 0.5

            composite = round(
                (text_relevance * 0.45 + evidence_score * 0.35 + recency_score * 0.20)
                + scope_bonus,
                4,
            )
            scored_lessons.append((composite, les))
            scores_by_id[les.lesson_id] = composite

        # Sort descending by composite score, with stable tie-breaker on lesson_id
        scored_lessons.sort(key=lambda pair: (pair[0], pair[1].lesson_id), reverse=True)

        selected = [les for _, les in scored_lessons[: query.limit]]
        return selected, {les.lesson_id: scores_by_id[les.lesson_id] for les in selected}

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _load_unindexed_candidates(
        self, query: MemoryQuery, candidate_scores: Dict[str, float]
    ) -> None:
        """Load initial candidate record IDs when no text query is supplied."""
        # 1. Facts
        try:
            cur = self._conn.execute(
                "SELECT fact_id FROM memory_facts WHERE is_superseded = 0 "
                "ORDER BY created_at DESC LIMIT ?;",
                (query.limit * 2,),
            )
            for row in cur.fetchall():
                candidate_scores[str(row[0])] = 0.5
        except sqlite3.OperationalError:
            pass

        # 2. Preferences
        try:
            cur = self._conn.execute(
                "SELECT preference_id FROM user_preferences WHERE is_superseded = 0 "
                "ORDER BY created_at DESC LIMIT ?;",
                (query.limit * 2,),
            )
            for row in cur.fetchall():
                candidate_scores[str(row[0])] = 0.7
        except sqlite3.OperationalError:
            pass

        # 3. Entities
        try:
            cur = self._conn.execute(
                "SELECT entity_id FROM entities ORDER BY created_at DESC LIMIT ?;",
                (query.limit * 2,),
            )
            for row in cur.fetchall():
                candidate_scores[str(row[0])] = 0.5
        except sqlite3.OperationalError:
            pass

    def _expand_entity_slugs(self, query: MemoryQuery, candidate_scores: Dict[str, float]) -> None:
        """Find entity record IDs matching requested canonical slugs."""
        if not query.entity_slugs:
            return
        placeholders = ",".join("?" for _ in query.entity_slugs)
        sql = f"SELECT entity_id FROM entities WHERE canonical_slug IN ({placeholders});"
        try:
            cur = self._conn.execute(sql, tuple(query.entity_slugs))
            for row in cur.fetchall():
                candidate_scores[str(row[0])] = 0.9
        except sqlite3.OperationalError:
            pass

    def _traverse_entity_relations(
        self,
        current_records: List[MemoryRecord],
        query: MemoryQuery,
        candidate_scores: Dict[str, float],
    ) -> List[MemoryRecord]:
        """Traverse entity graph edges up to max_relationship_depth."""
        entity_ids = {r.memory_id for r in current_records if r.category == MemoryCategory.ENTITY}
        if not entity_ids:
            return current_records

        traversed_ids: Set[str] = set(entity_ids)
        current_layer = set(entity_ids)

        for _depth in range(query.max_relationship_depth):
            if not current_layer:
                break
            placeholders = ",".join("?" for _ in current_layer)
            sql = f"""
            SELECT target_entity_id, source_entity_id, weight
            FROM entity_relationships
            WHERE source_entity_id IN ({placeholders}) OR target_entity_id IN ({placeholders});
            """
            params = list(current_layer) * 2
            next_layer: Set[str] = set()
            try:
                cur = self._conn.execute(sql, params)
                for tgt, src, weight in cur.fetchall():
                    for connected in (tgt, src):
                        if connected not in traversed_ids:
                            traversed_ids.add(connected)
                            next_layer.add(connected)
                            candidate_scores[connected] = round(0.4 * float(weight), 4)
            except sqlite3.OperationalError:
                break

            current_layer = next_layer

        # Fetch records for newly discovered entities
        discovered_records: List[MemoryRecord] = list(current_records)
        for ent_id in traversed_ids:
            if not any(r.memory_id == ent_id for r in current_records):
                ent_rec = self._store.get_entity(ent_id)
                if ent_rec:
                    discovered_records.append(ent_rec)

        return discovered_records
