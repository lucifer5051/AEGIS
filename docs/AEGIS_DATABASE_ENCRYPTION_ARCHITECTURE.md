# AEGIS / SAGE — Whole-Database Encryption Architecture Specification

**Document Reference:** `docs/AEGIS_DATABASE_ENCRYPTION_ARCHITECTURE.md`  
**Phase:** Prompt 037, Prompt 039, Prompt 040 (Key Rotation & Disaster Recovery)  
**Status:** SPECIFIED, IMPLEMENTED & VERIFIED  
**Date:** October 2026  

---

## 1. Architectural Overview & Objective

The AEGIS persistence layer safeguards autonomous agent operational state, user instructions, contact private memory, and communication streams. Prior to Prompt 037, payload fields within contact memory were encrypted using AES-256-GCM, but the SQLite structural envelope (schema, metadata, tasks, gateway updates, journals) remained unencrypted on disk.

Prompt 037 establishes a **Two-Tier Defense-in-Depth Cryptographic Architecture**:
1. **Tier 1 (Outer Envelope): Whole-Database Page-Level Encryption** via SQLCipher 4.19.0 Community (`sqlcipher3` 0.6.4). Every 4096-byte page, B-tree node, schema definition, and WAL entry is encrypted at rest using AES-256-CBC with per-page HMAC-SHA512 authentication.
2. **Tier 2 (Inner Payloads): Per-Contact Memory Isolation** via AES-256-GCM. Sensitive contact notes, facts, and conversation transcripts are independently encrypted using isolated 256-bit Data Encryption Keys (DEKs).

Even if an attacker bypasses disk encryption or acquires the database master key, they cannot read Contact A's private memory without Contact A's specific DPAPI-protected DEK. Conversely, even if an attacker attempts raw disk carving on the database file, they cannot read table names, schema metadata, tasks, or gateway message queues.

---

## 2. Cryptographic Architecture & Key Hierarchy

### 2.1 Key Hierarchy Diagram
```
=============================================================================
                     WINDOWS DATA PROTECTION API (DPAPI)
                     CryptProtectData / CryptUnprotectData
              Entropy: b"aegis:database_master_key:v1" (DB Key)
              Entropy: b"aegis:contact_dek:v1:<jid>" (Contact DEKs)
=============================================================================
                                     |
           +-------------------------+-------------------------+
           |                                                   |
           v                                                   v
+-------------------------------+               +-------------------------------+
|     Database Master Key       |               |    Per-Contact Master DEKs    |
|   32 bytes (256-bit CSPRNG)   |               |   32 bytes (256-bit CSPRNG)   |
| ~/.aegis/keys/aegis_db.key.enc|               | contact_keys.protected_dek    |
+-------------------------------+               +-------------------------------+
           |                                                   |
           v                                                   v
+-------------------------------+               +-------------------------------+
|       SQLCipher Engine        |               |       AES-256-GCM Engine      |
|  - AES-256-CBC (Page cipher)  |               |  - AES-256-GCM (Payload)      |
|  - HMAC-SHA512 (Page auth)    |               |  - 96-bit CSPRNG Nonce        |
|  - PBKDF2-HMAC-SHA512 (KDF)   |               |  - 128-bit Authentication Tag |
|  - WAL / SHM file encryption  |               |  - Cryptographically bound AAD|
+-------------------------------+               +-------------------------------+
           |                                                   |
           v                                                   v
+-------------------------------+               +-------------------------------+
| Whole-Database File Protection|               | Per-Contact Isolated Memory   |
| (Tables, Indexes, Tasks,      |               | (Private facts, sensitive     |
|  Gateway Updates, Audit Log)  |               |  conversation history)        |
+-------------------------------+               +-------------------------------+
```

### 2.2 Master Key Specification (`DatabaseKeyManager`)
- **Key Generation:** 32 bytes (256 bits) from OS CSPRNG (`secrets.token_bytes(32)`).
- **At-Rest Protection:** Wrapped using Windows DPAPI (`crypt32.dll` via `ctypes`):
  - Function: `CryptProtectData` with `CRYPTPROTECT_UI_FORBIDDEN`.
  - Secondary Entropy: `b"aegis:database_master_key:v1"`.
  - Storage Location: `%USERPROFILE%\.aegis\keys\aegis_db.key.enc`.
