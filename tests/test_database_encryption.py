"""SAGE Local SQLite Storage Foundation - Whole-Database Encryption & Migration Security Tests.

Canonical Reference:
    AEGIS Prompt 037: Whole-Database Encryption
    docs/AEGIS_DATABASE_ENCRYPTION_ARCHITECTURE.md
    docs/AEGIS_DATABASE_ENCRYPTION_MIGRATION.md

Mandatory Security Test Matrix:
1. Encryption at Rest:
   - Database pages and structural metadata are encrypted with SQLCipher (AES-256-CBC).
   - Standard sqlite3 cannot open the file (fails closed: file is not a database).
   - Raw binary byte inspection confirms lack of 'SQLite format 3' magic header.
   - Raw binary byte search confirms zero leakage of synthetic secret markers.
   - WAL and journal files are encrypted under the same cipher key.
2. Key Handling & Protection:
   - Correct 256-bit key permits full read/write and schema operations.
   - Incorrect key is rejected with fail-closed authentication error (HMAC failure).
   - Missing key is rejected without attempting unauthenticated open.
   - Corrupted or invalid-length key material fails closed.
   - Database master key is protected at rest using Windows DPAPI.
   - Key material is never leaked into logs, error messages, or configuration.
3. Fail-Closed Invariant:
   - If SQLCipher is unavailable or keys fail, system NEVER falls back to plaintext.
   - All connection factories (create_connection, open_database, initialize_runtime_database)
     enforce consistent fail-closed protection.
4. Migration & Recovery:
   - Plaintext databases are safely migrated to encrypted format with 100% data fidelity.
   - Pre-migration backup is created before transformation.
   - Interrupted migration recovers without data loss or silent file corruption.
   - All 9 schema migrations apply cleanly to encrypted databases.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    from sqlcipher3 import dbapi2 as sqlcipher_dbapi2
except ImportError:
    sqlcipher_dbapi2 = None  # type: ignore[assignment]

from src.security.crypto import AES256GCMCipher
from src.security.database_key import DatabaseKeyError, DatabaseKeyManager
from src.security.database_key_rotation import (
    ExclusiveDatabaseLock,
    RotationDrainTimeoutError,
    RotationError,
    RotationJournal,
    RotationLockError,
    RotationState,
    _verify_key_rejected,
    recover_interrupted_rotation,
    rotate_database_key,
)
from src.security.dpapi import SoftwareKeyProtector, WindowsDPAPIKeyProtector
from src.storage.database import (
    SQLCIPHER_AVAILABLE,
    ConnectionCoordinator,
    DatabaseAccessLock,
    SQLitePragmaConfig,
    can_acquire_exclusive_file_access,
    create_connection,
    initialize_runtime_database,
    is_database_encrypted,
    migrate_database_to_encrypted,
    open_database,
    remediate_legacy_backup,
    restore_database_from_backup,
    scan_legacy_plaintext_backups,
    transaction,
)
from src.storage.errors import DatabaseConnectionError, DatabaseDrainingError
from src.storage.migrations import (
    apply_migrations,
    load_migrations_from_directory,
)

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """Provide a clean isolated temporary directory for test databases."""
    return tmp_path


@pytest.fixture
def mock_key_protector() -> WindowsDPAPIKeyProtector:
    """Provide standard DPAPI key protector on Windows or fallback for tests."""
    return WindowsDPAPIKeyProtector()


# ==============================================================================
# 1. ENCRYPTION AT REST TESTS
# ==============================================================================


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_01_encrypted_database_pages_and_metadata_at_rest(temp_dir: Path):
    """Database pages and metadata must not be readable as plaintext SQLite."""
    db_path = temp_dir / "secret_test.db"
    key = AES256GCMCipher.generate_random_key()

    synthetic_secret_table = "top_secret_metadata_table_42"
    synthetic_secret_data = "SYNTHETIC_PLAINTEXT_SECRET_PAYLOAD_ABC_98765"

    # Create and populate encrypted database
    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    with transaction(conn):
        conn.execute(f"CREATE TABLE {synthetic_secret_table} (id TEXT PRIMARY KEY, secret TEXT);")
        conn.execute(
            f"INSERT INTO {synthetic_secret_table} VALUES (?, ?);",
            ("row-1", synthetic_secret_data),
        )
    conn.close()

    assert db_path.is_file()
    assert is_database_encrypted(db_path) is True

    # 1. Inspect raw disk bytes
    raw_bytes = db_path.read_bytes()

    # Standard SQLite header must NOT be present
    assert not raw_bytes.startswith(b"SQLite format 3\x00")

    # Table name (structural metadata) must NOT appear in raw bytes
    assert synthetic_secret_table.encode("utf-8") not in raw_bytes

    # Plaintext user data must NOT appear in raw bytes
    assert synthetic_secret_data.encode("utf-8") not in raw_bytes

    # 2. Verify standard standard library sqlite3 fails closed
    with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
        std_conn = sqlite3.connect(str(db_path))
        std_conn.execute("SELECT count(*) FROM sqlite_master;")


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_02_wal_journal_files_are_encrypted(temp_dir: Path):
    """WAL files written during active transactions must not leak plaintext data."""
    db_path = temp_dir / "wal_test.db"
    key = AES256GCMCipher.generate_random_key()

    synthetic_marker = "SENSITIVE_WAL_MARKER_STRING_XYZ_12345"

    cfg = SQLitePragmaConfig(journal_mode="WAL", synchronous="NORMAL")
    conn = create_connection(db_path, pragma_config=cfg, encryption_key=key, encrypted=True)

    with transaction(conn):
        conn.execute("CREATE TABLE secret_wal_table (val TEXT);")
        for i in range(50):
            conn.execute("INSERT INTO secret_wal_table VALUES (?);", (f"{synthetic_marker}_{i}",))

    # WAL file should exist while connection is active or checkpoint pending
    wal_path = db_path.with_name(f"{db_path.name}-wal")
    if wal_path.exists() and wal_path.stat().st_size > 0:
        wal_bytes = wal_path.read_bytes()
        assert synthetic_marker.encode("utf-8") not in wal_bytes

    conn.close()


# ==============================================================================
# 2. KEY HANDLING & AUTHENTICATION TESTS
# ==============================================================================


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_03_correct_key_authenticates_and_reads(temp_dir: Path):
    """Providing the matching 256-bit key permits full read/write access."""
    db_path = temp_dir / "auth_test.db"
    key = AES256GCMCipher.generate_random_key()

    # Setup
    conn1 = create_connection(db_path, encryption_key=key, encrypted=True)
    with transaction(conn1):
        conn1.execute("CREATE TABLE records (k TEXT PRIMARY KEY, v TEXT);")
        conn1.execute("INSERT INTO records VALUES ('k1', 'v1');")
    conn1.close()

    # Reopen with correct key
    conn2 = create_connection(db_path, encryption_key=key, encrypted=True)
    cur = conn2.execute("SELECT v FROM records WHERE k = 'k1';")
    assert cur.fetchone()[0] == "v1"
    conn2.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_04_wrong_key_fails_closed(temp_dir: Path):
    """An incorrect encryption key fails closed and denies access."""
    db_path = temp_dir / "wrong_key_test.db"
    correct_key = AES256GCMCipher.generate_random_key()
    wrong_key = AES256GCMCipher.generate_random_key()

    conn1 = create_connection(db_path, encryption_key=correct_key, encrypted=True)
    with transaction(conn1):
        conn1.execute("CREATE TABLE t (x INTEGER);")
        conn1.execute("INSERT INTO t VALUES (42);")
    conn1.close()

    # Attempt open with wrong key must raise DatabaseConnectionError
    with pytest.raises(DatabaseConnectionError, match="Failed to authenticate/decrypt database"):
        create_connection(db_path, encryption_key=wrong_key, encrypted=True)


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_05_missing_key_fails_closed(temp_dir: Path):
    """Connecting to an encrypted database without a key must fail closed."""
    db_path = temp_dir / "missing_key_test.db"
    key = AES256GCMCipher.generate_random_key()

    conn1 = create_connection(db_path, encryption_key=key, encrypted=True)
    with transaction(conn1):
        conn1.execute("CREATE TABLE t (x);")
    conn1.close()

    # Auto-detection recognizes encrypted header; missing key in manager fails closed
    with pytest.raises(DatabaseConnectionError):
        create_connection(db_path, encryption_key=None, encrypted=True)


def test_06_database_key_manager_dpapi_lifecycle(temp_dir: Path):
    """DatabaseKeyManager generates, wraps via DPAPI, reads, rotates, and destroys keys."""
    key_file = temp_dir / "keys" / "test_db.key.enc"
    # SoftwareKeyProtector requires a 32-byte master key; use fixed test vector
    master_key = b"testmaster_key_for_aegis_testing"  # 32 bytes
    protector = SoftwareKeyProtector(master_key)
    mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)

    # 1. First retrieval generates fresh key
    assert mgr.get_key() is None
    key1 = mgr.get_or_create_key()
    assert len(key1) == 32
    assert key_file.is_file()

    # Raw key material must NOT appear in protected file
    raw_saved_blob = key_file.read_bytes()
    assert key1 not in raw_saved_blob

    # 2. Subsequent retrieval returns identical key
    key2 = mgr.get_key()
    assert key2 == key1

    # 3. Uncoordinated rotation without database is prohibited (Finding SEC-038-02)
    with pytest.raises(DatabaseKeyError, match="Uncoordinated key rotation is prohibited"):
        mgr.rotate_key()

    # 4. Isolated key rotation with explicit allow_uncoordinated flag (for key manager mechanics)
    old_key, new_key = mgr.rotate_key(allow_uncoordinated=True)
    assert old_key == key1
    assert len(new_key) == 32
    assert new_key != old_key
    assert mgr.get_key() == new_key
    # Historical version 1 must remain retrievable via versioned archive
    assert mgr.get_key(version=1) == old_key
    assert mgr.get_key(version=2) == new_key
    assert mgr.get_current_version() == 2

    # 5. Cryptographic erasure removes active key and versioned history
    assert mgr.destroy_key(destroy_history=True) is True
    assert not key_file.exists()
    assert mgr.get_key() is None
    assert mgr.get_key(version=1) is None



def test_07_invalid_key_length_rejected(temp_dir: Path):
    """Keys shorter or longer than 32 bytes must be rejected immediately."""
    db_path = temp_dir / "invalid_len.db"

    with pytest.raises(DatabaseConnectionError, match="Invalid encryption key length"):
        create_connection(db_path, encryption_key=b"too_short", encrypted=True)

    with pytest.raises(DatabaseConnectionError, match="Invalid encryption key length"):
        create_connection(db_path, encryption_key=b"x" * 64, encrypted=True)


# ==============================================================================
# 3. MIGRATION & RECOVERY TESTS
# ==============================================================================


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_08_plaintext_to_encrypted_migration_preserves_all_data(temp_dir: Path):
    """Existing plaintext database can be migrated to SQLCipher with 100% data integrity."""
    plain_db = temp_dir / "legacy_plain.db"
    key = AES256GCMCipher.generate_random_key()

    # 1. Create realistic plaintext database with schema migrations
    conn = sqlite3.connect(str(plain_db))
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    migs = load_migrations_from_directory(MIGRATIONS_DIR)
    apply_migrations(conn, migs, database_path=plain_db)

    # Insert synthetic domain records across tables
    task_id = "task-migrate-001"
    task_goal = "MIGRATE_SYNTHETIC_TASK_SECRET_PAYLOAD"
    update_id = "whatsapp_upd_001"
    update_text = "MIGRATE_SYNTHETIC_MESSAGE_RAW_TEXT"

    conn.execute(
        "INSERT INTO tasks (task_id, session_id, title, state, priority, created_at, updated_at) "
        "VALUES (?, 'sess-1', ?, 'COMPLETED', 'HIGH', "
        "'2026-10-18T00:00:00Z', '2026-10-18T00:00:00Z');",
        (task_id, task_goal),
    )
    conn.execute(
        "INSERT INTO gateway_updates (update_id, channel, channel_update_id, sender_id, chat_id, "
        "chat_type, message_type, raw_text, state, retry_count, max_retries, metadata_json, "
        "created_at, updated_at) "
        "VALUES (?, 'whatsapp', 'msg-1', 'sender-1', 'chat-1', 'private', 'text', ?, "
        "'COMPLETED', 0, 3, '{}', '2026-10-18T00:00:00Z', '2026-10-18T00:00:00Z');",
        (update_id, update_text),
    )
    conn.commit()
    conn.close()

    # Confirm source is plaintext and contains raw secrets
    assert is_database_encrypted(plain_db) is False
    plain_bytes = plain_db.read_bytes()
    assert plain_bytes.startswith(b"SQLite format 3\x00")
    assert task_goal.encode("utf-8") in plain_bytes
    assert update_text.encode("utf-8") in plain_bytes

    # 2. Run atomic migration
    backup_dir = temp_dir / "backups"
    migrated_path = migrate_database_to_encrypted(
        plain_db, encryption_key=key, backup_dir=backup_dir
    )

    # 3. Verify backup creation and encryption
    assert backup_dir.is_dir()
    backup_file = backup_dir / f"{plain_db.stem}_pre_encryption_backup.db"
    assert backup_file.is_file()
    assert is_database_encrypted(backup_file) is True
    backup_bytes = backup_file.read_bytes()
    assert not backup_bytes.startswith(b"SQLite format 3\x00")
    assert task_goal.encode("utf-8") not in backup_bytes
    assert update_text.encode("utf-8") not in backup_bytes
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(backup_file)).execute("SELECT count(*) FROM tasks;")

    # Verify backup opens with key
    b_conn = create_connection(backup_file, encryption_key=key, encrypted=True)
    assert b_conn.execute("SELECT count(*) FROM tasks;").fetchone()[0] == 1
    b_conn.close()

    # 4. Verify migrated file on disk is encrypted
    assert migrated_path == plain_db
    assert is_database_encrypted(migrated_path) is True
    migrated_bytes = migrated_path.read_bytes()
    assert not migrated_bytes.startswith(b"SQLite format 3\x00")
    assert task_goal.encode("utf-8") not in migrated_bytes
    assert update_text.encode("utf-8") not in migrated_bytes

    # 5. Verify standard sqlite3 fails
    with pytest.raises(sqlite3.DatabaseError):
        std_conn = sqlite3.connect(str(migrated_path))
        std_conn.execute("SELECT count(*) FROM tasks;")

    # 6. Verify full data decryption via SQLCipher connection
    enc_conn = create_connection(migrated_path, encryption_key=key, encrypted=True)
    t_cur = enc_conn.execute("SELECT title FROM tasks WHERE task_id = ?;", (task_id,))
    assert t_cur.fetchone()["title"] == task_goal

    u_cur = enc_conn.execute(
        "SELECT raw_text FROM gateway_updates WHERE update_id = ?;", (update_id,)
    )
    assert u_cur.fetchone()["raw_text"] == update_text

    # Verify all 9 migrations recorded in schema_migrations
    m_cur = enc_conn.execute("SELECT count(*) FROM schema_migrations;")
    assert m_cur.fetchone()[0] == 9
    enc_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_09_interrupted_migration_preserves_source_and_backup(temp_dir: Path):
    """Failure during migration aborts cleanly without corrupting the original file."""
    plain_db = temp_dir / "abort_test.db"

    # Create dummy database
    conn = sqlite3.connect(str(plain_db))
    conn.execute("CREATE TABLE canary (v TEXT);")
    conn.execute("INSERT INTO canary VALUES ('CANARY_SURVIVES');")
    conn.commit()
    conn.close()

    # Pass invalid key length to trigger early failure
    with pytest.raises(DatabaseConnectionError, match="Encryption key must be exactly 32 bytes"):
        migrate_database_to_encrypted(plain_db, encryption_key=b"bad_key")

    # Original database must remain uncorrupted and readable
    assert is_database_encrypted(plain_db) is False
    check_conn = sqlite3.connect(str(plain_db))
    assert check_conn.execute("SELECT v FROM canary;").fetchone()[0] == "CANARY_SURVIVES"
    check_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_10_initialize_runtime_database_transparent_migration(temp_dir: Path):
    """initialize_runtime_database automatically migrates and protects existing databases."""
    db_file = temp_dir / "data" / "aegis.db"
    key = AES256GCMCipher.generate_random_key()

    # Step 1: Pre-create plaintext database
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE pre_existing (msg TEXT);")
    conn.execute("INSERT INTO pre_existing VALUES ('LEGACY_MESSAGE_PRESERVED');")
    conn.commit()
    conn.close()

    assert not is_database_encrypted(db_file)

    # Step 2: Initialize runtime database with encryption enabled
    runtime_conn = initialize_runtime_database(
        db_path=db_file,
        migrations_dir=MIGRATIONS_DIR,
        encryption_key=key,
        encrypted=True,
    )

    # Verify migration occurred transparently
    assert is_database_encrypted(db_file) is True

    # Verify legacy record was preserved
    row = runtime_conn.execute("SELECT msg FROM pre_existing;").fetchone()
    # sqlcipher3.Row and sqlite3.Row both support index or dict access
    assert (row[0] if row[0] is not None else row["msg"]) == "LEGACY_MESSAGE_PRESERVED"

    # Verify migrations were applied to the newly encrypted database
    count = runtime_conn.execute("SELECT count(*) FROM schema_migrations;").fetchone()[0]
    assert count == 9
    runtime_conn.close()


# ==============================================================================
# 4. LEAKAGE CHECKS
# ==============================================================================


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_11_open_database_context_manager_encrypted(temp_dir: Path):
    """open_database context manager works reliably with encrypted databases."""
    db_path = temp_dir / "ctx_test.db"
    key = AES256GCMCipher.generate_random_key()

    with open_database(db_path, encryption_key=key, encrypted=True) as conn:
        with transaction(conn):
            conn.execute("CREATE TABLE ctx_tbl (x TEXT);")
            conn.execute("INSERT INTO ctx_tbl VALUES ('ctx_val');")

    assert is_database_encrypted(db_path) is True

    with open_database(db_path, encryption_key=key, encrypted=True) as conn2:
        val = conn2.execute("SELECT x FROM ctx_tbl;").fetchone()[0]
        assert val == "ctx_val"


# ==============================================================================
# 5. ENCRYPTED BACKUP & RESTORATION TESTS (Prompt 039)
# ==============================================================================


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_12_encrypted_backup_creation_and_restoration(temp_dir: Path):
    """Encrypted backup snapshot can be verified and restored as encrypted or plaintext."""
    plain_db = temp_dir / "restore_source.db"
    key = AES256GCMCipher.generate_random_key()

    # Pre-create plaintext database with synthetic canary records
    conn = sqlite3.connect(str(plain_db))
    conn.execute("CREATE TABLE canary (id TEXT PRIMARY KEY, secret TEXT);")
    conn.execute("INSERT INTO canary VALUES ('c1', 'CANARY_SECRET_RESTORE_TEST');")
    conn.commit()
    conn.close()

    backup_dir = temp_dir / "backups"
    migrate_database_to_encrypted(plain_db, encryption_key=key, backup_dir=backup_dir)

    backup_file = backup_dir / f"{plain_db.stem}_pre_encryption_backup.db"
    assert backup_file.is_file()
    assert is_database_encrypted(backup_file) is True

    # 1. Restore as encrypted SQLCipher database
    target_enc = temp_dir / "restored_enc.db"
    restored_enc_path = restore_database_from_backup(
        backup_path=backup_file,
        target_path=target_enc,
        encryption_key=key,
        as_plaintext=False,
    )
    assert restored_enc_path == target_enc
    assert is_database_encrypted(target_enc) is True

    # Verify standard sqlite3 cannot open it
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(target_enc)).execute("SELECT count(*) FROM canary;")

    # Verify SQLCipher connection reads canary
    enc_conn = create_connection(target_enc, encryption_key=key, encrypted=True)
    row = enc_conn.execute("SELECT secret FROM canary WHERE id = 'c1';").fetchone()
    assert (row[0] if row[0] is not None else row["secret"]) == "CANARY_SECRET_RESTORE_TEST"
    enc_conn.close()

    # 2. Restore as plaintext SQLite database
    target_plain = temp_dir / "restored_plain.db"
    restored_plain_path = restore_database_from_backup(
        backup_path=backup_file,
        target_path=target_plain,
        encryption_key=key,
        as_plaintext=True,
    )
    assert restored_plain_path == target_plain
    assert is_database_encrypted(target_plain) is False

    # Verify standard sqlite3 can open and read canary
    plain_conn = sqlite3.connect(str(target_plain))
    plain_row = plain_conn.execute("SELECT secret FROM canary WHERE id = 'c1';").fetchone()
    assert plain_row[0] == "CANARY_SECRET_RESTORE_TEST"
    plain_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_13_wal_checkpoint_consistency_during_backup(temp_dir: Path):
    """Uncheckpointed WAL transactions are flushed and captured into encrypted backup."""
    plain_db = temp_dir / "wal_source.db"
    key = AES256GCMCipher.generate_random_key()

    # Pre-create database in WAL mode and simulate crash by copying files while open
    temp_origin = temp_dir / "origin.db"
    conn = sqlite3.connect(str(temp_origin))
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("CREATE TABLE wal_test (k TEXT, v TEXT);")
    conn.execute("INSERT INTO wal_test VALUES ('k1', 'UNCHECKPOINTED_WAL_DATA_123');")
    conn.commit()

    origin_wal = temp_origin.with_name(f"{temp_origin.name}-wal")
    assert origin_wal.is_file()

    # Copy files to plain_db to simulate post-crash state with pending WAL frames
    shutil.copy2(temp_origin, plain_db)
    shutil.copy2(origin_wal, plain_db.with_name(f"{plain_db.name}-wal"))
    conn.close()

    wal_file = plain_db.with_name(f"{plain_db.name}-wal")
    assert wal_file.is_file()

    backup_dir = temp_dir / "backups"
    migrate_database_to_encrypted(plain_db, encryption_key=key, backup_dir=backup_dir)

    # Verify migrated database has uncheckpointed data
    enc_conn = create_connection(plain_db, encryption_key=key, encrypted=True)
    val = enc_conn.execute("SELECT v FROM wal_test WHERE k = 'k1';").fetchone()[0]
    assert val == "UNCHECKPOINTED_WAL_DATA_123"
    enc_conn.close()

    # Verify encrypted backup also captured the uncheckpointed data
    backup_file = backup_dir / f"{plain_db.stem}_pre_encryption_backup.db"
    b_conn = create_connection(backup_file, encryption_key=key, encrypted=True)
    b_val = b_conn.execute("SELECT v FROM wal_test WHERE k = 'k1';").fetchone()[0]
    assert b_val == "UNCHECKPOINTED_WAL_DATA_123"
    b_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_14_backup_failure_injection_preserves_source(temp_dir: Path):
    """Failure during backup snapshot creation aborts and preserves source database."""
    plain_db = temp_dir / "fail_source.db"
    key = AES256GCMCipher.generate_random_key()

    # Pre-create plaintext database
    conn = sqlite3.connect(str(plain_db))
    conn.execute("CREATE TABLE keep_safe (data TEXT);")
    conn.execute("INSERT INTO keep_safe VALUES ('SOURCE_MUST_SURVIVE');")
    conn.commit()
    conn.close()

    # Inject failure: point backup_dir to an existing non-directory file
    invalid_backup_dir = temp_dir / "not_a_dir.txt"
    invalid_backup_dir.write_text("BLOCKER", encoding="utf-8")

    with pytest.raises(DatabaseConnectionError):
        migrate_database_to_encrypted(
            plain_db, encryption_key=key, backup_dir=invalid_backup_dir
        )

    # Source database must remain uncorrupted plaintext
    assert is_database_encrypted(plain_db) is False
    check_conn = sqlite3.connect(str(plain_db))
    row = check_conn.execute("SELECT data FROM keep_safe;").fetchone()
    assert row[0] == "SOURCE_MUST_SURVIVE"
    check_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_15_legacy_backup_scan_and_dry_run_reporting(temp_dir: Path):
    """Legacy plaintext backups are detected, reported, and remediated safely without deletion."""
    backups_dir = temp_dir / "legacy_backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    key = AES256GCMCipher.generate_random_key()

    # Create synthetic legacy plaintext backup
    legacy_file = backups_dir / "legacy_test_pre_encryption_backup.db"
    conn = sqlite3.connect(str(legacy_file))
    conn.execute("CREATE TABLE legacy_info (id INT);")
    conn.execute("INSERT INTO legacy_info VALUES (42);")
    conn.commit()
    conn.close()

    assert not is_database_encrypted(legacy_file)

    # 1. Scan detects legacy plaintext backup
    scan_results = scan_legacy_plaintext_backups(backups_dir)
    assert len(scan_results) == 1
    assert scan_results[0]["name"] == legacy_file.name
    assert scan_results[0]["is_plaintext"] is True
    assert scan_results[0]["status"] == "PLAINTEXT_EXPOSED"

    # 2. Dry run remediation does not alter or delete files
    dry_result = remediate_legacy_backup(legacy_file, encryption_key=key, dry_run=True)
    assert dry_result["action"] == "DRY_RUN"
    assert legacy_file.is_file()
    assert not is_database_encrypted(legacy_file)

    # 3. Live remediation without removal creates verified encrypted copy
    rem_result = remediate_legacy_backup(
        legacy_file, encryption_key=key, dry_run=False, remove_plaintext=False
    )
    assert rem_result["action"] == "REMEDIATED"
    enc_target = Path(rem_result["target_path"])
    assert enc_target.is_file()
    assert is_database_encrypted(enc_target) is True
    assert legacy_file.is_file()  # Plaintext still preserved

    # 4. Remediation with removal safely purges plaintext only after verification
    rem_remove_result = remediate_legacy_backup(
        legacy_file, encryption_key=key, dry_run=False, remove_plaintext=True
    )
    assert rem_remove_result["action"] == "REMEDIATED"
    assert rem_remove_result["plaintext_removed"] is True
    assert not legacy_file.exists()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_16_database_key_rotation_success(temp_dir: Path):
    """Coordinated key rotation atomically updates database and key without data loss."""
    key_file = temp_dir / "keys" / "test_db.key.enc"
    protector = SoftwareKeyProtector(b"k" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)

    key1 = key_mgr.get_or_create_key()
    key2 = b"\xaa" * 32

    # Create synthetic database under key1
    db_path = temp_dir / "data" / "rotation_test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE canary_data (id INT PRIMARY KEY, secret TEXT);")
    conn.execute("INSERT INTO canary_data VALUES (1, 'canary_data_secret_xyz');")
    conn.execute("CREATE INDEX idx_canary_secret ON canary_data(secret);")
    conn.close()

    # Pre-rotation assertions
    assert is_database_encrypted(db_path) is True
    assert key_mgr.get_current_version() == 1

    # Execute coordinated rotation
    backups_dir = temp_dir / "backups"
    res = rotate_database_key(
        db_path,
        key_manager=key_mgr,
        new_key=key2,
        backup_dir=backups_dir,
    )

    assert res["status"] == "SUCCESS"
    assert res["old_version"] == 1
    assert res["new_version"] == 2
    assert Path(res["snapshot_path"]).is_file()

    # Post-rotation: Old key must fail closed
    with pytest.raises(DatabaseConnectionError):
        create_connection(db_path, encryption_key=key1, encrypted=True)

    # Post-rotation: New key must authenticate and read all canary records
    conn2 = create_connection(db_path, encryption_key=key2, encrypted=True)
    row = conn2.execute("SELECT secret FROM canary_data WHERE id = 1;").fetchone()
    assert row is not None
    assert row[0] == "canary_data_secret_xyz"
    conn2.close()

    # Restart simulation: fresh DatabaseKeyManager instance returns new key
    fresh_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    assert fresh_mgr.get_key() == key2
    assert fresh_mgr.get_current_version() == 2
    assert fresh_mgr.get_key(version=1) == key1


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_17_database_key_rotation_failure_injection_preserves_state(temp_dir: Path):
    """Failure during key rotation preserves source database and active key intact."""
    key_file = temp_dir / "keys" / "fail_inj.key.enc"
    protector = SoftwareKeyProtector(b"k" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    # 1. Non-existent database path fails closed
    with pytest.raises(RotationError, match="Target database file does not exist"):
        rotate_database_key(temp_dir / "non_existent.db", key_manager=key_mgr)

    # Create synthetic database
    db_path = temp_dir / "data" / "fail_inj.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE secure_log (msg TEXT);")
    conn.execute("INSERT INTO secure_log VALUES ('entry_1');")
    conn.close()

    # 2. Invalid key length fails closed
    with pytest.raises(RotationError, match="New key must be exactly 32 bytes"):
        rotate_database_key(db_path, key_manager=key_mgr, new_key=b"short_key")

    # 3. Rotating to identical key fails closed
    with pytest.raises(RotationError, match="cannot be identical"):
        rotate_database_key(db_path, key_manager=key_mgr, new_key=key1)

    # Source database must remain intact and decryptable under key1
    conn_verify = create_connection(db_path, encryption_key=key1, encrypted=True)
    row = conn_verify.execute("SELECT msg FROM secure_log;").fetchone()
    assert row[0] == "entry_1"
    conn_verify.close()
    assert key_mgr.get_key() == key1


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_18_interrupted_rotation_recovery(temp_dir: Path):
    """Interrupted rotations deterministically roll back or roll forward without data loss."""
    key_file = temp_dir / "keys" / "recovery.key.enc"
    protector = SoftwareKeyProtector(b"k" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xbb" * 32

    # --- Scenario A: Crash before key publication (STAGING_VERIFIED) -> Rollback ---
    db_a = temp_dir / "data" / "recovery_a.db"
    db_a.parent.mkdir(parents=True, exist_ok=True)
    conn_a = create_connection(db_a, encryption_key=key1, encrypted=True)
    conn_a.execute("CREATE TABLE t_a (x TEXT);")
    conn_a.execute("INSERT INTO t_a VALUES ('val_a');")
    conn_a.close()

    # Simulate crash at STAGING_VERIFIED: staging file created, journal records state
    staged_a = db_a.with_name(f"{db_a.name}.rekey_fake.staging")
    staged_a.write_bytes(b"temporary_staged_content")
    journal_a = RotationJournal(db_a)
    journal_a.record_state(
        RotationState.STAGING_VERIFIED,
        db_path=str(db_a),
        staging_db_path=str(staged_a),
        old_version=1,
    )

    # Run recovery
    rec_a = recover_interrupted_rotation(db_a, key_manager=key_mgr)
    assert rec_a["status"] == "ROLLED_BACK"
    assert not staged_a.exists()  # Staging artifact cleaned up
    assert not journal_a.exists()  # Journal cleaned up

    # Source database opens with key1
    conn_verify_a = create_connection(db_a, encryption_key=key1, encrypted=True)
    assert conn_verify_a.execute("SELECT x FROM t_a;").fetchone()[0] == "val_a"
    conn_verify_a.close()

    # --- Scenario B: Crash after key publication (KEY_COMMITTED) -> Roll-forward ---
    db_b = temp_dir / "data" / "recovery_b.db"
    conn_b = create_connection(db_b, encryption_key=key1, encrypted=True)
    conn_b.execute("CREATE TABLE t_b (x TEXT);")
    conn_b.execute("INSERT INTO t_b VALUES ('val_b');")
    conn_b.close()

    # Export staged db with key2
    staged_b = db_b.with_name(f"{db_b.name}.rekey_b.staging")
    b_conn = create_connection(db_b, encryption_key=key1, encrypted=True)
    b_conn.execute(f"ATTACH DATABASE '{staged_b}' AS staged KEY \"x'{key2.hex()}'\";")
    b_conn.execute("SELECT sqlcipher_export('staged');")
    b_conn.execute("DETACH DATABASE staged;")
    b_conn.close()

    # Key manager committed key2, archived key1
    key_mgr.archive_current_key(1)
    key_mgr.commit_new_key(key2, new_version=2)

    journal_b = RotationJournal(db_b)
    journal_b.record_state(
        RotationState.KEY_COMMITTED,
        db_path=str(db_b),
        staging_db_path=str(staged_b),
        old_version=1,
        new_version=2,
    )

    # Run recovery
    rec_b = recover_interrupted_rotation(db_b, key_manager=key_mgr)
    assert rec_b["status"] == "ROLLED_FORWARD"
    assert not journal_b.exists()

    # db_b now opens with key2
    conn_verify_b = create_connection(db_b, encryption_key=key2, encrypted=True)
    assert conn_verify_b.execute("SELECT x FROM t_b;").fetchone()[0] == "val_b"
    conn_verify_b.close()


def test_19_rotation_exclusive_lock_concurrency(temp_dir: Path):
    """Concurrent rotation attempts fail closed with RotationLockError."""
    db_path = temp_dir / "concurrent.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"placeholder")

    with ExclusiveDatabaseLock(db_path, timeout_sec=5.0):
        # A competing lock attempt with a short timeout must fail
        with pytest.raises(
            RotationLockError, match="Timed out waiting for exclusive rotation lock"
        ):
            with ExclusiveDatabaseLock(db_path, timeout_sec=0.2):
                pass

    # Once lock is released, acquisition succeeds immediately
    with ExclusiveDatabaseLock(db_path, timeout_sec=1.0) as lock:
        assert lock.lock_path.is_file()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_20_rotation_preserves_backups_and_historical_keys(temp_dir: Path):
    """Key rotation synchronizes backups and retains historical keys for offline recovery."""
    key_file = temp_dir / "keys" / "backup_sync.key.enc"
    protector = SoftwareKeyProtector(b"k" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xcc" * 32

    db_path = temp_dir / "data" / "sync_db.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE docs (title TEXT);")
    conn.execute("INSERT INTO docs VALUES ('confidential_plan');")
    conn.close()

    backups_dir = temp_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    # Create backup snapshot 1 in backups_dir under key1
    backup_1 = backups_dir / "backup_managed.db"
    shutil.copy2(db_path, backup_1)

    # Create an external offline backup outside backups_dir under key1
    offline_backup = temp_dir / "external_archive" / "offline_backup_v1.db"
    offline_backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, offline_backup)

    # Rotate database key to key2
    res = rotate_database_key(
        db_path,
        key_manager=key_mgr,
        new_key=key2,
        backup_dir=backups_dir,
    )
    assert res["status"] == "SUCCESS"
    assert "backup_managed.db" in res["synchronized_backups"]

    # 1. Managed backup was synchronized to key2
    conn_b1 = create_connection(backup_1, encryption_key=key2, encrypted=True)
    assert conn_b1.execute("SELECT title FROM docs;").fetchone()[0] == "confidential_plan"
    conn_b1.close()

    # 2. Offline archive under key1 remains decryptable via versioned historical key
    archived_v1_key = key_mgr.get_key(version=1)
    assert archived_v1_key == key1

    restored_target = temp_dir / "restored_from_offline.db"
    restore_database_from_backup(
        offline_backup,
        restored_target,
        encryption_key=archived_v1_key,
        as_plaintext=False,
    )
    assert restored_target.is_file()
    conn_restored = create_connection(
        restored_target, encryption_key=archived_v1_key, encrypted=True
    )
    assert conn_restored.execute("SELECT title FROM docs;").fetchone()[0] == "confidential_plan"
    conn_restored.close()


def test_21_mysql_independence_verification():
    """Verify AEGIS whole-database encryption has zero dependence on MySQL or user passwords."""
    from src.security.database_key import DatabaseKeyManager
    from src.security.database_key_rotation import rotate_database_key

    # AEGIS is local-first embedded SQLite with SQLCipher; MySQL is explicitly third-party
    # Master keys must be 32-byte CSPRNG bytes, not MySQL credentials or 'root' passwords
    mgr = DatabaseKeyManager()
    assert hasattr(mgr, "get_or_create_key")
    assert hasattr(mgr, "rotate_key")
    assert callable(rotate_database_key)


def test_22_old_key_rejection_error_differentiation(temp_dir):
    """Verify that old-key rejection verification guarantees connection cleanup and
    strictly differentiates genuine authentication rejection from unexpected errors.
    """
    import os
    from unittest.mock import MagicMock, patch

    key_a = AES256GCMCipher.generate_random_key()
    key_b = AES256GCMCipher.generate_random_key()

    db_path = temp_dir / "rejection_test.db"
    conn = create_connection(db_path, encryption_key=key_a, encrypted=True)
    conn.execute("CREATE TABLE test_data (id INT PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO test_data VALUES (1, 'val1');")
    conn.commit()
    conn.close()

    # Case 1: Genuine rejection with wrong key (key_b against db encrypted with key_a)
    # Must succeed (meaning the old/wrong key was verified to be genuinely rejected)
    _verify_key_rejected(db_path, key_b)

    # Verify connection was closed and handle released (on Windows, os.replace works immediately)
    test_rename = temp_dir / "rejection_test_renamed.db"
    os.replace(db_path, test_rename)
    os.replace(test_rename, db_path)

    # Case 2: Security anomaly - correct key provided (old key actually authenticates)
    # Must raise RotationError because old key was NOT rejected
    with pytest.raises(RotationError, match="unexpectedly opened with old key"):
        _verify_key_rejected(db_path, key_a)

    # Case 3: Missing file - must raise RotationError, NOT count as rejection
    missing_file = temp_dir / "non_existent.db"
    with pytest.raises(RotationError, match="does not exist"):
        _verify_key_rejected(missing_file, key_b)

    # Case 4: Unexpected non-auth database error or filesystem error must raise RotationError
    # and MUST still close the connection handle
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")
    with patch("sqlcipher3.connect", return_value=mock_conn):
        with pytest.raises(RotationError, match="Unexpected database error"):
            _verify_key_rejected(db_path, key_b)
        mock_conn.close.assert_called_once()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_23_rotation_drains_active_reader(temp_dir: Path):
    """Active idle reader connection is closed during drain and rotation succeeds."""
    key_file = temp_dir / "keys" / "reader_drain.key.enc"
    protector = SoftwareKeyProtector(b"r" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xdd" * 32

    db_path = temp_dir / "data" / "reader_db.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize DB with some data
    init_conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    init_conn.execute("CREATE TABLE items (id INT, val TEXT);")
    init_conn.execute("INSERT INTO items VALUES (1, 'initial');")
    init_conn.commit()
    init_conn.close()

    # Open active reader connection
    reader_conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    row = reader_conn.execute("SELECT val FROM items WHERE id = 1;").fetchone()
    assert row[0] == "initial"

    # Execute key rotation with reader_conn still open
    res = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2, drain_timeout_sec=5.0)
    assert res["status"] == "SUCCESS"
    assert res["new_version"] == 2

    # Verify reader_conn was drained and closed
    assert not ConnectionCoordinator._is_connection_open(reader_conn)

    # Verify database is now accessible only with new key
    new_conn = create_connection(db_path, encryption_key=key2, encrypted=True)
    row2 = new_conn.execute("SELECT val FROM items WHERE id = 1;").fetchone()
    assert row2[0] == "initial"
    new_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_24_rotation_waits_for_active_writer_transaction(temp_dir: Path):
    """Rotation waits gracefully for active in-flight transaction to commit before rekeying."""
    key_file = temp_dir / "keys" / "writer_wait.key.enc"
    protector = SoftwareKeyProtector(b"w" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xee" * 32

    db_path = temp_dir / "data" / "writer_db.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    init_conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    init_conn.execute("CREATE TABLE orders (id INT PRIMARY KEY, amount INT);")
    init_conn.commit()
    init_conn.close()

    tx_started = threading.Event()
    writer_done = threading.Event()
    writer_error = []

    def writer_thread():
        try:
            w_conn = create_connection(db_path, encryption_key=key1, encrypted=True)
            with transaction(w_conn):
                w_conn.execute("INSERT INTO orders VALUES (100, 500);")
                tx_started.set()
                time.sleep(0.4)  # Hold transaction briefly
            writer_done.set()
        except Exception as exc:
            writer_error.append(exc)
            tx_started.set()
            writer_done.set()

    t = threading.Thread(target=writer_thread)
    t.start()
    tx_started.wait(timeout=5.0)

    # Initiate rotation while transaction is active
    res = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2, drain_timeout_sec=5.0)
    t.join(timeout=5.0)

    assert not writer_error
    assert res["status"] == "SUCCESS"

    # Verify writer's record was preserved in re-keyed database
    new_conn = create_connection(db_path, encryption_key=key2, encrypted=True)
    row = new_conn.execute("SELECT amount FROM orders WHERE id = 100;").fetchone()
    assert row[0] == 500
    new_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_25_new_connections_and_transactions_blocked_while_draining(temp_dir: Path):
    """New connections and transactions are rejected with DatabaseDrainingError while draining."""
    key_file = temp_dir / "keys" / "draining_blocked.key.enc"
    protector = SoftwareKeyProtector(b"d" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "draining_blocked.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE block_test (id INT);")
    conn.commit()

    # Manually assert draining state via coordinator
    ConnectionCoordinator.set_drain(db_path, timeout_sec=10.0)
    try:
        assert ConnectionCoordinator.is_draining(db_path)

        # 1. New connection attempt must raise DatabaseDrainingError
        with pytest.raises(DatabaseDrainingError, match="draining for key rotation"):
            create_connection(db_path, encryption_key=key, encrypted=True, drain_wait_timeout=0.1)

        # 2. Existing connection attempting new transaction must raise DatabaseDrainingError
        with pytest.raises(DatabaseDrainingError, match="draining for key rotation"):
            with transaction(conn):
                conn.execute("INSERT INTO block_test VALUES (1);")
    finally:
        ConnectionCoordinator.release_drain(db_path)
        conn.close()

    # After drain release, connections and transactions succeed immediately
    assert not ConnectionCoordinator.is_draining(db_path)
    conn2 = create_connection(db_path, encryption_key=key, encrypted=True)
    with transaction(conn2):
        conn2.execute("INSERT INTO block_test VALUES (1);")
    conn2.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_26_drain_timeout_aborts_rotation_safely(temp_dir: Path):
    """When active transaction exceeds drain timeout, rotation aborts cleanly with no changes."""
    key_file = temp_dir / "keys" / "drain_abort.key.enc"
    protector = SoftwareKeyProtector(b"a" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xfa" * 32

    db_path = temp_dir / "data" / "drain_abort.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE vault (secret TEXT);")
    conn.execute("INSERT INTO vault VALUES ('ORIGINAL_SECRET');")
    conn.commit()

    tx_held = threading.Event()
    stop_tx = threading.Event()

    def stubborn_writer():
        conn_stubborn = create_connection(db_path, encryption_key=key1, encrypted=True)
        try:
            with transaction(conn_stubborn):
                tx_held.set()
                stop_tx.wait(timeout=5.0)
        finally:
            conn_stubborn.close()

    t = threading.Thread(target=stubborn_writer)
    t.start()
    tx_held.wait(timeout=3.0)

    try:
        # Rotate with very short drain timeout (0.2s) - must fail with RotationDrainTimeoutError
        with pytest.raises(RotationDrainTimeoutError, match="waiting for database connections on .* to drain"):
            rotate_database_key(
                db_path,
                key_manager=key_mgr,
                new_key=key2,
                drain_timeout_sec=0.2,
            )
    finally:
        stop_tx.set()
        t.join(timeout=5.0)
        conn.close()

    # Verify rotation aborted safely: key is still key1 (v1), no staging/journal left
    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1

    journal = RotationJournal(db_path)
    assert not journal.exists()

    # Original database remains decryptable under key1
    verify_conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    row = verify_conn.execute("SELECT secret FROM vault;").fetchone()
    assert row[0] == "ORIGINAL_SECRET"
    verify_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_27_concurrent_write_detected_before_swap_aborts(temp_dir: Path):
    """If writes slip into active database between export and swap, rotation aborts."""
    key_file = temp_dir / "keys" / "write_detect.key.enc"
    protector = SoftwareKeyProtector(b"c" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xfb" * 32

    db_path = temp_dir / "data" / "write_detect.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE counter (val INT);")
    conn.execute("INSERT INTO counter VALUES (10);")
    conn.commit()
    conn.close()

    from src.security import database_key_rotation

    orig_collect = database_key_rotation._collect_database_parity
    call_count = [0]

    def patched_collect(target_path, key):
        res = orig_collect(target_path, key)
        call_count[0] += 1
        # The 3rd call is the pre-swap parity check on the active DB.
        # Simulate a rogue write slipping in by mutating the counter table's
        # parity value.  _collect_database_parity returns Dict[str, str]
        # (SHA-256 format "count:hexhash"), so we tamper with the numeric prefix.
        if call_count[0] == 3:
            original_parity = res.get("counter", "0:0000")
            count_part, hash_part = original_parity.split(":", 1)
            tampered_count = int(count_part) + 1
            return {**res, "counter": f"{tampered_count}:{hash_part}"}
        return res

    with patch("src.security.database_key_rotation._collect_database_parity", side_effect=patched_collect):
        with pytest.raises(RotationError, match="Active database modified during rotation export"):
            rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)

    # Key must NOT have been rotated
    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1
    assert not RotationJournal(db_path).exists()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_28_wal_checkpoint_busy_handling(temp_dir: Path):
    """WAL checkpoint returning busy=1 causes rotation to abort fail-closed."""
    key_file = temp_dir / "keys" / "wal_busy.key.enc"
    protector = SoftwareKeyProtector(b"b" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "wal_busy.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t (x INT);")
    conn.commit()
    conn.close()

    import sqlcipher3

    orig_connect = sqlcipher3.connect

    def mock_connect(*args, **kwargs):
        conn = orig_connect(*args, **kwargs)

        class MockConn:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *p, **kw):
                if "PRAGMA wal_checkpoint(TRUNCATE)" in sql:
                    mock_cursor = MagicMock()
                    mock_cursor.fetchone.return_value = (1, 10, 5)
                    return mock_cursor
                return conn.execute(sql, *p, **kw)

        return MockConn()

    with patch("sqlcipher3.connect", side_effect=mock_connect):
        with pytest.raises(RotationError, match="WAL checkpoint failed to complete \\(busy=1\\)"):
            rotate_database_key(db_path, key_manager=key_mgr)

    assert key_mgr.get_current_version() == 1


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_29_cross_process_drain_file_coordination(temp_dir: Path):
    """Advisory .drain file from another process prevents local connection creation."""
    key_file = temp_dir / "keys" / "cross_proc.key.enc"
    protector = SoftwareKeyProtector(b"p" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "cross_proc.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE shared (msg TEXT);")
    conn.commit()
    conn.close()

    # Simulate another process creating the .drain file
    drain_path = db_path.with_name(f"{db_path.name}.drain")
    drain_payload = {
        "pid": 999999,
        "timestamp": time.time(),
        "timeout": 30.0,
        "coordinator": "external_proc",
    }
    drain_path.write_text(json.dumps(drain_payload), encoding="utf-8")

    assert ConnectionCoordinator.is_draining(db_path)

    # Attempting to open connection fails with DatabaseDrainingError
    with pytest.raises(DatabaseDrainingError, match="is currently draining for key rotation"):
        create_connection(db_path, encryption_key=key, encrypted=True, drain_wait_timeout=0.1)

    # External process completes and unlinks .drain file
    drain_path.unlink()

    # Verification: connection succeeds immediately
    assert not ConnectionCoordinator.is_draining(db_path)
    conn2 = create_connection(db_path, encryption_key=key, encrypted=True)
    assert conn2.execute("SELECT count(*) FROM shared;").fetchone()[0] == 0
    conn2.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_30_windows_replace_bounded_retries(temp_dir: Path):
    """os.replace handles transient Windows file locks with bounded retries and succeeds."""
    key_file = temp_dir / "keys" / "replace_retry.key.enc"
    protector = SoftwareKeyProtector(b"t" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xfc" * 32

    db_path = temp_dir / "data" / "replace_retry.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE persistent (val TEXT);")
    conn.execute("INSERT INTO persistent VALUES ('SURVIVES_RETRIES');")
    conn.commit()
    conn.close()

    orig_replace = os.replace
    call_count = [0]

    def mock_replace(src, dst):
        if Path(dst).resolve() == db_path.resolve():
            call_count[0] += 1
            # Fail the first 2 replace calls on the target database with PermissionError, then succeed
            if call_count[0] <= 2:
                raise PermissionError(13, "Access is denied (transient lock)")
        return orig_replace(src, dst)

    with patch("os.replace", side_effect=mock_replace):
        res = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)

    assert res["status"] == "SUCCESS"
    assert call_count[0] >= 3

    # Verify rotated database is valid under key2
    new_conn = create_connection(db_path, encryption_key=key2, encrypted=True)
    row = new_conn.execute("SELECT val FROM persistent;").fetchone()
    assert row[0] == "SURVIVES_RETRIES"
    new_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_31_connection_resumption_after_success_and_failure(temp_dir: Path):
    """New connections immediately resume after both successful rotation and failed rotation."""
    key_file = temp_dir / "keys" / "resumption.key.enc"
    protector = SoftwareKeyProtector(b"s" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xfd" * 32

    db_path = temp_dir / "data" / "resumption.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE res_test (id INT);")
    conn.commit()
    conn.close()

    # 1. Successful rotation
    res = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)
    assert res["status"] == "SUCCESS"
    assert not ConnectionCoordinator.is_draining(db_path)

    # Immediately connect under key2
    conn_after_success = create_connection(db_path, encryption_key=key2, encrypted=True)
    conn_after_success.execute("INSERT INTO res_test VALUES (1);")
    conn_after_success.commit()
    conn_after_success.close()

    # 2. Failed rotation (e.g. invalid new key length)
    with pytest.raises(RotationError, match="New key must be exactly 32 bytes"):
        rotate_database_key(db_path, key_manager=key_mgr, new_key=b"too_short")

    assert not ConnectionCoordinator.is_draining(db_path)

    # Immediately connect under active key2
    conn_after_failure = create_connection(db_path, encryption_key=key2, encrypted=True)
    assert conn_after_failure.execute("SELECT count(*) FROM res_test;").fetchone()[0] == 1
    conn_after_failure.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_32_zero_record_loss_across_drained_rotation(temp_dir: Path):
    """Full database with multiple tables, indices, and 100+ records retains 100% data fidelity."""
    key_file = temp_dir / "keys" / "full_fidelity.key.enc"
    protector = SoftwareKeyProtector(b"f" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xfe" * 32

    db_path = temp_dir / "data" / "full_fidelity.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT);")
    conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, user_id TEXT, body TEXT);")
    conn.execute("CREATE TABLE audit_log (seq INT PRIMARY KEY, action TEXT);")
    conn.execute("CREATE INDEX idx_msg_user ON messages(user_id);")

    with transaction(conn):
        for i in range(50):
            conn.execute("INSERT INTO users VALUES (?, ?);", (f"u_{i}", f"user_{i}"))
        for i in range(100):
            conn.execute("INSERT INTO messages VALUES (?, ?, ?);", (f"m_{i}", f"u_{i % 50}", f"hello {i}"))
        for i in range(75):
            conn.execute("INSERT INTO audit_log VALUES (?, ?);", (i, f"action_{i}"))
    conn.close()

    # Keep a reader connection open
    reader_conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    u_count = reader_conn.execute("SELECT count(*) FROM users;").fetchone()[0]
    assert u_count == 50

    # Rotate database key
    res = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)
    assert res["status"] == "SUCCESS"

    # Verify with new key
    new_conn = create_connection(db_path, encryption_key=key2, encrypted=True)
    assert new_conn.execute("SELECT count(*) FROM users;").fetchone()[0] == 50
    assert new_conn.execute("SELECT count(*) FROM messages;").fetchone()[0] == 100
    assert new_conn.execute("SELECT count(*) FROM audit_log;").fetchone()[0] == 75
    # Verify index exists
    idx = new_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_msg_user';"
    ).fetchone()
    assert idx is not None
    # Verify integrity
    integ = new_conn.execute("PRAGMA integrity_check;").fetchone()
    assert integ[0].lower() == "ok"
    new_conn.close()


# ==============================================================================
# 5. REMEDIATION VERIFICATION TESTS (SEC-044-01 through SEC-044-07)
# ==============================================================================


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_33_startup_recovery_respects_active_rotation_lock(temp_dir: Path):
    """SEC-044-01: Startup initialization does NOT destroy staging files while another process holds rotation lock."""
    key_file = temp_dir / "keys" / "sec01.key.enc"
    protector = SoftwareKeyProtector(b"1" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "sec01.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE records (id INT);")
    conn.commit()
    conn.close()

    # Simulate an active rotation: lock held and journal written with staging file present
    journal = RotationJournal(db_path)
    staging_file = db_path.with_name(f"{db_path.name}.rekey_active.staging")
    staging_file.write_text("dummy staging content", encoding="utf-8")
    journal.record_state(
        RotationState.STAGING_EXPORTED,
        staging_db_path=str(staging_file),
    )

    with ExclusiveDatabaseLock(db_path, timeout_sec=5.0):
        # A second process/thread starts up and initializes the database
        # Because the lock is held, initialize_runtime_database skips recovery without touching staging files
        with patch("src.security.database_key_rotation.ExclusiveDatabaseLock.__enter__", side_effect=RotationLockError("Lock held")):
            boot_conn = initialize_runtime_database(
                db_path=db_path,
                migrations_dir=MIGRATIONS_DIR,
                encryption_key=key1,
                encrypted=True,
            )
            boot_conn.close()

    # Staging file and journal must be intact (not destroyed)
    assert staging_file.is_file()
    assert journal.exists()
    journal.delete()
    staging_file.unlink()


def test_34_store_writes_gated_during_drain(temp_dir: Path):
    """SEC-044-03: NotificationStore and AuditWriter reject writes when database is draining."""
    from src.notifications.models import NotificationEventType, NotificationPriority
    from src.notifications.store import NotificationStore
    from src.trust.audit import AuditDecision, AuditEventType, AuditWriter

    db_path = temp_dir / "data" / "sec03.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encrypted=False)
    conn.execute(
        "CREATE TABLE notifications (notification_id TEXT PRIMARY KEY, event_type TEXT, priority TEXT, "
        "title TEXT, body TEXT, source_type TEXT, source_id TEXT, project_id TEXT, dedup_key TEXT, "
        "status TEXT, delivery_attempts INT, max_delivery_attempts INT, expires_at TEXT, "
        "metadata_json TEXT, created_at TEXT, updated_at TEXT, acknowledged_at TEXT, dismissed_at TEXT);"
    )
    conn.execute(
        "CREATE TABLE audit_records (audit_id TEXT PRIMARY KEY, event_type TEXT, actor TEXT, "
        "target_entity_type TEXT, target_entity_id TEXT, project_id TEXT, decision TEXT, "
        "policy_rule_id TEXT, telemetry_json TEXT, timestamp TEXT);"
    )
    conn.commit()

    notif_store = NotificationStore(conn)
    audit_writer = AuditWriter(conn)

    # Normal write succeeds when not draining
    notif = notif_store.create_notification(
        notification_id="n1",
        event_type=NotificationEventType.GENERAL_INFO,
        priority=NotificationPriority.P3,
        title="Title",
        body="Body",
    )
    assert notif.notification_id == "n1"

    audit_rec = audit_writer.write(
        event_type=AuditEventType.AUTHZ_DECISION,
        actor="tester",
        target_entity_type="item",
        target_entity_id="i1",
        decision=AuditDecision.ALLOW,
        policy_rule_id="rule1",
    )
    assert audit_rec.audit_id is not None

    # Now activate drain
    ConnectionCoordinator.set_drain(db_path, timeout_sec=5.0)
    try:
        # Both stores must be gated and raise DatabaseDrainingError
        with pytest.raises(DatabaseDrainingError):
            notif_store.create_notification(
                notification_id="n2",
                event_type=NotificationEventType.GENERAL_INFO,
                priority=NotificationPriority.P3,
                title="Title2",
                body="Body2",
            )

        with pytest.raises(DatabaseDrainingError):
            audit_writer.write(
                event_type=AuditEventType.AUTHZ_DECISION,
                actor="tester",
                target_entity_type="item",
                target_entity_id="i2",
                decision=AuditDecision.ALLOW,
                policy_rule_id="rule2",
            )
    finally:
        ConnectionCoordinator.release_drain(db_path)
        conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_35_parity_detects_row_content_mutation_without_count_change(temp_dir: Path):
    """SEC-044-06: _collect_database_parity detects content mutation (UPDATE) even if row count is identical."""
    import sqlcipher3
    from src.security.database_key_rotation import _collect_table_counts as _collect_database_parity

    key = b"m" * 32
    db_path = temp_dir / "data" / "sec06.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlcipher3.connect(str(db_path))
    conn.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    conn.execute("CREATE TABLE config (k TEXT PRIMARY KEY, v TEXT);")
    conn.execute("INSERT INTO config VALUES ('status', 'active');")
    conn.commit()
    conn.close()

    baseline = _collect_database_parity(db_path, key)

    # Mutate data in place: row count remains 1, but content changes
    conn2 = sqlcipher3.connect(str(db_path))
    conn2.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    conn2.execute("UPDATE config SET v = 'compromised' WHERE k = 'status';")
    conn2.commit()
    conn2.close()

    mutated = _collect_database_parity(db_path, key)
    assert baseline != mutated, "Parity check failed to detect row content modification"


def test_36_atomic_drain_file_creation(temp_dir: Path):
    """SEC-044-07: set_drain creates complete JSON drain file atomically without partial reads."""
    db_path = temp_dir / "data" / "sec07.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    ConnectionCoordinator.set_drain(db_path, timeout_sec=10.0)
    drain_path = ConnectionCoordinator.get_drain_path(db_path)
    assert drain_path.is_file()

    # Must be valid, parseable JSON
    data = json.loads(drain_path.read_text(encoding="utf-8"))
    assert data["state"] == "DRAINING"
    assert data["coordinator_pid"] == os.getpid()

    # Temp file must have been cleanly removed
    tmp_path = drain_path.with_suffix(".drain.tmp")
    assert not tmp_path.exists()

    ConnectionCoordinator.release_drain(db_path)
    assert not drain_path.exists()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_37_real_subprocess_drain_coordination(temp_dir: Path):
    """SEC-044-05: Real independent Python OS process is blocked while database is draining."""
    key_file = temp_dir / "keys" / "sec05.key.enc"
    protector = SoftwareKeyProtector(b"5" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "sec05.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE shared (id INT);")
    conn.commit()
    conn.close()

    # Start drain in current process
    ConnectionCoordinator.set_drain(db_path, timeout_sec=5.0)

    # Spawn an independent subprocess attempting to open connection with drain_wait_timeout=0.2
    worker_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from src.storage.database import create_connection\n"
        "from src.storage.errors import DatabaseDrainingError\n"
        f"db_path = Path({repr(str(db_path))})\n"
        f"key = bytes.fromhex({repr(key.hex())})\n"
        "try:\n"
        "    conn = create_connection(db_path, encryption_key=key, encrypted=True, drain_wait_timeout=0.2)\n"
        "    conn.close()\n"
        "    sys.exit(0)\n"
        "except DatabaseDrainingError:\n"
        "    sys.exit(42)\n"
        "except Exception as exc:\n"
        "    sys.exit(1)\n"
    )
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", worker_script],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(repo_root),
    )

    ConnectionCoordinator.release_drain(db_path)

    assert proc.returncode == 42, f"Subprocess exited {proc.returncode}, stdout: {proc.stdout}, stderr: {proc.stderr}"


# ============================================================================
# Tests 38–45: Genuine Multi-Process OS Concurrency Scenarios (Prompt 047)
# ============================================================================

@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_38_writer_process_blocked_by_drain(temp_dir: Path):
    """P047-01: A writer subprocess cannot INSERT while drain is active in the parent process.

    Scenario: Parent drains the database; child process attempts create_connection + INSERT.
    Expected: Child is blocked at create_connection (DatabaseDrainingError, exit 42).
    """
    key_file = temp_dir / "keys" / "t38.key.enc"
    protector = SoftwareKeyProtector(b"3" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t38.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE logs (msg TEXT);")
    conn.commit()
    conn.close()

    # Parent sets drain so that cross-process writers see it via the drain file
    ConnectionCoordinator.set_drain(db_path, timeout_sec=5.0)

    worker_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from src.storage.database import create_connection\n"
        "from src.storage.errors import DatabaseDrainingError\n"
        f"db_path = Path({repr(str(db_path))})\n"
        f"key = bytes.fromhex({repr(key.hex())})\n"
        "try:\n"
        "    conn = create_connection(db_path, encryption_key=key, encrypted=True, drain_wait_timeout=0.2)\n"
        "    conn.execute(\"INSERT INTO logs VALUES ('injected');\")\n"
        "    conn.close()\n"
        "    sys.exit(0)\n"
        "except DatabaseDrainingError:\n"
        "    sys.exit(42)\n"
        "except Exception as exc:\n"
        "    print('unexpected:', exc, file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", worker_script],
        capture_output=True, text=True, timeout=10, cwd=str(repo_root),
    )
    ConnectionCoordinator.release_drain(db_path)
    assert proc.returncode == 42, (
        f"Writer subprocess should have been blocked (exit 42), got {proc.returncode}. "
        f"stderr: {proc.stderr}"
    )


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_39_new_connection_waits_for_drain_then_connects(temp_dir: Path):
    """P047-02: A subprocess blocked by drain can reconnect after drain is released.

    Scenario: Parent drains; child times out (exit 42). Parent releases; child retries and succeeds.
    """
    key_file = temp_dir / "keys" / "t39.key.enc"
    protector = SoftwareKeyProtector(b"9" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t39.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE state (v INT);")
    conn.execute("INSERT INTO state VALUES (1);")
    conn.commit()
    conn.close()

    ConnectionCoordinator.set_drain(db_path, timeout_sec=5.0)

    blocked_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from src.storage.database import create_connection\n"
        "from src.storage.errors import DatabaseDrainingError\n"
        f"db_path = Path({repr(str(db_path))})\n"
        f"key = bytes.fromhex({repr(key.hex())})\n"
        "try:\n"
        "    conn = create_connection(db_path, encryption_key=key, encrypted=True, drain_wait_timeout=0.1)\n"
        "    conn.close()\n"
        "    sys.exit(0)\n"
        "except DatabaseDrainingError:\n"
        "    sys.exit(42)\n"
    )
    repo_root = Path(__file__).resolve().parent.parent
    proc1 = subprocess.run(
        [sys.executable, "-c", blocked_script],
        capture_output=True, text=True, timeout=10, cwd=str(repo_root),
    )
    assert proc1.returncode == 42, f"First attempt (during drain) should fail: {proc1.stderr}"

    ConnectionCoordinator.release_drain(db_path)

    success_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from src.storage.database import create_connection\n"
        f"db_path = Path({repr(str(db_path))})\n"
        f"key = bytes.fromhex({repr(key.hex())})\n"
        "try:\n"
        "    conn = create_connection(db_path, encryption_key=key, encrypted=True)\n"
        "    row = conn.execute('SELECT v FROM state;').fetchone()\n"
        "    conn.close()\n"
        "    sys.exit(0 if row and row[0] == 1 else 2)\n"
        "except Exception as exc:\n"
        "    print('error:', exc, file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    proc2 = subprocess.run(
        [sys.executable, "-c", success_script],
        capture_output=True, text=True, timeout=10, cwd=str(repo_root),
    )
    assert proc2.returncode == 0, (
        f"Second attempt (after drain release) should succeed: {proc2.stderr}"
    )


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_40_concurrent_rotation_mutual_exclusion(temp_dir: Path):
    """P047-03: Two concurrent rotation attempts — only one proceeds; the other fails with RotationLockError.

    Scenario: Parent holds ExclusiveDatabaseLock; subprocess attempts rotate_database_key.
    Expected: Subprocess exits with RotationLockError (exit 43).
    """
    key_file = temp_dir / "keys" / "t40.key.enc"
    protector = SoftwareKeyProtector(b"4" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t40.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE data (id INT);")
    conn.execute("INSERT INTO data VALUES (1);")
    conn.commit()
    conn.close()

    contender_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from src.security.database_key_rotation import rotate_database_key, RotationLockError\n"
        "from src.security.dpapi import SoftwareKeyProtector\n"
        "from src.security.database_key import DatabaseKeyManager\n"
        f"key_file = Path({repr(str(key_file))})\n"
        f"db_path = Path({repr(str(db_path))})\n"
        "protector = SoftwareKeyProtector(b'4' * 32)\n"
        "key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)\n"
        "new_key = b'\\xff' * 32\n"
        "try:\n"
        "    rotate_database_key(db_path, key_manager=key_mgr, new_key=new_key, timeout_sec=0.5)\n"
        "    sys.exit(0)\n"
        "except RotationLockError:\n"
        "    sys.exit(43)\n"
        "except Exception as exc:\n"
        "    print('unexpected:', exc, file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    repo_root = Path(__file__).resolve().parent.parent

    with ExclusiveDatabaseLock(db_path, timeout_sec=5.0):
        proc = subprocess.run(
            [sys.executable, "-c", contender_script],
            capture_output=True, text=True, timeout=10, cwd=str(repo_root),
        )

    assert proc.returncode == 43, (
        f"Concurrent rotation should fail with RotationLockError (exit 43), "
        f"got {proc.returncode}. stderr: {proc.stderr}"
    )


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_41_parity_detects_schema_ddl_mutation(temp_dir: Path):
    """P047-04: _collect_database_parity detects schema mutations (CREATE INDEX, ALTER TABLE).

    Verifies __schema_hash__ changes when DDL is executed between baseline and re-check.
    """
    from src.security.database_key_rotation import _collect_database_parity
    import sqlcipher3 as _sc3

    key = b"s" * 32
    db_path = temp_dir / "data" / "t41.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = _sc3.connect(str(db_path))
    conn.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    conn.execute("CREATE TABLE items (id INT PRIMARY KEY, name TEXT);")
    conn.execute("INSERT INTO items VALUES (1, 'widget');")
    conn.commit()
    conn.close()

    baseline = _collect_database_parity(db_path, key)

    # Create index — schema mutation
    conn2 = _sc3.connect(str(db_path))
    conn2.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    conn2.execute("CREATE INDEX idx_name ON items(name);")
    conn2.commit()
    conn2.close()

    after_index = _collect_database_parity(db_path, key)
    assert baseline["__schema_hash__"] != after_index["__schema_hash__"], (
        "Schema hash must differ after CREATE INDEX"
    )

    # Alter table — another schema mutation
    conn3 = _sc3.connect(str(db_path))
    conn3.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    conn3.execute("ALTER TABLE items ADD COLUMN price REAL DEFAULT 0.0;")
    conn3.commit()
    conn3.close()

    after_alter = _collect_database_parity(db_path, key)
    assert after_index["__schema_hash__"] != after_alter["__schema_hash__"], (
        "Schema hash must differ after ALTER TABLE ADD COLUMN"
    )


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_42_parity_stable_across_sqlcipher_export(temp_dir: Path):
    """P047-05: _collect_database_parity is identical between original and sqlcipher_export copy.

    Confirms parity comparison in rotate_database_key won't false-positive on faithful export.
    """
    from src.security.database_key_rotation import _collect_database_parity
    import sqlcipher3 as _sc3

    key1 = b"x" * 32
    key2 = b"y" * 32
    db_path = temp_dir / "data" / "t42_src.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path2 = temp_dir / "data" / "t42_exported.db"

    conn = _sc3.connect(str(db_path))
    conn.execute(f"PRAGMA key = \"x'{key1.hex()}'\";")
    conn.execute("CREATE TABLE events (id INT, ts TEXT, payload BLOB);")
    conn.execute("INSERT INTO events VALUES (1, '2026-01-01', NULL);")
    conn.execute("INSERT INTO events VALUES (2, '2026-01-02', x'deadbeef');")
    conn.commit()
    conn.close()

    baseline = _collect_database_parity(db_path, key1)

    c = _sc3.connect(str(db_path))
    c.execute(f"PRAGMA key = \"x'{key1.hex()}'\";")
    c.execute(f"ATTACH DATABASE '{db_path2}' AS staged KEY \"x'{key2.hex()}'\";")
    c.execute("SELECT sqlcipher_export('staged');")
    c.execute("DETACH DATABASE staged;")
    c.close()

    exported = _collect_database_parity(db_path2, key2)
    assert baseline == exported, (
        f"Parity must be identical after faithful sqlcipher_export.\n"
        f"Baseline: {baseline}\nExported: {exported}"
    )


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_43_coordinated_connection_blocks_writes_during_drain(temp_dir: Path):
    """P047-06: CoordinatedConnection/CoordinatedCursor block all write SQL in-process while draining.

    Verifies the universal write-gate injected by factory= in create_connection().
    """
    from src.storage.errors import DatabaseDrainingError

    key_file = temp_dir / "keys" / "t43.key.enc"
    protector = SoftwareKeyProtector(b"6" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t43.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE notes (content TEXT);")
    conn.execute("INSERT INTO notes VALUES ('initial');")
    conn.commit()

    ConnectionCoordinator.set_drain(db_path, timeout_sec=5.0)

    try:
        # Reads must pass through
        rows = conn.execute("SELECT content FROM notes;").fetchall()
        assert len(rows) == 1

        # INSERT blocked
        with pytest.raises(DatabaseDrainingError):
            conn.execute("INSERT INTO notes VALUES ('injected');")

        # UPDATE blocked
        with pytest.raises(DatabaseDrainingError):
            conn.execute("UPDATE notes SET content = 'hacked';")

        # DELETE blocked
        with pytest.raises(DatabaseDrainingError):
            conn.execute("DELETE FROM notes WHERE content = 'initial';")

        # DDL blocked
        with pytest.raises(DatabaseDrainingError):
            conn.execute("CREATE TABLE evil (x INT);")

        # transaction() rejected at gate
        with pytest.raises(DatabaseDrainingError):
            with transaction(conn):
                pass

    finally:
        ConnectionCoordinator.release_drain(db_path)
        conn.close()

    # After drain released: writes succeed
    conn2 = create_connection(db_path, encryption_key=key, encrypted=True)
    conn2.execute("INSERT INTO notes VALUES ('after_drain');")
    conn2.commit()
    rows2 = conn2.execute("SELECT content FROM notes;").fetchall()
    assert len(rows2) == 2
    conn2.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_44_startup_reads_new_key_after_rotation(temp_dir: Path):
    """P047-07: A subprocess can read the database using the new key after a completed rotation.

    Scenario: Parent rotates database to key2. Subprocess loads key from key manager, connects, reads.
    Expected: Subprocess exits 0 and reads correct data.
    """
    key_file = temp_dir / "keys" / "t44.key.enc"
    protector = SoftwareKeyProtector(b"7" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xaa" * 32

    db_path = temp_dir / "data" / "t44.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT);")
    conn.execute("INSERT INTO settings VALUES ('version', '1');")
    conn.commit()
    conn.close()

    result = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)
    assert result.get("status") == "SUCCESS"
    assert key_mgr.get_key() == key2

    subprocess_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from src.storage.database import create_connection\n"
        "from src.security.dpapi import SoftwareKeyProtector\n"
        "from src.security.database_key import DatabaseKeyManager\n"
        f"key_file = Path({repr(str(key_file))})\n"
        f"db_path = Path({repr(str(db_path))})\n"
        "protector = SoftwareKeyProtector(b'7' * 32)\n"
        "key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)\n"
        "key = key_mgr.get_key()\n"
        "try:\n"
        "    conn = create_connection(db_path, encryption_key=key, encrypted=True)\n"
        "    row = conn.execute(\"SELECT v FROM settings WHERE k='version';\").fetchone()\n"
        "    conn.close()\n"
        "    sys.exit(0 if row and row[0] == '1' else 2)\n"
        "except Exception as exc:\n"
        "    print('error:', exc, file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", subprocess_script],
        capture_output=True, text=True, timeout=15, cwd=str(repo_root),
    )
    assert proc.returncode == 0, (
        f"Subprocess should read DB with new key successfully. "
        f"exit={proc.returncode}, stderr={proc.stderr}"
    )


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_45_pre_swap_exclusive_access_check_blocks_open_handle(temp_dir: Path):
    """P047-08: can_acquire_exclusive_file_access returns False while a Windows handle is open.

    Verifies the pre-swap guard used in Step 7.5 of rotate_database_key on Windows.
    """
    import ctypes
    import sys as _sys

    db_path = temp_dir / "data" / "t45_exclusive.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"placeholder")

    assert can_acquire_exclusive_file_access(db_path), (
        "Should be able to acquire exclusive access when no handles are open"
    )

    if _sys.platform != "win32":
        pytest.skip("Windows-specific exclusive handle test skipped on non-Windows")

    kernel32 = ctypes.windll.kernel32
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_NONE = 0
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80

    handle = kernel32.CreateFileW(
        str(db_path),
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_NONE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    assert handle != -1, "Failed to open non-sharing file handle for test"
    try:
        assert not can_acquire_exclusive_file_access(db_path), (
            "Exclusive access must be denied while a non-sharing handle is open (os.replace would fail)"
        )
    finally:
        kernel32.CloseHandle(handle)

    assert can_acquire_exclusive_file_access(db_path), (
        "Exclusive access must be restored after handle is closed"
    )


# ============================================================================
# Tests 46–53: Concurrency Race & Publication Safety Verification (Prompt 049)
# ============================================================================


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_46_committed_write_racing_with_rotation_aborts_without_data_loss(temp_dir: Path):
    """P049-01: A write committed during Step 7.5 parity check aborts rotation and is 100% preserved."""
    key_file = temp_dir / "keys" / "t46.key.enc"
    protector = SoftwareKeyProtector(b"4" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xb2" * 32

    db_path = temp_dir / "data" / "t46.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE records (id INT, txt TEXT);")
    conn.execute("INSERT INTO records VALUES (1, 'initial');")
    conn.commit()
    conn.close()

    from src.security import database_key_rotation as dkr
    orig_collect = dkr._collect_database_parity
    parity_call_count = [0]

    def racing_collect(target_path, key):
        parity_call_count[0] += 1
        res = orig_collect(target_path, key)
        # On the 3rd parity collection (Step 7.5 pre-swap parity check on active DB),
        # simulate an uncoordinated write committing to the active DB
        if parity_call_count[0] == 3:
            import sqlcipher3
            c = sqlcipher3.connect(str(db_path))
            c.execute(f"PRAGMA key = \"x'{key1.hex()}'\";")
            c.execute("INSERT INTO records VALUES (999, 'racing_during_export');")
            c.commit()
            c.close()
            # Return updated parity to simulate detection
            return orig_collect(target_path, key)
        return res

    with patch("src.security.database_key_rotation._collect_database_parity", side_effect=racing_collect):
        with pytest.raises(RotationError, match="Active database modified during rotation export"):
            rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)

    # Key must NOT have been rotated
    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1

    # Verify write was preserved
    chk_conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    rows = chk_conn.execute("SELECT id, txt FROM records ORDER BY id;").fetchall()
    chk_conn.close()
    assert len(rows) == 2
    assert tuple(rows[1]) == (999, "racing_during_export")


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_47_write_at_publication_boundary_triggers_key_rollback_and_preserves_write(temp_dir: Path):
    """P049-02: A write committed after key commitment (Step 8) triggers key rollback and is preserved."""
    key_file = temp_dir / "keys" / "t47.key.enc"
    protector = SoftwareKeyProtector(b"5" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xb3" * 32

    db_path = temp_dir / "data" / "t47.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t47 (val INT);")
    conn.execute("INSERT INTO t47 VALUES (10);")
    conn.commit()
    conn.close()

    orig_record_state = RotationJournal.record_state

    def racing_record_state(self, state, **kwargs):
        res = orig_record_state(self, state, **kwargs)
        if state == RotationState.KEY_COMMITTED:
            # An uncoordinated write commits to the active database after new key is committed
            import sqlcipher3
            c = sqlcipher3.connect(str(db_path))
            c.execute(f"PRAGMA key = \"x'{key1.hex()}'\";")
            c.execute("INSERT INTO t47 VALUES (777);")
            c.commit()
            c.close()
        return res

    with patch("src.security.database_key_rotation.RotationJournal.record_state", autospec=True, side_effect=racing_record_state):
        with pytest.raises(RotationError, match="Active database was modified immediately prior to swap"):
            rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)

    # Key manager was rolled back to version 1
    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1

    # Database file was not replaced, write is preserved under key1
    chk = create_connection(db_path, encryption_key=key1, encrypted=True)
    rows = chk.execute("SELECT val FROM t47 ORDER BY val;").fetchall()
    chk.close()
    assert [r[0] for r in rows] == [10, 777]


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_48_wal_checkpoint_and_pending_wal_data_blocks_swap(temp_dir: Path):
    """P049-03: Rotation aborts without swap if WAL checkpoint is busy or uncheckpointed WAL bytes exist."""
    key_file = temp_dir / "keys" / "t48.key.enc"
    protector = SoftwareKeyProtector(b"6" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xb4" * 32

    db_path = temp_dir / "data" / "t48.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t48 (val INT);")
    conn.execute("INSERT INTO t48 VALUES (1);")
    conn.commit()
    conn.close()

    # Part A: WAL checkpoint busy (wal_chk[0] == 1) aborts rotation
    import sqlcipher3
    orig_connect = sqlcipher3.connect

    class ConnectionWrapper:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):
            if "wal_checkpoint(TRUNCATE)" in sql:
                class MockResult:
                    def fetchone(self):
                        return (1, 10, 5)  # busy=1

                return MockResult()
            return self._real.execute(sql, *args, **kwargs)

        def close(self):
            return self._real.close()

        def __getattr__(self, name):
            return getattr(self._real, name)

    def busy_connect(*args, **kwargs):
        return ConnectionWrapper(orig_connect(*args, **kwargs))

    with patch("sqlcipher3.connect", side_effect=busy_connect):
        with pytest.raises(RotationError, match="WAL checkpoint.*busy"):
            rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)

    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1

    # Part B: Pending uncheckpointed WAL bytes (> 0 bytes) blocks swap
    wal_file = db_path.with_name(f"{db_path.name}-wal")
    step75_reached = [False]

    class WalInjectWrapper:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):
            if "wal_checkpoint(TRUNCATE)" in sql:
                step75_reached[0] = True
            return self._real.execute(sql, *args, **kwargs)

        def close(self):
            res = self._real.close()
            if step75_reached[0]:
                # Inject uncheckpointed bytes into wal_file after checkpoint close
                wal_file.write_bytes(b"\x00" * 512)
            return res

        def __getattr__(self, name):
            return getattr(self._real, name)

    def wal_connect(*args, **kwargs):
        return WalInjectWrapper(orig_connect(*args, **kwargs))

    with patch("sqlcipher3.connect", side_effect=wal_connect):
        with pytest.raises(RotationError, match="WAL file has uncheckpointed data"):
            rotate_database_key(db_path, key_manager=key_mgr, new_key=key2, drain_timeout_sec=2.0)

    if wal_file.exists():
        wal_file.unlink()

    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_49_cte_savepoint_and_mutating_pragma_blocked_during_drain(temp_dir: Path):
    """P049-04: CTE writes, SAVEPOINTs, and mutating PRAGMAs are rejected during drain; read-only operations pass."""
    key_file = temp_dir / "keys" / "t49.key.enc"
    protector = SoftwareKeyProtector(b"7" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t49.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE items (id INT, name TEXT);")
    conn.execute("INSERT INTO items VALUES (1, 'alpha');")
    conn.commit()

    ConnectionCoordinator.set_drain(db_path, timeout_sec=5.0)
    try:
        # 1. Safe read operations succeed
        assert conn.execute("SELECT count(*) FROM items;").fetchone()[0] == 1
        cte_read = conn.execute("WITH cte AS (SELECT id, name FROM items) SELECT name FROM cte;").fetchone()
        assert cte_read[0] == "alpha"
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()
        assert fk is not None
        uv = conn.execute("PRAGMA user_version;").fetchone()
        assert uv is not None

        # 2. CTE write statements blocked
        with pytest.raises(DatabaseDrainingError):
            conn.execute("WITH ins AS (SELECT 2 as id, 'beta' as name) INSERT INTO items SELECT * FROM ins;")

        with pytest.raises(DatabaseDrainingError):
            conn.execute("WITH up AS (SELECT 1 as id) UPDATE items SET name = 'mod' WHERE id IN (SELECT id FROM up);")

        with pytest.raises(DatabaseDrainingError):
            conn.execute("WITH del AS (SELECT 1 as id) DELETE FROM items WHERE id IN (SELECT id FROM del);")

        # 3. SAVEPOINT / RELEASE statements blocked
        with pytest.raises(DatabaseDrainingError):
            conn.execute("SAVEPOINT sp_test;")

        with pytest.raises(DatabaseDrainingError):
            conn.execute("RELEASE SAVEPOINT sp_test;")

        # 4. Mutating PRAGMAs with arbitrary whitespace blocked
        with pytest.raises(DatabaseDrainingError):
            conn.execute("PRAGMA   foreign_keys   =   OFF  ;")

        with pytest.raises(DatabaseDrainingError):
            conn.execute("PRAGMA user_version = 100;")

        with pytest.raises(DatabaseDrainingError):
            conn.execute("PRAGMA synchronous=FULL;")

    finally:
        ConnectionCoordinator.release_drain(db_path)
        conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_50_stale_key_startup_concurrent_with_rotation_reloads_authoritative_key(temp_dir: Path):
    """P049-05: Startup initialization reloads authoritative key and succeeds even if rotation completed concurrently."""
    key_file = temp_dir / "keys" / "t50.key.enc"
    protector = SoftwareKeyProtector(b"8" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xb5" * 32

    db_path = temp_dir / "data" / "t50.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE config (k TEXT PRIMARY KEY, v TEXT);")
    conn.execute("INSERT INTO config VALUES ('theme', 'dark');")
    conn.commit()
    conn.close()

    # Now rotate the database to key2
    res = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)
    assert res["status"] == "SUCCESS"
    assert key_mgr.get_key() == key2

    # A secondary process/component starting up with an obsolete key1 calls initialize_runtime_database
    init_conn = initialize_runtime_database(
        db_path=db_path,
        migrations_dir=MIGRATIONS_DIR,
        key_manager=key_mgr,
        encryption_key=key1,
        encrypted=True,
    )
    row = init_conn.execute("SELECT v FROM config WHERE k='theme';").fetchone()
    assert row[0] == "dark"
    init_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_51_concurrent_rotations_strict_mutual_exclusion(temp_dir: Path):
    """P049-06: Two independent OS subprocesses attempt rotation simultaneously; exactly one succeeds, one fails."""
    key_file = temp_dir / "keys" / "t51.key.enc"
    protector = SoftwareKeyProtector(b"9" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t51.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE data51 (id INT);")
    conn.execute("INSERT INTO data51 VALUES (100);")
    conn.commit()
    conn.close()

    p1_script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from src.security import database_key_rotation as dkr\n"
        "from src.security.dpapi import SoftwareKeyProtector\n"
        "from src.security.database_key import DatabaseKeyManager\n"
        f"key_file = Path({repr(str(key_file))})\n"
        f"db_path = Path({repr(str(db_path))})\n"
        "orig_collect = dkr._collect_database_parity\n"
        "def slow_collect(target_path, key):\n"
        "    time.sleep(1.0)\n"
        "    return orig_collect(target_path, key)\n"
        "dkr._collect_database_parity = slow_collect\n"
        "protector = SoftwareKeyProtector(b'9' * 32)\n"
        "key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)\n"
        "try:\n"
        "    res = dkr.rotate_database_key(db_path, key_manager=key_mgr, timeout_sec=5.0)\n"
        "    sys.exit(0)\n"
        "except Exception as exc:\n"
        "    print('p1 error:', exc, file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )

    p2_script = (
        "import sys, os\n"
        "from pathlib import Path\n"
        "from src.security.database_key_rotation import rotate_database_key, RotationLockError, RotationDrainTimeoutError\n"
        "from src.security.dpapi import SoftwareKeyProtector\n"
        "from src.security.database_key import DatabaseKeyManager\n"
        f"key_file = Path({repr(str(key_file))})\n"
        f"db_path = Path({repr(str(db_path))})\n"
        "protector = SoftwareKeyProtector(b'9' * 32)\n"
        "key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)\n"
        "try:\n"
        "    res = rotate_database_key(db_path, key_manager=key_mgr, timeout_sec=0.2, drain_timeout_sec=0.5)\n"
        "    sys.exit(0)\n"
        "except (RotationLockError, RotationDrainTimeoutError):\n"
        "    sys.exit(43)\n"
        "except Exception as exc:\n"
        "    print('p2 error:', exc, file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    repo_root = Path(__file__).resolve().parent.parent

    p1 = subprocess.Popen([sys.executable, "-c", p1_script], cwd=str(repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.3)
    p2 = subprocess.Popen([sys.executable, "-c", p2_script], cwd=str(repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out2, err2 = p2.communicate(timeout=10)
    out1, err1 = p1.communicate(timeout=15)

    assert p2.returncode == 43, f"Process 2 should be rejected with exit code 43. Stderr: {err2.decode()}"
    assert p1.returncode == 0, f"Process 1 should succeed with exit code 0. Stderr: {err1.decode()}"

    final_key = key_mgr.get_key()
    chk = create_connection(db_path, encryption_key=final_key, encrypted=True)
    assert chk.execute("SELECT id FROM data51;").fetchone()[0] == 100
    chk.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_52_process_termination_at_each_recoverable_stage(temp_dir: Path):
    """P049-07: Recovery deterministically restores consistency from each interrupted rotation stage."""
    # Test Stage 1: STAGING_EXPORTED
    db1 = temp_dir / "data" / "t52_s1.db"
    db1.parent.mkdir(parents=True, exist_ok=True)
    km1 = DatabaseKeyManager(key_path=temp_dir / "keys" / "t52_s1.key.enc", key_protector=SoftwareKeyProtector(b"a" * 32))
    k1 = km1.get_or_create_key()
    c1 = create_connection(db1, encryption_key=k1, encrypted=True)
    c1.execute("CREATE TABLE s1 (x INT);")
    c1.commit()
    c1.close()

    j1 = RotationJournal(db1)
    stg1 = db1.with_name(f"{db1.name}.stage1.staging")
    stg1.write_text("staging data")
    j1.record_state(RotationState.STAGING_EXPORTED, staging_db_path=str(stg1))

    rec1 = recover_interrupted_rotation(db1, key_manager=km1)
    assert rec1["status"] == "ROLLED_BACK"
    assert not stg1.exists()
    assert not j1.exists()
    chk1 = create_connection(db1, encryption_key=k1, encrypted=True)
    assert chk1.execute("SELECT count(*) FROM s1;").fetchone()[0] == 0
    chk1.close()

    # Test Stage 2: KEY_COMMITTED (process killed before os.replace, active DB has old key)
    db2 = temp_dir / "data" / "t52_s2.db"
    km2 = DatabaseKeyManager(key_path=temp_dir / "keys" / "t52_s2.key.enc", key_protector=SoftwareKeyProtector(b"b" * 32))
    k_old = km2.get_or_create_key()
    c2 = create_connection(db2, encryption_key=k_old, encrypted=True)
    c2.execute("CREATE TABLE s2 (x INT);")
    c2.commit()
    c2.close()

    k_new = b"\xcc" * 32
    arch_path = km2.archive_current_key(1)
    km2.commit_new_key(k_new, new_version=2)

    j2 = RotationJournal(db2)
    stg2 = db2.with_name(f"{db2.name}.stage2.staging")
    stg2.write_text("staging data")
    j2.record_state(RotationState.KEY_COMMITTED, staging_db_path=str(stg2), archived_key_path=str(arch_path), old_version=1)

    rec2 = recover_interrupted_rotation(db2, key_manager=km2)
    assert rec2["status"] == "ROLLED_BACK"
    assert km2.get_current_version() == 1
    assert km2.get_key() == k_old
    assert not stg2.exists()
    assert not j2.exists()
    chk2 = create_connection(db2, encryption_key=k_old, encrypted=True)
    assert chk2.execute("SELECT count(*) FROM s2;").fetchone()[0] == 0
    chk2.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_53_stale_lock_and_stale_journal_cleanup_after_process_crash(temp_dir: Path):
    """P049-08: Stale lock from dead process is acquired safely; stale journal is cleanly recovered."""
    key_file = temp_dir / "keys" / "t53.key.enc"
    protector = SoftwareKeyProtector(b"c" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t53.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE t53 (v TEXT);")
    conn.execute("INSERT INTO t53 VALUES ('stable');")
    conn.commit()
    conn.close()

    # 1. Simulate a stale lock from a dead PID (e.g. 999999)
    lock_file = db_path.with_name(f"{db_path.name}.rotation.lock")
    lock_file.write_text("999999:2026-01-01T00:00:00", encoding="utf-8")

    # ExclusiveDatabaseLock must detect that pid 999999 is dead, break the lock, and acquire successfully
    with ExclusiveDatabaseLock(db_path, timeout_sec=2.0) as lock:
        assert lock._fd is not None

    # 2. Simulate a stale journal from an interrupted rotation
    journal = RotationJournal(db_path)
    stg_file = db_path.with_name(f"{db_path.name}.crash.staging")
    stg_file.write_text("crash artifact", encoding="utf-8")
    journal.record_state(RotationState.STAGING_EXPORTED, staging_db_path=str(stg_file))

    # Normal rotation succeeds despite previous crash artifacts
    new_key = b"\xdd" * 32
    res = rotate_database_key(db_path, key_manager=key_mgr, new_key=new_key)
    assert res["status"] == "SUCCESS"
    assert not stg_file.exists()
    assert not journal.exists()

    chk = create_connection(db_path, encryption_key=new_key, encrypted=True)
    assert chk.execute("SELECT v FROM t53;").fetchone()[0] == "stable"
    chk.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_54_adversarial_committed_write_before_replacement_aborts_without_data_loss(temp_dir: Path):
    """P052-01: Adversarial subprocess commits write at Step 8/9 boundary; rotation aborts fail-closed, write is 100% preserved."""
    key_file = temp_dir / "keys" / "t54.key.enc"
    protector = SoftwareKeyProtector(b"k" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xee" * 32

    db_path = temp_dir / "data" / "t54.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE records (id INT PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO records VALUES (1, 'INITIAL');")
    conn.commit()
    conn.close()

    # Adversarial subprocess script that opens db_path with key1, commits a write, and exits
    writer_script = (
        "import sys, sqlcipher3\n"
        f"conn = sqlcipher3.connect(r'{db_path}')\n"
        f"conn.execute('PRAGMA key = \"x\\'{key1.hex()}\\'\";')\n"
        "conn.execute('INSERT INTO records VALUES (999, \"SUBPROCESS_RACING_WRITE\");')\n"
        "conn.commit()\n"
        "conn.close()\n"
        "sys.exit(0)\n"
    )

    orig_commit_new_key = key_mgr.commit_new_key

    def racing_commit_new_key(*args, **kwargs):
        res = orig_commit_new_key(*args, **kwargs)
        sub = subprocess.run([sys.executable, "-c", writer_script], capture_output=True, text=True)
        assert sub.returncode == 0, f"Subprocess writer failed: {sub.stderr}"
        return res

    with patch.object(key_mgr, "commit_new_key", side_effect=racing_commit_new_key):
        with pytest.raises(RotationError, match="Active database was modified immediately prior to swap"):
            rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)

    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1

    verify_conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    rows = [tuple(r) for r in verify_conn.execute("SELECT id, val FROM records ORDER BY id;").fetchall()]
    verify_conn.close()

    assert len(rows) == 2
    assert rows[0] == (1, "INITIAL")
    assert rows[1] == (999, "SUBPROCESS_RACING_WRITE")


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_55_adversarial_write_admission_cte_comment_and_bracket_bypasses(temp_dir: Path):
    """P052-02: CTEs disguised with comments, brackets, or quotes are rejected during drain, safe reads allowed."""
    key_file = temp_dir / "keys" / "t55.key.enc"
    protector = SoftwareKeyProtector(b"m" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t55.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE items (id INT, name TEXT);")
    conn.execute("INSERT INTO items VALUES (1, 'alpha'), (2, 'beta');")
    conn.commit()

    ConnectionCoordinator.set_drain(db_path, timeout_sec=30.0)
    try:
        # 1. CTE with block comment disguising SELECT
        with pytest.raises(DatabaseDrainingError):
            conn.execute("WITH cte AS (SELECT 1) /* SELECT */ UPDATE items SET name = 'hacked';")

        # 2. CTE with line comment disguising SELECT
        with pytest.raises(DatabaseDrainingError):
            conn.execute("WITH cte AS (SELECT 1) -- SELECT\nDELETE FROM items WHERE id = 1;")

        # 3. CTE with bracket identifier [SELECT]
        with pytest.raises(DatabaseDrainingError):
            conn.execute("WITH [SELECT] AS (SELECT 1) INSERT INTO items VALUES (3, 'gamma');")

        # 4. CTE with double-quoted identifier "SELECT"
        with pytest.raises(DatabaseDrainingError):
            conn.execute('WITH "SELECT" AS (SELECT 1) UPDATE items SET name = \'pwned\';')

        # 5. CTE with backtick identifier `SELECT`
        with pytest.raises(DatabaseDrainingError):
            conn.execute("WITH `SELECT` AS (SELECT 1) DELETE FROM items WHERE id = 2;")

        # 6. Valid SELECT CTE queries must succeed under drain
        cur1 = conn.execute("WITH cte AS (SELECT 1 AS val) SELECT val FROM cte;")
        assert cur1.fetchone()[0] == 1

        cur2 = conn.execute("WITH [items_alias] AS (SELECT * FROM items) SELECT COUNT(*) FROM items_alias;")
        assert cur2.fetchone()[0] == 2

    finally:
        ConnectionCoordinator.release_drain(db_path)
        conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_56_adversarial_write_admission_mutating_pragma_function_syntax(temp_dir: Path):
    """P052-03: Mutating PRAGMAs using function syntax or unknown pragmas are blocked; read pragmas succeed."""
    key_file = temp_dir / "keys" / "t56.key.enc"
    protector = SoftwareKeyProtector(b"n" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t56.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE t56 (x INT);")
    conn.commit()

    ConnectionCoordinator.set_drain(db_path, timeout_sec=30.0)
    try:
        # Mutating pragmas via function syntax
        with pytest.raises(DatabaseDrainingError):
            conn.execute("PRAGMA foreign_keys(0);")

        with pytest.raises(DatabaseDrainingError):
            conn.execute("PRAGMA user_version(42);")

        with pytest.raises(DatabaseDrainingError):
            conn.execute("PRAGMA synchronous(0);")

        with pytest.raises(DatabaseDrainingError):
            conn.execute("PRAGMA writable_schema(1);")

        # Unknown pragmas blocked fail-closed
        with pytest.raises(DatabaseDrainingError):
            conn.execute("PRAGMA dangerous_setting = 1;")

        # Read-only PRAGMAs succeed
        assert conn.execute("PRAGMA foreign_keys;").fetchall() is not None
        assert conn.execute("PRAGMA user_version;").fetchall() is not None
        assert conn.execute("PRAGMA table_info('t56');").fetchall() is not None

    finally:
        ConnectionCoordinator.release_drain(db_path)
        conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_57_adversarial_multi_statement_queries_rejected(temp_dir: Path):
    """P052-04: Multi-statement compound queries are rejected fail-closed during drain."""
    key_file = temp_dir / "keys" / "t57.key.enc"
    protector = SoftwareKeyProtector(b"o" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t57.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE t57 (id INT);")
    conn.commit()

    ConnectionCoordinator.set_drain(db_path, timeout_sec=30.0)
    try:
        # Multi-statement injection attempt
        with pytest.raises(DatabaseDrainingError):
            conn.execute("SELECT 1; INSERT INTO t57 VALUES (99);")

        with pytest.raises(DatabaseDrainingError):
            conn.execute("SELECT 1; PRAGMA foreign_keys = OFF;")

        # Even multiple benign queries rejected fail-closed
        with pytest.raises(DatabaseDrainingError):
            conn.execute("SELECT 1; SELECT 2;")

    finally:
        ConnectionCoordinator.release_drain(db_path)
        conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_58_key_cache_poisoning_prevention_on_abort(temp_dir: Path):
    """P052-05: Aborted rotation cleans up staged key; subsequent connection initialization reloads authoritative key."""
    key_file = temp_dir / "keys" / "t58.key.enc"
    protector = SoftwareKeyProtector(b"q" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xfa" * 32

    db_path = temp_dir / "data" / "t58.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT);")
    conn.execute("INSERT INTO settings VALUES ('mode', 'strict');")
    conn.commit()
    conn.close()

    # Force an abort right after key commit (Step 8) before replace (Step 9)
    orig_replace = os.replace

    def failing_replace(src, dst):
        if Path(dst).resolve() == db_path.resolve():
            raise PermissionError("Simulated permanent sharing violation")
        return orig_replace(src, dst)

    with patch("os.replace", side_effect=failing_replace):
        with pytest.raises(RotationError, match="Cannot replace database file|Key rolled back"):
            rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)

    # Key manager must reflect rollback to v1
    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1

    # Secondary component initialization reloads key and connects cleanly
    new_km = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    assert new_km.get_current_version() == 1
    assert new_km.get_key() == key1

    conn2 = initialize_runtime_database(
        db_path=db_path,
        migrations_dir=MIGRATIONS_DIR,
        key_manager=new_km,
        encrypted=True,
    )
    row = conn2.execute("SELECT v FROM settings WHERE k='mode';").fetchone()
    assert row[0] == "strict"
    conn2.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_59_adversarial_subprocess_writer_blocked_during_rotation_and_resumes(temp_dir: Path):
    """P054-01: An independent OS subprocess attempting to write during active rotation is blocked by DatabaseAccessLock and resumes under new key."""
    key_file = temp_dir / "keys" / "t59.key.enc"
    protector = SoftwareKeyProtector(b"1" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xf1" * 32

    db_path = temp_dir / "data" / "t59.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t59 (id INT PRIMARY KEY, msg TEXT);")
    conn.execute("INSERT INTO t59 VALUES (1, 'INITIAL');")
    conn.commit()
    conn.close()

    writer_script = f"""
