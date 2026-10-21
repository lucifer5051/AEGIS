# AEGIS / SAGE — Database Encryption Migration & Disaster Recovery Runbook

**Document Reference:** `docs/AEGIS_DATABASE_ENCRYPTION_MIGRATION.md`  
**Phase:** Prompt 037 (Whole-Database Encryption)  
**Status:** VALIDATED & OPERATIONAL  
**Date:** October 2026  

---

## 1. Overview & Purpose

This document provides the operational runbook and disaster-recovery procedures for transitioning the canonical AEGIS SQLite database from unencrypted plaintext to SQLCipher 4.19.0 Community page-level encryption.

The migration procedure is designed with **zero data loss, atomic execution, pre-migration snapshot protection, and fail-closed rollback guarantees**.

---

## 2. Prerequisites & Verification

Before running migration procedures, verify the operational environment:

1. **Python Environment:** Python 3.12.10 running in `.aegis/venv`.
2. **SQLCipher Driver:** `sqlcipher3` 0.6.4 installed and importable.
   ```powershell
   & "C:\Users\digvi\.aegis\venv\Scripts\python.exe" -c "import sqlcipher3; print(sqlcipher3.__version__)"
   # Output must be: 0.6.4
   ```
3. **Storage Boundary Enforcement:**
   - Target database must reside in `%USERPROFILE%\.aegis\data\aegis.db`.
   - Never place databases inside OneDrive, desktop directories, or repository source trees.
4. **Key Management Foundation:**
   - `%USERPROFILE%\.aegis\keys\aegis_db.key.enc` must be initialized or permitted to generate via Windows DPAPI.

---

## 3. Automated Migration Workflow

The migration is implemented in `src/storage/database.py` via `migrate_database_to_encrypted()` and is invoked automatically by `initialize_runtime_database()`.

```
+-------------------------------------------------------------------------+
|                  migrate_database_to_encrypted(source_path)             |
+-------------------------------------------------------------------------+
                                    |
            1. Pre-Check: Is source already encrypted?
               Yes --> Return immediately (Idempotent NO-OP)
               No  --> Proceed to Step 2
                                    |
            2. WAL Checkpoint:
               PRAGMA wal_checkpoint(TRUNCATE) to flush pending commits
                                    |
            3. Encrypted Backup Snapshot:
               Export source -> ~/.aegis/backups/<stem>_backup_<id>.staging
               via sqlcipher_export with master encryption key
               Verify: PRAGMA integrity_check == "ok"
               Verify: header lacks SQLite plaintext magic
               os.replace staging -> ~/.aegis/backups/<stem>_pre_encryption_backup.db
                                    |
            4. Staged SQLCipher Export for Active DB:
               Open source in read-only mode
               ATTACH ~/.aegis/data/aegis.db.encrypted.staging AS encrypted
               PRAGMA encrypted.key = "x'<hex_key>'";
               SELECT sqlcipher_export('encrypted');
               DETACH DATABASE encrypted;
                                    |
            5. Cryptographic Integrity Verification:
               Open staging file with sqlcipher3 + key
               PRAGMA integrity_check; (Assert result == "ok")
               SELECT count(*) FROM sqlite_master;
                                    |
            6. Atomic Replacement:
               os.replace(staging_file, source_file)
               Verify replaced DB authenticates via SQLCipher
               Clean up residual plaintext .wal and .shm files
                                    |
            7. Verification Complete:
               Both active database and backup snapshot are fully encrypted.
+-------------------------------------------------------------------------+
```

### 3.1 Python Execution Example
```python
from pathlib import Path
from src.security.database_key import DatabaseKeyManager
from src.storage.database import migrate_database_to_encrypted
from src.storage.paths import get_default_db_path

db_path = get_default_db_path()
key_mgr = DatabaseKeyManager()
master_key = key_mgr.get_or_create_key()

# Atomically migrate database with verified encrypted backup
migrated_path = migrate_database_to_encrypted(
    source_path=db_path,
    encryption_key=master_key,
)
print(f"Successfully encrypted database at: {migrated_path}")
```

---