- **Key Lifetime:** Persisted across application restarts; unlocked into process memory only when opening database connections.
- **Key Destruction:** `destroy_key()` method securely truncates and removes the encrypted key file.
- **Key Rotation:** `rotate_key(new_key)` generates and protects a new key while returning `(old_key, new_key)` to enable online database rekeying via `PRAGMA rekey`.

---

## 3. SQLCipher Engine Configuration & PRAGMAs

SQLCipher executes in-process as a replacement C SQLite engine. When opening an encrypted connection, the following configuration is strictly applied before any database interaction:

### 3.1 Initial Key Binding
```sql
PRAGMA key = "x'<64-char-hex-encoded-256-bit-key>'";
```
- Must be executed immediately after opening the native connection.
- Hex format (`x'...'`) delivers the raw 256-bit key directly to SQLCipher without KDF stretching overhead, ensuring deterministic, instantaneous key binding.

### 3.2 SQLCipher Security Defaults (Community 4.19.0)
- **Cipher Algorithm:** AES-256-CBC.
- **Page Size:** 4096 bytes (`PRAGMA cipher_page_size = 4096;`).
- **HMAC Authentication:** SHA512 (`PRAGMA cipher_hmac_algorithm = HMAC_SHA512;`).
- **Key Derivation (if passphrase used):** PBKDF2 with 256,000 iterations (`PRAGMA kdf_iter = 256000;`).
- **WAL Encryption:** Full page-level encryption of Write-Ahead Log records (`PRAGMA cipher_use_hmac = ON;`).