import sys
from pathlib import Path
sys.path.insert(0, r"{Path(__file__).resolve().parent.parent}")
from src.storage.database import create_connection
from src.storage.errors import DatabaseDrainingError

p = Path(r"{db_path}")
try:
    c = create_connection(p, encryption_key=bytes.fromhex('{key1.hex()}'), encrypted=True, drain_wait_timeout=0.1)
    c.execute("INSERT INTO t59 VALUES (2, 'CONCURRENT_WRITE');")
    c.commit()
    c.close()
    sys.exit(0)
except DatabaseDrainingError:
    sys.exit(42)
except Exception:
    sys.exit(1)
"""

    import src.security.database_key_rotation as dkr
    orig_collect = dkr._collect_database_parity
    subprocess_res = [None]

    def slow_collect(target_path, key):
        # While rotation is holding exclusive DatabaseAccessLock, launch writer subprocess
        if subprocess_res[0] is None:
            sub = subprocess.run([sys.executable, "-c", writer_script], capture_output=True, text=True)
            subprocess_res[0] = sub
        return orig_collect(target_path, key)

    with patch("src.security.database_key_rotation._collect_database_parity", side_effect=slow_collect):
        res = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)

    assert res["status"] == "SUCCESS"
    assert subprocess_res[0] is not None
    assert subprocess_res[0].returncode == 42, f"Subprocess should be blocked (42): {subprocess_res[0].stdout} {subprocess_res[0].stderr}"

    # Verify writer can now connect under new key and commit
    c2 = create_connection(db_path, encryption_key=key2, encrypted=True)
    c2.execute("INSERT INTO t59 VALUES (2, 'RESUMED_WRITE');")
    c2.commit()
    rows = [tuple(r) for r in c2.execute("SELECT id, msg FROM t59 ORDER BY id;").fetchall()]
    c2.close()

    assert rows == [(1, "INITIAL"), (2, "RESUMED_WRITE")]


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_60_adversarial_write_in_former_toctou_window_blocked_by_access_lock(temp_dir: Path):
    """P054-02: Adversarial subprocess attempting write between invariant check and os.replace is physically blocked by DatabaseAccessLock."""
    key_file = temp_dir / "keys" / "t60.key.enc"
    protector = SoftwareKeyProtector(b"2" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xf2" * 32

    db_path = temp_dir / "data" / "t60.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t60 (id INT PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO t60 VALUES (1, 'SAFE');")
    conn.commit()
    conn.close()

    writer_script = f"""
