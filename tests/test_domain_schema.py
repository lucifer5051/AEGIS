"""AEGIS Domain Schema Foundation Tests.

Covers: projects, memory_evidence, memory_facts, user_preferences,
        entities, entity_relationships, conversations, messages.

All tests operate on temporary in-memory or temp-file databases.
The production runtime DB (%USERPROFILE%\\.aegis\\data\\aegis.db) is NEVER touched.

Document Reference: docs/specifications/AEGIS_DOMAIN_SCHEMA_FOUNDATION.md
Architecture Reference: docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md
"""

import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, List

import pytest

from src.storage.database import (
    create_connection,
    transaction,
)
from src.storage.health import DatabaseHealthState, check_database_health
from src.storage.migrations import (
    apply_migrations,
    bootstrap_schema_migrations,
    compute_sql_checksum,
    get_current_schema_version,
    load_migrations_from_directory,
)
from src.storage.trust_levels import (
    MEMORY_TRUST_LEVEL_SEMANTICS,
    NON_CANONICAL_TRUST_ALIASES,
    MemoryTrustLevel,
    get_trust_level_semantic,
    resolve_trust_level,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

# SQL fragments for INSERT statements (split to honour line-length limits)
_INS_CONV = (
    "INSERT INTO conversations "
    "(conversation_id, project_id, channel, channel_conversation_id,"
    " status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?);"
)
_INS_MSG = (
    "INSERT INTO messages "
    "(message_id, conversation_id, role, channel,"
    " message_seq, content, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?);"
)
_INS_ENTITY = (
    "INSERT INTO entities "
    "(entity_id, project_id, entity_type, name,"
    " canonical_slug, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?);"
)
_INS_PROJ = (
    "INSERT INTO projects "
    "(project_id, name, slug, status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?);"
)
_INS_FACT = (
    "INSERT INTO memory_facts "
    "(fact_id, project_id, trust_level, category, subject, predicate, object,"
    " provenance_source, valid_from, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


@contextmanager
def _tmp_db() -> Generator[sqlite3.Connection, None, None]:
    """Open a temporary file-based SQLite database, apply all migrations, yield connection."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = create_connection(db_path)
        try:
            migrations = load_migrations_from_directory(MIGRATIONS_DIR)
            bootstrap_schema_migrations(conn)
            apply_migrations(conn, migrations)
            yield conn
        finally:
            conn.close()
    finally:
        for ext in ("", "-wal", "-shm"):
            Path(db_path + ext).unlink(missing_ok=True)


def _get_tables(conn: sqlite3.Connection) -> List[str]:
    """Return list of user-created table names."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
    ).fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# 1. MIGRATION LIFECYCLE
# ---------------------------------------------------------------------------


def test_01_all_domain_migrations_apply() -> None:
    """All migrations (0001-0004) apply without error."""
    with _tmp_db() as conn:
        version = get_current_schema_version(conn)
        assert version == 4


def test_02_migration_ordering_is_correct() -> None:
    """Migrations are loaded in strictly ascending version order."""
    migrations = load_migrations_from_directory(MIGRATIONS_DIR)
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)
    assert versions == [1, 2, 3, 4]


def test_03_foreign_keys_enabled() -> None:
    """PRAGMA foreign_keys = ON after migration."""
    with _tmp_db() as conn:
        row = conn.execute("PRAGMA foreign_keys;").fetchone()
        assert row[0] == 1


def test_04_schema_version_after_all_migrations() -> None:
    """Schema version is 4 after all migrations applied."""
    with _tmp_db() as conn:
        assert get_current_schema_version(conn) == 4


def test_05_migration_rerun_is_safe() -> None:
    """Applying migrations twice skips already-applied ones (idempotent)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = create_connection(db_path)
        migrations = load_migrations_from_directory(MIGRATIONS_DIR)
        bootstrap_schema_migrations(conn)
        apply_migrations(conn, migrations)
        # Re-apply — must not raise or duplicate
        applied = apply_migrations(conn, migrations)
        assert len(applied) == 4
        conn.close()
    finally:
        for ext in ("", "-wal", "-shm"):
            Path(db_path + ext).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. TABLE EXISTENCE
# ---------------------------------------------------------------------------


def test_06_expected_domain_tables_exist() -> None:
    """All domain tables are created after migration."""
    expected = {
        "storage_metadata",
        "schema_migrations",
        "projects",
        "memory_evidence",
        "memory_facts",
        "user_preferences",
        "entities",
        "entity_relationships",
        "conversations",
        "messages",
    }
    with _tmp_db() as conn:
        tables = set(_get_tables(conn))
        for t in expected:
            assert t in tables, f"Missing table: {t}"


def test_07_no_unexpected_domain_tables() -> None:
    """Only the expected set of tables exists — no extra domain tables."""
    allowed = {
        "storage_metadata",
        "schema_migrations",
        "projects",
        "memory_evidence",
        "memory_facts",
        "user_preferences",
        "entities",
        "entity_relationships",
        "conversations",
        "messages",
    }
    with _tmp_db() as conn:
        tables = set(_get_tables(conn))
        unexpected = tables - allowed
        assert unexpected == set(), f"Unexpected tables found: {unexpected}"


# ---------------------------------------------------------------------------
# 3. PROJECTS
# ---------------------------------------------------------------------------


def test_08_projects_insert() -> None:
    """Can insert a valid project."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Test Project", "test-project", "ACTIVE", now, now))
        row = conn.execute("SELECT * FROM projects WHERE project_id = ?;", (pid,)).fetchone()
        assert row["name"] == "Test Project"
        assert row["status"] == "ACTIVE"