### 3.3 Runtime Operational PRAGMAs
Once authentication succeeds, the standard SAGE durability and integrity pragmas are enforced:
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 30000;
```

---

## 4. Connection Lifecycle & Fail-Closed Invariants

### 4.1 Factory Functions in `src/storage/database.py`

#### 1. `create_connection(db_path, encryption_key=None, encrypted=None, timeout_sec=30.0)`
- **Auto-Detection:**
  - If `db_path` points to an existing file and `is_database_encrypted(db_path)` returns `True`, encryption is automatically required.
  - If `encrypted=True`, encryption is strictly required.
  - If `encrypted=False`, standard plaintext SQLite connection is created (used for legacy or non-sensitive scratch tests).
  - If `encrypted=None` and `encryption_key` is provided, encryption is required.
- **Key Resolution:** If encryption is required and `encryption_key` is omitted, `DatabaseKeyManager.get_key()` retrieves the DPAPI-protected key from `%USERPROFILE%\.aegis\keys\aegis_db.key.enc`.
- **Fail-Closed Authentication:**
  Immediately following `PRAGMA key`, the connection executes:
  ```sql
  SELECT count(*) FROM sqlite_master;
  ```
  If key is invalid or header cannot be authenticated, the connection is immediately closed and a `DatabaseConnectionError` is raised. **No unauthenticated connection is ever returned.**

#### 2. `open_database(db_path, encryption_key=None, encrypted=None, timeout_sec=30.0)`
- Context manager wrapper around `create_connection`.
- Ensures proper connection closure, transaction rollback on uncaught exceptions, and clean resource release.

#### 3. `initialize_runtime_database(db_path=None, migrations_dir=None, encryption_key=None, encrypted=None)`
- Canonical SAGE database bootstrapper.
- Resolves default path (`%USERPROFILE%\.aegis\data\aegis.db`) and validates storage boundaries.
- **Automatic Transparent Migration:** If encryption is enabled (`encrypted=True` or `SQLCIPHER_AVAILABLE=True` by default) and an existing database file is detected to be plaintext, it automatically calls `migrate_database_to_encrypted` before connecting.
- Applies all pending schema migrations monotonically.

### 4.2 Fail-Closed Invariant Summary
- **No Plaintext Fallback:** If SQLCipher is unavailable, missing, or encounters an invalid key when encryption is requested, the operation aborts with `DatabaseConnectionError`. Plaintext SQLite is NEVER substituted.
- **Zero-Secret Logging:** Master key hex and byte values are never passed to loggers, formatters, error strings, or external audit records.
- **Exception Uniformity:** `DatabaseError`, `IntegrityError`, and `OperationalError` exported from `src.storage.database` cover both `sqlite3` and `sqlcipher3` exception hierarchies to prevent unhandled exception leaks.

---

## 5. Concurrency, Locking & File Lifecycle

1. **WAL Mode Compatibility:** SQLCipher fully supports SQLite's Write-Ahead Logging. Readers do not block writers, and writers do not block readers.
2. **WAL File Encryption:** SQLCipher encrypts WAL frames using the identical page-level cipher. WAL file inspection confirms that transactions written during active execution contain no plaintext fragments.
3. **Multi-Thread Safety:** `sqlcipher3` compiles with SQLite multithreaded mode (`SQLITE_THREADSAFE=1`). Connections must not be shared across concurrent threads; thread-local or pooled connections are used.
4. **Temporary Files:** SQLCipher encrypts internal temporary databases and sort files when configured with memory or disk-backed temporary storage.

---

## 6. Encrypted Backup & Disaster-Recovery Architecture

Prompt 039 introduces a native encrypted backup and restoration architecture to ensure zero persistent plaintext snapshots:

1. **Native SQLCipher Export Snapshots:** Pre-migration backups are created by attaching the backup staging file directly to the source database within SQLCipher (`ATTACH ... AS enc_backup KEY "x'<hex_key>'"; SELECT sqlcipher_export('enc_backup');`).
2. **Snapshot Encryption Invariant:** The backup file is an authenticated SQLCipher database encrypted with AES-256-CBC and HMAC-SHA512 using the master database key. It contains no `SQLite format 3\x00` header and cannot be opened with standard `sqlite3`.
3. **WAL Consistency:** Prior to backup snapshot export, `PRAGMA wal_checkpoint(TRUNCATE);` is executed to flush all committed transaction frames into the main database file, preventing transaction omission.
4. **Two-Stage Atomic Publication:** The backup is exported to a unique staging path (`<stem>_backup_<uuid>.staging`), verified with `PRAGMA integrity_check;`, and atomically published to `<stem>_pre_encryption_backup.db` via `os.replace`.
5. **Programmatic Restoration (`restore_database_from_backup`):** Backups can be restored both as encrypted databases (standard production restore) or exported to plaintext (emergency diagnostic rollback) with complete cryptographic integrity verification.
6. **Legacy Backup Isolation:** `scan_legacy_plaintext_backups` and `remediate_legacy_backup` safely inventory and remediate pre-existing plaintext backups without automatic startup deletion.

---

## 7. Master Key Rotation, Concurrency Locking & Disaster Recovery (Prompt 040 / SEC-038-02)

To resolve finding SEC-038-02 (key rotation desynchronization), AEGIS implements a coordinated, transactional, and recoverable master key rotation subsystem (`src/security/database_key_rotation.py`):

### 7.1 Key Invariants
1. **Never Rekey Keyfile in Isolation:** Rekeying strictly binds the physical database rekeying to the DPAPI key update in an atomic two-phase transaction.
2. **Atomic Rekeying via Staged SQLCipher Export:**
   - Rekeying exports the database to an isolated staging file (`<db>.rekey_<uuid>.staging`) via `ATTACH ... KEY "x'<new_key>'"` followed by `SELECT sqlcipher_export('staged')`.
   - Before replacing the source file, both `PRAGMA integrity_check` and comprehensive schema/table/row-count parity checks are executed on the staging database.
3. **Cross-Process Exclusive Maintenance Locking (`ExclusiveDatabaseLock`):**
   - Implements a deterministic lock file (`<db>.rotation.lock`) preventing concurrent key rotations, startup conflicts, or database write corruption.
   - Crash-resilient stale lock detection: validates lock age (>300s) and inspects PID liveness. On Windows, uses non-signaling Win32 `OpenProcess` via `ctypes` (avoiding unsafe POSIX signal 0 emulation that would dispatch `CTRL_C_EVENT` to console process groups).
4. **Crash-Resilient State Machine Journal (`RotationJournal`):**
   - Records transition states to `<db>.rotation_journal.json`: `IDLE` -> `PREPARING` -> `SNAPSHOT_CREATED` -> `STAGING_EXPORTED` -> `STAGING_VERIFIED` -> `KEY_COMMITTED` -> `DATABASE_COMMITTED`.
   - **Deterministic Crash Recovery (`recover_interrupted_rotation`):**
     - If crash occurs before `KEY_COMMITTED`: Rolls back cleanly by removing staging files and journal, preserving the original database and active key.
     - If crash occurs after `KEY_COMMITTED`: Rolls forward by completing `os.replace` of the verified staged database, verifying authentication under the new key, synchronizing backups, and clearing the journal.
   - `initialize_runtime_database()` automatically checks for and resolves interrupted rotations upon application boot before opening connections.
5. **Key Versioning & Historical Archive:**
   - `DatabaseKeyManager` supports monotonically incrementing key versions.
   - Prior key material is archived in DPAPI-protected historical records (`archive_current_key(version)`).
   - Historical backups created under older key versions remain decryptable on demand via `key_manager.get_key(version=N)`.
6. **Backup Synchronization:**
   - Active rotation inspects the managed backups directory (`%USERPROFILE%\.aegis\backups\`) and rekeys existing backups under the new master key to ensure operational restore readiness.
7. **Strict Old-Key Rejection Verification (`_verify_key_rejected`):**
   - Post-rotation strictly asserts that the old key is rejected with fail-closed authentication failure, closing handles immediately to prevent file locking on Windows.

---

## 8. Coordinated Connection Draining & Windows File Replacement (Prompt 043 / Risk A)

To eliminate the risks of database corruption, lost concurrent writes, and Windows `os.replace` handle contention (WinError 32 `PermissionError`) during whole-database key rotation, AEGIS implements **Coordinated Connection Draining and Admission Control** (`src/storage/database.py`, `src/security/database_key_rotation.py`):

### 8.1 Invariants & Mechanics
1. **Connection Coordinator (`ConnectionCoordinator`):**
   - Tracks active database connections across threads using an internal registry purged of closed instances.
   - Publishes a cross-process advisory drain file (`<db>.drain`) containing coordinator PID, start timestamp, and timeout metadata.
   - Controls thread admission via in-process `threading.Event` synchronization.
2. **Admission Control & Transaction Rejection (`DatabaseDrainingError`):**
   - `create_connection()` and `open_database()` inspect `ConnectionCoordinator.is_draining(validated_path)`. While draining is active, connection attempts block up to `drain_wait_timeout` before failing closed with `DatabaseDrainingError`.
   - `transaction(conn)` checks draining status on entry. If draining begins while an idle connection is open, new transactions are rejected immediately.
3. **Graceful Draining & Idle Connection Eviction:**
   - On rotation start, `ConnectionCoordinator.drain()` immediately closes all idle connections.
   - Connections with active transactions are marked for deferred closure upon transaction completion (`COMMIT` or `ROLLBACK`).
   - If active transactions fail to complete within `drain_timeout_sec`, rotation aborts safely with `RotationDrainTimeoutError`, releasing the drain flag without modifying the database or master key.
4. **Deterministic Win32 Exclusive File Access Verification:**
   - Uses `can_acquire_exclusive_file_access(file_path)` via `kernel32.CreateFileW` with `dwShareMode=0` (`FILE_SHARE_NONE`) to verify that zero operating system handles are open on the main database or WAL file prior to attempting file replacement.
5. **Pre-Swap Active Database Parity Check:**
   - Immediately prior to committing the new key (`KEY_COMMITTED`), `rotate_database_key()` re-verifies that table and row counts on the active database match the baseline recorded before export (`baseline_counts`). If any write slipped in, rotation aborts fail-closed, deletes the staging file, and preserves the original database.
6. **Explicit WAL Checkpoint Return Code Verification:**
   - Inspects `PRAGMA wal_checkpoint(TRUNCATE)` return values `(busy, log, checkpointed)`. If `busy == 1`, rotation aborts rather than ignoring open reader holds.
7. **Bounded Replacement Retries on Windows:**
   - `os.replace(staging_db_file, validated_path)` and `RotationJournal.record_state()` execute bounded retries with exponential backoff (up to 5 attempts) to absorb transient filesystem locks from antivirus or search indexers.
8. **Crash-Resilient Cleanup:**
   - Both successful rotation and safe aborts release the drain file in an outer `finally:` block.
   - Boot-time recovery (`recover_interrupted_rotation`) automatically unlinks stale `<db>.drain` files to prevent permanent lockouts after ungraceful termination.


