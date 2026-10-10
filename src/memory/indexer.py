"""SAGE EKM Full-Text Search (FTS5) Derived Indexer.

Canonical Reference:
- docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md §2 (Derived Invariant)
- docs/specifications/AEGIS_DATA_CLASSIFICATION_MATRIX.md (DERIVED_REBUILDABLE)

Invariants:
- Derived indexes (FTS5) are NEVER authoritative storage.
- 100% rebuildable from authoritative relational tables (facts, preferences, entities).
- Safe escaping of raw queries prevents syntax crashes on punctuation/special tokens.
- Scope-aware search (project-scoped or global).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import List, Optional, Tuple

from src.memory.models import MemoryRecord

logger = logging.getLogger(__name__)

# Pattern to strip or escape problematic FTS5 characters
_FTS5_SPECIAL_CHARS = re.compile(r'["*^:()\[\]{}]')


def _expand_camel_case(text: str) -> str:
    """Split CamelCase words so both compound and constituent tokens can match in FTS5."""
    split = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    if split != text:
        return f"{text} {split}"
    return text


def sanitize_fts5_query(query: str) -> str:
    """Sanitize raw input text for safe SQLite FTS5 matching.

    Replaces punctuation and syntax tokens with spaces and combines tokens
    with OR/AND logic so malformed user input never crashes FTS5.
    """
    expanded = _expand_camel_case(query)
    cleaned = _FTS5_SPECIAL_CHARS.sub(" ", expanded).strip()
    words = [w.strip() for w in cleaned.split() if w.strip()]
    if not words:
        return ""
    # Safe prefix matching for words
    safe_terms = [f'"{w}"*' for w in words]
    return " OR ".join(safe_terms)


class FTS5IndexManager:
    """Manages the derived SQLite FTS5 virtual table for rapid lexical search."""

    FTS_TABLE_NAME = "ekm_fts"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.ensure_table()

    def ensure_table(self) -> None:
        """Create the FTS5 virtual table if it does not exist."""
        sql = f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {self.FTS_TABLE_NAME} USING fts5(
            record_id UNINDEXED,
            category UNINDEXED,
            project_id UNINDEXED,
            is_superseded UNINDEXED,
            title,
            content,
            tags,
            tokenize='porter unicode61'
        );
        """
        try:
            self._conn.execute(sql)
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 table creation warning: %s", exc)

    def index_record(self, record: MemoryRecord) -> None:
        """Insert or update a record in the derived FTS5 index."""
        self.remove_record(record.memory_id)
        tags_str = " ".join(record.tags)
        expanded_title = _expand_camel_case(record.title)
        expanded_content = _expand_camel_case(record.content)

        sql = f"""
        INSERT INTO {self.FTS_TABLE_NAME} (
            record_id, category, project_id, is_superseded, title, content, tags
        ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        self._conn.execute(
            sql,
            (
                record.memory_id,
                record.category.value,
                record.project_id,
                1 if record.is_superseded else 0,
                expanded_title,
                expanded_content,
                tags_str,
            ),
        )

    def mark_superseded(self, record_id: str) -> None:
        """Mark record as superseded in FTS5 index."""
        sql = f"UPDATE {self.FTS_TABLE_NAME} SET is_superseded = 1 WHERE record_id = ?;"
        try:
            self._conn.execute(sql, (record_id,))
        except sqlite3.OperationalError:
            pass

    def remove_record(self, record_id: str) -> None:
        """Remove a record from the FTS5 index by record_id."""
        sql = f"DELETE FROM {self.FTS_TABLE_NAME} WHERE record_id = ?;"
        try:
            self._conn.execute(sql, (record_id,))
        except sqlite3.OperationalError:
            pass

    def search(
        self,
        query_text: str,
        project_id: Optional[str] = None,
        include_global: bool = True,
        include_superseded: bool = False,
        limit: int = 20,
    ) -> List[Tuple[str, float]]:
        """Search the FTS5 index and return (record_id, score) pairs.

        Score is normalized so higher is better.
        """
        safe_query = sanitize_fts5_query(query_text)
        if not safe_query:
            return []

        # Construct scope and supersession filtering
        clauses = [f"{self.FTS_TABLE_NAME} MATCH ?"]
        params: List[object] = [safe_query]

        if not include_superseded:
            clauses.append("is_superseded = 0")

        if project_id is not None:
            if include_global:
                clauses.append("(project_id = ? OR project_id IS NULL)")
                params.append(project_id)
            else:
                clauses.append("project_id = ?")
                params.append(project_id)
        elif not include_global:
            clauses.append("project_id IS NOT NULL")

        where_clause = " AND ".join(clauses)
        params.append(max(1, min(limit, 100)))

        # SQLite FTS5 bm25() returns negative numbers where smaller/more negative is better.
        # We invert it: -bm25(...) so higher is better.
        sql = f"""
        SELECT record_id, -bm25({self.FTS_TABLE_NAME}) AS score
        FROM {self.FTS_TABLE_NAME}
        WHERE {where_clause}
        ORDER BY score DESC
        LIMIT ?;
        """
        try:
            cursor = self._conn.execute(sql, params)
            return [(str(row[0]), float(row[1])) for row in cursor.fetchall()]
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 search error on query %r: %s", query_text, exc)
            return []

    def rebuild_index(self) -> int:
        """Drop and completely rebuild the FTS5 index from canonical relational tables.

        Returns total count of indexed records.
        """
        try:
            self._conn.execute(f"DROP TABLE IF EXISTS {self.FTS_TABLE_NAME};")
        except sqlite3.OperationalError:
            pass
        self.ensure_table()

        total = 0

        # 1. Index memory_facts
        try:
            facts_cur = self._conn.execute(
                "SELECT fact_id, category, project_id, is_superseded, subject, predicate, object "
                "FROM memory_facts;"
            )
            for row in facts_cur.fetchall():
                f_id, cat, proj, is_sup, subj, pred, obj = row
                content = _expand_camel_case(f"{subj} {pred} {obj}")
                title = _expand_camel_case(f"{subj} {pred}")
                self._conn.execute(
                    f"INSERT INTO {self.FTS_TABLE_NAME} ("
                    f"record_id, category, project_id, is_superseded, title, content, tags) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (f_id, "fact", proj, is_sup, title, content, f"{cat} {pred}"),
                )
                total += 1
        except sqlite3.OperationalError:
            pass

        # 2. Index user_preferences
        try:
            pref_cur = self._conn.execute(
                "SELECT preference_id, domain, project_id, is_superseded, "
                "preference_key, preference_value FROM user_preferences;"
            )
            for row in pref_cur.fetchall():
                p_id, dom, proj, is_sup, key, val = row
                content = _expand_camel_case(f"{key} = {val}")
                title = _expand_camel_case(key)
                self._conn.execute(
                    f"INSERT INTO {self.FTS_TABLE_NAME} ("
                    f"record_id, category, project_id, is_superseded, title, content, tags) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (p_id, "preference", proj, is_sup, title, content, dom),
                )
                total += 1
        except sqlite3.OperationalError:
            pass

        # 3. Index entities
        try:
            ent_cur = self._conn.execute(
                "SELECT entity_id, entity_type, project_id, name, canonical_slug, description "
                "FROM entities;"
            )
            for row in ent_cur.fetchall():
                e_id, e_type, proj, name, slug, desc = row
                content = _expand_camel_case(f"{name} ({slug}): {desc or ''}")
                title = _expand_camel_case(name)
                self._conn.execute(
                    f"INSERT INTO {self.FTS_TABLE_NAME} ("
                    f"record_id, category, project_id, is_superseded, title, content, tags) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (e_id, "entity", proj, 0, title, content, f"{e_type} {slug}"),
                )
                total += 1
        except sqlite3.OperationalError:
            pass

        return total