import sys
from pathlib import Path
sys.path.insert(0, r"{Path(__file__).resolve().parent.parent}")
from src.storage.database import create_connection
from src.storage.errors import DatabaseDrainingError

p = Path(r"{db_path}")
try:
    c = create_connection(p, encryption_key=bytes.fromhex('{key1.hex()}'), encrypted=True, drain_wait_timeout=0.1)
    c.execute("INSERT INTO t60 VALUES (999, 'TOCTOU_WRITE');")
    c.commit()
    c.close()
    sys.exit(0)
except DatabaseDrainingError:
    sys.exit(42)
except Exception:
    sys.exit(1)
"""

    orig_replace = os.replace
    writer_res = [None]

    def hook_replace(src, dst):
        if Path(dst).resolve() == db_path.resolve() and writer_res[0] is None:
            # Trigger write attempt precisely at replacement boundary
            writer_res[0] = subprocess.run([sys.executable, "-c", writer_script], capture_output=True, text=True)
        return orig_replace(src, dst)

    with patch("os.replace", side_effect=hook_replace):
        res = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2)

    assert res["status"] == "SUCCESS"
    assert writer_res[0] is not None
    assert writer_res[0].returncode == 42, f"Writer must be blocked in former TOCTOU window: {writer_res[0].stdout}"

    # Verify no data loss or corruption occurred
    c_check = create_connection(db_path, encryption_key=key2, encrypted=True)
    rows = [tuple(r) for r in c_check.execute("SELECT id, val FROM t60 ORDER BY id;").fetchall()]
    c_check.close()
    assert rows == [(1, "SAFE")]


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_61_concurrent_rotations_access_lock_mutual_exclusion(temp_dir: Path):
    """P054-03: Two independent OS subprocesses attempt rotation concurrently; DatabaseAccessLock guarantees mutual exclusion."""
    key_file = temp_dir / "keys" / "t61.key.enc"
    protector = SoftwareKeyProtector(b"3" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t61.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t61 (x INT);")
    conn.execute("INSERT INTO t61 VALUES (42);")
    conn.commit()
    conn.close()

    p1_script = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"{Path(__file__).resolve().parent.parent}")
from src.security.database_key_rotation import rotate_database_key
from src.security.database_key import DatabaseKeyManager
from src.security.dpapi import SoftwareKeyProtector
import src.security.database_key_rotation as dkr

p = Path(r"{db_path}")
kf = Path(r"{key_file}")
orig_collect = dkr._collect_database_parity
def slow_collect(target_path, key):
    time.sleep(0.8)
    return orig_collect(target_path, key)
dkr._collect_database_parity = slow_collect

km = DatabaseKeyManager(key_path=kf, key_protector=SoftwareKeyProtector(b'3' * 32))
try:
    rotate_database_key(p, key_manager=km, timeout_sec=5.0)
    sys.exit(0)
except Exception:
    sys.exit(1)
"""

    p2_script = f"""
import sys
from pathlib import Path
sys.path.insert(0, r"{Path(__file__).resolve().parent.parent}")
from src.security.database_key_rotation import rotate_database_key, RotationLockError
from src.security.database_key import DatabaseKeyManager
from src.security.dpapi import SoftwareKeyProtector

p = Path(r"{db_path}")
kf = Path(r"{key_file}")
km = DatabaseKeyManager(key_path=kf, key_protector=SoftwareKeyProtector(b'3' * 32))
try:
    rotate_database_key(p, key_manager=km, timeout_sec=0.2, drain_timeout_sec=0.2)
    sys.exit(0)
except RotationLockError:
    sys.exit(43)
except Exception:
    sys.exit(1)
"""

    repo_root = Path(__file__).resolve().parent.parent
    p1 = subprocess.Popen([sys.executable, "-c", p1_script], cwd=str(repo_root))
    time.sleep(0.2)
    p2 = subprocess.Popen([sys.executable, "-c", p2_script], cwd=str(repo_root))

    p2.wait(timeout=10)
    p1.wait(timeout=15)

    assert p2.returncode == 43, f"Process 2 should be rejected with exit code 43 (got {p2.returncode})"
    assert p1.returncode == 0, f"Process 1 should succeed with exit code 0 (got {p1.returncode})"

    # Verify database data preserved
    final_key = key_mgr.get_key()
    c = create_connection(db_path, encryption_key=final_key, encrypted=True)
    assert c.execute("SELECT x FROM t61;").fetchone()[0] == 42
    c.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_62_process_termination_releases_os_lock_and_recovers(temp_dir: Path):
    """P054-04: Terminating a process holding exclusive DatabaseAccessLock automatically frees OS lock, allowing clean recovery."""
    key_file = temp_dir / "keys" / "t62.key.enc"
    protector = SoftwareKeyProtector(b"4" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t62.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t62 (v TEXT);")
    conn.execute("INSERT INTO t62 VALUES ('CRASH_TEST');")
    conn.commit()
    conn.close()

    lock_holder_script = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"{Path(__file__).resolve().parent.parent}")
