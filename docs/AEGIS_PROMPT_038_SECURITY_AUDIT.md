# AEGIS / SAGE — Prompt 038 Independent Security Audit Report
## Whole-Database Encryption, Backup Exposure, Migration Safety & Cryptographic Verification

**Document Reference:** `docs/AEGIS_PROMPT_038_SECURITY_AUDIT.md`  
**Audit Phase:** Prompt 038 (Post-Implementation Independent Audit of Prompt 037)  
**Audit Mode:** READ-ONLY STATIC & ISOLATED DYNAMIC AUDIT  
**Date:** October 2026  
**Auditor:** AEGIS Security Audit & Verification Engineering  

---

## 1. Executive Verdict

### Verdict: **FAIL** (Remediation Required Before Live WhatsApp Pairing)

While SQLCipher 4.19.0 Community page-level encryption, Windows DPAPI master key protection, and per-contact AES-256-GCM memory isolation are genuinely installed and functionally operational in the active database engine, this independent audit has identified **two critical/high-severity security flaws**:

1. **CRITICAL BACKUP EXPOSURE (Finding SEC-038-01):** The migration utility (`migrate_database_to_encrypted` in `src/storage/database.py:360-368`) creates a pre-migration backup copy of the plaintext database using `shutil.copy2` **before** performing SQLCipher encryption. This backup file (`{stem}_pre_encryption_backup.db`) is **never encrypted, never shredded, and permanently retained in plaintext** at `%USERPROFILE%\.aegis\backups\`. Anyone with filesystem access can read all historical tasks, messages, gateway updates, and metadata directly in plaintext SQLite, completely defeating at-rest encryption.
2. **KEY ROTATION DESYNCHRONIZATION (Finding SEC-038-02):** `DatabaseKeyManager.rotate_key()` (`src/security/database_key.py:124-150`) generates and writes a new DPAPI-protected key to `%USERPROFILE%\.aegis\keys\aegis_db.key.enc`, but **does not re-encrypt or execute `PRAGMA rekey` on the database file**. Invoking key rotation immediately desynchronizes the key from the database, permanently locking out database access on the next connection attempt.

**Live WhatsApp Pairing Verdict:** **STRICTLY BLOCKED** until Finding SEC-038-01 (plaintext backup persistence) and Finding SEC-038-02 (key rotation desynchronization) are remediated.

---

## 2. Findings Summary Table

| Finding ID | Severity | Status | Affected Files & Lines | Description / Summary | Blocks Live Pairing? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SEC-038-01** | **High** | **Verified** | `src/storage/database.py:360-368`<br>`tests/test_database_encryption.py:322` | **Plaintext Pre-Migration Backup Persistence:** Migration copies the plaintext SQLite database to the backup directory before encryption. The backup file is never encrypted or cleaned up, persisting in plaintext indefinitely. | **YES** |
| **SEC-038-02** | **High** | **Verified** | `src/security/database_key.py:124-150` | **Key Rotation Does Not Rekey Database:** `rotate_key()` replaces the master key in the keyfile but never invokes `PRAGMA rekey` on the database file, bricking access on restart. | **YES** |
| **SEC-038-03** | **Medium** | **Verified** | `src/storage/database.py:473-494` | **Automatic Startup Migration Without User Consent:** `initialize_runtime_database()` automatically mutates and migrates unencrypted databases on disk without explicit user authorization or concurrency lock checks. | **NO** |
| **SEC-038-04** | **Medium** | **Verified** | `pyproject.toml:20-22` | **Undeclared Security Dependencies:** Neither `sqlcipher3` nor `cryptography` is declared in `pyproject.toml` dependencies (`dependencies = []`), risking silent omissions in clean environments. | **YES** |
| **SEC-038-05** | **Low** | **Verified** | `docs/AEGIS_DATABASE_ENCRYPTION_AUDIT.md:46`<br>`docs/AEGIS_DATABASE_ENCRYPTION_ARCHITECTURE.md:27` | **Unsupported FIPS Compliance Claim:** Previous report claims SQLCipher Community is "FIPS-compliant". Direct runtime query reveals Community build has zero FIPS compiler flags and is not FIPS 140-2/3 certified. | **NO** |
| **SEC-038-06** | **Informational** | **Verified** | `CHANGELOG.md:40`<br>`docs/AEGIS_PROJECT_STATE.md:1673` | **Test Count Discrepancy:** Previous report states 685 tests passed. Full repository contains 696 tests (11 tests in two e2e demonstration files were excluded during test execution via `--ignore`). | **NO** |

---

## 3. Dedicated Audit Sections

### 3.1 SQLCipher Backend Verification (Phase B)

#### Verified Facts:
1. **Installed Driver:** Python package `sqlcipher3` version `0.6.4` is installed in `%USERPROFILE%\.aegis\venv\Lib\site-packages\sqlcipher3`.
2. **Runtime Engine Version:** Direct runtime inspection via `PRAGMA cipher_version;` returns:
   ```
   cipher_version: 4.19.0 community
   sqlite_version: 3.53.4
   ```
3. **Connection Factories:**
   - [`create_connection()`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/storage/database.py#L98) in `src/storage/database.py`:
     - Line 140: If encryption is required/detected, validates `SQLCIPHER_AVAILABLE`.
     - Line 164: Connects via `sqlcipher3.connect(...)`.
     - Line 169: Sets `conn.row_factory = sqlcipher3.Row`.
     - Line 174-175: Binds raw key via `PRAGMA key = "x'<HEX>'";` immediately before any query.
     - Line 179-184: Executes fail-closed authentication test `SELECT count(*) FROM sqlite_master;`. If authentication fails (wrong key or corrupted header), closes connection and raises `DatabaseConnectionError`.
   - [`open_database()`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/storage/database.py#L225) in `src/storage/database.py`:
     - Wraps `create_connection()` in context manager, propagating encryption parameters and rolling back transactions on unhandled exceptions.
4. **Binary Page Inspection:**
   - Raw binary inspection of encrypted database files created by `create_connection` or `migrate_database_to_encrypted` confirms that the standard 16-byte header `b"SQLite format 3\x00"` is absent at byte offset 0.
   - Standard `sqlite3.connect` fails closed with `sqlite3.DatabaseError: file is not a database`.

#### Inferences:
- The active database connection path correctly binds SQLCipher 4.19.0 Community and enforces fail-closed authentication when encryption is requested or an encrypted header is detected.

---

### 3.2 Backup and Snapshot Encryption (Phase C — Critical Audit Target)

#### Classification: **FAIL** (Plaintext Sensitive Backups Persist on Disk)

#### Verified Facts & Code Path Analysis:
In [`src/storage/database.py:360-368`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/storage/database.py#L360-L368), `migrate_database_to_encrypted()` performs backup snapshot creation:
```python
# 1. Create pre-migration backup snapshot
b_dir = Path(backup_dir) if backup_dir else src.parent / "backups"
b_dir.mkdir(parents=True, exist_ok=True)
backup_path = b_dir / f"{src.stem}_pre_encryption_backup.db"
try:
    shutil.copy2(src, backup_path)
    logger.info("Created pre-encryption backup snapshot at '%s'", backup_path)