def test_09_projects_duplicate_id_rejected() -> None:
    """Duplicate project_id is rejected (PRIMARY KEY)."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "P1", "p1", "ACTIVE", now, now))
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(_INS_PROJ, (pid, "P2", "p2", "ACTIVE", now, now))


def test_10_projects_duplicate_slug_rejected() -> None:
    """Duplicate slug is rejected (UNIQUE)."""
    with _tmp_db() as conn:
        now = _now_utc()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (_uuid(), "P1", "unique-slug", "ACTIVE", now, now))
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(_INS_PROJ, (_uuid(), "P2", "unique-slug", "ACTIVE", now, now))


def test_11_projects_invalid_status_rejected() -> None:
    """Invalid project status rejected by CHECK constraint."""
    with _tmp_db() as conn:
        now = _now_utc()
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(_INS_PROJ, (_uuid(), "P", "p-x", "INVALID_STATUS", now, now))


# ---------------------------------------------------------------------------
# 4. MEMORY FACTS
# ---------------------------------------------------------------------------


def test_12_memory_fact_global_scope() -> None:
    """Memory fact with project_id=NULL is global scope."""
    with _tmp_db() as conn:
        now = _now_utc()
        fid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO memory_facts "
                "(fact_id, project_id, trust_level, category, subject, predicate,"
                " object, provenance_source, valid_from, created_at, updated_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (fid, "M4", "user", "User", "prefers", "Python 3.12", "user", now, now, now),
            )
        row = conn.execute("SELECT * FROM memory_facts WHERE fact_id = ?;", (fid,)).fetchone()
        assert row["project_id"] is None
        assert row["trust_level"] == "M4"


def test_13_memory_fact_project_scope() -> None:
    """Memory fact with project_id is project-scoped."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "MyProject", "my-project", "ACTIVE", now, now))
        fid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                _INS_FACT, (fid, pid, "M2", "domain", "API", "version", "v3", "tool", now, now, now)
            )
        row = conn.execute(
            "SELECT project_id FROM memory_facts WHERE fact_id = ?;", (fid,)
        ).fetchone()
        assert row["project_id"] == pid


def test_14_memory_fact_trust_levels_m0_to_m4() -> None:
    """All trust levels M0-M4 are accepted."""
    with _tmp_db() as conn:
        now = _now_utc()
        for level in ("M0", "M1", "M2", "M3", "M4"):
            with transaction(conn) as cur:
                cur.execute(
                    _INS_FACT,
                    (_uuid(), None, level, "system", "S", "P", "O", "system", now, now, now),
                )
        count = conn.execute("SELECT COUNT(*) FROM memory_facts;").fetchone()[0]
        assert count == 5


def test_15_memory_fact_invalid_trust_level_rejected() -> None:
    """Invalid trust level rejected by CHECK constraint."""
    with _tmp_db() as conn:
        now = _now_utc()
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(
                    _INS_FACT, (_uuid(), None, "M5", "domain", "S", "P", "O", "tool", now, now, now)
                )


def test_16_memory_fact_temporal_fields() -> None:
    """valid_from, valid_until, created_at, updated_at stored correctly."""
    with _tmp_db() as conn:
        now = _now_utc()
        fid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO memory_facts "
                "(fact_id, project_id, trust_level, category, subject, predicate,"
                " object, provenance_source, valid_from, valid_until,"
                " created_at, updated_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (fid, "M1", "domain", "S", "P", "O", "tool", now, "2027-01-01T00:00:00Z", now, now),
            )
        row = conn.execute(
            "SELECT valid_from, valid_until FROM memory_facts WHERE fact_id=?;", (fid,)
        ).fetchone()
        assert row["valid_until"] == "2027-01-01T00:00:00Z"


def test_17_memory_fact_supersession() -> None:
    """Supersession chain: fact B marks fact A as predecessor via superseded_by."""
    with _tmp_db() as conn:
        now = _now_utc()
        fid_a = _uuid()
        fid_b = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                _INS_FACT,
                (fid_a, None, "M3", "user", "U", "uses", "Py 3.11", "user", now, now, now),
            )
            cur.execute(
                "INSERT INTO memory_facts "
                "(fact_id, project_id, trust_level, category, subject, predicate,"
                " object, provenance_source, valid_from, is_superseded, superseded_by,"
                " created_at, updated_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?);",
                (fid_b, "M4", "user", "U", "uses", "Py 3.12", "user", now, fid_a, now, now),
            )
        row_b = conn.execute(
            "SELECT superseded_by FROM memory_facts WHERE fact_id=?;", (fid_b,)
        ).fetchone()
        assert row_b["superseded_by"] == fid_a


def test_18_memory_fact_confidence_out_of_range_rejected() -> None:
    """Confidence outside [0.0, 1.0] is rejected."""
    with _tmp_db() as conn:
        now = _now_utc()
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(
                    "INSERT INTO memory_facts "
                    "(fact_id, project_id, trust_level, category, subject, predicate,"
                    " object, confidence, provenance_source, valid_from,"
                    " created_at, updated_at) "
                    "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    (_uuid(), "M1", "domain", "S", "P", "O", 1.5, "tool", now, now, now),
                )


# ---------------------------------------------------------------------------
# 5. MEMORY EVIDENCE
# ---------------------------------------------------------------------------


def test_19_evidence_relationship() -> None:
    """Memory fact can reference an evidence record."""
    with _tmp_db() as conn:
        now = _now_utc()
        eid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO memory_evidence "
                "(evidence_id, project_id, evidence_type, source_reference,"
                " raw_content_snippet, content_hash, is_tainted, trust_score, recorded_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, 0, 0.9, ?);",
                (eid, "tool_output", "tool:run_tests", "Tests passed.", "abc123", now),
            )
        fid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO memory_facts "
                "(fact_id, project_id, trust_level, category, subject, predicate,"
                " object, provenance_source, evidence_id, valid_from,"
                " created_at, updated_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (fid, "M2", "system", "Tests", "pass", "True", "tool", eid, now, now, now),
            )
        row = conn.execute(
            "SELECT evidence_id FROM memory_facts WHERE fact_id=?;", (fid,)
        ).fetchone()
        assert row["evidence_id"] == eid