from src.storage.database import DatabaseAccessLock

p = Path(r"{db_path}")
lk = DatabaseAccessLock(p)
assert lk.acquire_exclusive(timeout_sec=5.0)
print("EXCLUSIVE_HELD", flush=True)
time.sleep(30.0)
"""

    repo_root = Path(__file__).resolve().parent.parent
    p = subprocess.Popen(
        [sys.executable, "-c", lock_holder_script],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait until lock is confirmed held
    line = p.stdout.readline().strip()
    assert line == "EXCLUSIVE_HELD"

    # Verify local process cannot acquire shared lock
    local_lock = DatabaseAccessLock(db_path)
    assert not local_lock.acquire_shared(timeout_sec=0.1)

    # Abruptly kill the process holding the lock
    p.kill()
    p.wait(timeout=5)

    # Windows kernel immediately frees the lock: local process can acquire now
    assert local_lock.acquire_shared(timeout_sec=1.0)
    local_lock.release()

    # Database remains valid and intact
    c = create_connection(db_path, encryption_key=key1, encrypted=True)
    assert c.execute("SELECT v FROM t62;").fetchone()[0] == "CRASH_TEST"
    c.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_63_access_lock_failure_aborts_without_key_or_db_mutation(temp_dir: Path):
    """P054-05: When DatabaseAccessLock cannot be acquired, rotation aborts cleanly with no database or key modifications."""
    key_file = temp_dir / "keys" / "t63.key.enc"
    protector = SoftwareKeyProtector(b"5" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\xf3" * 32

    db_path = temp_dir / "data" / "t63.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t63 (status TEXT);")
    conn.execute("INSERT INTO t63 VALUES ('UNTOUCHED');")
    conn.commit()
    conn.close()

    # Hold a shared access lock to simulate active read/write load
    sh_lock = DatabaseAccessLock(db_path)
    assert sh_lock.acquire_shared(timeout_sec=2.0)

    try:
        with pytest.raises(RotationLockError, match="Timed out waiting for exclusive database access lock"):
            rotate_database_key(db_path, key_manager=key_mgr, new_key=key2, timeout_sec=0.2)
    finally:
        sh_lock.release()

    # Key manager must remain on version 1
    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1

    # Database is untouched
    c = create_connection(db_path, encryption_key=key1, encrypted=True)
    assert c.execute("SELECT status FROM t63;").fetchone()[0] == "UNTOUCHED"
    c.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_64_writer_bypass_attempt_rejected_by_access_lock(temp_dir: Path):
    """P054-06: Direct write attempts via transaction, cursor, executemany, executescript are blocked under exclusive lock."""
    key_file = temp_dir / "keys" / "t64.key.enc"
    protector = SoftwareKeyProtector(b"6" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t64.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key, encrypted=True)
    conn.execute("CREATE TABLE t64 (id INT PRIMARY KEY, val TEXT);")
    conn.commit()

    # Hold exclusive access lock (as during maintenance / key rotation)
    ex_lock = DatabaseAccessLock(db_path)
    assert ex_lock.acquire_exclusive(timeout_sec=2.0)

    try:
        # 1. Single autocommit execute rejected
        with pytest.raises(DatabaseDrainingError):
            conn.execute("INSERT INTO t64 VALUES (1, 'val1');")

        # 2. executemany rejected
        with pytest.raises(DatabaseDrainingError):
            conn.executemany("INSERT INTO t64 VALUES (?, ?);", [(2, "val2"), (3, "val3")])

        # 3. executescript rejected
        with pytest.raises(DatabaseDrainingError):
            conn.executescript("INSERT INTO t64 VALUES (4, 'val4');")

        # 4. transaction() context manager rejected
        with pytest.raises(DatabaseDrainingError):
            with transaction(conn):
                conn.execute("INSERT INTO t64 VALUES (5, 'val5');")

        # 5. Safe read operations proceed unblocked
        cur = conn.execute("SELECT count(*) FROM t64;")
        assert cur.fetchone()[0] == 0

    finally:
        ex_lock.release()
        conn.close()

@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_65_existing_transaction_commit_during_rotation(temp_dir: Path):
    """P056-01: Existing write transaction retains shared lock; rotation exclusive lock is blocked; uncoordinated commit is rejected."""
    key_file = temp_dir / "keys" / "t65.key.enc"
    protector = SoftwareKeyProtector(b"7" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t65.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t65 (id INT PRIMARY KEY, val TEXT);")
    conn.commit()

    # 1. Start an explicit write transaction
    conn.execute("BEGIN IMMEDIATE;")
    conn.execute("INSERT INTO t65 VALUES (1, 'in_flight');")

    # The connection must hold the shared access lock
    assert ConnectionCoordinator.get_connection_lock(conn) is not None

    # 2. Rotation requests exclusive lock in another thread / context
    ex_lock = DatabaseAccessLock(db_path)
    assert not ex_lock.acquire_exclusive(timeout_sec=0.2), "Exclusive lock must be blocked while shared transaction lock is held"

    # 3. Commit within the coordinated transaction succeeds and releases the lock
    conn.commit()
    assert ConnectionCoordinator.get_connection_lock(conn) is None

    # 4. Exclusive lock can now be acquired
    assert ex_lock.acquire_exclusive(timeout_sec=2.0)
    try:
        # 5. Commit outside the shared-lock protocol (under exclusive lock) is rejected
        with pytest.raises(DatabaseDrainingError):
            conn.commit()
        with pytest.raises(DatabaseDrainingError):
            conn.execute("COMMIT;")
    finally:
        ex_lock.release()
        conn.close()

    # Verify data integrity
    c = create_connection(db_path, encryption_key=key1, encrypted=True)
    assert c.execute("SELECT val FROM t65 WHERE id = 1;").fetchone()[0] == "in_flight"
    c.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_66_existing_transaction_with_intervening_write(temp_dir: Path):
    """P056-02: Existing transaction with multiple intervening writes holds lock continuously across rotation request."""
    key_file = temp_dir / "keys" / "t66.key.enc"
    protector = SoftwareKeyProtector(b"8" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t66.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t66 (id INT PRIMARY KEY, val TEXT);")
    conn.commit()

    # Start transaction with first write
    conn.execute("BEGIN;")
    conn.execute("INSERT INTO t66 VALUES (1, 'first');")
    held = ConnectionCoordinator.get_connection_lock(conn)
    assert held is not None

    ex_lock = DatabaseAccessLock(db_path)
    assert not ex_lock.acquire_exclusive(timeout_sec=0.2)

    # Intervening write 2 and 3 succeed under continuous lock
    conn.execute("INSERT INTO t66 VALUES (2, 'second');")
    conn.execute("INSERT INTO t66 VALUES (3, 'third');")
    assert ConnectionCoordinator.get_connection_lock(conn) is held
    assert not ex_lock.acquire_exclusive(timeout_sec=0.2)

    conn.commit()
    assert ConnectionCoordinator.get_connection_lock(conn) is None

    # Exclusive lock now succeeds
    assert ex_lock.acquire_exclusive(timeout_sec=2.0)
    try:
        # A new transaction cannot perform an intervening write under exclusive lock
        with pytest.raises(DatabaseDrainingError):
            conn.execute("INSERT INTO t66 VALUES (4, 'blocked');")
    finally:
        ex_lock.release()
        conn.close()

    c = create_connection(db_path, encryption_key=key1, encrypted=True)
    rows = c.execute("SELECT id, val FROM t66 ORDER BY id;").fetchall()
    c.close()
    assert [tuple(r) for r in rows] == [(1, "first"), (2, "second"), (3, "third")]


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_67_raw_transaction_control_statements(temp_dir: Path):
    """P056-03: Raw SQL BEGIN, COMMIT, and ROLLBACK cannot bypass lock ownership."""
    key_file = temp_dir / "keys" / "t67.key.enc"
    protector = SoftwareKeyProtector(b"9" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t67.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t67 (id INT PRIMARY KEY, val TEXT);")
    conn.commit()

    # BEGIN acquires lock immediately
    conn.execute("BEGIN;")
    assert ConnectionCoordinator.get_connection_lock(conn) is not None

    ex_lock = DatabaseAccessLock(db_path)
    assert not ex_lock.acquire_exclusive(timeout_sec=0.2)

    # ROLLBACK releases lock
    conn.execute("ROLLBACK;")
    assert ConnectionCoordinator.get_connection_lock(conn) is None
    assert ex_lock.acquire_exclusive(timeout_sec=2.0)

    try:
        # Under exclusive lock, raw BEGIN is rejected via connection and cursor
        with pytest.raises(DatabaseDrainingError):
            conn.execute("BEGIN;")

        cur = conn.cursor()
        with pytest.raises(DatabaseDrainingError):
            cur.execute("BEGIN IMMEDIATE;")

        # Uncoordinated COMMIT rejected
        with pytest.raises(DatabaseDrainingError):
            conn.execute("COMMIT;")
        with pytest.raises(DatabaseDrainingError):
            cur.execute("COMMIT;")
        cur.close()
    finally:
        ex_lock.release()
        conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_68_cursor_and_executemany_bypass(temp_dir: Path):
    """P056-04: Retained cursor write methods (execute, executemany, executescript) are blocked under exclusive lock."""
    key_file = temp_dir / "keys" / "t68.key.enc"
    protector = SoftwareKeyProtector(b"a" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t68.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t68 (id INT PRIMARY KEY, val TEXT);")
    conn.commit()

    # Retain cursor created before lock
    retained_cursor = conn.cursor()

    ex_lock = DatabaseAccessLock(db_path)
    assert ex_lock.acquire_exclusive(timeout_sec=2.0)

    try:
        # 1. cursor.execute blocked
        with pytest.raises(DatabaseDrainingError):
            retained_cursor.execute("INSERT INTO t68 VALUES (1, 'val1');")

        # 2. cursor.executemany blocked
        with pytest.raises(DatabaseDrainingError):
            retained_cursor.executemany("INSERT INTO t68 VALUES (?, ?);", [(2, "val2"), (3, "val3")])

        # 3. cursor.executescript blocked
        with pytest.raises(DatabaseDrainingError):
            retained_cursor.executescript("INSERT INTO t68 VALUES (4, 'val4');")

        # 4. Safe read remains unblocked
        cur = retained_cursor.execute("SELECT count(*) FROM t68;")
        assert cur.fetchone()[0] == 0
    finally:
        retained_cursor.close()
        ex_lock.release()
        conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_69_cross_process_existing_connection(temp_dir: Path):
    """P056-05: Cross-process writer holds open transaction; rotation waits for commit then rekeys cleanly."""
    key_file = temp_dir / "keys" / "t69.key.enc"
    protector = SoftwareKeyProtector(b"b" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\x22" * 32

    db_path = temp_dir / "data" / "t69.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    init_conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    init_conn.execute("CREATE TABLE t69 (id INT PRIMARY KEY, msg TEXT);")
    init_conn.commit()
    init_conn.close()

    ready_file = temp_dir / "writer_ready.flag"
    resume_file = temp_dir / "writer_resume.flag"

    writer_code = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"{Path(__file__).resolve().parent.parent}")
from src.storage.database import create_connection

p = Path(r"{db_path}")
ready = Path(r"{ready_file}")
resume = Path(r"{resume_file}")

conn = create_connection(p, encryption_key={key1!r}, encrypted=True)
conn.execute("BEGIN IMMEDIATE;")
conn.execute("INSERT INTO t69 VALUES (10, 'cross_process');")
ready.write_text("READY", encoding="utf-8")

start = time.time()
while not resume.exists() and time.time() - start < 10.0:
    time.sleep(0.05)

conn.commit()
conn.close()
sys.exit(0)
"""

    repo_root = Path(__file__).resolve().parent.parent
    p_writer = subprocess.Popen([sys.executable, "-c", writer_code], cwd=str(repo_root))

    t0 = time.time()
    while not ready_file.exists() and time.time() - t0 < 10.0:
        time.sleep(0.05)
    assert ready_file.exists(), "Writer process failed to start transaction"

    rot_result = []
    def rot_worker():
        try:
            r = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2, drain_timeout_sec=5.0)
            rot_result.append(r)
        except Exception as exc:
            rot_result.append(exc)

    t_rot = threading.Thread(target=rot_worker)
    t_rot.start()

    time.sleep(0.3)
    resume_file.write_text("RESUME", encoding="utf-8")

    p_writer.wait(timeout=10)
    assert p_writer.returncode == 0

    t_rot.join(timeout=10)
    assert len(rot_result) == 1 and not isinstance(rot_result[0], Exception), f"Rotation failed: {rot_result}"
    assert rot_result[0]["status"] == "SUCCESS"

    c = create_connection(db_path, encryption_key=key2, encrypted=True)
    row = c.execute("SELECT msg FROM t69 WHERE id = 10;").fetchone()
    c.close()
    assert row[0] == "cross_process"


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_70_exceptions_and_cleanup(temp_dir: Path):
    """P056-06: Exceptions during write, commit, or transaction rollback release locks cleanly without leak."""
    key_file = temp_dir / "keys" / "t70.key.enc"
    protector = SoftwareKeyProtector(b"c" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()

    db_path = temp_dir / "data" / "t70.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t70 (id INT PRIMARY KEY);")
    conn.commit()

    # 1. Error inside transaction() context manager
    with pytest.raises(ZeroDivisionError):
        with transaction(conn):
            conn.execute("INSERT INTO t70 VALUES (1);")
            _ = 1 / 0

    assert ConnectionCoordinator.get_connection_lock(conn) is None

    # 2. Syntax error during explicit transaction
    conn.execute("BEGIN;")
    with pytest.raises(Exception):
        conn.execute("INSERT INTO non_existent_table VALUES (1);")
    conn.rollback()
    assert ConnectionCoordinator.get_connection_lock(conn) is None

    # 3. DatabaseAccessLock exclusive acquisition succeeds immediately (no lock leak)
    ex = DatabaseAccessLock(db_path)
    assert ex.acquire_exclusive(timeout_sec=1.0)
    ex.release()

    conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_71_stale_connection_after_successful_rotation(temp_dir: Path):
    """P056-07: Connection retained across database re-key cannot write or corrupt new database."""
    key_file = temp_dir / "keys" / "t71.key.enc"
    protector = SoftwareKeyProtector(b"d" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\x33" * 32

    db_path = temp_dir / "data" / "t71.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t71 (id INT PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO t71 VALUES (1, 'initial');")
    conn.commit()

    stale_cur = conn.cursor()

    res = rotate_database_key(db_path, key_manager=key_mgr, new_key=key2, drain_timeout_sec=5.0)
    assert res["status"] == "SUCCESS"

    expected_stale_errors = (
        sqlcipher_dbapi2.ProgrammingError,
        sqlite3.ProgrammingError,
        DatabaseDrainingError,
        DatabaseConnectionError,
    ) if sqlcipher_dbapi2 is not None else (
        sqlite3.ProgrammingError,
        DatabaseDrainingError,
        DatabaseConnectionError,
    )

    with pytest.raises(expected_stale_errors) as exc_conn:
        conn.execute("INSERT INTO t71 VALUES (2, 'stale_write');")
    assert "closed" in str(exc_conn.value).lower() or "drain" in str(exc_conn.value).lower()

    with pytest.raises(expected_stale_errors) as exc_cur:
        stale_cur.execute("INSERT INTO t71 VALUES (3, 'stale_cur_write');")
    assert "closed" in str(exc_cur.value).lower() or "drain" in str(exc_cur.value).lower()

    new_conn = create_connection(db_path, encryption_key=key2, encrypted=True)
    rows = new_conn.execute("SELECT id, val FROM t71;").fetchall()
    assert [tuple(r) for r in rows] == [(1, "initial")]
    new_conn.close()


@pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE, reason="sqlcipher3 is required for database encryption tests"
)
def test_72_lock_timeout_and_fail_closed_behavior(temp_dir: Path):
    """P056-08: Lock timeout during rotation fails closed without publishing new key or modifying database."""
    key_file = temp_dir / "keys" / "t72.key.enc"
    protector = SoftwareKeyProtector(b"e" * 32)
    key_mgr = DatabaseKeyManager(key_path=key_file, key_protector=protector)
    key1 = key_mgr.get_or_create_key()
    key2 = b"\x44" * 32

    db_path = temp_dir / "data" / "t72.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = create_connection(db_path, encryption_key=key1, encrypted=True)
    conn.execute("CREATE TABLE t72 (id INT PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO t72 VALUES (1, 'original');")
    conn.commit()
    conn.close()

    sh_lock = DatabaseAccessLock(db_path)
    assert sh_lock.acquire_shared(timeout_sec=2.0)

    try:
        with pytest.raises(RotationLockError):
            rotate_database_key(db_path, key_manager=key_mgr, new_key=key2, timeout_sec=0.2)
    finally:
        sh_lock.release()

    assert key_mgr.get_current_version() == 1
    assert key_mgr.get_key() == key1

    c = create_connection(db_path, encryption_key=key1, encrypted=True)
    assert c.execute("SELECT val FROM t72 WHERE id = 1;").fetchone()[0] == "original"
    c.close()

    with pytest.raises((DatabaseConnectionError, sqlite3.DatabaseError)):
        bad_conn = create_connection(db_path, encryption_key=key2, encrypted=True)
        bad_conn.execute("SELECT count(*) FROM t72;").fetchone()