except Exception as exc:
    raise DatabaseConnectionError(f"Failed to create pre-encryption backup: {exc}") from exc
```

1. **Source State at Backup Time:** `src` is the unencrypted plaintext SQLite database.
2. **Copy Mechanism:** `shutil.copy2(src, backup_path)` performs a raw byte copy of the plaintext database file.
3. **Encryption State of Backup:** The backup file `{src.stem}_pre_encryption_backup.db` is 100% **PLAINTEXT**.
4. **Test Confirmation:** In [`tests/test_database_encryption.py:322`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_database_encryption.py#L322):
   ```python
   assert not is_database_encrypted(backup_file)
   ```
   The test suite explicitly asserts that the backup file is unencrypted, proving that plaintext backup persistence was known during test implementation.
5. **Post-Migration Lifecycle:** Once `os.replace(staging_file, src)` replaces the primary database with the encrypted file, **the plaintext backup file remains permanently on disk** in `%USERPROFILE%\.aegis\backups\`.
6. **No Cleanup or Shredding:** There is zero code to delete, overwrite, shred, or encrypt `backup_path` after successful migration.
7. **WAL State Omission:** `shutil.copy2` only copies the main `.db` file. If the plaintext database had uncheckpointed transactions in `aegis.db-wal`, those transactions are omitted from the backup copy.
8. **Documentation Discrepancy:** `docs/AEGIS_DATABASE_ENCRYPTION_MIGRATION.md` claims backups are timestamped (`pre_encryption_snapshot_<timestamp>.db`). The actual code hardcodes `{src.stem}_pre_encryption_backup.db`, causing subsequent migrations of identically named databases to overwrite previous backups.

#### Security Impact:
Whole-database encryption on `aegis.db` is completely bypassed if an attacker or unauthorized local process can read `%USERPROFILE%\.aegis\backups\aegis_pre_encryption_backup.db`.

---

### 3.3 Automatic Migration Safety (Phase D)

#### Verified Facts:
1. **Trigger Path:** [`src/storage/database.py:473-494`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/storage/database.py#L473-L494) in `initialize_runtime_database()`:
   ```python
   use_encryption = encrypted
   if use_encryption is None:
       use_encryption = SQLCIPHER_AVAILABLE
   ...
   if use_encryption and active_key and not is_memory and validated_path.is_file():
       if not is_database_encrypted(validated_path):
           migrate_database_to_encrypted(validated_path, active_key)
   ```
2. **Zero User Confirmation:** Automatic migration occurs during normal startup without an explicit CLI confirmation flag, user prompt, or configuration opt-in.
3. **Lock Contention on Windows:** If another process (e.g. background worker or sidecar) holds an open SQLite handle on `validated_path`, `os.replace(staging_file, src)` will raise Windows `PermissionError` (WinError 32). The migration catches this and unlinks `staging_file`, but raises `DatabaseConnectionError`, causing application startup to crash.
4. **Idempotence:** If `is_database_encrypted(src)` is True, `migrate_database_to_encrypted` returns `src` immediately without performing any file operations.
5. **Staging File Safety:** Staging file is named `{src.name}.encrypted.staging`. It is created via `ATTACH ... KEY "x'<hex_key>'"` and `sqlcipher_export`. The staging file is encrypted from byte 0. If an error occurs, the staging file is unlinked.

---

### 3.4 Key Management & Rotation (Phase E)

#### Verified Facts:
1. **Key Generation:** [`src/security/database_key.py:66`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/security/database_key.py#L66) generates 256 bits via `AES256GCMCipher.generate_random_key()` (`secrets.token_bytes(32)` via `AESGCM.generate_key`).
2. **DPAPI Protection:** Protected using Windows DPAPI via `WindowsDPAPIKeyProtector.protect(raw_key, optional_entropy=b"aegis:database_master_key:v1")`.
3. **Key File Location:** `%USERPROFILE%\.aegis\keys\aegis_db.key.enc`.
4. **Key Rotation Flaw (Finding SEC-038-02):**
   [`src/security/database_key.py:124-150`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/security/database_key.py#L124-L150):
   ```python
   def rotate_key(self, new_key: Optional[bytes] = None) -> Tuple[bytes, bytes]:
       old_key = self.get_key()
       fresh_key = new_key or AES256GCMCipher.generate_random_key()
       protected_blob = self._protector.protect(fresh_key, optional_entropy=DB_KEY_ENTROPY)
       ...
       os.replace(temp_name, self._key_path)
       return old_key, fresh_key
   ```
   `rotate_key()` writes `fresh_key` to `self._key_path` and returns `(old_key, fresh_key)`. **It never re-encrypts the database file using `PRAGMA rekey`**. Calling `rotate_key()` immediately breaks database authentication on next startup.
5. **Key Destruction:** [`destroy_key()`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/security/database_key.py#L152) calls `self._key_path.unlink()`. It removes the file entry from the filesystem, but does not overwrite the disk sectors before unlinking.

---

### 3.5 Per-Contact Encryption & Isolation (Phase F)

#### Verified Facts:
1. **Independent Cryptographic Layer:** Per-contact memory in `src/memory/contact_memory.py` is independent of SQLCipher page encryption.
2. **Algorithm:** AES-256-GCM AEAD (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`) with CSPRNG 96-bit nonces (`os.urandom(12)`).
3. **AAD Binding:** Associated Authenticated Data binds `aegis:contact_memory:v1:<jid>:<record_id>:<record_type>`.
4. **Storage Scoping:** All queries enforce `WHERE contact_jid = ?`. Contact A can never retrieve Contact B's ciphertext or unprotect Contact B's DEK.
5. **Two-Tier Model Integrity:** SQLCipher page encryption does not interfere with or degrade per-contact AES-256-GCM encryption.