def test_20_evidence_invalid_type_rejected() -> None:
    """Invalid evidence_type rejected by CHECK constraint."""
    with _tmp_db() as conn:
        now = _now_utc()
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(
                    "INSERT INTO memory_evidence "
                    "(evidence_id, project_id, evidence_type, source_reference,"
                    " raw_content_snippet, content_hash, recorded_at) "
                    "VALUES (?, NULL, ?, ?, ?, ?, ?);",
                    (_uuid(), "invalid_type", "src", "snippet", "hash", now),
                )


def test_21_external_web_evidence_is_tainted() -> None:
    """External web evidence stored with is_tainted=1 is queryable."""
    with _tmp_db() as conn:
        now = _now_utc()
        eid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO memory_evidence "
                "(evidence_id, project_id, evidence_type, source_reference,"
                " raw_content_snippet, content_hash, is_tainted, trust_score, recorded_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, 1, 0.2, ?);",
                (eid, "external_web", "https://example.com", "content", "deadbeef", now),
            )
        row = conn.execute(
            "SELECT is_tainted FROM memory_evidence WHERE evidence_id=?;", (eid,)
        ).fetchone()
        assert row["is_tainted"] == 1


# ---------------------------------------------------------------------------
# 6. ENTITY RELATIONSHIPS
# ---------------------------------------------------------------------------


def test_22_entity_relationship_insert() -> None:
    """Can insert entities and a directed relationship between them."""
    with _tmp_db() as conn:
        now = _now_utc()
        eid1, eid2, rid = _uuid(), _uuid(), _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_ENTITY, (eid1, None, "person", "Alice", "alice", now, now))
            cur.execute(_INS_ENTITY, (eid2, None, "project", "ProjX", "proj-x", now, now))
            cur.execute(
                "INSERT INTO entity_relationships "
                "(relation_id, project_id, source_entity_id, target_entity_id,"
                " relation_type, weight, trust_level, valid_from, created_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?);",
                (rid, eid1, eid2, "author_of", 1.0, "M3", now, now),
            )
        row = conn.execute(
            "SELECT relation_type FROM entity_relationships WHERE relation_id=?;", (rid,)
        ).fetchone()
        assert row["relation_type"] == "author_of"


def test_23_invalid_entity_type_rejected() -> None:
    """Invalid entity_type rejected by CHECK constraint."""
    with _tmp_db() as conn:
        now = _now_utc()
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(
                    _INS_ENTITY,
                    (_uuid(), None, "invalid_entity_type", "X", "x", now, now),
                )


def test_24_entity_relationship_invalid_source_rejected() -> None:
    """Relationship referencing non-existent source entity rejected (FK)."""
    with _tmp_db() as conn:
        now = _now_utc()
        eid_real = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_ENTITY, (eid_real, None, "concept", "Real", "real", now, now))
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(
                    "INSERT INTO entity_relationships "
                    "(relation_id, project_id, source_entity_id, target_entity_id,"
                    " relation_type, weight, trust_level, valid_from, created_at) "
                    "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?);",
                    (_uuid(), "nonexistent-entity-id", eid_real, "references", 1.0, "M1", now, now),
                )


# ---------------------------------------------------------------------------
# 7. CONVERSATIONS
# ---------------------------------------------------------------------------


def test_25_conversation_insert() -> None:
    """Can insert a project-scoped conversation."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid, cid = _uuid(), _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "MyProject", "my-proj", "ACTIVE", now, now))
            cur.execute(_INS_CONV, (cid, pid, "desktop", "window-001", "ACTIVE", now, now))
        row = conn.execute(
            "SELECT channel FROM conversations WHERE conversation_id=?;", (cid,)
        ).fetchone()
        assert row["channel"] == "desktop"


def test_26_conversation_channel_validation() -> None:
    """Invalid channel value rejected by CHECK constraint."""
    with _tmp_db() as conn:
        now = _now_utc()
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(_INS_CONV, (_uuid(), None, "telegram", "tg-001", "ACTIVE", now, now))


def test_27_conversation_whatsapp_channel() -> None:
    """WhatsApp is a valid conversation channel."""
    with _tmp_db() as conn:
        now = _now_utc()
        cid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_CONV, (cid, None, "whatsapp", "+1234567890@c.us", "ACTIVE", now, now))
        row = conn.execute(
            "SELECT channel FROM conversations WHERE conversation_id=?;", (cid,)
        ).fetchone()
        assert row["channel"] == "whatsapp"


def test_28_conversation_global_scope() -> None:
    """Conversation with project_id=NULL is unscoped (global)."""
    with _tmp_db() as conn:
        now = _now_utc()
        cid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_CONV, (cid, None, "system_cli", "cli-session-1", "ACTIVE", now, now))
        row = conn.execute(
            "SELECT project_id FROM conversations WHERE conversation_id=?;", (cid,)
        ).fetchone()
        assert row["project_id"] is None


# ---------------------------------------------------------------------------
# 8. MESSAGES
# ---------------------------------------------------------------------------


def test_29_messages_insert() -> None:
    """Can insert multiple ordered messages into a conversation."""
    with _tmp_db() as conn:
        now = _now_utc()
        cid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_CONV, (cid, None, "desktop", "win-1", "ACTIVE", now, now))
        with transaction(conn) as cur:
            for seq, role, content in [
                (1, "user", "Hello AEGIS"),
                (2, "assistant", "Hello! How can I help?"),
            ]:
                cur.execute(_INS_MSG, (_uuid(), cid, role, "desktop", seq, content, now))
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?;", (cid,)
        ).fetchone()[0]
        assert count == 2


def test_30_message_ordering_unique_constraint() -> None:
    """Duplicate message_seq within same conversation rejected (UNIQUE)."""
    with _tmp_db() as conn:
        now = _now_utc()
        cid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_CONV, (cid, None, "desktop", "win-2", "ACTIVE", now, now))
            cur.execute(_INS_MSG, (_uuid(), cid, "user", "desktop", 1, "First", now))
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(_INS_MSG, (_uuid(), cid, "user", "desktop", 1, "Dup", now))


def test_31_message_role_validation() -> None:
    """Invalid message role rejected by CHECK constraint."""
    with _tmp_db() as conn:
        now = _now_utc()
        cid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_CONV, (cid, None, "desktop", "win-3", "ACTIVE", now, now))
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(_INS_MSG, (_uuid(), cid, "admin", "desktop", 1, "Hi", now))


def test_32_message_invalid_conversation_fk_rejected() -> None:
    """Message referencing non-existent conversation_id rejected (FK)."""
    with _tmp_db() as conn:
        now = _now_utc()
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(
                    _INS_MSG, (_uuid(), "nonexistent-convo-id", "user", "desktop", 1, "Hi", now)
                )


# ---------------------------------------------------------------------------
# 9. PROJECT-OWNED RELATIONSHIPS
# ---------------------------------------------------------------------------


def test_33_project_owned_memory_relationship() -> None:
    """Project-owned memory fact has correct project_id FK."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Proj", "proj", "ACTIVE", now, now))
        fid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                _INS_FACT, (fid, pid, "M1", "domain", "X", "Y", "Z", "conversation", now, now, now)
            )
        row = conn.execute(
            "SELECT f.fact_id FROM memory_facts f "
            "JOIN projects p ON f.project_id = p.project_id "
            "WHERE p.project_id = ?;",
            (pid,),
        ).fetchone()
        assert row["fact_id"] == fid


