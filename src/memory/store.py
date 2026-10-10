"""SAGE EKM Memory Store -- Authoritative Canonical SQLite Storage.

Canonical Reference:
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
- migrations/0002_ekm_schema.sql

Invariants:
- Canonical storage resides in relational SQLite tables
  (memory_facts, user_preferences, entities, etc.).
- Derived FTS5 indexes are auxiliary and rebuildable.
- Raw secrets are strictly prohibited: SecretLeakError raised prior to persistence.
- Trust levels strictly frozen at M0-M4.
- Historical records are preserved via supersession chains.
- Global records have project_id IS NULL; project-scoped records have project_id NOT NULL.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from src.memory.indexer import FTS5IndexManager
from src.memory.models import (
    EntityCreate,
    EvidenceCreate,
    FactCreate,
    MemoryCategory,
    MemoryRecord,
    MemoryScope,
    PreferenceCreate,
    RelationCreate,
)
from src.storage.trust_levels import MemoryTrustLevel
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


class EKMMemoryStore:
    """Canonical repository for SAGE Experience & Knowledge Memory (EKM)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        indexer: Optional[FTS5IndexManager] = None,
    ) -> None:
        self._conn = conn
        self._indexer = indexer or FTS5IndexManager(conn)

    @property
    def indexer(self) -> FTS5IndexManager:
        return self._indexer

    # =========================================================================
    # EVIDENCE WRITES
    # =========================================================================

    def add_evidence(self, item: EvidenceCreate) -> str:
        """Record evidence ensuring zero secrets in raw snippets."""
        _check_no_secrets(item.raw_content_snippet)
        _check_no_secrets(item.source_reference)

        evidence_id = str(uuid4())
        content_hash = hashlib.sha256(item.raw_content_snippet.encode("utf-8")).hexdigest()
        now = _utc_now_iso()

        sql = """
        INSERT INTO memory_evidence (
            evidence_id, project_id, evidence_type, source_reference,
            raw_content_snippet, content_hash, is_tainted, trust_score, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        self._conn.execute(
            sql,
            (
                evidence_id,
                item.project_id,
                item.evidence_type,
                item.source_reference,
                item.raw_content_snippet,
                content_hash,
                1 if item.is_tainted else 0,
                item.trust_score,
                now,
            ),
        )
        return evidence_id

    # =========================================================================
    # FACT WRITES
    # =========================================================================

    def add_fact(self, item: FactCreate) -> str:
        """Persist a memory fact and index in derived FTS5."""
        _check_no_secrets(item.subject)
        _check_no_secrets(item.predicate)
        _check_no_secrets(item.object)
        for tag in item.tags:
            _check_no_secrets(tag)

        fact_id = str(uuid4())
        now = _utc_now_iso()
        valid_from = item.valid_from or now

        sql = """
        INSERT INTO memory_facts (
            fact_id, project_id, trust_level, category, subject, predicate, object,
            confidence, importance, provenance_source, evidence_id,
            is_superseded, superseded_by, superseded_reason, valid_from, valid_until,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?);
        """
        self._conn.execute(
            sql,
            (
                fact_id,
                item.project_id,
                item.trust_level.value,
                item.category,
                item.subject,
                item.predicate,
                item.object,
                item.confidence,
                item.importance,
                item.provenance_source,
                item.evidence_id,
                valid_from,
                item.valid_until,
                now,
                now,
            ),
        )

        record = MemoryRecord(
            memory_id=fact_id,
            category=MemoryCategory.FACT,
            scope=MemoryScope.PROJECT if item.project_id else MemoryScope.GLOBAL,
            project_id=item.project_id,
            trust_level=item.trust_level,
            title=f"{item.subject} {item.predicate}",
            content=f"{item.subject} {item.predicate} {item.object}",
            payload={
                "subject": item.subject,
                "predicate": item.predicate,
                "object": item.object,
                "fact_category": item.category,
            },
            provenance_source=item.provenance_source,
            evidence_id=item.evidence_id,
            confidence=item.confidence,
            importance=item.importance,
            is_superseded=False,
            valid_from=valid_from,
            valid_until=item.valid_until,
            tags=item.tags,
            created_at=now,
            updated_at=now,
        )
        self._indexer.index_record(record)
        return fact_id

    def supersede_fact(
        self,
        fact_id: str,
        successor_fact_id: str,
        reason: Optional[str] = None,
    ) -> None:
        """Mark a fact as superseded by a successor fact."""
        now = _utc_now_iso()
        sql = """
        UPDATE memory_facts
        SET is_superseded = 1, superseded_by = ?, superseded_reason = ?, updated_at = ?
        WHERE fact_id = ?;
        """
        self._conn.execute(sql, (successor_fact_id, reason, now, fact_id))
        self._indexer.mark_superseded(fact_id)

    # =========================================================================
    # PREFERENCE WRITES
    # =========================================================================

    def add_preference(self, item: PreferenceCreate) -> str:
        """Persist user preference and index in derived FTS5."""
        _check_no_secrets(item.preference_key)
        _check_no_secrets(item.preference_value)

        pref_id = str(uuid4())
        now = _utc_now_iso()

        sql = """
        INSERT INTO user_preferences (
            preference_id, project_id, domain, preference_key, preference_value,
            preference_type, trust_level, is_superseded, superseded_by,
            source_evidence_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?);
        """
        self._conn.execute(
            sql,
            (
                pref_id,
                item.project_id,
                item.domain,
                item.preference_key,
                item.preference_value,
                item.preference_type,
                item.trust_level.value,
                item.source_evidence_id,
                now,
                now,
            ),
        )

        record = MemoryRecord(
            memory_id=pref_id,
            category=MemoryCategory.PREFERENCE,
            scope=MemoryScope.PROJECT if item.project_id else MemoryScope.GLOBAL,
            project_id=item.project_id,
            trust_level=item.trust_level,
            title=item.preference_key,
            content=f"{item.preference_key} = {item.preference_value}",
            payload={
                "domain": item.domain,
                "key": item.preference_key,
                "value": item.preference_value,
                "type": item.preference_type,
            },
            provenance_source="user",
            evidence_id=item.source_evidence_id,
            confidence=1.0,
            importance=0.8,
            is_superseded=False,
            valid_from=now,
            tags=(item.domain, item.preference_key),
            created_at=now,
            updated_at=now,
        )
        self._indexer.index_record(record)
        return pref_id

    def supersede_preference(self, pref_id: str, successor_pref_id: str) -> None:
        """Mark a preference as superseded by a successor preference."""
        now = _utc_now_iso()
        sql = """
        UPDATE user_preferences
        SET is_superseded = 1, superseded_by = ?, updated_at = ?
        WHERE preference_id = ?;
        """
        self._conn.execute(sql, (successor_pref_id, now, pref_id))
        self._indexer.mark_superseded(pref_id)

    # =========================================================================
    # ENTITY & RELATION WRITES
    # =========================================================================

    def add_entity(self, item: EntityCreate) -> str:
        """Persist a named entity and index in derived FTS5."""
        _check_no_secrets(item.name)
        _check_no_secrets(item.canonical_slug)
        if item.description:
            _check_no_secrets(item.description)
        if item.metadata_json:
            _check_no_secrets(item.metadata_json)

        entity_id = str(uuid4())
        now = _utc_now_iso()

        sql = """
        INSERT INTO entities (
            entity_id, project_id, entity_type, name, canonical_slug,
            description, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        self._conn.execute(
            sql,
            (
                entity_id,
                item.project_id,
                item.entity_type,
                item.name,
                item.canonical_slug,
                item.description,
                item.metadata_json,
                now,
                now,
            ),
        )

        content = f"{item.name} ({item.canonical_slug}): {item.description or ''}"
        record = MemoryRecord(
            memory_id=entity_id,
            category=MemoryCategory.ENTITY,
            scope=MemoryScope.PROJECT if item.project_id else MemoryScope.GLOBAL,
            project_id=item.project_id,
            trust_level=MemoryTrustLevel.M2,
            title=item.name,
            content=content,
            payload={
                "name": item.name,
                "slug": item.canonical_slug,
                "type": item.entity_type,
            },
            provenance_source="system",
            confidence=1.0,
            importance=0.6,
            is_superseded=False,
            valid_from=now,
            tags=(item.entity_type, item.canonical_slug),
            created_at=now,
            updated_at=now,
        )
        self._indexer.index_record(record)
        return entity_id

    def add_relation(self, item: RelationCreate) -> str:
        """Persist a relation between entities in the knowledge graph."""
        relation_id = str(uuid4())
        now = _utc_now_iso()
        valid_from = item.valid_from or now

        sql = """
        INSERT INTO entity_relationships (
            relation_id, project_id, source_entity_id, target_entity_id,
            relation_type, weight, trust_level, valid_from, valid_until, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        self._conn.execute(
            sql,
            (
                relation_id,
                item.project_id,
                item.source_entity_id,
                item.target_entity_id,
                item.relation_type,
                item.weight,
                item.trust_level.value,
                valid_from,
                item.valid_until,
                now,
            ),
        )
        return relation_id

    # =========================================================================
    # READ / RECONSTRUCTION
    # =========================================================================

    def get_fact(self, fact_id: str) -> Optional[MemoryRecord]:
        """Fetch fact by ID and wrap as MemoryRecord."""
        sql = """
        SELECT fact_id, project_id, trust_level, category, subject, predicate, object,
               confidence, importance, provenance_source, evidence_id,
               is_superseded, superseded_by, superseded_reason, valid_from, valid_until,
               created_at, updated_at
        FROM memory_facts
        WHERE fact_id = ?;
        """
        cur = self._conn.execute(sql, (fact_id,))
        row = cur.fetchone()
        if not row:
            return None

        return MemoryRecord(
            memory_id=row[0],
            category=MemoryCategory.FACT,
            scope=MemoryScope.PROJECT if row[1] else MemoryScope.GLOBAL,
            project_id=row[1],
            trust_level=MemoryTrustLevel(row[2]),
            title=f"{row[4]} {row[5]}",
            content=f"{row[4]} {row[5]} {row[6]}",
            payload={
                "subject": row[4],
                "predicate": row[5],
                "object": row[6],
                "category": row[3],
            },
            provenance_source=row[9],
            evidence_id=row[10],
            confidence=float(row[7]),
            importance=float(row[8]),
            is_superseded=bool(row[11]),
            superseded_by=row[12],
            superseded_reason=row[13],
            valid_from=row[14],
            valid_until=row[15],
            tags=(row[3], row[5]),
            created_at=row[16],
            updated_at=row[17],
        )

    def get_preference(self, pref_id: str) -> Optional[MemoryRecord]:
        """Fetch preference by ID and wrap as MemoryRecord."""
        sql = """
        SELECT preference_id, project_id, domain, preference_key, preference_value,
               preference_type, trust_level, is_superseded, superseded_by,
               source_evidence_id, created_at, updated_at
        FROM user_preferences
        WHERE preference_id = ?;
        """
        cur = self._conn.execute(sql, (pref_id,))
        row = cur.fetchone()
        if not row:
            return None

        return MemoryRecord(
            memory_id=row[0],
            category=MemoryCategory.PREFERENCE,
            scope=MemoryScope.PROJECT if row[1] else MemoryScope.GLOBAL,
            project_id=row[1],
            trust_level=MemoryTrustLevel(row[6]),
            title=row[3],
            content=f"{row[3]} = {row[4]}",
            payload={
                "domain": row[2],
                "key": row[3],
                "value": row[4],
                "type": row[5],
            },
            provenance_source="user",
            evidence_id=row[9],
            confidence=1.0,
            importance=0.8,
            is_superseded=bool(row[7]),
            superseded_by=row[8],
            valid_from=row[10],
            tags=(row[2], row[3]),
            created_at=row[10],
            updated_at=row[11],
        )

    def get_entity(self, entity_id: str) -> Optional[MemoryRecord]:
        """Fetch entity by ID and wrap as MemoryRecord."""
        sql = """
        SELECT entity_id, project_id, entity_type, name, canonical_slug,
               description, metadata_json, created_at, updated_at
        FROM entities
        WHERE entity_id = ?;
        """
        cur = self._conn.execute(sql, (entity_id,))
        row = cur.fetchone()
        if not row:
            return None

        content = f"{row[3]} ({row[4]}): {row[5] or ''}"
        return MemoryRecord(
            memory_id=row[0],
            category=MemoryCategory.ENTITY,
            scope=MemoryScope.PROJECT if row[1] else MemoryScope.GLOBAL,
            project_id=row[1],
            trust_level=MemoryTrustLevel.M2,
            title=row[3],
            content=content,
            payload={
                "name": row[3],
                "slug": row[4],
                "type": row[2],
            },
            provenance_source="system",
            confidence=1.0,
            importance=0.6,
            is_superseded=False,
            valid_from=row[7],
            tags=(row[2], row[4]),
            created_at=row[7],
            updated_at=row[8],
        )

    def get_record(self, record_id: str) -> Optional[MemoryRecord]:
        """Fetch any memory record by ID across facts, preferences, and entities."""
        fact = self.get_fact(record_id)
        if fact:
            return fact
        pref = self.get_preference(record_id)
        if pref:
            return pref
        return self.get_entity(record_id)

    def rebuild_index(self) -> int:
        """Completely rebuild derived FTS5 index from canonical relational tables."""
        return self._indexer.rebuild_index()