---

### 3.6 Persistent-Store & Side-Channel Inventory (Phase G)

| Persistent Store | Location | Content | Encryption Status | Residual Risk |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Database** | `%USERPROFILE%\.aegis\data\aegis.db` | All schemas, tasks, gateway messages, contact memory, audit log | **Encrypted** (SQLCipher AES-256-CBC) | None (validated fail-closed) |
| **WAL Journal** | `%USERPROFILE%\.aegis\data\aegis.db-wal` | Active transaction frames | **Encrypted** (SQLCipher AES-256-CBC) | None (verified via negative byte search) |
| **Shared Memory** | `%USERPROFILE%\.aegis\data\aegis.db-shm` | WAL index / coordination | **Unencrypted** (SQLite internal index) | Low (contains no application payloads) |
| **Pre-Migration Backup** | `%USERPROFILE%\.aegis\backups\*_backup.db` | Full historical plaintext database | **PLAINTEXT** | **CRITICAL (SEC-038-01)** |
| **Staging Database** | `%USERPROFILE%\.aegis\data\*.encrypted.staging` | Transient export during migration | **Encrypted** (SQLCipher AES-256-CBC) | Low (unlinked immediately) |
| **Master Key File** | `%USERPROFILE%\.aegis\keys\aegis_db.key.enc` | 256-bit database master key | **Encrypted** (Windows DPAPI) | Low (bound to user context + entropy) |
| **Application Logs** | `%USERPROFILE%\.aegis\logs\` | Runtime diagnostics | **Plaintext text files** | Medium (no raw secrets, but task goals logged) |

---

### 3.7 Test Count & Coverage Reconciliation (Phase H)

#### Verified Facts:
1. **Executed Command in Prompt 037:**
   ```powershell
   & "C:\Users\digvi\.aegis\venv\Scripts\pytest.exe" tests\ -q --ignore=tests\test_e2e_demonstration.py --ignore=tests\test_agent_loop_e2e.py
   ```
   - **Output:** Exactly **685 passed** in 52.02s.
2. **Excluded Test Files:**
   - `tests/test_agent_loop_e2e.py`: 6 tests.
   - `tests/test_e2e_demonstration.py`: 5 tests.
   - **Total Excluded:** 11 tests.
3. **Execution of Excluded Tests:**
   When executed directly in this audit:
   ```powershell
   pytest tests\test_agent_loop_e2e.py tests\test_e2e_demonstration.py -v
   ```
   - **Output:** Exactly **11 passed** in 1.08s (0 failures).
4. **Total Repository Test Count:**
   $685 + 11 = \mathbf{696\ \text{tests}}$ in repository. All 696 tests pass.
5. **Phase E Security Tests:** All 11 tests in `tests/test_database_encryption.py` pass in 0.71s using real SQLCipher and DPAPI.

---

### 3.8 Dependency Provenance & FIPS Claim (Phase I)

#### Verified Facts:
1. **Installed Binary Wheel:** `sqlcipher3` 0.6.4 installed in `.aegis/venv`.
2. **`pyproject.toml` Declaration:** In `pyproject.toml:20-22`, `dependencies = []`. Neither `sqlcipher3` nor `cryptography` is declared as a project dependency.
3. **FIPS Claim Evaluation (Finding SEC-038-05):**
   - The Prompt 037 report described SQLCipher Community as "FIPS-compliant".
   - Direct inspection of the running binary via `PRAGMA cipher_version;` returns `4.19.0 community`.
   - Inspection of `PRAGMA compile_options;` shows 50 compilation options, with **zero FIPS flags or crypto module validations**.
   - SQLCipher Community uses standard OpenSSL/LibreSSL or TomCrypt cryptography. It is **NOT** a FIPS 140-2 or FIPS 140-3 validated cryptographic module.
   - True FIPS 140-2 validation requires Zetetic's commercial *SQLCipher FIPS Edition*, which links against a certified cryptographic module.
   - **Verdict on FIPS Claim:** **UNSUPPORTED & WITHDRAWN**. The software uses standard NIST-approved algorithms (AES-256, HMAC-SHA512), but the installed binary is not FIPS certified.

---

## 4. Prioritized Remediation Plan

To clear the blocker for live WhatsApp pairing, the following remediations must be executed in order:

### Remediation 1: Fix Plaintext Backup Exposure (SEC-038-01) — Priority: CRITICAL
In `src/storage/database.py:360-368`:
- Option A (Recommended): Export the backup snapshot directly as an encrypted SQLCipher database using `ATTACH ... AS backup KEY "x'<hex_key>'"; SELECT sqlcipher_export('backup');`.
- Option B: If a plaintext pre-migration snapshot is retained temporarily for rollback, encrypt the snapshot file using DPAPI or AES-256-GCM before writing to disk, or immediately purge the plaintext backup once `os.replace` succeeds and verification passes.

### Remediation 2: Implement True Rekeying in Key Rotation (SEC-038-02) — Priority: HIGH
In `src/security/database_key.py:124-150` and `src/storage/database.py`:
- Implement `rekey_database(db_path, old_key, new_key)` executing `PRAGMA rekey = "x'<new_hex>'";` on an authenticated connection before persisting the new key to `aegis_db.key.enc`.
- Make the keyfile update atomic with successful database rekeying.

### Remediation 3: Declare Dependencies in `pyproject.toml` (SEC-038-04) — Priority: MEDIUM
Add `sqlcipher3>=0.6.4` and `cryptography>=43.0.0` to `dependencies` in `pyproject.toml` with appropriate platform constraints.

### Remediation 4: Require Explicit Consent for Startup Migration (SEC-038-03) — Priority: MEDIUM
In `initialize_runtime_database()`, require an explicit `auto_migrate=True` parameter or configuration flag before automatically mutating an on-disk database.

---

## 5. Audit Acceptance Verification

- [x] Actual database connection paths inventoried.
- [x] SQLCipher implementation and installed/declarative versions reconciled.
- [x] Plaintext backup and staging risks investigated and identified.
- [x] Automatic migration behavior inspected without modifying live data.
- [x] Key rotation and destruction inspected without modifying live keys.
- [x] Per-contact encryption and isolation assessed.
- [x] Persistent stores and side channels inventoried.
- [x] Test counts reconciled (685 reported with `--ignore`, 696 total in repository).
- [x] FIPS claim investigated, verified as unsupported for Community Edition, and withdrawn.
- [x] Findings strictly distinguish verified facts from inferences.
- [x] Zero live databases, keys, backups, or Git state modified.
- [x] Zero WhatsApp connections or outbound transmissions occurred.
- [x] Audit report file `docs/AEGIS_PROMPT_038_SECURITY_AUDIT.md` created as sole modified artifact.