def test_34_project_owned_conversation_relationship() -> None:
    """Project-owned conversation queries correctly via JOIN."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid, cid = _uuid(), _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Proj2", "proj2", "ACTIVE", now, now))
            cur.execute(_INS_CONV, (cid, pid, "desktop", "win-4", "ACTIVE", now, now))
        row = conn.execute(
            "SELECT c.conversation_id FROM conversations c "
            "JOIN projects p ON c.project_id = p.project_id "
            "WHERE p.project_id = ?;",
            (pid,),
        ).fetchone()
        assert row["conversation_id"] == cid


def test_35_global_memory_survives_project_deletion_simulation() -> None:
    """Global memory (project_id=NULL) is unaffected when a project is deleted."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()
        global_fid = _uuid()
        project_fid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "TempProj", "temp-proj", "ACTIVE", now, now))
            # Global memory (NULL project scope)
            cur.execute(
                _INS_FACT,
                (global_fid, None, "M4", "user", "Global", "knows", "SQL", "user", now, now, now),
            )
            # Project-owned memory
            cur.execute(
                _INS_FACT,
                (
                    project_fid,
                    pid,
                    "M1",
                    "domain",
                    "Proj",
                    "created",
                    "2026",
                    "user",
                    now,
                    now,
                    now,
                ),
            )
        # Simulate project deletion
        with transaction(conn) as cur:
            cur.execute("DELETE FROM projects WHERE project_id = ?;", (pid,))

        # Project memory cascades away
        project_row = conn.execute(
            "SELECT fact_id FROM memory_facts WHERE fact_id=?;", (project_fid,)
        ).fetchone()
        assert project_row is None

        # Global memory survives
        global_row = conn.execute(
            "SELECT fact_id FROM memory_facts WHERE fact_id=?;", (global_fid,)
        ).fetchone()
        assert global_row is not None
        assert global_row["fact_id"] == global_fid


def test_36_invalid_project_fk_in_memory_fact_rejected() -> None:
    """Memory fact referencing non-existent project_id rejected (FK)."""
    with _tmp_db() as conn:
        now = _now_utc()
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn) as cur:
                cur.execute(
                    _INS_FACT,
                    (
                        _uuid(),
                        "nonexistent-proj-id",
                        "M1",
                        "domain",
                        "S",
                        "P",
                        "O",
                        "tool",
                        now,
                        now,
                        now,
                    ),
                )


# ---------------------------------------------------------------------------
# 10. HEALTH AND INTEGRITY
# ---------------------------------------------------------------------------


def test_37_integrity_check_passes() -> None:
    """PRAGMA integrity_check passes on fully migrated database."""
    with _tmp_db() as conn:
        result = conn.execute("PRAGMA integrity_check(20);").fetchone()[0]
        assert result == "ok"


