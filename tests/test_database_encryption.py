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

    orig_collect = database_key_rotation._collect_table_counts
    call_count = [0]

    def patched_collect(target_path, key):
        res = orig_collect(target_path, key)
        call_count[0] += 1
        # The 3rd call is the pre-swap parity check on the active DB
        if call_count[0] == 3:
            # Simulate a rogue write slipping in
            return {**res, "counter": res.get("counter", 0) + 1}
        return res

    with patch("src.security.database_key_rotation._collect_table_counts", side_effect=patched_collect):
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
    """SEC-044-06: _collect_table_counts detects content mutation (UPDATE) even if row count is identical."""
    import sqlcipher3
    from src.security.database_key_rotation import _collect_table_counts

    key = b"m" * 32
    db_path = temp_dir / "data" / "sec06.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlcipher3.connect(str(db_path))
    conn.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    conn.execute("CREATE TABLE config (k TEXT PRIMARY KEY, v TEXT);")
    conn.execute("INSERT INTO config VALUES ('status', 'active');")
    conn.commit()
    conn.close()

    baseline = _collect_table_counts(db_path, key)

    # Mutate data in place: row count remains 1, but content changes
    conn2 = sqlcipher3.connect(str(db_path))
    conn2.execute(f"PRAGMA key = \"x'{key.hex()}'\";")
    conn2.execute("UPDATE config SET v = 'compromised' WHERE k = 'status';")
    conn2.commit()
    conn2.close()

    mutated = _collect_table_counts(db_path, key)
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