## 4. Disaster Recovery & Rollback Procedures

### 4.1 Automatic Failure Abort
If any step during migration fails (e.g., disk full, permission denied, process termination, invalid key):
1. The temporary staging files (`*.staging`) are unlinked immediately.
2. The original plaintext database file is untouched and remains fully operational.
3. If failure occurs after backup creation, the verified encrypted backup in `%USERPROFILE%\.aegis\backups\` remains preserved.
4. An explanatory `DatabaseConnectionError` is raised.

### 4.2 Programmatic Restoration (`restore_database_from_backup`)
The system provides a tested restoration function in `src/storage/database.py`:

```python
from src.security.database_key import DatabaseKeyManager
from src.storage.database import restore_database_from_backup
from src.storage.paths import get_default_backups_dir, get_default_db_path

backup_file = get_default_backups_dir() / "aegis_pre_encryption_backup.db"
target_db = get_default_db_path()
key = DatabaseKeyManager().get_key()

# Option A: Restore as encrypted SQLCipher database (standard production recovery)
restore_database_from_backup(
    backup_path=backup_file,
    target_path=target_db,
    encryption_key=key,
    as_plaintext=False,
)

# Option B: Emergency rollback to plaintext SQLite database (diagnostic recovery)
restore_database_from_backup(
    backup_path=backup_file,
    target_path=target_db,
    encryption_key=key,
    as_plaintext=True,
)
```

---

## 5. Legacy Plaintext Backup Detection & Remediation Runbook

If legacy plaintext backups were generated by earlier implementations, they can be safely identified and remediated without service interruption.

### 5.1 Scanning for Legacy Plaintext Backups
```python
from src.storage.database import scan_legacy_plaintext_backups

findings = scan_legacy_plaintext_backups()
for f in findings:
    print(f"Backup: {f['name']} | Status: {f['status']} | Size: {f['size_bytes']} bytes")
```

### 5.2 Opt-In Safe Remediation (Dry-Run First)
Remediation is **never executed automatically during startup**. It requires explicit invocation:

```python
from src.security.database_key import DatabaseKeyManager
from src.storage.database import remediate_legacy_backup

key = DatabaseKeyManager().get_key()
legacy_file = "C:/Users/.../.aegis/backups/legacy_pre_encryption_backup.db"

# Step 1: Dry run (no files modified)
dry_run_report = remediate_legacy_backup(legacy_file, encryption_key=key, dry_run=True)
print(dry_run_report)

# Step 2: Live remediation with plaintext removal only after verification
remediate_report = remediate_legacy_backup(
    legacy_file,
    encryption_key=key,
    dry_run=False,
    remove_plaintext=True,
)
print(remediate_report)
```

---

## 6. Verification Checklist

Execute the following checklist to confirm successful migration:

- [x] **WAL Consistency:** Committed WAL transactions are flushed via `PRAGMA wal_checkpoint(TRUNCATE)` before snapshotting.
- [x] **Backup Encryption Verified:** Pre-migration backup is a SQLCipher-encrypted database (`is_database_encrypted(backup) == True`).
- [x] **Zero Plaintext Backup Leakage:** Raw binary inspection confirms absence of `SQLite format 3\x00` and zero secret marker strings in the backup file.
- [x] **Standard sqlite3 Fails on Backup:** Opening backup with standard `sqlite3` raises `DatabaseError: file is not a database`.
- [x] **SQLCipher Opens Backup:** Opening with `sqlcipher3` + `PRAGMA key` executes queries successfully.
- [x] **Restoration Verified:** `restore_database_from_backup` tested and verified for both encrypted and plaintext targets.
- [x] **Integrity Checks Pass:** `PRAGMA integrity_check;` returns `ok` on both active database and backup snapshot.
- [x] **Schema & Data Preservation:** Record counts across tasks, gateway updates, contact memory, and schema migrations match pre-migration counts.
- [x] **Legacy Scanner Operational:** `scan_legacy_plaintext_backups()` detects unencrypted legacy files without reading contents.
- [x] **No Unintended Deletion:** Existing legacy backups are not deleted automatically during startup.