def test_38_health_check_healthy() -> None:
    """DatabaseHealthReport returns HEALTHY after full migration."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = create_connection(db_path)
        migrations = load_migrations_from_directory(MIGRATIONS_DIR)
        bootstrap_schema_migrations(conn)
        apply_migrations(conn, migrations)
        conn.close()
        report = check_database_health(db_path)
        assert report.state == DatabaseHealthState.HEALTHY, (
            f"Health: {report.state}, messages: {report.diagnostic_messages}"
        )
    finally:
        for ext in ("", "-wal", "-shm"):
            Path(str(db_path) + ext).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 11. SECURITY
# ---------------------------------------------------------------------------


def test_39_no_secrets_in_domain_tables() -> None:
    """No domain table column names suggest credential storage."""
    secret_terms = {"password", "secret", "api_key", "token", "oauth", "private_key", "credential"}
    with _tmp_db() as conn:
        tables = [
            "projects",
            "memory_facts",
            "memory_evidence",
            "user_preferences",
            "entities",
            "entity_relationships",
            "conversations",
            "messages",
        ]
        for table in tables:
            info = conn.execute(f"PRAGMA table_info({table});").fetchall()
            col_names = {row["name"].lower() for row in info}
            leaked = col_names & secret_terms
            assert leaked == set(), f"Table '{table}' has suspicious column(s): {leaked}"


def test_40_no_network_access_in_schema() -> None:
    """Schema operations use only local SQLite; no network access."""
    with _tmp_db() as conn:
        version = get_current_schema_version(conn)
        assert version == 4


# ---------------------------------------------------------------------------
# 12. FULL LIFECYCLE SMOKE TEST
# ---------------------------------------------------------------------------


def test_41_domain_schema_full_smoke_test() -> None:
    """Full lifecycle smoke test: temp DB → all migrations → all domains → FK check
    → integrity_check → health check → close → cleanup.
    Never uses the production runtime database.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        # 1. Open and connect
        conn = create_connection(db_path)
        migrations = load_migrations_from_directory(MIGRATIONS_DIR)

        # 2. Apply all migrations
        bootstrap_schema_migrations(conn)
        apply_migrations(conn, migrations)
        assert get_current_schema_version(conn) == 4

        now = _now_utc()

        # 3. Insert project
        pid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Smoke Test Project", "smoke-test", "ACTIVE", now, now))

        # 4. Insert evidence
        eid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO memory_evidence "
                "(evidence_id, project_id, evidence_type, source_reference,"
                " raw_content_snippet, content_hash, is_tainted, trust_score, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, 0.95, ?);",
                (eid, pid, "tool_output", "tool:pytest", "All tests passed.", "sha256abc", now),
            )

        # 5. Insert project-scoped memory fact
        proj_fid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO memory_facts "
                "(fact_id, project_id, trust_level, category, subject, predicate,"
                " object, provenance_source, evidence_id, valid_from,"
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    proj_fid,
                    pid,
                    "M2",
                    "domain",
                    "AEGIS",
                    "status",
                    "operational",
                    "tool",
                    eid,
                    now,
                    now,
                    now,
                ),
            )

        # 6. Insert global memory fact
        global_fid = _uuid()
        with transaction(conn) as cur:
            cur.execute(
                _INS_FACT,
                (
                    global_fid,
                    None,
                    "M4",
                    "user",
                    "User",
                    "prefers",
                    "Python",
                    "user",
                    now,
                    now,
                    now,
                ),
            )

        # 7. Insert entities and relationship
        e1, e2, rel_id = _uuid(), _uuid(), _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_ENTITY, (e1, pid, "concept", "AEGIS", "aegis-system", now, now))
            cur.execute(_INS_ENTITY, (e2, pid, "tool", "pytest", "pytest-tool", now, now))
            cur.execute(
                "INSERT INTO entity_relationships "
                "(relation_id, project_id, source_entity_id, target_entity_id,"
                " relation_type, weight, trust_level, valid_from, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (rel_id, pid, e1, e2, "depends_on", 1.0, "M2", now, now),
            )

        # 8. Insert conversation and messages
        cid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_CONV, (cid, pid, "desktop", "win-smoke", "ACTIVE", now, now))
        with transaction(conn) as cur:
            for seq, role, content in [
                (1, "user", "What is AEGIS?"),
                (2, "assistant", "AEGIS is an AI system."),
                (3, "user", "Tell me more."),
            ]:
                cur.execute(_INS_MSG, (_uuid(), cid, role, "desktop", seq, content, now))

        # 9. Verify FK constraint with intentionally bad reference
        fk_rejected = False
        try:
            with transaction(conn) as cur:
                cur.execute(
                    _INS_MSG,
                    (_uuid(), "nonexistent-convo", "user", "desktop", 1, "Test", now),
                )
        except sqlite3.IntegrityError:
            fk_rejected = True
        assert fk_rejected, "Expected FK IntegrityError was not raised"

        # 10. integrity_check
        result = conn.execute("PRAGMA integrity_check(20);").fetchone()[0]
        assert result == "ok"

        # 11. Verify final counts
        assert conn.execute("SELECT COUNT(*) FROM projects;").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_facts;").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM memory_evidence;").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM conversations;").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages;").fetchone()[0] == 3

        conn.close()

        # 12. Health check (post-close reopen)
        report = check_database_health(db_path)
        assert report.state == DatabaseHealthState.HEALTHY

    finally:
        for ext in ("", "-wal", "-shm"):
            Path(str(db_path) + ext).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 13. PROMPT 013: DOMAIN SCHEMA CONSISTENCY & RECONSTRUCTION TESTS
# ---------------------------------------------------------------------------


def test_42_canonical_trust_levels_accepted() -> None:
    """Canonical trust levels M0-M4 are accepted across all memory domain tables."""
    with _tmp_db() as conn:
        now = _now_utc()
        for level in ("M0", "M1", "M2", "M3", "M4"):
            with transaction(conn) as cur:
                # 1. memory_facts
                cur.execute(
                    _INS_FACT,
                    (_uuid(), None, level, "domain", "S", "P", "O", "system", now, now, now),
                )
                # 2. user_preferences
                cur.execute(
                    "INSERT INTO user_preferences "
                    "(preference_id, project_id, domain, preference_key, preference_value,"
                    " preference_type, trust_level, created_at, updated_at) "
                    "VALUES (?, NULL, 'coding', ?, 'val', 'string', ?, ?, ?);",
                    (_uuid(), f"key_{level}", level, now, now),
                )

        count_facts = conn.execute("SELECT COUNT(*) FROM memory_facts;").fetchone()[0]
        count_prefs = conn.execute("SELECT COUNT(*) FROM user_preferences;").fetchone()[0]
        assert count_facts == 5
        assert count_prefs == 5


