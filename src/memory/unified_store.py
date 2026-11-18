"""SAGE Unified Memory Store -- Authoritative Persistent Storage Layer.

Canonical Reference:
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
- PROMPT 068: SAGE Unified Memory Engine

Invariants:
- Canonical storage resides in relational SQLite tables (unified_memories, memory_relationships).
- Derived FTS5 virtual table (unified_memories_fts) provides rapid lexical matching.
- Raw secrets are strictly prohibited: SecretLeakError raised prior to persistence.
- Trust levels strictly frozen at M0-M4.
- Historical records are preserved via supersession chains.
- Identity and access scopes strictly enforced (e.g., contact:<jid>, user:<id>, project:<id>).
- Deterministic, offline, and thread-safe via transaction boundaries.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from uuid import uuid4

from src.memory.indexer import sanitize_fts5_query
from src.memory.models import (
    CognitiveMemoryCategory,
    MemoryCategory,
    MemoryProvenanceType,
    MemoryRelationship,
    MemorySensitivity,
    UnifiedMemoryQuery,
    UnifiedMemoryRecord,
    map_to_cognitive_category,
)
from src.storage.database import transaction
from src.storage.trust_levels import MemoryTrustLevel
from src.trust.audit import _check_no_secrets
from src.trust.errors import SecretLeakError

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


class UnifiedMemoryStore:
    """Canonical persistence and search repository for SAGE Unified Memory."""

    TABLE_MEMORIES = "unified_memories"
    TABLE_RELATIONSHIPS = "memory_relationships"
    TABLE_FTS = "unified_memories_fts"

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialize the unified memory store with an active SQLite connection."""
        self._conn = conn
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create canonical relational and auxiliary FTS5 tables and indexes if missing."""
        with transaction(self._conn, immediate=True) as cur:
            # 1. Main Unified Memories Table
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_MEMORIES} (
                    memory_id           TEXT PRIMARY KEY,
                    category            TEXT NOT NULL,
                    cognitive_category  TEXT NOT NULL,
                    content             TEXT NOT NULL,
                    summary             TEXT NULL,
                    source              TEXT NOT NULL,
                    source_reference    TEXT NULL,
                    provenance_type     TEXT NOT NULL,
                    trust_level         TEXT NOT NULL,
                    confidence          REAL NOT NULL DEFAULT 1.0,
                    importance          REAL NOT NULL DEFAULT 0.5,
                    sensitivity         TEXT NOT NULL DEFAULT 'internal',
                    access_scope        TEXT NOT NULL DEFAULT 'global',
                    owner_id            TEXT NULL,
                    project_id          TEXT NULL,
                    task_id             TEXT NULL,
                    session_id          TEXT NULL,
                    retention_policy    TEXT NOT NULL DEFAULT 'permanent',
                    is_superseded       INTEGER NOT NULL DEFAULT 0,
                    superseded_by       TEXT NULL,
                    superseded_reason   TEXT NULL,
                    valid_from          TEXT NOT NULL,
                    valid_until         TEXT NULL,
                    tags_json           TEXT NOT NULL DEFAULT '[]',
                    metadata_json       TEXT NOT NULL DEFAULT '{{}}',
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL
                );
                """
            )

            # Indexes on Main Table
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_unified_mem_cog ON {self.TABLE_MEMORIES} (cognitive_category);"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_unified_mem_cat ON {self.TABLE_MEMORIES} (category);"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_unified_mem_access ON {self.TABLE_MEMORIES} (access_scope);"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_unified_mem_owner ON {self.TABLE_MEMORIES} (owner_id);"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_unified_mem_proj ON {self.TABLE_MEMORIES} (project_id);"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_unified_mem_task ON {self.TABLE_MEMORIES} (task_id);"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_unified_mem_created ON {self.TABLE_MEMORIES} (created_at);"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_unified_mem_superseded ON {self.TABLE_MEMORIES} (is_superseded);"
            )

            # 2. Memory Relationships Table
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_RELATIONSHIPS} (
                    relationship_id     TEXT PRIMARY KEY,
                    source_memory_id    TEXT NOT NULL,
                    target_memory_id    TEXT NOT NULL,
                    relation_type       TEXT NOT NULL,
                    weight              REAL NOT NULL DEFAULT 1.0,
                    metadata_json       TEXT NOT NULL DEFAULT '{{}}',
                    created_at          TEXT NOT NULL,
                    FOREIGN KEY (source_memory_id) REFERENCES {self.TABLE_MEMORIES} (memory_id) ON DELETE CASCADE,
                    FOREIGN KEY (target_memory_id) REFERENCES {self.TABLE_MEMORIES} (memory_id) ON DELETE CASCADE
                );
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_mem_rel_src ON {self.TABLE_RELATIONSHIPS} (source_memory_id);"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_mem_rel_tgt ON {self.TABLE_RELATIONSHIPS} (target_memory_id);"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_mem_rel_type ON {self.TABLE_RELATIONSHIPS} (relation_type);"
            )

            # 3. FTS5 Virtual Table for rapid text matching
            cur.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {self.TABLE_FTS} USING fts5(
                    memory_id UNINDEXED,
                    content,
                    summary,
                    tags,
                    tokenize = 'porter unicode61'
                );
                """
            )

    # =========================================================================
    # RECORD PERSISTENCE & MUTATION
    # =========================================================================

    def store_record(self, record: UnifiedMemoryRecord) -> str:
        """Store or replace a unified memory record, validating zero secrets."""
        _check_no_secrets(record.content)
        if record.summary:
            _check_no_secrets(record.summary)
        if record.tags:
            _check_no_secrets(" ".join(record.tags))
        meta_d = dict(record.metadata)
        if record.subject:
            meta_d["subject"] = record.subject
        if record.predicate:
            meta_d["predicate"] = record.predicate
        meta_str = json.dumps(meta_d, sort_keys=True)
        _check_no_secrets(meta_str)

        tags_json = json.dumps(list(record.tags))

        with transaction(self._conn, immediate=True) as cur:
            cur.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_MEMORIES} (
                    memory_id, category, cognitive_category, content, summary,
                    source, source_reference, provenance_type, trust_level,
                    confidence, importance, sensitivity, access_scope, owner_id,
                    project_id, task_id, session_id, retention_policy,
                    is_superseded, superseded_by, superseded_reason,
                    valid_from, valid_until, tags_json, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    record.memory_id,
                    record.category.value if hasattr(record.category, "value") else str(record.category),
                    record.cognitive_category.value
                    if hasattr(record.cognitive_category, "value")
                    else str(record.cognitive_category),
                    record.content,
                    record.summary,
                    record.source,
                    record.source_reference,
                    record.provenance_type.value
                    if hasattr(record.provenance_type, "value")
                    else str(record.provenance_type),
                    record.trust_level.value
                    if hasattr(record.trust_level, "value")
                    else str(record.trust_level),
                    record.confidence,
                    record.importance,
                    record.sensitivity.value
                    if hasattr(record.sensitivity, "value")
                    else str(record.sensitivity),
                    record.access_scope,
                    record.owner_id,
                    record.project_id,
                    record.task_id,
                    record.session_id,
                    record.retention_policy,
                    1 if record.is_superseded else 0,
                    record.superseded_by,
                    record.superseded_reason,
                    record.valid_from,
                    record.valid_until,
                    tags_json,
                    meta_str,
                    record.created_at,
                    record.updated_at,
                ),
            )

            # Update FTS5 auxiliary index
            cur.execute(f"DELETE FROM {self.TABLE_FTS} WHERE memory_id = ?;", (record.memory_id,))
            cur.execute(
                f"INSERT INTO {self.TABLE_FTS} (memory_id, content, summary, tags) VALUES (?, ?, ?, ?);",
                (record.memory_id, record.content, record.summary or "", " ".join(record.tags)),
            )

        return record.memory_id

    def get_record(self, memory_id: str) -> Optional[UnifiedMemoryRecord]:
        """Retrieve a unified memory record by its primary ID."""
        cursor = self._conn.execute(
            f"SELECT * FROM {self.TABLE_MEMORIES} WHERE memory_id = ?;", (memory_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_record(cursor, row)

    def supersede_record(
        self,
        old_memory_id: str,
        new_memory_id: str | UnifiedMemoryRecord,
        reason: str = "superseded_by_newer_record",
    ) -> str:
        """Mark an existing memory as superseded by a successor memory record."""
        if isinstance(new_memory_id, UnifiedMemoryRecord):
            target_id = new_memory_id.memory_id
            self.store_record(new_memory_id)
        else:
            target_id = str(new_memory_id)

        now = _utc_now_iso()
        with transaction(self._conn, immediate=True) as cur:
            cur.execute(
                f"""
                UPDATE {self.TABLE_MEMORIES}
                SET is_superseded = 1,
                    superseded_by = ?,
                    superseded_reason = ?,
                    updated_at = ?
                WHERE memory_id = ? AND is_superseded = 0;
                """,
                (target_id, reason, now, old_memory_id),
            )
            return target_id

    def delete_record(self, memory_id: str, hard_delete: bool = False) -> bool:
        """Delete a memory record.

        If hard_delete is False (default), marks record as superseded / expired.
        If hard_delete is True, purges record from database, relationships, and FTS.
        """
        if not hard_delete:
            now = _utc_now_iso()
            with transaction(self._conn, immediate=True) as cur:
                res = cur.execute(
                    f"""
                    UPDATE {self.TABLE_MEMORIES}
                    SET is_superseded = 1,
                        superseded_reason = 'user_deleted',
                        valid_until = ?,
                        updated_at = ?
                    WHERE memory_id = ?;
                    """,
                    (now, now, memory_id),
                )
                return res.rowcount > 0
        else:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    f"DELETE FROM {self.TABLE_RELATIONSHIPS} WHERE source_memory_id = ? OR target_memory_id = ?;",
                    (memory_id, memory_id),
                )
                cur.execute(f"DELETE FROM {self.TABLE_FTS} WHERE memory_id = ?;", (memory_id,))
                res = cur.execute(
                    f"DELETE FROM {self.TABLE_MEMORIES} WHERE memory_id = ?;", (memory_id,)
                )
                return res.rowcount > 0

    # =========================================================================
    # RELATIONSHIP PERSISTENCE & TRAVERSAL
    # =========================================================================

    def add_relationship(self, rel: MemoryRelationship) -> str:
        """Persist a directed relationship edge between memory records."""
        meta_str = json.dumps(rel.metadata, sort_keys=True)
        _check_no_secrets(meta_str)

        with transaction(self._conn, immediate=True) as cur:
            cur.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_RELATIONSHIPS} (
                    relationship_id, source_memory_id, target_memory_id,
                    relation_type, weight, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    rel.relationship_id,
                    rel.source_memory_id,
                    rel.target_memory_id,
                    rel.relation_type,
                    rel.weight,
                    meta_str,
                    rel.created_at,
                ),
            )
        return rel.relationship_id

    def get_relationships(
        self,
        memory_id: str,
        direction: str = "both",
    ) -> List[MemoryRelationship]:
        """Fetch relationship edges for a memory record."""
        if direction == "outgoing":
            sql = f"SELECT * FROM {self.TABLE_RELATIONSHIPS} WHERE source_memory_id = ?;"
            params = (memory_id,)
        elif direction == "incoming":
            sql = f"SELECT * FROM {self.TABLE_RELATIONSHIPS} WHERE target_memory_id = ?;"
            params = (memory_id,)
        else:
            sql = (
                f"SELECT * FROM {self.TABLE_RELATIONSHIPS} "
                f"WHERE source_memory_id = ? OR target_memory_id = ?;"
            )
            params = (memory_id, memory_id)

        cursor = self._conn.execute(sql, params)
        cols = [d[0] for d in cursor.description]
        results = []
        for row in cursor.fetchall():
            row_dict = dict(zip(cols, row))
            results.append(
                MemoryRelationship(
                    relationship_id=row_dict["relationship_id"],
                    source_memory_id=row_dict["source_memory_id"],
                    target_memory_id=row_dict["target_memory_id"],
                    relation_type=row_dict["relation_type"],
                    weight=float(row_dict["weight"]),
                    metadata=json.loads(row_dict["metadata_json"] or "{}"),
                    created_at=row_dict["created_at"],
                )
            )
        return results

    # =========================================================================
    # LEXICAL & FTS5 SEARCH
    # =========================================================================

    def search_fts(self, query_text: str, limit: int = 50) -> List[Tuple[str, float]]:
        """Perform full-text search using SQLite FTS5 index.

        Returns:
            List of (memory_id, bm25_score) ordered by descending relevance.
        """
        sanitized = sanitize_fts5_query(query_text)
        if not sanitized:
            return []

        sql = f"""
        SELECT memory_id, bm25({self.TABLE_FTS}) as score
        FROM {self.TABLE_FTS}
        WHERE {self.TABLE_FTS} MATCH ?
        ORDER BY score ASC
        LIMIT ?;
        """
        try:
            cursor = self._conn.execute(sql, (sanitized, limit))
            # bm25 returns negative numbers in FTS5 where lower is better; invert to positive score
            matches = []
            for row in cursor.fetchall():
                mid, raw_score = row[0], row[1]
                norm_score = max(0.01, 1.0 / (1.0 + max(0.0, raw_score)))
                matches.append((mid, norm_score))
            return matches
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 query failed: %s (sanitized: %r)", exc, sanitized)
            return []

    # =========================================================================
    # STRUCTURED QUERYING & FILTERING
    # =========================================================================

    def query_records(self, query: UnifiedMemoryQuery) -> List[UnifiedMemoryRecord]:
        """Query unified memories applying structured constraints and filters."""
        clauses = []
        params: List[Any] = []

        # 1. Superseded filter
        if not query.include_superseded:
            clauses.append("is_superseded = 0")

        # 2. Expiration filter
        now_iso = query.as_of_time or _utc_now_iso()
        if not query.include_expired:
            clauses.append("(valid_until IS NULL OR valid_until >= ?)")
            params.append(now_iso)
        clauses.append("valid_from <= ?")
        params.append(now_iso)

        # 3. Cognitive Category filter
        if query.cognitive_categories:
            placeholders = ", ".join("?" for _ in query.cognitive_categories)
            clauses.append(f"cognitive_category IN ({placeholders})")
            params.extend(c.value for c in query.cognitive_categories)

        # 4. Granular Category filter
        if query.categories:
            placeholders = ", ".join("?" for _ in query.categories)
            clauses.append(f"category IN ({placeholders})")
            params.extend(c.value for c in query.categories)

        # 5. Access Scope and Owner Isolation
        # Isolation invariant: if owner_id is provided (e.g. WhatsApp contact JID),
        # only records matching owner_id OR global records are accessible.
        if query.owner_id is not None:
            clauses.append("(owner_id = ? OR access_scope = 'global' OR access_scope IS NULL)")
            params.append(query.owner_id)
        elif query.access_scope is not None:
            clauses.append("(access_scope = ? OR access_scope = 'global')")
            params.append(query.access_scope)

        # 6. Project Scope filter
        if query.project_id is not None:
            clauses.append("(project_id = ? OR project_id IS NULL)")
            params.append(query.project_id)

        # 7. Task Scope filter
        if query.task_id is not None:
            clauses.append("(task_id = ? OR task_id IS NULL)")
            params.append(query.task_id)

        # 8. Session Scope filter
        if query.session_id is not None:
            clauses.append("(session_id = ? OR session_id IS NULL)")
            params.append(query.session_id)

        # 9. Confidence and Importance thresholds
        if query.min_confidence > 0.0:
            clauses.append("confidence >= ?")
            params.append(query.min_confidence)
        if query.min_importance > 0.0:
            clauses.append("importance >= ?")
            params.append(query.min_importance)

        # 10. Sensitivity ceiling
        if query.max_sensitivity is not None:
            allowed_sensitivities = self._allowed_sensitivities(query.max_sensitivity)
            placeholders = ", ".join("?" for _ in allowed_sensitivities)
            clauses.append(f"sensitivity IN ({placeholders})")
            params.extend(allowed_sensitivities)

        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT * FROM {self.TABLE_MEMORIES}
        {where_clause}
        ORDER BY importance DESC, updated_at DESC
        LIMIT ?;
        """
        params.append(query.limit)

        cursor = self._conn.execute(sql, tuple(params))
        return [self._row_to_record(cursor, r) for r in cursor.fetchall()]

    def list_all(self, limit: int = 100) -> List[UnifiedMemoryRecord]:
        """List active records up to limit."""
        cursor = self._conn.execute(
            f"SELECT * FROM {self.TABLE_MEMORIES} WHERE is_superseded = 0 ORDER BY updated_at DESC LIMIT ?;",
            (limit,),
        )
        return [self._row_to_record(cursor, r) for r in cursor.fetchall()]

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _allowed_sensitivities(self, max_sens: MemorySensitivity) -> List[str]:
        tiers = [
            MemorySensitivity.PUBLIC.value,
            MemorySensitivity.INTERNAL.value,
            MemorySensitivity.CONFIDENTIAL.value,
            MemorySensitivity.RESTRICTED.value,
        ]
        target = max_sens.value if hasattr(max_sens, "value") else str(max_sens)
        if target in tiers:
            idx = tiers.index(target)
            return tiers[: idx + 1]
        return tiers[:2]

    def _row_to_record(self, cursor: sqlite3.Cursor, row: Tuple[Any, ...]) -> UnifiedMemoryRecord:
        cols = [d[0] for d in cursor.description]
        d = dict(zip(cols, row))
        tags = tuple(json.loads(d.get("tags_json") or "[]"))
        meta = json.loads(d.get("metadata_json") or "{}")

        cat_val = d["category"]
        try:
            category = MemoryCategory(cat_val)
        except ValueError:
            category = MemoryCategory.FACT

        cog_val = d["cognitive_category"]
        try:
            cognitive_category = CognitiveMemoryCategory(cog_val)
        except ValueError:
            cognitive_category = map_to_cognitive_category(category)

        prov_val = d["provenance_type"]
        try:
            provenance_type = MemoryProvenanceType(prov_val)
        except ValueError:
            provenance_type = MemoryProvenanceType.EXTRACTED

        sens_val = d["sensitivity"]
        try:
            sensitivity = MemorySensitivity(sens_val)
        except ValueError:
            sensitivity = MemorySensitivity.INTERNAL

        trust_val = d["trust_level"]
        try:
            trust_level = MemoryTrustLevel(trust_val)
        except ValueError:
            trust_level = MemoryTrustLevel.M1

        return UnifiedMemoryRecord(
            memory_id=d["memory_id"],
            category=category,
            cognitive_category=cognitive_category,
            content=d["content"],
            summary=d["summary"],
            source=d["source"],
            source_reference=d["source_reference"],
            provenance_type=provenance_type,
            trust_level=trust_level,
            confidence=float(d["confidence"]),
            importance=float(d["importance"]),
            sensitivity=sensitivity,
            access_scope=d["access_scope"],
            owner_id=d["owner_id"],
            project_id=d["project_id"],
            task_id=d["task_id"],
            session_id=d["session_id"],
            retention_policy=d["retention_policy"],
            is_superseded=bool(d["is_superseded"]),
            superseded_by=d["superseded_by"],
            superseded_reason=d["superseded_reason"],
            valid_from=d["valid_from"],
            valid_until=d["valid_until"],
            tags=tags,
            metadata=meta,
            subject=d.get("subject") or meta.get("subject"),
            predicate=d.get("predicate") or meta.get("predicate"),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
        )