def test_43_non_canonical_trust_aliases_rejected_by_check_constraint() -> None:
    """Non-canonical aliases (e.g., M0_TRANSIENT, M4_AXIOMATIC) are rejected by CHECK constraint."""
    with _tmp_db() as conn:
        now = _now_utc()
        forbidden_aliases = [
            "M0_TRANSIENT",
            "M0_RAW",
            "M1_AGENT_CLAIM",
            "M2_TOOL_SUPPORTED",
            "M3_VERIFIED",
            "M4_AXIOMATIC",
            "M4_USER_CONFIRMED",
            "M5",
            "RAW",
            "CONFIRMED",
        ]
        for alias in forbidden_aliases:
            with pytest.raises(sqlite3.IntegrityError):
                with transaction(conn) as cur:
                    cur.execute(
                        _INS_FACT,
                        (_uuid(), None, alias, "domain", "S", "P", "O", "tool", now, now, now),
                    )


def test_44_trust_level_enum_and_semantics_mapping() -> None:
    """MemoryTrustLevel enum and MEMORY_TRUST_LEVEL_SEMANTICS strictly match frozen architecture."""
    expected_semantics = {
        "M0": "Raw Observation",
        "M1": "Agent Claim",
        "M2": "Tool-Supported",
        "M3": "Verified",
        "M4": "User-Confirmed",
    }
    assert MEMORY_TRUST_LEVEL_SEMANTICS == expected_semantics
    for code, semantic in expected_semantics.items():
        enum_member = MemoryTrustLevel(code)
        assert enum_member.value == code
        assert get_trust_level_semantic(enum_member) == semantic
        assert get_trust_level_semantic(code) == semantic


def test_45_trust_level_resolver_alias_normalization() -> None:
    """resolve_trust_level normalizes legacy aliases and rejects invalid codes."""
    # Canonical resolution
    for code in ("M0", "M1", "M2", "M3", "M4"):
        assert resolve_trust_level(code) == MemoryTrustLevel(code)
        assert resolve_trust_level(code.lower()) == MemoryTrustLevel(code)

    # Legacy alias normalization
    assert "M0_TRANSIENT" in NON_CANONICAL_TRUST_ALIASES
    assert resolve_trust_level("M0_TRANSIENT") == MemoryTrustLevel.M0
    assert resolve_trust_level("M1_AGENT_CLAIM") == MemoryTrustLevel.M1
    assert resolve_trust_level("M2_TOOL_SUPPORTED") == MemoryTrustLevel.M2
    assert resolve_trust_level("M3_VERIFIED") == MemoryTrustLevel.M3
    assert resolve_trust_level("M4_AXIOMATIC") == MemoryTrustLevel.M4

    # Invalid code rejection
    for invalid in ("M5", "M_INVALID", "UNKNOWN", ""):
        with pytest.raises(ValueError):
            resolve_trust_level(invalid)


def test_46_project_memory_ownership_cascade() -> None:
    """Project deletion cascades to all project-scoped memory domain records."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Project Cascade", "proj-cascade", "ACTIVE", now, now))
            # 1. evidence
            eid = _uuid()
            cur.execute(
                "INSERT INTO memory_evidence "
                "(evidence_id, project_id, evidence_type, source_reference,"
                " raw_content_snippet, content_hash, recorded_at) "
                "VALUES (?, ?, 'tool_output', 'src', 'snippet', 'hash', ?);",
                (eid, pid, now),
            )
            # 2. facts
            cur.execute(
                _INS_FACT,
                (_uuid(), pid, "M2", "domain", "S", "P", "O", "tool", now, now, now),
            )
            # 3. preferences
            cur.execute(
                "INSERT INTO user_preferences "
                "(preference_id, project_id, domain, preference_key, preference_value,"
                " preference_type, trust_level, created_at, updated_at) "
                "VALUES (?, ?, 'coding', 'indent', '4', 'string', 'M4', ?, ?);",
                (_uuid(), pid, now, now),
            )
            # 4. entities
            ent_id = _uuid()
            cur.execute(_INS_ENTITY, (ent_id, pid, "concept", "Concept", "slug-c", now, now))
            # 5. entity_relationships
            ent_target = _uuid()
            cur.execute(_INS_ENTITY, (ent_target, pid, "concept", "Target", "slug-t", now, now))
            cur.execute(
                "INSERT INTO entity_relationships "
                "(relation_id, project_id, source_entity_id, target_entity_id,"
                " relation_type, trust_level, valid_from, created_at) "
                "VALUES (?, ?, ?, ?, 'related_to', 'M3', ?, ?);",
                (_uuid(), pid, ent_id, ent_target, now, now),
            )

        # Delete project
        with transaction(conn) as cur:
            cur.execute("DELETE FROM projects WHERE project_id = ?;", (pid,))

        # Verify all project records cascaded
        for table in [
            "memory_evidence",
            "memory_facts",
            "user_preferences",
            "entities",
            "entity_relationships",
        ]:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = ?;", (pid,)
            ).fetchone()[0]
            assert count == 0, f"Table '{table}' failed to cascade delete on project deletion"


def test_47_global_memory_preservation() -> None:
    """Global memory records (project_id IS NULL) are 100% preserved after project deletion."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()

        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Transient Proj", "transient-proj", "ACTIVE", now, now))
            # Global records
            cur.execute(
                _INS_FACT,
                (_uuid(), None, "M4", "user", "User", "prefers", "Python", "user", now, now, now),
            )
            cur.execute(
                "INSERT INTO user_preferences "
                "(preference_id, project_id, domain, preference_key, preference_value,"
                " preference_type, trust_level, created_at, updated_at) "
                "VALUES (?, NULL, 'communication', 'tone', 'direct', 'string', 'M4', ?, ?);",
                (_uuid(), now, now),
            )
            # Project records
            cur.execute(
                _INS_FACT,
                (_uuid(), pid, "M1", "domain", "Local", "scope", "val", "tool", now, now, now),
            )

        # Delete project
        with transaction(conn) as cur:
            cur.execute("DELETE FROM projects WHERE project_id = ?;", (pid,))

        # Global records survive
        facts = conn.execute(
            "SELECT COUNT(*) FROM memory_facts WHERE project_id IS NULL;"
        ).fetchone()[0]
        prefs = conn.execute(
            "SELECT COUNT(*) FROM user_preferences WHERE project_id IS NULL;"
        ).fetchone()[0]
        assert facts == 1
        assert prefs == 1


def test_48_project_conversation_deletion_cascade() -> None:
    """Project-owned conversations and their messages CASCADE on project deletion."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()
        cid = _uuid()
        mid1 = _uuid()
        mid2 = _uuid()

        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Proj Conv", "proj-conv", "ACTIVE", now, now))
            cur.execute(_INS_CONV, (cid, pid, "desktop", "win-1", "ACTIVE", now, now))
            cur.execute(_INS_MSG, (mid1, cid, "user", "desktop", 1, "Msg 1", now))
            cur.execute(_INS_MSG, (mid2, cid, "assistant", "desktop", 2, "Msg 2", now))

        # Delete project
        with transaction(conn) as cur:
            cur.execute("DELETE FROM projects WHERE project_id = ?;", (pid,))

        # Both conversation and its messages must be cascaded away (NOT set to NULL!)
        conv_count = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?;", (cid,)
        ).fetchone()[0]
        msg_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (cid,)
        ).fetchone()[0]
        assert conv_count == 0, "Project conversation was not deleted on project CASCADE"
        assert msg_count == 0, "Messages were not cascaded on conversation deletion"


def test_49_global_conversation_preservation() -> None:
    """Global conversations (project_id IS NULL) and their messages survive project deletion."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()
        proj_cid = _uuid()
        glob_cid = _uuid()

        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Proj X", "proj-x", "ACTIVE", now, now))
            # Project conversation
            cur.execute(_INS_CONV, (proj_cid, pid, "desktop", "win-proj", "ACTIVE", now, now))
            cur.execute(_INS_MSG, (_uuid(), proj_cid, "user", "desktop", 1, "Proj msg", now))
            # Global conversation (e.g. system CLI)
            cur.execute(_INS_CONV, (glob_cid, None, "system_cli", "cli-main", "ACTIVE", now, now))
            cur.execute(_INS_MSG, (_uuid(), glob_cid, "user", "system_cli", 1, "Global msg", now))

        # Delete project
        with transaction(conn) as cur:
            cur.execute("DELETE FROM projects WHERE project_id = ?;", (pid,))

        # Project conversation is gone
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?;", (proj_cid,)
            ).fetchone()[0]
            == 0
        )

        # Global conversation and message survive
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?;", (glob_cid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (glob_cid,)
            ).fetchone()[0]
            == 1
        )


def test_50_end_to_end_fk_cascade_matrix() -> None:
    """Verify complete cascade matrix across all 7 domain tables simultaneously."""
    with _tmp_db() as conn:
        now = _now_utc()
        pid = _uuid()
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Full Matrix", "full-matrix", "ACTIVE", now, now))
            # 1. evidence
            eid = _uuid()
            cur.execute(
                "INSERT INTO memory_evidence "
                "(evidence_id, project_id, evidence_type, source_reference,"
                " raw_content_snippet, content_hash, recorded_at) "
                "VALUES (?, ?, 'tool_output', 'src', 'snippet', 'hash', ?);",
                (eid, pid, now),
            )
            # 2. facts
            cur.execute(
                _INS_FACT,
                (_uuid(), pid, "M2", "domain", "S", "P", "O", "tool", now, now, now),
            )
            # 3. preferences
            cur.execute(
                "INSERT INTO user_preferences "
                "(preference_id, project_id, domain, preference_key, preference_value,"
                " preference_type, trust_level, created_at, updated_at) "
                "VALUES (?, ?, 'coding', 'k', 'v', 'string', 'M3', ?, ?);",
                (_uuid(), pid, now, now),
            )
            # 4. entities
            e1 = _uuid()
            e2 = _uuid()
            cur.execute(_INS_ENTITY, (e1, pid, "concept", "C1", "c1", now, now))
            cur.execute(_INS_ENTITY, (e2, pid, "concept", "C2", "c2", now, now))
            # 5. relationships
            cur.execute(
                "INSERT INTO entity_relationships "
                "(relation_id, project_id, source_entity_id, target_entity_id,"
                " relation_type, trust_level, valid_from, created_at) "
                "VALUES (?, ?, ?, ?, 'related_to', 'M2', ?, ?);",
                (_uuid(), pid, e1, e2, now, now),
            )
            # 6. conversations
            cid = _uuid()
            cur.execute(_INS_CONV, (cid, pid, "desktop", "win-matrix", "ACTIVE", now, now))
            # 7. messages
            cur.execute(_INS_MSG, (_uuid(), cid, "user", "desktop", 1, "Matrix msg", now))

        # Delete project
        with transaction(conn) as cur:
            cur.execute("DELETE FROM projects WHERE project_id = ?;", (pid,))

        # Verify 0 project rows in all tables
        for table in [
            "memory_evidence",
            "memory_facts",
            "user_preferences",
            "entities",
            "entity_relationships",
            "conversations",
        ]:
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id = ?;", (pid,)
                ).fetchone()[0]
                == 0
            )

        # Messages cascaded through conversation
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (cid,)
            ).fetchone()[0]
            == 0
        )

        # Foreign key integrity check passes
        assert conn.execute("PRAGMA foreign_key_check;").fetchall() == []


def test_51_cross_entity_relationship_cascade() -> None:
    """Deleting source or target entity cascades to entity_relationships."""
    with _tmp_db() as conn:
        now = _now_utc()
        e_src = _uuid()
        e_tgt = _uuid()
        rel_id = _uuid()

        with transaction(conn) as cur:
            # Global entities
            cur.execute(_INS_ENTITY, (e_src, None, "concept", "Source", "src-slug", now, now))
            cur.execute(_INS_ENTITY, (e_tgt, None, "concept", "Target", "tgt-slug", now, now))
            cur.execute(
                "INSERT INTO entity_relationships "
                "(relation_id, project_id, source_entity_id, target_entity_id,"
                " relation_type, trust_level, valid_from, created_at) "
                "VALUES (?, NULL, ?, ?, 'depends_on', 'M3', ?, ?);",
                (rel_id, e_src, e_tgt, now, now),
            )

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM entity_relationships WHERE relation_id = ?;", (rel_id,)
            ).fetchone()[0]
            == 1
        )

        # Delete source entity
        with transaction(conn) as cur:
            cur.execute("DELETE FROM entities WHERE entity_id = ?;", (e_src,))

        # Edge must cascade
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM entity_relationships WHERE relation_id = ?;", (rel_id,)
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("PRAGMA foreign_key_check;").fetchall() == []


def test_52_migration_0004_idempotency() -> None:
    """Migration 0004 executes cleanly and is idempotent when re-applied."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = create_connection(db_path)
        migrations = load_migrations_from_directory(MIGRATIONS_DIR)
        bootstrap_schema_migrations(conn)
        apply_migrations(conn, migrations)

        # Apply again
        reapplied = apply_migrations(conn, migrations)
        assert len(reapplied) == 4
        assert get_current_schema_version(conn) == 4
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        conn.close()
    finally:
        for ext in ("", "-wal", "-shm"):
            Path(db_path + ext).unlink(missing_ok=True)


def test_53_migration_checksum_integrity() -> None:
    """All migrations (0001-0004) maintain valid, strictly monotonic checksums."""
    migrations = load_migrations_from_directory(MIGRATIONS_DIR)
    assert len(migrations) == 4
    for m in migrations:
        computed = compute_sql_checksum(m.sql)
        assert m.checksum == computed, f"Checksum mismatch in migration {m.migration_id}"


def test_54_full_migration_from_fresh_database() -> None:
    """Fresh database migrates from v0 to v4 with all tables, constraints, and indexes."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = create_connection(db_path)
        bootstrap_schema_migrations(conn)
        assert get_current_schema_version(conn) == 0

        migrations = load_migrations_from_directory(MIGRATIONS_DIR)
        apply_migrations(conn, migrations)
        assert get_current_schema_version(conn) == 4

        # Verify all 10 tables exist
        tables = _get_tables(conn)
        expected = {
            "schema_migrations",
            "storage_metadata",
            "projects",
            "memory_evidence",
            "memory_facts",
            "user_preferences",
            "entities",
            "entity_relationships",
            "conversations",
            "messages",
        }
        assert expected.issubset(set(tables))
        assert conn.execute("PRAGMA foreign_key_check;").fetchall() == []
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        conn.close()
    finally:
        for ext in ("", "-wal", "-shm"):
            Path(db_path + ext).unlink(missing_ok=True)


def test_55_migration_from_existing_v3_database() -> None:
    """Upgrading an existing v3 database to v4 preserves data and updates FK cascade."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = create_connection(db_path)
        bootstrap_schema_migrations(conn)
        all_migrations = load_migrations_from_directory(MIGRATIONS_DIR)

        # 1. Apply only migrations 1-3 (Prompt 012 baseline)
        v3_migrations = [m for m in all_migrations if m.version <= 3]
        apply_migrations(conn, v3_migrations)
        assert get_current_schema_version(conn) == 3

        now = _now_utc()
        pid = _uuid()
        proj_cid = _uuid()
        glob_cid = _uuid()
        mid1 = _uuid()
        mid2 = _uuid()

        # 2. Pre-populate data in v3 schema
        with transaction(conn) as cur:
            cur.execute(_INS_PROJ, (pid, "Legacy Proj", "legacy-proj", "ACTIVE", now, now))
            cur.execute(_INS_CONV, (proj_cid, pid, "desktop", "win-v3-proj", "ACTIVE", now, now))
            cur.execute(_INS_MSG, (mid1, proj_cid, "user", "desktop", 1, "V3 Proj Msg", now))
            cur.execute(
                _INS_CONV,
                (glob_cid, None, "system_cli", "cli-v3-glob", "ACTIVE", now, now),
            )
            cur.execute(_INS_MSG, (mid2, glob_cid, "user", "system_cli", 1, "V3 Glob Msg", now))

        # 3. Now apply migration 0004
        apply_migrations(conn, all_migrations)
        assert get_current_schema_version(conn) == 4

        # 4. Verify all pre-existing data was preserved
        assert conn.execute("SELECT COUNT(*) FROM conversations;").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM messages;").fetchone()[0] == 2

        # 5. Verify the updated FK behavior: deleting project now CASCADES conversations
        with transaction(conn) as cur:
            cur.execute("DELETE FROM projects WHERE project_id = ?;", (pid,))

        # Project conversation and message are purged
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?;", (proj_cid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM messages WHERE message_id = ?;", (mid1,)).fetchone()[
                0
            ]
            == 0
        )

        # Global conversation and message survive
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?;", (glob_cid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM messages WHERE message_id = ?;", (mid2,)).fetchone()[
                0
            ]
            == 1
        )

        assert conn.execute("PRAGMA foreign_key_check;").fetchall() == []
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        conn.close()
    finally:
        for ext in ("", "-wal", "-shm"):
            Path(db_path + ext).unlink(missing_ok=True)
